"""
auth.py — Authentication routes for Kijiji Tanzania
Register, Login, OTP, Facebook, Google, Logout, Forgot/Reset password

Templates zote ziko kwenye templates/auth/:
    login.html, register.html, forgot_password.html,
    reset_password.html, verify_otp.html, verify_reset_otp.html
"""
import re
import sqlite3
import secrets
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlparse

import requests
from flask import (
    render_template, request, redirect, url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

# Google Identity Services — VERIFY token
try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
except ImportError:
    id_token = None
    google_requests = None

from db import get_db_connection
from helpers import (
    generate_otp, send_otp_email, sanitize_username,
    base_username_from_email, username_is_taken, suggest_usernames,
    apply_user_language, save_user_language, now_tz,
    SUPPORTED_LANGUAGES
)

# ====================== CONFIG ======================
GOOGLE_CLIENT_ID = "1083614983079-k0oeie8lkao98r62m91dc0aqcgomhk15.apps.googleusercontent.com"
FACEBOOK_APP_ID = "28132131163112538"
# TODO: badilisha (Reset App Secret) kisha uiweke kwenye environment variable
FACEBOOK_APP_SECRET = "613688758b4443fc4adb0b3429be4196"
FACEBOOK_AUTHORIZE_URL = "https://www.facebook.com/v23.0/dialog/oauth"
FACEBOOK_TOKEN_URL = "https://graph.facebook.com/v23.0/oauth/access_token"
FACEBOOK_USER_INFO_URL = "https://graph.facebook.com/me"

ADMIN_EMAIL = 'matondomaduhu135@gmail.com'

OTP_TTL_MINUTES = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_OTP_ATTEMPTS = 5


# ====================== HELPERS ======================

def _row_get(row, key, default=None):
    """Soma column kutoka sqlite3.Row bila kuvunjika kama column haipo."""
    try:
        if key in row.keys():
            val = row[key]
            return default if val is None else val
    except Exception:
        pass
    return default


def _parse_dt(value):
    """Badilisha string/datetime kutoka DB kuwa datetime (au None)."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _safe_equals(a, b):
    """Linganisha strings kwa constant-time. False kama mojawapo ni tupu."""
    if not a or not b:
        return False
    return secrets.compare_digest(
        str(a).encode('utf-8'), str(b).encode('utf-8')
    )


def _is_admin_email(email):
    return (email or '').strip().lower() == ADMIN_EMAIL


def _safe_next(url):
    """Rudisha URL ya 'next' tu kama ni ya site hii (zuia open redirect)."""
    if not url or not isinstance(url, str) or '\\' in url:
        return None
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        if parsed.netloc != request.host:
            return None
    return url


def _set_login_session(user_id, username, email, role):
    session['user_id'] = user_id
    session['username'] = username
    session['email'] = email
    session['role'] = role or 'user'
    session['is_admin'] = _is_admin_email(email)
    session.permanent = True


# ---------- OTP attempt limiter (inahifadhiwa kwenye DB, si session) ----------

def _ensure_attempts_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS otp_attempts (
            key TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    ''')


def _get_attempts(conn, key):
    _ensure_attempts_table(conn)
    row = conn.execute(
        'SELECT attempts FROM otp_attempts WHERE key = ?', (key,)
    ).fetchone()
    return int(row[0]) if row else 0


def _add_attempt(conn, key):
    """Ongeza jaribio moja; rudisha jumla mpya. Caller afanye commit."""
    _ensure_attempts_table(conn)
    now = str(datetime.now())
    conn.execute(
        'INSERT OR IGNORE INTO otp_attempts (key, attempts, updated_at) VALUES (?, 0, ?)',
        (key, now)
    )
    conn.execute(
        'UPDATE otp_attempts SET attempts = attempts + 1, updated_at = ? WHERE key = ?',
        (now, key)
    )
    return _get_attempts(conn, key)


def _reset_attempts(conn, key):
    _ensure_attempts_table(conn)
    conn.execute('DELETE FROM otp_attempts WHERE key = ?', (key,))


# ---------- Facebook helpers ----------

def _ensure_facebook_id_column(conn):
    """Ongeza column facebook_id kwenye users kama haipo (auto-migration)."""
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
        if 'facebook_id' not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN facebook_id TEXT')
            conn.commit()
    except Exception as e:
        print('[FB] ensure facebook_id column error:', e)


def _save_facebook_id(conn, user_id, facebook_id):
    try:
        conn.execute(
            "UPDATE users SET facebook_id = ? "
            "WHERE id = ? AND (facebook_id IS NULL OR facebook_id = '')",
            (facebook_id, user_id)
        )
        conn.commit()
    except Exception as e:
        print('[FB] save facebook_id warning:', e)


def _pick_fb_username(conn, name, email, facebook_id):
    base = (
        sanitize_username(name.replace(' ', '_'))
        if name else base_username_from_email(email)
    )
    if not base or len(base) < 3:
        base = f'fbuser{facebook_id[-6:]}'
    base = base[:24]

    username = base
    suffix = 0
    while username_is_taken(conn, username):
        suffix += 1
        if suffix > 50:
            username = f'fb_{facebook_id[-12:]}'
            break
        username = f'{base}{suffix}'

    username = sanitize_username(username) or f'fbuser{facebook_id[-8:]}'
    if len(username) < 3:
        username = f'fb{facebook_id[-10:]}'
    return username


def _insert_facebook_user(conn, username, email, password_hash,
                          full_name, picture_url, created):
    """INSERT kamili; kama schema ni ya zamani, tumia INSERT rahisi.
    IntegrityError inatupwa juu kwa caller."""
    try:
        cur = conn.execute(
            """INSERT INTO users
               (username, email, password_hash, full_name, bio, profile_pic,
                auth_provider, is_email_verified, role, created_at)
               VALUES (?, ?, ?, ?, '', ?, 'facebook', 1, 'user', ?)""",
            (username, email, password_hash, full_name, picture_url, created)
        )
        new_id = cur.lastrowid
    except sqlite3.OperationalError as oe:
        conn.rollback()
        print('[FB] OperationalError (schema?), minimal insert:', oe)
        cur = conn.execute(
            """INSERT INTO users
               (username, email, password_hash, bio, profile_pic, role)
               VALUES (?, ?, ?, '', ?, 'user')""",
            (username, email, password_hash, picture_url)
        )
        new_id = cur.lastrowid
        for sql, params in [
            ('UPDATE users SET full_name = ? WHERE id = ?', (full_name, new_id)),
            ('UPDATE users SET auth_provider = ? WHERE id = ?', ('facebook', new_id)),
            ('UPDATE users SET is_email_verified = 1 WHERE id = ?', (new_id,)),
            ('UPDATE users SET created_at = ? WHERE id = ?', (created, new_id)),
        ]:
            try:
                conn.execute(sql, params)
            except Exception:
                pass
    conn.commit()
    return new_id


# ---------- Google helper ----------

def _username_from_google_name(name, email=''):
    """Tengeneza username salama kutoka jina la Google (fallback: email)."""
    raw = (name or '').strip().lower()
    raw = re.sub(r'[^a-z0-9_]+', '_', raw)
    raw = re.sub(r'_+', '_', raw).strip('_')
    if len(raw) < 3:
        base = (email or 'user').split('@')[0].lower()
        base = re.sub(r'[^a-z0-9_]+', '_', base)
        base = re.sub(r'_+', '_', base).strip('_') or 'user'
        raw = base
    if len(raw) > 24:
        raw = raw[:24].rstrip('_')
    if raw and raw[0].isdigit():
        raw = 'u_' + raw
    return raw or 'user'


# ---------- Verification kwa user aliyepo lakini email haijathibitishwa ----------

def _send_verification_for_existing_user(user):
    """Tuma OTP mpya kwa user aliyeko kwenye `users` lakini
    is_email_verified != 1 (mfano ameingia bila kumaliza usajili).
    Rudisha True kama OTP ipo/imetumwa, False kama email imeshindwa."""
    conn = get_db_connection()
    try:
        email = user['email'].strip().lower()
        now = datetime.now()

        pending = conn.execute(
            'SELECT id, expires_at, resend_available_at '
            'FROM pending_registrations WHERE email = ? LIMIT 1',
            (email,)
        ).fetchone()

        # OTP ya hivi karibuni bado ipo → usitume nyingine (cooldown)
        if pending:
            avail = _parse_dt(pending['resend_available_at'])
            exp = _parse_dt(pending['expires_at'])
            if avail and exp and now < avail and now < exp:
                return True

        otp = generate_otp()
        expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
        resend_at = now + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        full_name = _row_get(user, 'full_name')

        if pending:
            conn.execute(
                '''UPDATE pending_registrations
                   SET username = ?, password_hash = ?, otp = ?,
                       expires_at = ?, resend_available_at = ?, full_name = ?
                   WHERE email = ?''',
                (user['username'], user['password_hash'], otp,
                 expires_at, resend_at, full_name, email)
            )
        else:
            conn.execute(
                '''INSERT INTO pending_registrations
                   (username, email, password_hash, otp, expires_at,
                    resend_available_at, full_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (user['username'], email, user['password_hash'], otp,
                 expires_at, resend_at, full_name)
            )
        _reset_attempts(conn, f'register:{email}')
        conn.commit()

        return bool(send_otp_email(email, otp, purpose='register'))
    except Exception as e:
        print('[login] send verification error:', e)
        traceback.print_exc()
        return False
    finally:
        conn.close()


# ====================== ROUTES ======================

def register_auth_routes(app):

    # ====================== REGISTER ======================

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'GET':
            return render_template(
                'auth/register.html',
                suggested_username='',
                username_suggestions=[],
                form_email='',
                form_username='',
                form_full_name=''
            )

        # ========== POST ==========
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        raw_username = (request.form.get('username') or '').strip()
        # Jina la kawaida — SI unique
        full_name = (request.form.get('full_name') or '').strip()
        if len(full_name) > 80:
            full_name = full_name[:80]

        def _reg_form(**extra):
            ctx = {
                'suggested_username': extra.get(
                    'suggested_username',
                    (sanitize_username(full_name.replace(' ', '_')) if full_name
                     else (base_username_from_email(email) if email else ''))
                ),
                'username_suggestions': extra.get('username_suggestions', []),
                'form_email': email,
                'form_username': extra.get('form_username', raw_username),
                'form_full_name': full_name,
            }
            return render_template('auth/register.html', **ctx)

        if not email or not password:
            flash('Tafadhali jaza email na nenosiri!', 'error')
            return _reg_form()

        if not full_name or len(full_name) < 2:
            flash('Tafadhali andika jina lako (angalau herufi 2).', 'error')
            return _reg_form()

        if len(password) < 6:
            flash('Nenosiri liwe angalau herufi 6.', 'error')
            return _reg_form()

        # Username: kutoka form AU auto kutoka jina (full_name), email ni fallback tu
        if raw_username:
            username = sanitize_username(raw_username)
        elif full_name:
            username = sanitize_username(full_name.replace(' ', '_'))
        else:
            username = base_username_from_email(email)

        if not username or len(username) < 3:
            flash('Username iwe angalau herufi 3 (a-z, 0-9, _).', 'error')
            return _reg_form(form_username=raw_username)

        password_hash = generate_password_hash(password)
        conn = get_db_connection()

        try:
            # Email tayari imesajiliwa?
            existing_email = conn.execute(
                'SELECT id FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                (email,)
            ).fetchone()
            if existing_email:
                flash(
                    'Email hii tayari imeshasajiliwa. '
                    'Tafadhali tumia email nyingine au ingia.',
                    'error'
                )
                return _reg_form(form_username=username)

            # Username imeshatumika? → onyesha suggestions
            if username_is_taken(conn, username, exclude_email=email):
                suggestions = suggest_usernames(
                    conn, username, limit=6, exclude_email=email
                )
                flash(
                    f'Username “{username}” imeshatumika. '
                    'Chagua nyingine au tumia mapendekezo hapa chini.',
                    'error'
                )
                return _reg_form(
                    suggested_username=(
                        suggestions[0] if suggestions
                        else (sanitize_username(full_name.replace(' ', '_')) if full_name
                              else base_username_from_email(email))
                    ),
                    username_suggestions=suggestions,
                    form_username=username
                )

            # ========== GENERATE OTP ==========
            otp = generate_otp()
            now = datetime.now()
            expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
            resend_available_at = now + timedelta(seconds=RESEND_COOLDOWN_SECONDS)

            pending_user = conn.execute(
                'SELECT id FROM pending_registrations WHERE email = ?',
                (email,)
            ).fetchone()

            if pending_user:
                conn.execute(
                    '''
                    UPDATE pending_registrations
                    SET username = ?,
                        password_hash = ?,
                        otp = ?,
                        expires_at = ?,
                        resend_available_at = ?,
                        full_name = ?
                    WHERE email = ?
                    ''',
                    (username, password_hash, otp, expires_at,
                     resend_available_at, full_name, email)
                )
            else:
                conn.execute(
                    '''
                    INSERT INTO pending_registrations (
                        username, email, password_hash, otp,
                        expires_at, resend_available_at, full_name
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (username, email, password_hash, otp,
                     expires_at, resend_available_at, full_name)
                )

            _reset_attempts(conn, f'register:{email}')
            conn.commit()

            if send_otp_email(email, otp, purpose="register"):
                session['pending_email'] = email
                flash(
                    'OTP imetumwa kwenye email yako. '
                    'Thibitisha ndani ya dakika 5.',
                    'success'
                )
                return redirect(url_for('verify_otp'))

            # Email imeshindwa → futa pending
            conn.execute(
                'DELETE FROM pending_registrations WHERE email = ?',
                (email,)
            )
            conn.commit()
            flash('Imeshindwa kutuma OTP. Jaribu tena baadaye.', 'error')
            return _reg_form(form_username=username, suggested_username=username)

        except sqlite3.IntegrityError:
            conn.rollback()
            flash('Username au Email hii tayari imeshasajiliwa!', 'error')
            return redirect(url_for('register'))

        except Exception as e:
            conn.rollback()
            print(f"REGISTER ERROR: {e}")
            traceback.print_exc()
            flash('Kuna tatizo limetokea wakati wa usajili. Jaribu tena.', 'error')
            return redirect(url_for('register'))

        finally:
            conn.close()

    @app.route('/api/check-username')
    def api_check_username():
        """AJAX: angalia kama username ipo free + toa mapendekezo."""
        raw = (request.args.get('username') or '').strip()
        email = (request.args.get('email') or '').strip().lower() or None
        username = sanitize_username(raw)

        if not username:
            return jsonify({
                'ok': False, 'available': False, 'username': '',
                'message': 'Andika username (angalau herufi 3).',
                'suggestions': []
            })

        if len(username) < 3:
            return jsonify({
                'ok': False, 'available': False, 'username': username,
                'message': 'Username iwe angalau herufi 3.',
                'suggestions': []
            })

        if len(username) > 30:
            return jsonify({
                'ok': False, 'available': False, 'username': username,
                'message': 'Username isizidi herufi 30.',
                'suggestions': []
            })

        conn = get_db_connection()
        try:
            taken = username_is_taken(conn, username, exclude_email=email)
            suggestions = []
            if taken:
                suggestions = suggest_usernames(
                    conn, username, limit=6, exclude_email=email
                )
                message = f'Username “{username}” imeshatumika.'
            else:
                message = f'Username “{username}” inapatikana ✓'
            return jsonify({
                'ok': True,
                'available': not taken,
                'username': username,
                'message': message,
                'suggestions': suggestions
            })
        finally:
            conn.close()

    # ====================== VERIFY OTP (REGISTER) ======================

    @app.route('/verify-otp', methods=['GET', 'POST'])
    def verify_otp():
        if 'pending_email' not in session:
            return redirect(url_for('register'))

        email = session['pending_email'].strip().lower()
        attempts_key = f'register:{email}'
        conn = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, username, email, password_hash, otp, expires_at, full_name
                FROM pending_registrations
                WHERE email = ?
                LIMIT 1
            ''', (email,))
            pending = cursor.fetchone()

            if not pending:
                session.pop('pending_email', None)
                flash('Usajili huu haupatikani. Tafadhali jisajili tena.', 'error')
                return redirect(url_for('register'))

            pending_id = pending['id']
            username = pending['username']
            pending_email = pending['email']
            password_hash = pending['password_hash']
            db_otp = pending['otp']
            full_name = _row_get(pending, 'full_name')

            expires_at = _parse_dt(pending['expires_at'])
            if not expires_at:
                flash('Muda wa OTP haujatambulika. Tafadhali jisajili tena.', 'error')
                return redirect(url_for('register'))

            if datetime.now() > expires_at:
                flash('OTP imeexpire. Bonyeza Resend OTP kupata code mpya.', 'error')
                return render_template('auth/verify_otp.html', email=email)

            # ---------------- GET ----------------
            if request.method != 'POST':
                return render_template('auth/verify_otp.html', email=email)

            # ---------------- POST ----------------
            user_otp = request.form.get('otp', '').strip()

            if not user_otp:
                flash('Tafadhali weka OTP.', 'error')
                return render_template('auth/verify_otp.html', email=email)

            if not _safe_equals(user_otp, db_otp):
                attempts = _add_attempt(conn, attempts_key)
                if attempts >= MAX_OTP_ATTEMPTS:
                    # Fanya OTP i-expire — lazima aombe mpya
                    cursor.execute(
                        'UPDATE pending_registrations SET expires_at = ? WHERE id = ?',
                        (datetime.now() - timedelta(seconds=1), pending_id)
                    )
                    conn.commit()
                    flash(
                        'Umejaribu mara nyingi mno. '
                        'Bonyeza Resend OTP kupata code mpya.',
                        'error'
                    )
                else:
                    conn.commit()
                    left = MAX_OTP_ATTEMPTS - attempts
                    flash(
                        f'OTP si sahihi. Majaribio yaliyobaki: {left}.',
                        'error'
                    )
                return render_template('auth/verify_otp.html', email=email)

            # ---------------- OTP SAHIHI ----------------
            existing_by_email = cursor.execute('''
                SELECT id, username, is_blocked, is_email_verified
                FROM users
                WHERE email = ? COLLATE NOCASE
                LIMIT 1
            ''', (pending_email,)).fetchone()

            is_new_account = False

            if existing_by_email:
                if _row_get(existing_by_email, 'is_blocked', 0) == 1:
                    session.pop('pending_email', None)
                    flash('Akaunti yako imezuiwa na Admin!', 'error')
                    return redirect(url_for('login'))

                if _row_get(existing_by_email, 'is_email_verified', 0) == 1:
                    session.pop('pending_email', None)
                    flash(
                        'Email hii tayari imethibitishwa. Tafadhali ingia.',
                        'error'
                    )
                    return redirect(url_for('login'))

                # User alikuwepo lakini email haikuthibitishwa → thibitisha
                user_id = existing_by_email['id']
                cursor.execute(
                    'UPDATE users SET is_email_verified = 1 WHERE id = ?',
                    (user_id,)
                )
            else:
                taken = cursor.execute(
                    'SELECT id FROM users WHERE username = ? LIMIT 1',
                    (username,)
                ).fetchone()
                if taken:
                    session.pop('pending_email', None)
                    flash('Username hii tayari imeshatumika. Jisajili tena.', 'error')
                    return redirect(url_for('register'))

                cursor.execute('''
                    INSERT INTO users (
                        username, email, password_hash,
                        is_verified, is_email_verified, full_name
                    )
                    VALUES (?, ?, ?, 0, 1, ?)
                ''', (username, pending_email, password_hash, full_name))
                user_id = cursor.lastrowid
                is_new_account = True

            cursor.execute(
                'DELETE FROM pending_registrations WHERE id = ?', (pending_id,)
            )
            try:
                cursor.execute(
                    "DELETE FROM otps WHERE email = ? AND purpose = 'register'",
                    (pending_email,)
                )
            except sqlite3.OperationalError:
                pass  # table otps haipo — endelea
            _reset_attempts(conn, attempts_key)
            conn.commit()

            user = conn.execute(
                'SELECT * FROM users WHERE id = ?', (user_id,)
            ).fetchone()
            if not user:
                flash('Imeshindikana kuunda akaunti. Jaribu tena.', 'error')
                return redirect(url_for('register'))

            # ---------------- AUTO LOGIN ----------------
            session.pop('pending_email', None)
            _set_login_session(
                user['id'], user['username'], user['email'],
                _row_get(user, 'role', 'user')
            )

            if not is_new_account:
                # User wa zamani aliyekamilisha uthibitisho
                apply_user_language(user)
                flash('Email imethibitishwa! Karibu tena.', 'success')
                return redirect(url_for('home'))

            lang = session.get('language', 'sw')
            if lang not in SUPPORTED_LANGUAGES:
                lang = 'sw'
            try:
                save_user_language(user['id'], lang)
            except Exception as e:
                print(f'[verify_otp] save_user_language error: {e}')
            session['language'] = lang
            session['from_registration'] = True

            flash(
                'Akaunti imethibitishwa! Tafadhali chagua lugha yako kwanza.',
                'success'
            )
            return redirect(url_for('change_language'))

        except sqlite3.IntegrityError:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            flash('Email au Username hii tayari imetumika.', 'error')
            return redirect(url_for('register'))

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print(f'VERIFY OTP ERROR: {e}')
            traceback.print_exc()
            flash(
                'Kuna tatizo limetokea wakati wa kuthibitisha OTP. Jaribu tena.',
                'error'
            )
            return render_template('auth/verify_otp.html', email=email)

        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/resend-otp', methods=['POST'])
    def resend_otp():
        if 'pending_email' not in session:
            flash('Hakuna usajili unaosubiri kuthibitishwa.', 'error')
            return redirect(url_for('register'))

        email = session['pending_email'].strip().lower()
        conn = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, resend_available_at
                FROM pending_registrations
                WHERE email = ?
                LIMIT 1
            ''', (email,))
            pending = cursor.fetchone()

            if not pending:
                session.pop('pending_email', None)
                flash('Usajili huu haupatikani. Tafadhali jisajili tena.', 'error')
                return redirect(url_for('register'))

            # ---------- Cooldown ----------
            resend_available_at = _parse_dt(pending['resend_available_at'])
            if resend_available_at and datetime.now() < resend_available_at:
                remaining = max(
                    1, int((resend_available_at - datetime.now()).total_seconds())
                )
                flash(
                    f'Tafadhali subiri sekunde {remaining} '
                    f'kabla ya kutuma OTP nyingine.',
                    'warning'
                )
                return redirect(url_for('verify_otp'))

            # ---------- OTP mpya ----------
            otp = generate_otp()
            now = datetime.now()
            expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
            resend_available_at = now + timedelta(seconds=RESEND_COOLDOWN_SECONDS)

            cursor.execute('''
                UPDATE pending_registrations
                SET otp = ?, expires_at = ?, resend_available_at = ?
                WHERE email = ?
            ''', (otp, expires_at, resend_available_at, email))
            _reset_attempts(conn, f'register:{email}')
            conn.commit()

            if send_otp_email(email, otp, purpose='register'):
                flash(
                    'OTP mpya imetumwa kwenye email yako. '
                    'Ina dakika 5 kabla ya ku-expire.',
                    'success'
                )
            else:
                flash('Imeshindwa kutuma OTP mpya. Jaribu tena baadaye.', 'error')

            return redirect(url_for('verify_otp'))

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print(f'RESEND OTP ERROR: {e}')
            traceback.print_exc()
            flash(
                'Kuna tatizo limetokea wakati wa kutuma OTP mpya. Jaribu tena.',
                'error'
            )
            return redirect(url_for('verify_otp'))

        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # ====================== LOGIN ======================

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = (request.form.get('email') or '').strip().lower()
            password = request.form.get('password') or ''

            conn = get_db_connection()
            try:
                user = conn.execute(
                    'SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                    (email,)
                ).fetchone()
            finally:
                conn.close()

            if user and check_password_hash(user['password_hash'], password):

                if _row_get(user, 'is_blocked', 0) == 1:
                    flash('Akaunti yako imezuiwa na Admin!', 'error')
                    return redirect(url_for('login'))

                # Email haijathibitishwa → tuma OTP mpya na peleka kuthibitisha
                if _row_get(user, 'is_email_verified', 0) != 1:
                    if _send_verification_for_existing_user(user):
                        session['pending_email'] = user['email'].strip().lower()
                        flash(
                            'Tafadhali thibitisha email yako kwanza kwa OTP.',
                            'warning'
                        )
                        return redirect(url_for('verify_otp'))
                    flash(
                        'Imeshindwa kutuma OTP ya kuthibitisha. Jaribu tena.',
                        'error'
                    )
                    return redirect(url_for('login'))

                # Deactivated → re-activate automatically
                try:
                    was_deactivated = int(_row_get(user, 'is_deactivated', 0) or 0)
                except Exception:
                    was_deactivated = 0
                if was_deactivated:
                    conn2 = get_db_connection()
                    try:
                        conn2.execute(
                            'UPDATE users SET is_deactivated = 0, deactivated_at = NULL WHERE id = ?',
                            (user['id'],)
                        )
                        conn2.commit()
                    finally:
                        conn2.close()

                _set_login_session(
                    user['id'], user['username'], user['email'],
                    _row_get(user, 'role', 'user')
                )
                apply_user_language(user)  # lugha ya permanent kutoka DB

                if was_deactivated:
                    flash('Karibu tena! Akaunti yako imerejeshwa kikamilifu.')
                else:
                    flash('Umeingia kikamilifu!')

                next_url = _safe_next(session.pop('next_after_login', None))
                if next_url:
                    return redirect(next_url)
                return redirect(url_for('home'))

            flash('Email au Nenosiri sio sahihi!', 'error')

        return render_template('auth/login.html')

    # ====================== FACEBOOK LOGIN ======================

    @app.route('/login/facebook')
    def facebook_login():
        """Anza Facebook OAuth.
        Redirect URI LAZIMA ilingane EXACT na ile iliyosajiliwa kwenye Facebook App
        (Facebook Login → Settings → Valid OAuth Redirect URIs).
        """
        try:
            redirect_uri = url_for('facebook_callback', _external=True).rstrip('/')
        except Exception as e:
            print('[FB] url_for error:', e)
            flash('Hitilafu ya mfumo: imeshindikana kutengeneza Facebook redirect URL.')
            return redirect(url_for('login'))

        print('[FB] login redirect_uri =', redirect_uri)

        if not FACEBOOK_APP_ID or not FACEBOOK_APP_SECRET:
            flash('Facebook Login haijasanidiwa (App ID / App Secret zinakosekana).')
            return redirect(url_for('login'))

        # CSRF protection
        state = secrets.token_urlsafe(24)
        session['fb_oauth_state'] = state

        params = {
            'client_id': FACEBOOK_APP_ID,
            'redirect_uri': redirect_uri,
            'scope': 'public_profile,email',
            'response_type': 'code',
            'state': state,
        }
        return redirect(f"{FACEBOOK_AUTHORIZE_URL}?{urlencode(params)}")

    @app.route('/login/facebook/callback')
    def facebook_callback():
        """Callback baada ya Facebook OAuth.

        1. Hakiki state (CSRF) + kama user amekataa / hakuna code
        2. code → access token
        3. Pata profile (id, name, email)
        4. Ipo kwenye DB (facebook_id au email) → LOGIN
        5. Haipo → UNDA akaunti mpya + login
        """
        def _fail(msg):
            flash(msg, 'error')
            return redirect(url_for('login'))

        expected_state = session.pop('fb_oauth_state', None)
        received_state = request.args.get('state')

        code = request.args.get('code')
        error = request.args.get('error')
        error_reason = (
            request.args.get('error_reason')
            or request.args.get('error_description')
            or ''
        )

        # ----- User amekataa au Facebook imerudisha error -----
        if error:
            print('[FB] OAuth error from Facebook:', error, error_reason)
            if error == 'access_denied':
                return _fail('Umekatisha Facebook Login. Jaribu tena ukitaka.')
            return _fail(f'Facebook Login imekataliwa: {error_reason or error}')

        # ----- CSRF state -----
        if not _safe_equals(expected_state, received_state):
            print('[FB] state mismatch')
            return _fail('Ombi la Facebook Login halikuthibitishwa. Jaribu tena.')

        if not code:
            print('[FB] callback bila code. args=', dict(request.args))
            return _fail('Facebook Login haikukamilika (hakuna authorization code). Jaribu tena.')

        try:
            redirect_uri = url_for('facebook_callback', _external=True).rstrip('/')
        except Exception as e:
            print('[FB] callback url_for error:', e)
            return _fail('Hitilafu ya mfumo wakati wa Facebook callback.')

        try:
            # ========== 1. ACCESS TOKEN ==========
            try:
                token_response = requests.get(
                    FACEBOOK_TOKEN_URL,
                    params={
                        'client_id': FACEBOOK_APP_ID,
                        'client_secret': FACEBOOK_APP_SECRET,
                        'redirect_uri': redirect_uri,
                        'code': code
                    },
                    timeout=20
                )
            except requests.Timeout:
                print('[FB] token request TIMEOUT')
                return _fail('Facebook imechukua muda mrefu kujibu. Angalia internet yako kisha jaribu tena.')
            except requests.RequestException as re_err:
                print('[FB] token request network error:', re_err)
                return _fail('Imeshindikana kuwasiliana na Facebook. Angalia muunganisho wa internet.')

            try:
                token_data = token_response.json()
            except ValueError:
                print('[FB] token response si JSON:', token_response.text[:300])
                return _fail('Jibu lisilotegemewa kutoka Facebook (token). Jaribu tena baadaye.')

            if 'error' in token_data:
                print('[FB] token error:', token_data)
                print('[FB] redirect_uri used:', redirect_uri)
                fb_err = token_data.get('error', {})
                if isinstance(fb_err, dict):
                    fb_detail = (
                        fb_err.get('message')
                        or fb_err.get('error_user_msg')
                        or fb_err.get('type')
                        or str(fb_err)
                    )
                    fb_code = fb_err.get('code')
                else:
                    fb_detail = str(fb_err)
                    fb_code = None

                hint = ''
                detail_lower = (fb_detail or '').lower()
                if 'redirect' in detail_lower or 'uri' in detail_lower:
                    hint = ' → Redirect URI haitalingani. Iwe EXACT: ' + redirect_uri
                elif 'secret' in detail_lower or 'client' in detail_lower or fb_code in (1, 101, 190):
                    hint = ' → Angalia App Secret / App ID kwenye Facebook Developer.'
                elif 'code' in detail_lower and ('expired' in detail_lower or 'invalid' in detail_lower):
                    hint = ' → Code imeisha muda. Bonyeza Continue with Facebook tena.'

                return _fail(
                    'Imeshindikana kupata Facebook access token. '
                    f'Detail: {str(fb_detail)[:160]}{hint}'
                )

            access_token = token_data.get('access_token')
            if not access_token:
                print('[FB] no access_token in response:', token_data)
                return _fail(
                    'Imeshindikana kupata Facebook access token (hakuna token). '
                    'Angalia App Secret na Valid OAuth Redirect URI.'
                )

            # ========== 2. USER PROFILE ==========
            try:
                user_response = requests.get(
                    FACEBOOK_USER_INFO_URL,
                    params={
                        'fields': 'id,name,email,picture.type(large)',
                        'access_token': access_token
                    },
                    timeout=20
                )
                facebook_user = user_response.json()
            except requests.Timeout:
                return _fail('Facebook imechukua muda mrefu kutoa taarifa za profile. Jaribu tena.')
            except Exception as e:
                print('[FB] user info request error:', e)
                return _fail('Imeshindikana kupata taarifa za akaunti yako ya Facebook.')

            if 'error' in facebook_user:
                print('[FB] user info error:', facebook_user)
                fb_err = facebook_user.get('error', {})
                msg = fb_err.get('message') if isinstance(fb_err, dict) else str(fb_err)
                return _fail(f'Facebook haikutoa taarifa za mtumiaji: {str(msg)[:120]}')

            facebook_id = str(facebook_user.get('id') or '').strip()
            name = (facebook_user.get('name') or '').strip()
            email = (facebook_user.get('email') or '').strip().lower()
            email_from_fb = bool(email)

            if not facebook_id:
                return _fail('Facebook haikutoa ID ya mtumiaji. Jaribu tena au tumia email/password.')

            # Email inaweza kukosekana kama user hakutoa ruhusa
            if not email:
                email = f'fb_{facebook_id}@facebook.local'
                print('[FB] email haipo — placeholder:', email)

            try:
                picture_url = (
                    facebook_user.get('picture', {}).get('data', {}).get('url')
                )
            except Exception:
                picture_url = None

            # ========== 3. DATABASE ==========
            try:
                conn = get_db_connection()
            except Exception as e:
                print('[FB] DB connection error:', e)
                return _fail('Hitilafu ya database. Jaribu tena baadaye.')

            try:
                _ensure_facebook_id_column(conn)

                # Tafuta kwa facebook_id kwanza, kisha kwa email
                user = conn.execute(
                    'SELECT * FROM users WHERE facebook_id = ? LIMIT 1',
                    (facebook_id,)
                ).fetchone()
                if not user:
                    user = conn.execute(
                        'SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                        (email,)
                    ).fetchone()

                # ----- ACCOUNT ILIYOPO → LOGIN -----
                if user:
                    if _row_get(user, 'is_blocked', 0) == 1:
                        return _fail('Akaunti yako imezuiwa na Admin. Wasiliana na support.')

                    try:
                        if _row_get(user, 'is_deactivated', 0) == 1:
                            conn.execute(
                                'UPDATE users SET is_deactivated = 0, deactivated_at = NULL WHERE id = ?',
                                (user['id'],)
                            )
                        conn.execute(
                            "UPDATE users SET auth_provider = 'facebook' "
                            "WHERE id = ? AND (auth_provider IS NULL OR auth_provider = 'local' OR auth_provider = '')",
                            (user['id'],)
                        )
                        if picture_url and not _row_get(user, 'profile_pic'):
                            conn.execute(
                                'UPDATE users SET profile_pic = ? WHERE id = ?',
                                (picture_url, user['id'])
                            )
                        conn.commit()
                    except Exception as e:
                        print('[FB] update existing user warning:', e)

                    _save_facebook_id(conn, user['id'], facebook_id)

                    session.clear()
                    _set_login_session(
                        user['id'], user['username'], user['email'],
                        _row_get(user, 'role', 'user')
                    )
                    try:
                        apply_user_language(user)
                    except Exception:
                        session['language'] = 'sw'

                    if email_from_fb:
                        flash('Umeingia kupitia Facebook kikamilifu! (akaunti iliyopo)')
                    else:
                        flash('Umeingia kupitia Facebook kikamilifu!')
                    return redirect(url_for('home'))

                # ----- ACCOUNT MPYA -----
                try:
                    username = _pick_fb_username(conn, name, email, facebook_id)
                except Exception as e:
                    print('[FB] pick username error:', e)
                    username = f'fb_{facebook_id[-16:]}'

                password_hash = generate_password_hash(secrets.token_urlsafe(24))
                full_name = name[:80] if name else username
                created = now_tz()

                try:
                    new_id = _insert_facebook_user(
                        conn, username, email, password_hash,
                        full_name, picture_url, created
                    )
                except sqlite3.IntegrityError as ie:
                    conn.rollback()
                    print('[FB] IntegrityError on create:', ie)

                    # Labda email sasa ipo (race) → login kama existing
                    user = conn.execute(
                        'SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                        (email,)
                    ).fetchone()
                    if user:
                        _save_facebook_id(conn, user['id'], facebook_id)
                        session.clear()
                        _set_login_session(
                            user['id'], user['username'], user['email'],
                            _row_get(user, 'role', 'user')
                        )
                        session['language'] = 'sw'
                        flash('Umeingia kupitia Facebook kikamilifu! (akaunti iliyopo)')
                        return redirect(url_for('home'))

                    # Username conflict → jaribu username nyingine
                    username = f'fb_{facebook_id[-14:]}'
                    try:
                        new_id = _insert_facebook_user(
                            conn, username, email, password_hash,
                            full_name, picture_url, created
                        )
                    except Exception as e2:
                        conn.rollback()
                        print('[FB] second insert failed:', e2)
                        return _fail(
                            'Imeshindikana kuunda akaunti ya Facebook '
                            '(email au username inatumika). Jaribu tena au tumia login ya kawaida.'
                        )
                except Exception as e:
                    conn.rollback()
                    print('[FB] create user error:', e)
                    traceback.print_exc()
                    return _fail(
                        'Kuna tatizo wakati wa kuunda akaunti ya Facebook. '
                        f'Sababu: {str(e)[:140]}'
                    )

                _save_facebook_id(conn, new_id, facebook_id)

                session.clear()
                _set_login_session(new_id, username, email, 'user')
                session['language'] = 'sw'

                flash('Karibu! Akaunti yako ya Facebook imeundwa kikamilifu.')
                return redirect(url_for('home'))

            except Exception as e:
                print('[FB] DB/logic error:', e)
                traceback.print_exc()
                return _fail('Hitilafu wakati wa kuhifadhi taarifa za Facebook. Jaribu tena.')

            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        except Exception as e:
            print('FACEBOOK LOGIN ERROR (outer):', e)
            traceback.print_exc()
            return _fail(
                'Kuna tatizo lisilotegemewa wakati wa kuingia kupitia Facebook. '
                'Angalia App Secret, Redirect URI, na jaribu tena.'
            )

    # ====================== GOOGLE LOGIN ======================

    @app.route('/auth/google', methods=['POST'])
    def google_auth():
        token = request.form.get('credential')

        # CSRF protection — njia mbili:
        # 1) Redirect mode (data-login_uri): Google inatuma g_csrf_token
        #    kwenye cookie na form; lazima zilingane.
        # 2) JS callback mode: token hiyo haipo, kwa hiyo tunahakiki
        #    kwamba ombi linatoka kwenye site yetu (Origin/Referer).
        cookie_csrf = request.cookies.get('g_csrf_token')
        body_csrf = request.form.get('g_csrf_token')

        if cookie_csrf or body_csrf:
            if not _safe_equals(cookie_csrf, body_csrf):
                print('[google_auth] g_csrf_token mismatch')
                flash('Ombi la Google login halikuthibitishwa. Jaribu tena.')
                return redirect(url_for('login'))
        else:
            origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
            origin_host = urlparse(origin).netloc
            if origin_host and origin_host != request.host:
                print('[google_auth] origin mismatch:', origin_host, '!=', request.host)
                flash('Ombi la Google login halikuthibitishwa. Jaribu tena.')
                return redirect(url_for('login'))

        if not token:
            flash('Google login imeshindikana. Token haipo.')
            return redirect(url_for('login'))

        if id_token is None or google_requests is None:
            print('[google_auth] ERROR: google-auth library haipo. Install: pip install google-auth')
            flash('Google login haijawekwa vizuri kwenye server. Wasiliana na admin.')
            return redirect(url_for('login'))

        conn = None
        try:
            idinfo = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                GOOGLE_CLIENT_ID
            )

            email = (idinfo.get('email') or '').strip().lower()
            name = idinfo.get('name', '') or ''
            picture = idinfo.get('picture', '') or ''

            if not email:
                flash('Google haikutoa email. Jaribu tena.')
                return redirect(url_for('login'))

            if idinfo.get('email_verified') is False:
                flash('Email ya Google haijathibitishwa. Tumia akaunti nyingine.')
                return redirect(url_for('login'))

            # Kama Google haikutoa picha, tengeneza avatar ya herufi
            if not picture:
                avatar_name = name.strip() if name else email.split('@')[0]
                picture = (
                    'https://ui-avatars.com/api/?name=' + quote(avatar_name)
                    + '&background=random&color=fff&size=128&bold=true'
                )

            conn = get_db_connection()
            cursor = conn.cursor()

            user = cursor.execute(
                'SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                (email,)
            ).fetchone()

            # ---------- USER YUPO → LOGIN ----------
            if user:
                if _row_get(user, 'is_blocked', 0) == 1:
                    flash('Akaunti yako imezuiwa na Admin!')
                    return redirect(url_for('login'))

                try:
                    was_deactivated = int(_row_get(user, 'is_deactivated', 0) or 0)
                except Exception:
                    was_deactivated = 0
                if was_deactivated:
                    cursor.execute(
                        'UPDATE users SET is_deactivated = 0, deactivated_at = NULL WHERE id = ?',
                        (user['id'],)
                    )
                    conn.commit()

                try:
                    cursor.execute(
                        "UPDATE users SET auth_provider = 'google' "
                        "WHERE id = ? AND (auth_provider IS NULL OR auth_provider = '' OR auth_provider = 'local')",
                        (user['id'],)
                    )
                except Exception:
                    pass

                try:
                    cur_fn = _row_get(user, 'full_name', '') or ''
                    if name and not str(cur_fn).strip():
                        cursor.execute(
                            'UPDATE users SET full_name = ? WHERE id = ?',
                            (name[:80], user['id'])
                        )
                    cur_pic = _row_get(user, 'profile_pic', '') or ''
                    if picture and (not cur_pic or str(cur_pic).startswith('http')):
                        cursor.execute(
                            'UPDATE users SET profile_pic = ? WHERE id = ?',
                            (picture, user['id'])
                        )
                    conn.commit()
                except Exception as e:
                    print('[google_auth] update profile:', e)

                _set_login_session(
                    user['id'], user['username'], user['email'],
                    _row_get(user, 'role', 'user')
                )
                apply_user_language(user)  # lugha ya permanent kutoka DB

                if was_deactivated:
                    flash(f'Karibu tena, {user["username"]}! Akaunti yako imerejeshwa.')
                else:
                    flash(f'Karibu tena, {user["username"]}!')

                next_url = _safe_next(session.pop('next_after_login', None))
                if next_url:
                    return redirect(next_url)
                return redirect(url_for('home'))

            # ---------- USER MPYA → REGISTER AUTOMATIC ----------
            full_name = name.strip()[:80] or None
            base_username = _username_from_google_name(name, email)

            username = base_username
            counter = 1
            while cursor.execute(
                'SELECT id FROM users WHERE username = ?', (username,)
            ).fetchone():
                username = f'{base_username}{counter}'
                counter += 1
                if counter > 9999:
                    username = 'user' + secrets.token_hex(4)
                    break

            password_hash = generate_password_hash(secrets.token_urlsafe(32))
            pic_val = picture or None

            try:
                cursor.execute(
                    "INSERT INTO users (username, email, password_hash, full_name, profile_pic, bio, "
                    "is_verified, is_email_verified, auth_provider) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'google')",
                    (username, email, password_hash, full_name, pic_val, '')
                )
            except Exception as e:
                print('[google_auth] insert full_name failed:', e)
                conn.rollback()
                try:
                    cursor.execute(
                        "INSERT INTO users (username, email, password_hash, profile_pic, bio, "
                        "is_verified, is_email_verified, auth_provider) "
                        "VALUES (?, ?, ?, ?, ?, 0, 1, 'google')",
                        (username, email, password_hash, pic_val, '')
                    )
                    new_row_id = cursor.lastrowid
                    if full_name:
                        try:
                            cursor.execute(
                                'UPDATE users SET full_name = ? WHERE id = ?',
                                (full_name, new_row_id)
                            )
                        except Exception:
                            pass
                except Exception as e2:
                    print('[google_auth] insert fallback:', e2)
                    conn.rollback()
                    cursor.execute(
                        "INSERT INTO users (username, email, password_hash, profile_pic, bio, "
                        "is_verified, is_email_verified) VALUES (?, ?, ?, ?, ?, 0, 1)",
                        (username, email, password_hash, pic_val, '')
                    )

            conn.commit()

            new_row = cursor.execute(
                'SELECT id FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                (email,)
            ).fetchone()
            new_user_id = new_row['id']

            _set_login_session(new_user_id, username, email, 'user')

            lang = session.get('language', 'sw')
            if lang not in SUPPORTED_LANGUAGES:
                lang = 'sw'
            save_user_language(new_user_id, lang)
            session['language'] = lang
            session['from_registration'] = True

            display = full_name or username
            flash('Karibu %s! Tafadhali chagua lugha yako kwanza.' % display)
            return redirect(url_for('change_language'))

        except ValueError as ve:
            print('[google_auth] ValueError (token si sahihi):', ve)
            flash('Google token si sahihi. Jaribu tena.')
            return redirect(url_for('login'))
        except Exception as e:
            print('[google_auth] ERROR:', e)
            traceback.print_exc()
            flash('Kuna tatizo la Google login. Jaribu tena baadaye.')
            return redirect(url_for('login'))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # ====================== LOGOUT ======================

    @app.route('/logout')
    def logout():
        session.clear()
        flash('Umetoka kwenye mfumo.')
        return redirect(url_for('login'))

    # ====================== FORGOT / RESET PASSWORD ======================

    @app.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password():
        if request.method == 'POST':
            email = (request.form.get('email') or '').strip().lower()
            if not email:
                flash('Tafadhali weka email yako.', 'error')
                return redirect(url_for('forgot_password'))

            sent_ok = True
            conn = get_db_connection()
            try:
                user = conn.execute(
                    'SELECT id FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                    (email,)
                ).fetchone()

                if user:
                    otp = generate_otp()
                    expires_at = datetime.now() + timedelta(minutes=OTP_TTL_MINUTES)

                    # Futa OTP za zamani ili iwe moja tu halali
                    conn.execute(
                        "DELETE FROM otps WHERE email = ? AND purpose = 'reset'",
                        (email,)
                    )
                    conn.execute(
                        'INSERT INTO otps (email, otp, purpose, expires_at) VALUES (?, ?, ?, ?)',
                        (email, otp, 'reset', expires_at)
                    )
                    _reset_attempts(conn, f'reset:{email}')
                    conn.commit()
                    sent_ok = bool(send_otp_email(email, otp, purpose='reset'))
            finally:
                conn.close()

            if not sent_ok:
                flash('Imeshindwa kutuma OTP. Jaribu tena.', 'error')
                return redirect(url_for('forgot_password'))

            # Majibu ni sawa iwe email ipo au haipo (usifichue akaunti)
            session['reset_email'] = email
            session.pop('reset_verified', None)
            session['reset_resend_at'] = (
                datetime.now() + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
            ).isoformat()
            flash(
                'Ikiwa email ipo, OTP imetumwa. Inaexpire baada ya dakika 5.',
                'success'
            )
            return redirect(url_for('verify_reset_otp'))

        return render_template('auth/forgot_password.html')

    @app.route('/verify-reset-otp', methods=['GET', 'POST'])
    def verify_reset_otp():
        if 'reset_email' not in session:
            return redirect(url_for('forgot_password'))

        email = session['reset_email'].strip().lower()

        if request.method == 'POST':
            user_otp = request.form.get('otp', '').strip()
            attempts_key = f'reset:{email}'

            conn = get_db_connection()
            try:
                # Tayari amefikia kikomo cha majaribio?
                if _get_attempts(conn, attempts_key) >= MAX_OTP_ATTEMPTS:
                    flash(
                        'Umejaribu mara nyingi mno. '
                        'Bonyeza “Tuma OTP tena” kupata code mpya.',
                        'error'
                    )
                    return render_template('auth/verify_reset_otp.html', email=email)

                result = conn.execute('''
                    SELECT otp, expires_at FROM otps
                    WHERE email = ? AND purpose = 'reset'
                    ORDER BY id DESC LIMIT 1
                ''', (email,)).fetchone()

                if not result:
                    # Tunahesabu kama jaribio ovu (haifichui kama email ipo)
                    _add_attempt(conn, attempts_key)
                    conn.commit()
                    flash('OTP si sahihi.', 'error')
                    return render_template('auth/verify_reset_otp.html', email=email)

                db_otp, raw_expires = result[0], result[1]
                expires_at = _parse_dt(raw_expires)

                if not expires_at or datetime.now() > expires_at:
                    flash(
                        'OTP imeexpire. Bonyeza “Tuma OTP tena” kupata code mpya.',
                        'error'
                    )
                    return render_template('auth/verify_reset_otp.html', email=email)

                if not _safe_equals(user_otp, db_otp):
                    attempts = _add_attempt(conn, attempts_key)
                    if attempts >= MAX_OTP_ATTEMPTS:
                        conn.execute(
                            "DELETE FROM otps WHERE email = ? AND purpose = 'reset'",
                            (email,)
                        )
                        conn.commit()
                        flash(
                            'Umejaribu mara nyingi mno. '
                            'Bonyeza “Tuma OTP tena” kupata code mpya.',
                            'error'
                        )
                    else:
                        conn.commit()
                        left = MAX_OTP_ATTEMPTS - attempts
                        flash(
                            f'OTP si sahihi. Majaribio yaliyobaki: {left}.',
                            'error'
                        )
                    return render_template('auth/verify_reset_otp.html', email=email)

                # OTP sahihi
                conn.execute(
                    "DELETE FROM otps WHERE email = ? AND purpose = 'reset'",
                    (email,)
                )
                _reset_attempts(conn, attempts_key)
                conn.commit()
            finally:
                conn.close()

            session['reset_verified'] = True
            session.pop('reset_resend_at', None)
            flash('OTP imethibitishwa. Sasa weka nenosiri jipya.', 'success')
            return redirect(url_for('reset_password'))

        return render_template('auth/verify_reset_otp.html', email=email)

    @app.route('/resend-reset-otp', methods=['POST'])
    def resend_reset_otp():
        """Tuma OTP mpya ya reset password ikiwa ya kwanza haikufika."""
        if 'reset_email' not in session:
            flash('Hakuna ombi la kubadilisha nenosiri linalosubiri.', 'error')
            return redirect(url_for('forgot_password'))

        email = session['reset_email'].strip().lower()

        # Rate limit: sekunde 60
        resend_at = _parse_dt(session.get('reset_resend_at'))
        if resend_at and datetime.now() < resend_at:
            remaining = max(1, int((resend_at - datetime.now()).total_seconds()))
            flash(
                f'Tafadhali subiri sekunde {remaining} kabla ya kutuma OTP nyingine.',
                'warning'
            )
            return redirect(url_for('verify_reset_otp'))

        sent_ok = True
        conn = get_db_connection()
        try:
            user = conn.execute(
                'SELECT id FROM users WHERE email = ? COLLATE NOCASE LIMIT 1',
                (email,)
            ).fetchone()

            # Email haipo → jifanye kama imetumwa (usifichue akaunti)
            if user:
                otp = generate_otp()
                expires_at = datetime.now() + timedelta(minutes=OTP_TTL_MINUTES)
                conn.execute(
                    "DELETE FROM otps WHERE email = ? AND purpose = 'reset'",
                    (email,)
                )
                conn.execute(
                    'INSERT INTO otps (email, otp, purpose, expires_at) VALUES (?, ?, ?, ?)',
                    (email, otp, 'reset', expires_at)
                )
                _reset_attempts(conn, f'reset:{email}')
                conn.commit()
                sent_ok = bool(send_otp_email(email, otp, purpose='reset'))
        finally:
            conn.close()

        session['reset_resend_at'] = (
            datetime.now() + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        ).isoformat()

        if sent_ok:
            flash(
                'Ikiwa email ipo, OTP mpya imetumwa. '
                'Ina dakika 5 kabla ya ku-expire.',
                'success'
            )
        else:
            flash('Imeshindwa kutuma OTP mpya. Jaribu tena baadaye.', 'error')

        return redirect(url_for('verify_reset_otp'))

    @app.route('/reset-password', methods=['GET', 'POST'])
    def reset_password():
        if 'reset_email' not in session or not session.get('reset_verified'):
            return redirect(url_for('forgot_password'))

        email = session['reset_email'].strip().lower()

        if request.method == 'POST':
            password = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')

            if not password or len(password) < 6:
                flash('Nenosiri liwe angalau herufi 6.', 'error')
                return render_template('auth/reset_password.html')

            if password != confirm:
                flash('Nenosiri hazifanani.', 'error')
                return render_template('auth/reset_password.html')

            password_hash = generate_password_hash(password)
            conn = get_db_connection()
            try:
                conn.execute(
                    'UPDATE users SET password_hash = ? WHERE email = ? COLLATE NOCASE',
                    (password_hash, email)
                )
                conn.commit()
            finally:
                conn.close()

            session.pop('reset_email', None)
            session.pop('reset_verified', None)
            session.pop('reset_resend_at', None)

            flash('Nenosiri limebadilishwa kwa mafanikio! Sasa unaweza kuingia.', 'success')
            return redirect(url_for('login'))

        return render_template('auth/reset_password.html')

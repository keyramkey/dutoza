"""
helpers.py — Shared utilities (Kijiji Tanzania)
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os, sqlite3, time, secrets, uuid, json, random, re, requests
from urllib.parse import quote
from io import BytesIO
from functools import wraps
from flask import request, redirect, url_for, session, flash, jsonify, current_app
from werkzeug.utils import secure_filename
from pywebpush import webpush, WebPushException
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    qrcode = ERROR_CORRECT_H = Image = ImageDraw = ImageFont = None

from db import get_db_connection, BASE_DIR, DB_PATH, UPLOAD_FOLDER, UPLOAD_BADGES_FOLDER

app = None
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "HWkzToktm9g98f8srg5Lo6MPDggTrxwGFfwzLnq7XYQ")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "BDLkkmrKM007eSEby3amhKG3or37FOULg6bpmgnKL_M2HsNLxZXhIVA9fmqva0YPYVd3wo3U4omh_CwZUMqHZAo")
VAPID_CLAIMS = {"sub": "mailto:keyaramadhan0@gmail.com"}
GMAIL_ADDRESS = "matondomaduhu135@gmail.com"
GMAIL_APP_PASSWORD = "neftrxmcxidjourc"
SUPPORTED_LANGUAGES = {
    'sw': {'name': 'Kiswahili', 'flag': '🇹🇿'},
    'en': {'name': 'English', 'flag': '🇬🇧'},
    'fr': {'name': 'Français', 'flag': '🇫🇷'},
    'es': {'name': 'Español', 'flag': '🇪🇸'},
    'pt': {'name': 'Português', 'flag': '🇵🇹'},
    'de': {'name': 'Deutsch', 'flag': '🇩🇪'},
    'it': {'name': 'Italiano', 'flag': '🇮🇹'},
    'ar': {'name': 'العربية', 'flag': '🇸🇦'},
    'hi': {'name': 'हिन्दी', 'flag': '🇮🇳'},
    'zh': {'name': '中文', 'flag': '🇨🇳'},
    'ru': {'name': 'Русский', 'flag': '🇷🇺'},
    'tr': {'name': 'Türkçe', 'flag': '🇹🇷'},
}
LINKUP_CATEGORIES = [
    ('michezo', 'Michezo'), ('muziki', 'Muziki'), ('biashara', 'Biashara'),
    ('comedy', 'Comedy / Ucheshi'), ('habari', 'Habari / News'),
    ('teknolojia', 'Teknolojia'), ('elimu', 'Elimu'), ('dini', 'Dini'),
    ('siasa', 'Siasa'), ('afya', 'Afya'), ('burudani', 'Burudani'),
    ('fashion', 'Fashion / Mitindo'), ('chakula', 'Chakula'),
    ('usafiri', 'Usafiri'), ('jumuiya', 'Jumuiya'), ('general', 'Nyingine'),
]
MAX_LINKUPS_PER_USER = 3
IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
VIDEO_EXTS = {'mp4', 'webm', 'ogg', 'mov', 'avi'}
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_VIDEO_BYTES = 24 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif',
    'mp4', 'webm', 'ogg', 'mov', 'avi', '3gp', 'mkv',
    'mp3', 'wav', 'aac', 'm4a', 'opus',
    'pdf', 'doc', 'docx', 'txt', 'zip', 'rar', '7z',
    'html', 'htm', 'css', 'js', 'json', 'csv', 'xlsx', 'xls', 'ppt', 'pptx'
}
RESTRICTION_HOURS = 2
WARNINGS_BEFORE_RESTRICTION = 3
TRANSLATIONS_DIR = os.path.join(BASE_DIR, 'translations')

def inject_linkup_identity():
    """Global template vars: identity_is_linkup, identity_url, active_linkup, ..."""
    out = {
        'identity_is_linkup': False,
        'identity_url': None,
        'identity_name': None,
        'identity_username': None,
        'identity_pic': None,
        'active_linkup': None,
        'posting_as_linkup': False,
    }
    try:
        uid = session.get('user_id')
        if not uid:
            return out
        conn = get_db_connection()
        try:
            lu = resolve_active_linkup(conn, uid)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if lu:
            uname = lu.get('username') or ''
            out.update({
                'identity_is_linkup': True,
                'identity_url': f'/linkup/{uname}',
                'identity_name': lu.get('display_name') or uname,
                'identity_username': uname,
                'identity_pic': lu.get('profile_pic'),
                'active_linkup': lu,
                'posting_as_linkup': True,
            })
    except Exception as e:
        print('[inject_linkup_identity]', e)
    return out


def init_helpers(_app, **kwargs):
    global app
    app = _app
    for k, v in kwargs.items():
        if k.upper() in globals():
            globals()[k.upper()] = v
    app.config.setdefault('UPLOAD_FOLDER', UPLOAD_FOLDER)
    app.config.setdefault('UPLOAD_BADGES_FOLDER', UPLOAD_BADGES_FOLDER)
    app.config.setdefault('MAX_CONTENT_LENGTH', 24 * 1024 * 1024)
    # Linkup identity available on EVERY template (base, feed, chat, ...)
    try:
        app.context_processor(inject_linkup_identity)
    except Exception as e:
        print('[init_helpers] context_processor:', e)

def load_translations(language='sw'):
    """Optional JSON-based translations (fallback). Babel .po/.mo is primary."""
    if language not in SUPPORTED_LANGUAGES:
        language = 'sw'

    file_path = os.path.join(TRANSLATIONS_DIR, f'{language}.json')

    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}




def apply_user_language(user):
    """Weka lugha ya user kwenye session (baada ya login)."""
    lang = 'sw'
    if user is not None:
        try:
            if hasattr(user, 'keys') and 'language' in user.keys():
                lang = user['language'] or 'sw'
            elif isinstance(user, dict) and user.get('language'):
                lang = user['language']
        except Exception:
            pass
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'sw'
    session['language'] = lang
    session.modified = True
    return lang


def save_user_language(user_id, language):
    """Hifadhi lugha kwenye DB kwa user (permanent across devices)."""
    if not user_id or language not in SUPPORTED_LANGUAGES:
        return
    try:
        conn = get_db_connection()
        conn.execute(
            'UPDATE users SET language = ? WHERE id = ?',
            (language, user_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print('[language] save error:', e)

def avatar_url(pic, name=None):
    """URL ya profile pic: Google https, local upload, au letter-avatar."""
    if pic:
        s = str(pic).strip()
        if s.startswith('http://') or s.startswith('https://'):
            return s
        # local filename
        try:
            return url_for('static', filename='uploads/' + s)
        except Exception:
            return '/static/uploads/' + s
    # Hakuna picha → letter avatar (kama Google default A, B, C...)
    label = (name or 'U').strip() or 'U'
    # ui-avatars: herufi + rangi tofauti kulingana na jina
    return (
        'https://ui-avatars.com/api/?name='
        + quote(label)
        + '&background=random&color=fff&size=128&bold=true'
    )


def get_profile_public_url(username):
    """Link ya public ya profile (inatumika ndani ya QR)."""
    try:
        return url_for('profile', username=username, _external=True)
    except Exception:
        try:
            base = request.host_url.rstrip('/') if request else ''
            return f"{base}/profile/{username}"
        except Exception:
            return f"/profile/{username}"


def _load_profile_avatar_pil(profile_user, size=120):
    """Pakua / fungua profile pic kama PIL Image (circle)."""
    if Image is None:
        return None
    pic = None
    try:
        pic = profile_user['profile_pic'] if hasattr(profile_user, 'keys') else profile_user.get('profile_pic')
    except Exception:
        pic = None

    img = None
    try:
        if pic:
            s = str(pic).strip()
            if s.startswith('http://') or s.startswith('https://'):
                r = requests.get(s, timeout=8)
                if r.status_code == 200:
                    img = Image.open(BytesIO(r.content)).convert('RGBA')
            else:
                local = os.path.join(app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER), s)
                if os.path.isfile(local):
                    img = Image.open(local).convert('RGBA')
    except Exception as e:
        print('[qr] avatar load error:', e)
        img = None

    if img is None:
        name = 'U'
        try:
            name = (profile_user['full_name'] or profile_user['username'] or 'U')
        except Exception:
            pass
        letter = (str(name).strip() or 'U')[0].upper()
        img = Image.new('RGBA', (size, size), (30, 181, 58, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', max(size // 2, 24))
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), letter, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((size - tw) / 2, (size - th) / 2 - 4), letter, fill='white', font=font)

    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    output = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    output.paste(img, (0, 0), mask=mask)
    return output


def generate_profile_qr_card(profile_user, box_size=8):
    """
    QR card: jina + @username + avatar + QR ya profile URL.
    Rudisha PIL Image (RGB).
    """
    if qrcode is None or Image is None:
        raise RuntimeError('Install: pip install "qrcode[pil]" Pillow')

    username = profile_user['username']
    try:
        full_name = profile_user['full_name'] if profile_user['full_name'] else username
    except Exception:
        full_name = username
    profile_url = get_profile_public_url(username)

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=box_size,
        border=2,
    )
    qr.add_data(profile_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color='#0f172a', back_color='white').convert('RGBA')

    padding = 28
    avatar_size = 96
    qr_size = qr_img.size[0]
    width = max(qr_size + padding * 2, 360)
    header_h = 130
    height = header_h + qr_size + padding * 2 + 36

    card = Image.new('RGB', (width, height), '#ffffff')
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width, 8], fill='#1EB53A')

    avatar = _load_profile_avatar_pil(profile_user, size=avatar_size)
    if avatar:
        ax = (width - avatar_size) // 2
        card.paste(avatar, (ax, 20), avatar)

    try:
        font_name = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 20)
        font_user = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 14)
        font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)
    except Exception:
        font_name = font_user = font_small = ImageFont.load_default()

    def center_text(text, font, y, fill='#0f172a'):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((width - tw) / 2, y), text, font=font, fill=fill)

    name_text = (str(full_name) or username)[:32]
    center_text(name_text, font_name, 20 + avatar_size + 8, '#0f172a')
    center_text(f'@{username}', font_user, 20 + avatar_size + 32, '#64748b')

    qx = (width - qr_size) // 2
    qy = header_h + 8
    card.paste(qr_img, (qx, qy), qr_img if qr_img.mode == 'RGBA' else None)
    center_text('Kijiji Tanzania · Scan to open profile', font_small, height - 28, '#94a3b8')
    return card.convert('RGB')


# ====================== LINKUP HELPERS ======================

LINKUP_CATEGORIES = [
    ('michezo', 'Michezo'),
    ('muziki', 'Muziki'),
    ('biashara', 'Biashara'),
    ('comedy', 'Comedy / Ucheshi'),
    ('habari', 'Habari / News'),
    ('teknolojia', 'Teknolojia'),
    ('elimu', 'Elimu'),
    ('dini', 'Dini'),
    ('siasa', 'Siasa'),
    ('afya', 'Afya'),
    ('burudani', 'Burudani'),
    ('fashion', 'Fashion / Mitindo'),
    ('chakula', 'Chakula'),
    ('usafiri', 'Usafiri'),
    ('jumuiya', 'Jumuiya'),
    ('general', 'Nyingine'),
]

MAX_LINKUPS_PER_USER = 3


def user_has_kijiji_mode(conn, user_id):
    try:
        row = conn.execute(
            'SELECT village_mode FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        return bool(row and row['village_mode'])
    except Exception:
        return False


def get_user_linkups(conn, user_id, active_only=True):
    try:
        if active_only:
            rows = conn.execute(
                'SELECT * FROM linkups WHERE owner_user_id = ? AND COALESCE(is_active,1)=1 ORDER BY id ASC',
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM linkups WHERE owner_user_id = ? ORDER BY id ASC',
                (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_linkup_by_id(conn, linkup_id):
    try:
        row = conn.execute('SELECT * FROM linkups WHERE id = ?', (linkup_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_linkup_by_username(conn, username):
    try:
        row = conn.execute(
            'SELECT * FROM linkups WHERE username = ? COLLATE NOCASE AND COALESCE(is_active,1)=1',
            (username,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def linkup_username_taken(conn, username, exclude_id=None):
    """Username unique across users + linkups."""
    u = sanitize_username(username)
    if not u:
        return True
    row = conn.execute(
        'SELECT id FROM users WHERE username = ? COLLATE NOCASE LIMIT 1', (u,)
    ).fetchone()
    if row:
        return True
    if exclude_id:
        row = conn.execute(
            'SELECT id FROM linkups WHERE username = ? COLLATE NOCASE AND id != ? LIMIT 1',
            (u, exclude_id)
        ).fetchone()
    else:
        row = conn.execute(
            'SELECT id FROM linkups WHERE username = ? COLLATE NOCASE LIMIT 1', (u,)
        ).fetchone()
    return bool(row)


def get_active_linkup_id():
    """Session active Linkup (None = posting as personal profile)."""
    try:
        lid = session.get('active_linkup_id')
        return int(lid) if lid else None
    except (TypeError, ValueError):
        return None


def set_active_linkup_id(linkup_id):
    if linkup_id is None:
        session.pop('active_linkup_id', None)
    else:
        session['active_linkup_id'] = int(linkup_id)
    session.modified = True


def resolve_active_linkup(conn, user_id):
    """Rudisha linkup dict ikiwa session ina active linkup ya user huyu, else None."""
    lid = get_active_linkup_id()
    if not lid:
        return None
    lu = get_linkup_by_id(conn, lid)
    if not lu or int(lu['owner_user_id']) != int(user_id) or not lu.get('is_active', 1):
        set_active_linkup_id(None)
        return None
    return lu


def is_following_linkup(conn, user_id, linkup_id):
    if not user_id or not linkup_id:
        return False
    try:
        row = conn.execute(
            'SELECT id FROM linkup_follows WHERE follower_id = ? AND linkup_id = ?',
            (user_id, linkup_id)
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def count_linkup_followers(conn, linkup_id):
    try:
        n = conn.execute(
            'SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = ?', (linkup_id,)
        ).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


def followed_linkup_ids(conn, user_id):
    try:
        rows = conn.execute(
            'SELECT linkup_id FROM linkup_follows WHERE follower_id = ?', (user_id,)
        ).fetchall()
        return {int(r['linkup_id']) for r in rows}
    except Exception:
        return set()


def enrich_posts_with_linkup(conn, posts_list):
    """
    Post za Linkup: onyesha jina/avatar/username YA LINKUP (si profile ya mmiliki).
    Pia: followers_count + is_following_linkup (kwa batani ya Follow kwenye feed).
    """
    if not posts_list:
        return posts_list
    ids = set()
    for p in posts_list:
        lid = p.get('linkup_id')
        if lid:
            try:
                ids.add(int(lid))
            except (TypeError, ValueError):
                pass
    if not ids:
        for p in posts_list:
            p['is_linkup_post'] = False
            p['linkup'] = None
            p['profile_url'] = None
            p['is_following_linkup'] = False
            p['linkup_followers_count'] = 0
        return posts_list
    placeholders = ','.join('?' * len(ids))
    try:
        rows = conn.execute(
            f'SELECT * FROM linkups WHERE id IN ({placeholders}) AND COALESCE(is_active,1)=1',
            tuple(ids)
        ).fetchall()
        by_id = {int(r['id']): dict(r) for r in rows}
    except Exception:
        by_id = {}

    # Followers counts (batch + fallback per-id)
    followers_map = {}
    try:
        frows = conn.execute(
            f'SELECT linkup_id, COUNT(*) AS c FROM linkup_follows '
            f'WHERE linkup_id IN ({placeholders}) GROUP BY linkup_id',
            tuple(ids)
        ).fetchall()
        for r in frows:
            try:
                lid_k = int(r['linkup_id'] if hasattr(r, 'keys') else r[0])
                cnt = int(r['c'] if hasattr(r, 'keys') else r[1] or 0)
                followers_map[lid_k] = cnt
            except Exception:
                pass
    except Exception as e:
        print('[enrich_posts_with_linkup] batch followers:', e)
        followers_map = {}
    # Fallback: count individually for any missing ids
    for _lid in ids:
        if _lid not in followers_map:
            try:
                n = conn.execute(
                    'SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = ?',
                    (_lid,)
                ).fetchone()[0]
                followers_map[int(_lid)] = int(n or 0)
            except Exception:
                followers_map[int(_lid)] = 0

    # Viewer follows which linkups?
    viewer_id = None
    try:
        viewer_id = session.get('user_id')
        if viewer_id is not None:
            viewer_id = int(viewer_id)
    except Exception:
        viewer_id = None
    following_set = set()
    if viewer_id:
        try:
            frows = conn.execute(
                f'SELECT linkup_id FROM linkup_follows '
                f'WHERE follower_id = ? AND linkup_id IN ({placeholders})',
                (viewer_id,) + tuple(ids)
            ).fetchall()
            following_set = {int(r['linkup_id']) for r in frows}
        except Exception:
            following_set = set()

    for p in posts_list:
        lid = p.get('linkup_id')
        try:
            lid = int(lid) if lid else None
        except (TypeError, ValueError):
            lid = None
        if lid and lid in by_id:
            lu = by_id[lid]
            # Hifadhi original owner (kwa admin / ownership checks)
            p['owner_username'] = p.get('username')
            p['owner_full_name'] = p.get('full_name')
            p['owner_profile_pic'] = p.get('profile_pic')
            p['owner_user_id'] = p.get('user_id')

            p['is_linkup_post'] = True
            p['linkup'] = lu
            p['linkup_id'] = lid
            p['linkup_display_name'] = lu.get('display_name')
            p['linkup_username'] = lu.get('username')
            p['linkup_category'] = lu.get('category')
            p['linkup_profile_pic'] = lu.get('profile_pic')
            p['linkup_verified'] = bool(lu.get('is_verified'))
            p['linkup_followers_count'] = int(followers_map.get(lid, 0) or 0)
            p['followers_count'] = p['linkup_followers_count']  # alias for UI
            p['is_following_linkup'] = lid in following_set
            # owner wa Linkup asijione Follow button
            try:
                p['is_linkup_owner'] = bool(
                    viewer_id and int(lu.get('owner_user_id') or 0) == viewer_id
                )
            except Exception:
                p['is_linkup_owner'] = False

            # === DISPLAY kama Linkup (feed/card inatumia hizi) ===
            p['full_name'] = lu.get('display_name') or lu.get('username')
            p['username'] = lu.get('username')
            if lu.get('profile_pic'):
                p['profile_pic'] = lu.get('profile_pic')
            if lu.get('is_verified'):
                p['is_verified'] = 1
            p['profile_url'] = f"/linkup/{lu.get('username')}"
        else:
            p['is_linkup_post'] = False
            p['linkup'] = None
            p['profile_url'] = None
            p['is_following_linkup'] = False
            p['linkup_followers_count'] = 0
            p['is_linkup_owner'] = False
    return posts_list






def attach_original_posts(conn, posts_list):
    """Kwa kila post yenye repost_of, jaza post['orig'] = dict ya post asili."""
    if not posts_list:
        return posts_list
    orig_ids = set()
    for p in posts_list:
        rid = p.get('repost_of')
        if rid:
            try:
                orig_ids.add(int(rid))
            except (TypeError, ValueError):
                pass
    if not orig_ids:
        for p in posts_list:
            p['orig'] = None
        return posts_list
    placeholders = ','.join('?' * len(orig_ids))
    by_id = {}
    try:
        rows = conn.execute(
            "SELECT p.id, p.user_id, p.content, p.file_path, p.media_type, "
            "p.created_at, p.linkup_id, p.repost_of, "
            "u.username, u.full_name, u.profile_pic, u.is_verified "
            "FROM posts p JOIN users u ON u.id = p.user_id "
            "WHERE p.id IN (" + placeholders + ")",
            tuple(orig_ids)
        ).fetchall()
        for r in rows:
            d = dict(r)
            try:
                d['is_verified'] = int(d.get('is_verified') or 0)
            except Exception:
                d['is_verified'] = 0
            by_id[int(d['id'])] = d
    except Exception as e:
        print('[attach_original_posts]', e)
        by_id = {}
    if by_id:
        try:
            orig_list = list(by_id.values())
            enrich_posts_with_linkup(conn, orig_list)
            for o in orig_list:
                by_id[int(o['id'])] = o
        except Exception as e:
            print('[attach_original_posts] linkup enrich:', e)
    for p in posts_list:
        rid = p.get('repost_of')
        try:
            rid = int(rid) if rid else None
        except (TypeError, ValueError):
            rid = None
        p['orig'] = by_id.get(rid) if rid else None
    return posts_list


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif',
    'mp4', 'webm', 'ogg', 'mov', 'avi', '3gp', 'mkv',
    'mp3', 'wav', 'aac', 'm4a', 'opus',
    'pdf', 'doc', 'docx', 'txt', 'zip', 'rar', '7z',
    'html', 'htm', 'css', 'js', 'json', 'csv', 'xlsx', 'xls', 'ppt', 'pptx'
}

def now_tz():
    """Muda wa sasa - Africa/Dar_es_Salaam"""
    return datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')


RESTRICTION_HOURS = 2          # muda wa kuzuiwa (saa) baada ya warning ya 3
WARNINGS_BEFORE_RESTRICTION = 3  # warnings ngapi kabla ya kuzuiwa kwa muda


def get_user_restriction_status(conn, user_id):
    """
    Angalia kama user ana restriction inayoendelea sasa hivi.
    Rudisha (is_restricted: bool, restricted_until: str|None).
    Ikiwa muda wa restriction umekwisha, inaifuta moja kwa moja.
    """
    row = conn.execute(
        'SELECT restricted_until FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    if not row or not row['restricted_until']:
        return False, None

    try:
        until = datetime.strptime(row['restricted_until'], '%Y-%m-%d %H:%M:%S')
        until = until.replace(tzinfo=ZoneInfo("Africa/Dar_es_Salaam"))
    except (ValueError, TypeError):
        return False, None

    now = datetime.now(ZoneInfo("Africa/Dar_es_Salaam"))
    if now >= until:
        # muda umekwisha - futa restriction
        conn.execute(
            'UPDATE users SET restricted_until = NULL WHERE id = ?', (user_id,)
        )
        conn.commit()
        return False, None

    return True, row['restricted_until']


def flag_post_for_admin(conn, post_id, user_id, flag_type, nsfw_score, labels):
    """Weka post kwenye foleni ya admin kuipitia."""
    conn.execute(
        '''
        INSERT INTO moderation_flags
            (post_id, user_id, flag_type, nsfw_score, labels, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ''',
        (post_id, user_id, flag_type, nsfw_score, ', '.join(labels or []), now_tz())
    )


def register_nsfw_violation(conn, post_id, user_id, nsfw_score, labels):
    """
    Ongeza warning_count ya user, notification, na kama amefikia kiwango cha
    WARNINGS_BEFORE_RESTRICTION, mzuie kwa muda (RESTRICTION_HOURS).
    """
    row = conn.execute(
        'SELECT warning_count FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    current_warnings = (row['warning_count'] if row and row['warning_count'] else 0) + 1

    warning_message = (
        f"🚫 WARNING #{current_warnings}: Chapisho lako limetambuliwa kuwa linakiuka "
        f"sheria za maudhui (uchi/ngono). Post HAIJACHAPISHWA hadharani. "
        f"Ukiongeza ukiukaji (kila warning ya 3), akaunti itazuiwa kwa masaa {RESTRICTION_HOURS}."
    )

    restricted = False
    if current_warnings % WARNINGS_BEFORE_RESTRICTION == 0:
        restricted = True
        until_dt = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")) + timedelta(hours=RESTRICTION_HOURS)
        until_str = until_dt.strftime('%Y-%m-%d %H:%M:%S')
        warning_message = (
            f"🚫 WARNING #{current_warnings} + AKAUNTI IMEZUIWA: "
            f"Umezuiwa kuchapisha mpaka {until_str} kwa kukiuka sheria za uchi/ngono. "
            f"Ukiongeza, Admin anaweza kuzuia akaunti kabisa."
        )
        conn.execute(
            'UPDATE users SET warning_count = ?, warning_message = ?, restricted_until = ? WHERE id = ?',
            (current_warnings, warning_message, until_str, user_id)
        )
        flag_post_for_admin(conn, post_id, user_id, 'escalation', nsfw_score, labels)
    else:
        conn.execute(
            'UPDATE users SET warning_count = ?, warning_message = ? WHERE id = ?',
            (current_warnings, warning_message, user_id)
        )

    # Notification ndani ya app (sio flash tu)
    try:
        conn.execute(
            """INSERT INTO notifications
               (user_id, sender_id, type, post_id, message, is_read, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (user_id, user_id, 'nsfw_warning', post_id, warning_message, now_tz())
        )
    except Exception as e:
        print('[register_nsfw_violation] notification error:', e)

    return current_warnings, restricted


def record_status_view(conn, status_id, viewer_id):
    """Rekodi view ya status. Rudi True kama imeandikwa / tayari ipo."""
    if not status_id or not viewer_id:
        return False
    try:
        status_id = int(status_id)
        viewer_id = int(viewer_id)
    except (TypeError, ValueError):
        return False
    try:
        # Usijirekodi kama view ya status yako
        owner = conn.execute(
            'SELECT user_id FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        if not owner:
            return False
        if int(owner['user_id']) == viewer_id:
            return False
        conn.execute(
            '''
            INSERT OR IGNORE INTO status_views (status_id, viewer_id, viewed_at)
            VALUES (?, ?, ?)
            ''',
            (status_id, viewer_id, now_tz())
        )
        return True
    except Exception:
        # Fallback: table inaweza kuwa na user_id badala ya viewer_id (schema ya zamani)
        try:
            conn.execute(
                '''
                INSERT OR IGNORE INTO status_views (status_id, user_id, viewed_at)
                VALUES (?, ?, ?)
                ''',
                (status_id, viewer_id, now_tz())
            )
            return True
        except Exception:
            return False


def count_status_views(conn, status_id):
    """Hesabu viewers wa status (bila kujumuisha mmiliki)."""
    try:
        status_id = int(status_id)
    except (TypeError, ValueError):
        return 0
    try:
        n = conn.execute(
            'SELECT COUNT(*) FROM status_views WHERE status_id = ?',
            (status_id,)
        ).fetchone()[0]
        return int(n or 0)
    except Exception:
        try:
            n = conn.execute(
                'SELECT COUNT(*) FROM status_views WHERE status_id = ?',
                (status_id,)
            ).fetchone()[0]
            return int(n or 0)
        except Exception:
            return 0

def allowed_video(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'mp4', 'webm', 'mov', 'avi'}


def generate_otp():
    return str(random.randint(100000, 999999))


def base_username_from_email(email):
    """Tengeneza username kutoka sehemu ya kabla ya @ kwenye email."""
    local = (email or '').split('@')[0].lower()
    local = local.replace('.', '_').replace('-', '_').replace('+', '_')
    base = ''.join(c for c in local if c.isalnum() or c == '_')
    base = base.strip('_')
    if not base:
        base = 'user'
    if len(base) > 24:
        base = base[:24]
    return base


def sanitize_username(username):
    """Safisha username: herufi, namba, underscore tu."""
    u = (username or '').strip().lower()
    u = ''.join(c for c in u if c.isalnum() or c == '_')
    u = u.strip('_')
    if len(u) > 30:
        u = u[:30]
    return u


def username_is_taken(conn, username, exclude_email=None):
    """Angalia kama username inatumika kwenye users au pending_registrations."""
    if not username:
        return True
    row = conn.execute(
        'SELECT id FROM users WHERE username = ? COLLATE NOCASE LIMIT 1',
        (username,)
    ).fetchone()
    if row:
        return True
    if exclude_email:
        row = conn.execute(
            '''SELECT id FROM pending_registrations
               WHERE username = ? COLLATE NOCASE AND email != ? LIMIT 1''',
            (username, exclude_email)
        ).fetchone()
    else:
        row = conn.execute(
            '''SELECT id FROM pending_registrations
               WHERE username = ? COLLATE NOCASE LIMIT 1''',
            (username,)
        ).fetchone()
    return bool(row)


def suggest_usernames(conn, desired, limit=6, exclude_email=None):
    """Toa mapendekezo ya username yanayofanana na ile aliyotaka, yaliyo free."""
    base = sanitize_username(desired) or 'user'
    if len(base) < 2:
        base = 'user'
    candidates = []
    candidates.append(base)
    candidates.append(f'{base}1')
    candidates.append(f'{base}2')
    candidates.append(f'{base}_tz')
    candidates.append(f'{base}_{random.randint(10, 99)}')
    candidates.append(f'{base}{random.randint(100, 999)}')
    year = datetime.now().year
    candidates.append(f'{base}{year}')
    candidates.append(f'{base}_{year}')
    for _ in range(8):
        candidates.append(f'{base}{random.randint(10, 9999)}')

    seen = set()
    free = []
    for c in candidates:
        c = sanitize_username(c)
        if not c or c in seen:
            continue
        seen.add(c)
        if not username_is_taken(conn, c, exclude_email=exclude_email):
            free.append(c)
        if len(free) >= limit:
            break
    return free


def send_otp_email(to_email, otp, purpose="register"):
    if purpose == "register":
        subject = "OTP yako ya Usajili - Thibitisha Akaunti"
        message = f"""
Habari,

OTP yako ya kuthibitisha akaunti ni:

{otp}

OTP hii itaexpire baada ya dakika 5.
Usishiriki nambari hii na mtu yeyote.

Asante.
"""
    else:
        subject = "OTP ya Kubadilisha Password"
        message = f"""
Habari,

OTP yako ya kubadilisha password ni:

{otp}

OTP hii itaexpire baada ya dakika 5.
Usishiriki nambari hii na mtu yeyote.

Asante.
"""

    msg = MIMEMultipart()
    msg['From'] = GMAIL_ADDRESS
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(message, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print("Error sending email:", str(e))
        return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Tafadhali ingia kwanza', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def create_notification(user_id, sender_id, ntype, message="", post_id=None, url="/notifications"):
    """Alias for notify_user (older routes call create_notification)."""
    return notify_user(user_id, sender_id, ntype, message=message, post_id=post_id, url=url)


def notify_user(user_id, sender_id, ntype, message="", post_id=None, url="/notifications"):
    """Insert notification + send Web Push. Safe to call from anywhere."""
    if not user_id or user_id == sender_id:
        return

    sender = None
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO notifications
               (user_id, sender_id, type, post_id, message, is_read, created_at)
               VALUES (?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)""",
            (user_id, sender_id, ntype, post_id, message or "")
        )
        conn.commit()
        sender = conn.execute(
            "SELECT username, profile_pic FROM users WHERE id = ?",
            (sender_id,)
        ).fetchone()
    except Exception as e:
        print("[notify_user] insert/select error:", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    who = f"@{sender['username']}" if sender else "Mtu"
    if ntype == "like":
        body = f"{who} amependa posti yako"
        if post_id:
            url = f"/post/{post_id}"
    elif ntype == "comment":
        body = f"{who} ameweka maoni kwenye posti yako"
        if post_id:
            url = f"/post/{post_id}"
    elif ntype == "save":
        body = f"{who} amehifadhi posti yako"
        if post_id:
            url = f"/post/{post_id}"
    elif ntype in ("share", "repost"):
        body = f"{who} ameshare / amerepost posti yako"
        if post_id:
            url = f"/post/{post_id}"
    elif ntype == "message":
        body = f"{who} amekutumia ujumbe"
        if sender:
            url = f"/chat/{sender['username']}"
    elif ntype == "follow":
        body = f"{who} amekufollow"
        if sender:
            url = f"/profile/{sender['username']}"
    elif ntype == "follow_back":
        body = f"{who} amekufollow back — sasa mmekuwa friends"
        if sender:
            url = f"/profile/{sender['username']}"
    else:
        body = message or "Taarifa mpya kutoka Kijiji"

    icon = None
    if sender and sender["profile_pic"]:
        try:
            icon = url_for('static', filename='uploads/' + sender['profile_pic'], _external=True)
        except Exception:
            icon = None

    try:
        send_web_push(user_id, "Kijiji Tanzania", body, url, icon=icon)
    except Exception as e:
        print("[PUSH] notify_user error:", e)


def send_web_push(user_id, title, body, url="/notifications", icon=None):
    """Tuma push kwa subscriptions zote za user."""
    conn = get_db_connection()
    subs = conn.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
        (user_id,)
    ).fetchall()
    conn.close()

    print(f"[PUSH] user={user_id} subs={len(subs)} title={title}")

    if not subs:
        print("[PUSH] Hakuna subscription — user a-Allow notifications tena")
        return

    if not icon:
        try:
            icon = url_for('static', filename='images/default-avatar.png', _external=True)
        except Exception:
            icon = "/static/images/default-avatar.png"

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "icon": icon
    })

    dead = []
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
                timeout=10,
            )
            print("[PUSH] OK →", s["endpoint"][:50])
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            print("[PUSH] WebPushException:", status, e)
            if status in (404, 410):
                dead.append(s["endpoint"])
        except Exception as e:
            print("[PUSH] Error:", e)

    if dead:
        conn = get_db_connection()
        for ep in dead:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (ep,))
        conn.commit()
        conn.close()


# ######################################################################

# ==========================================================
# ========== GROUP A HELPERS (hashtags, mute, sessions) ==========
# ==========================================================

def extract_hashtags(text):
    """Rudisha list ya hashtags (lowercase, bila #)."""
    if not text:
        return []
    tags = re.findall(r'#([A-Za-z0-9_\u00C0-\u024F]{2,40})', text)
    seen = set()
    out = []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def save_post_hashtags(conn, post_id, content):
    """Hifadhi hashtags za post kwenye DB."""
    tags = extract_hashtags(content or '')
    if not tags:
        return
    for tag in tags:
        row = conn.execute(
            'SELECT id, use_count FROM hashtags WHERE tag = ? COLLATE NOCASE',
            (tag,)
        ).fetchone()
        if row:
            hid = row['id']
            conn.execute(
                'UPDATE hashtags SET use_count = COALESCE(use_count, 0) + 1 WHERE id = ?',
                (hid,)
            )
        else:
            cur = conn.execute(
                'INSERT INTO hashtags (tag, use_count) VALUES (?, 1)',
                (tag,)
            )
            hid = cur.lastrowid
        try:
            conn.execute(
                'INSERT OR IGNORE INTO post_hashtags (post_id, hashtag_id) VALUES (?, ?)',
                (post_id, hid)
            )
        except Exception:
            pass


def user_is_muted(conn, viewer_id, author_id):
    """True kama viewer amemute author."""
    if not viewer_id or not author_id or viewer_id == author_id:
        return False
    try:
        row = conn.execute(
            'SELECT id FROM mutes WHERE muter_id = ? AND muted_id = ?',
            (viewer_id, author_id)
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def muted_ids_for(conn, user_id):
    """Set ya user ids ambao user amewamute."""
    try:
        rows = conn.execute(
            'SELECT muted_id FROM mutes WHERE muter_id = ?', (user_id,)
        ).fetchall()
        return {r['muted_id'] for r in rows}
    except Exception:
        return set()


def can_view_status(conn, status_row, viewer_id):
    """Angalia privacy ya status."""
    try:
        owner_id = status_row['user_id']
    except Exception:
        owner_id = status_row.get('user_id')
    privacy = 'public'
    try:
        if hasattr(status_row, 'keys') and 'privacy' in status_row.keys():
            privacy = status_row['privacy'] or 'public'
        elif isinstance(status_row, dict):
            privacy = status_row.get('privacy') or 'public'
    except Exception:
        privacy = 'public'
    if not viewer_id:
        return privacy == 'public'
    if int(owner_id) == int(viewer_id):
        return True
    if privacy == 'public':
        return True
    if privacy == 'followers':
        row = conn.execute(
            'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
            (viewer_id, owner_id)
        ).fetchone()
        return bool(row)
    if privacy == 'close_friends':
        row = conn.execute(
            'SELECT id FROM close_friends WHERE user_id = ? AND friend_id = ?',
            (owner_id, viewer_id)
        ).fetchone()
        return bool(row)
    return False


def get_client_device_info():
    ua = (request.headers.get('User-Agent') or '')[:200]
    return ua or 'Unknown device'


def record_user_session(user_id):
    """Rekodi / sasisha session token baada ya login."""
    if not user_id:
        return
    token = session.get('_sid')
    if not token:
        token = secrets.token_hex(24)
        session['_sid'] = token
        session.modified = True
    try:
        conn = get_db_connection()
        existing = conn.execute(
            'SELECT id FROM user_sessions WHERE session_token = ?',
            (token,)
        ).fetchone()
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')[:60]
        device = get_client_device_info()
        if existing:
            conn.execute(
                '''UPDATE user_sessions
                   SET last_active = ?, is_current = 1, user_id = ?, device_info = ?, ip_address = ?
                   WHERE session_token = ?''',
                (now_tz(), user_id, device, ip, token)
            )
        else:
            conn.execute(
                '''INSERT INTO user_sessions
                   (user_id, session_token, device_info, ip_address, created_at, last_active, is_current)
                   VALUES (?, ?, ?, ?, ?, ?, 1)''',
                (user_id, token, device, ip, now_tz(), now_tz())
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print('[session] record error:', e)


def publish_due_scheduled_posts():
    """Chapisha drafts zilizopangwa na scheduled_at <= sasa + arifu mmiliki."""
    try:
        conn = get_db_connection()
        now = now_tz()
        rows = conn.execute(
            "SELECT id, user_id, content FROM posts "
            "WHERE COALESCE(is_draft, 0) = 1 "
            "AND scheduled_at IS NOT NULL AND scheduled_at <= ? LIMIT 50",
            (now,)
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE posts SET is_draft = 0, published_at = ?, scheduled_at = NULL WHERE id = ?",
                (now, r['id'])
            )
            try:
                create_notification(
                    r['user_id'], r['user_id'], 'system',
                    'Draft yako imechapishwa kiotomatiki',
                    post_id=r['id'], url='/home'
                )
            except Exception:
                pass
        if rows:
            conn.commit()
        conn.close()
    except Exception as e:
        print('[schedule] publish error:', e)



# #########################  APPLICATION ROUTES  #########################
# ######################################################################



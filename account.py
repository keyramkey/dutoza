"""
account.py — Account settings, drafts, insights, sessions, kijiji-mode,
data deletion, deactivate, push/PWA, explore/trending helpers
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import sqlite3
import time
import secrets
import uuid
import json
from flask import (
    render_template, request, redirect, url_for, session, flash, jsonify,
    send_file, send_from_directory
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db_connection, UPLOAD_FOLDER, BASE_DIR
from helpers import (
    login_required, now_tz, allowed_file, notify_user, create_notification,
    publish_due_scheduled_posts, muted_ids_for, save_post_hashtags,
    enrich_posts_with_linkup, set_active_linkup_id, user_has_kijiji_mode,
    get_user_linkups, resolve_active_linkup, get_linkup_by_id,
    sanitize_username, IMAGE_EXTS, VIDEO_EXTS, ALLOWED_EXTENSIONS,
    MAX_LINKUPS_PER_USER, LINKUP_CATEGORIES, VAPID_PUBLIC_KEY, send_web_push,
    SUPPORTED_LANGUAGES, apply_user_language, save_user_language
)


def register_account_routes(app):
    # ----- original lines 3911-4437 -----
    # ====================== STATIC PAGES / LEGAL / HELP ======================

    @app.route('/terms')
    def terms():
        return render_template('pages/terms.html')

    @app.route('/about')
    def about():
        return render_template('pages/about.html')

    @app.route('/privacy-policy')
    def privacy_policy():
        return render_template('pages/privacy_policy.html')

    # ====================== DATA DELETION REQUEST ======================

    @app.route('/help-center')
    def help_center():
        return render_template('pages/help_center.html')

    @app.route('/social-links')
    def social_links():
        return render_template('account/social_links.html')

    # ====================== FORGOT PASSWORD ======================

    @app.route('/contact-admin', methods=['GET', 'POST'])
    def contact_admin():
        if request.method == 'POST':
            name = request.form.get('name')
            email = request.form.get('email')
            message = request.form.get('message')

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO admin_messages (name, email, message) VALUES (?, ?, ?)", (name, email, message))
            conn.commit()
            conn.close()

            return render_template('pages/contact_admin.html', success=True)

        return render_template('pages/contact_admin.html')

    @app.route('/data-deletion', methods=['GET', 'POST'])
    def data_deletion():
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()

            if not email:
                flash('Tafadhali weka email ya akaunti yako.', 'error')
                return redirect(url_for('data_deletion'))

            conn = get_db_connection()

            user = conn.execute(
                'SELECT id, email FROM users WHERE email = ?',
                (email,)
            ).fetchone()

            if not user:
                conn.close()
                flash(
                    'Ikiwa akaunti yenye email hiyo ipo, ombi lako litashughulikiwa.',
                    'success'
                )
                return redirect(url_for('data_deletion'))

            conn.execute(
                '''
                INSERT INTO data_deletion_requests
                (user_id, email, status)
                VALUES (?, ?, 'pending')
                ''',
                (user['id'], user['email'])
            )

            conn.commit()
            conn.close()

            flash(
                'Ombi lako la kufuta akaunti na data limepokelewa.',
                'success'
            )

            return redirect(url_for('data_deletion'))

        return render_template('pages/data_deletion.html')

    @app.route('/deactivate', methods=['GET', 'POST'])
    @login_required
    def deactivate():
        """Deactivate account (soft). Login ya kawaida ina-reactivate."""
        user_id = session['user_id']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            conn.close()
            session.clear()
            return redirect(url_for('login'))

        # Tambua kama ni Google account (SI kutumia profile_pic — inaweza kuwa http kwa mtu yeyote)
        provider = 'local'
        try:
            provider = (user['auth_provider'] if 'auth_provider' in user.keys() else 'local') or 'local'
        except Exception:
            provider = 'local'
        if provider != 'google':
            bio = (user['bio'] if 'bio' in user.keys() else '') or ''
            if bio == 'Joined with Google':
                provider = 'google'
        is_google = (provider == 'google')

        if request.method == 'POST':
            confirm = (request.form.get('confirm') or '').strip().lower()
            password = request.form.get('password') or ''
            if confirm not in ('ndiyo', 'yes', 'deactivate'):
                conn.close()
                flash('Andika "ndiyo" ili kuthibitisha.')
                return redirect(url_for('deactivate'))

            # Local: nenosiri lazima. Google: hakuna nenosiri — thibitisha kwa "ndiyo" tu
            if not is_google:
                try:
                    if not password or not check_password_hash(user['password_hash'], password):
                        conn.close()
                        flash('Nenosiri si sahihi.')
                        return redirect(url_for('deactivate'))
                except Exception:
                    conn.close()
                    flash('Nenosiri si sahihi.')
                    return redirect(url_for('deactivate'))

            conn.execute(
                'UPDATE users SET is_deactivated = 1, deactivated_at = ? WHERE id = ?',
                (now_tz(), user_id)
            )
            conn.commit()
            conn.close()
            session.clear()
            if is_google:
                flash('Akaunti yako imezimwa. Unaweza kuirejesha kwa kuingia tena kwa Google.')
            else:
                flash('Akaunti yako imezimwa. Unaweza kuirejesha kwa kuingia tena kwa email na nenosiri.')
            return redirect(url_for('login'))

        conn.close()
        return render_template('account/deactivate.html', user=user, is_google=is_google)

    @app.route('/delete_account', methods=['GET', 'POST'])
    @login_required
    def delete_account():
        """Hard delete kamili ya akaunti + data zote zinazohusiana + faili za disk."""
        user_id = session['user_id']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            conn.close()
            session.clear()
            return redirect(url_for('login'))

        # Tambua provider (Google vs local)
        provider = 'local'
        try:
            provider = (user['auth_provider'] if 'auth_provider' in user.keys() else 'local') or 'local'
        except Exception:
            provider = 'local'
        if provider != 'google':
            bio = (user['bio'] if 'bio' in user.keys() else '') or ''
            if bio == 'Joined with Google':
                provider = 'google'
        is_google = (provider == 'google')

        if request.method == 'GET':
            conn.close()
            try:
                return render_template('account/delete_account.html', user=user, is_google=is_google)
            except Exception:
                return redirect(url_for('settings') if 'settings' in app.view_functions else url_for('home'))

        # ---------- POST: thibitisha ----------
        confirm = (request.form.get('confirm') or '').strip().lower()
        password = request.form.get('password') or ''
        typed_username = (request.form.get('username_confirm') or '').strip()

        ok_words = ('futa', 'delete', 'ndiyo', 'yes')
        if confirm not in ok_words:
            conn.close()
            flash('Andika "FUTA" au "DELETE" ili kuthibitisha kufuta akaunti.')
            return redirect(request.referrer or url_for('delete_account'))

        if not typed_username or typed_username.lower() != (user['username'] or '').lower():
            conn.close()
            flash('Andika username yako kamili ili kuthibitisha kufuta akaunti.')
            return redirect(request.referrer or url_for('delete_account'))

        if not is_google:
            try:
                if not password or not check_password_hash(user['password_hash'], password):
                    conn.close()
                    flash('Nenosiri si sahihi.')
                    return redirect(request.referrer or url_for('delete_account'))
            except Exception:
                conn.close()
                flash('Nenosiri si sahihi.')
                return redirect(request.referrer or url_for('delete_account'))

        # ---------- Kusanya faili za kufuta baada ya DB commit ----------
        files_to_remove = []

        def _add_upload_file(name):
            if not name:
                return
            name = str(name).strip()
            if not name or name.startswith('http://') or name.startswith('https://'):
                return
            base = os.path.basename(name.replace('\\', '/'))
            if not base:
                return
            path = os.path.join(app.config.get('UPLOAD_FOLDER') or UPLOAD_FOLDER, base)
            files_to_remove.append(path)

        def _add_badge_file(name):
            if not name:
                return
            name = str(name).strip()
            base = os.path.basename(name.replace('\\', '/'))
            if not base:
                return
            folder = app.config.get('UPLOAD_BADGES_FOLDER') or UPLOAD_BADGES_FOLDER
            files_to_remove.append(os.path.join(folder, base))

        try:
            # Profile / cover
            try:
                _add_upload_file(user['profile_pic'] if 'profile_pic' in user.keys() else None)
                _add_upload_file(user['cover_photo'] if 'cover_photo' in user.keys() else None)
            except Exception:
                pass

            # Posts media
            for row in conn.execute(
                'SELECT file_path FROM posts WHERE user_id = ? AND file_path IS NOT NULL',
                (user_id,)
            ).fetchall():
                try:
                    _add_upload_file(row['file_path'])
                except Exception:
                    _add_upload_file(row[0] if row else None)

            # Statuses media + music
            try:
                for row in conn.execute(
                    'SELECT file_path, music_path FROM statuses WHERE user_id = ?',
                    (user_id,)
                ).fetchall():
                    try:
                        _add_upload_file(row['file_path'])
                        _add_upload_file(row['music_path'] if 'music_path' in row.keys() else None)
                    except Exception:
                        pass
            except Exception:
                pass

            # Community posts media
            try:
                for row in conn.execute(
                    'SELECT file_path FROM community_posts WHERE user_id = ? AND file_path IS NOT NULL',
                    (user_id,)
                ).fetchall():
                    try:
                        _add_upload_file(row['file_path'])
                    except Exception:
                        pass
            except Exception:
                pass

            # Chat media
            try:
                for row in conn.execute(
                    '''SELECT file_path FROM private_messages
                       WHERE (sender_id = ? OR receiver_id = ?) AND file_path IS NOT NULL''',
                    (user_id, user_id)
                ).fetchall():
                    try:
                        _add_upload_file(row['file_path'])
                    except Exception:
                        pass
            except Exception:
                pass

            # Badge documents
            try:
                for row in conn.execute(
                    'SELECT id_document_path FROM badge_requests WHERE user_id = ? AND id_document_path IS NOT NULL',
                    (user_id,)
                ).fetchall():
                    try:
                        _add_badge_file(row['id_document_path'])
                    except Exception:
                        pass
            except Exception:
                pass

            # ---------- TRANSACTION ----------
            conn.execute('BEGIN')

            # Statuses
            try:
                status_ids = [
                    r[0] for r in conn.execute(
                        'SELECT id FROM statuses WHERE user_id = ?', (user_id,)
                    ).fetchall()
                ]
                for sid in status_ids:
                    conn.execute('DELETE FROM status_reactions WHERE status_id = ?', (sid,))
                    conn.execute('DELETE FROM status_views WHERE status_id = ?', (sid,))
                conn.execute('DELETE FROM status_reactions WHERE user_id = ?', (user_id,))
                try:
                    conn.execute('DELETE FROM status_views WHERE viewer_id = ?', (user_id,))
                except Exception:
                    try:
                        conn.execute('DELETE FROM status_views WHERE user_id = ?', (user_id,))
                    except Exception:
                        pass
                conn.execute('DELETE FROM statuses WHERE user_id = ?', (user_id,))
            except Exception as e:
                print('[delete_account] statuses:', e)

            # Posts za user (+ related)
            post_ids = [
                r[0] for r in conn.execute(
                    'SELECT id FROM posts WHERE user_id = ?', (user_id,)
                ).fetchall()
            ]
            for pid in post_ids:
                comment_ids = [
                    r[0] for r in conn.execute(
                        'SELECT id FROM comments WHERE post_id = ?', (pid,)
                    ).fetchall()
                ]
                for cid in comment_ids:
                    try:
                        conn.execute('DELETE FROM comment_likes WHERE comment_id = ?', (cid,))
                    except Exception:
                        pass
                conn.execute('DELETE FROM comments WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM likes WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM saved_posts WHERE post_id = ?', (pid,))
                try:
                    conn.execute('DELETE FROM reposts WHERE original_post_id = ?', (pid,))
                except Exception:
                    pass
                try:
                    conn.execute('DELETE FROM reports WHERE post_id = ?', (pid,))
                except Exception:
                    pass
                try:
                    conn.execute('DELETE FROM post_views WHERE post_id = ?', (pid,))
                except Exception:
                    pass
                try:
                    conn.execute('DELETE FROM notifications WHERE post_id = ?', (pid,))
                except Exception:
                    pass
            conn.execute('DELETE FROM posts WHERE user_id = ?', (user_id,))

            # Comments alizoandika kwenye posts za wengine
            try:
                my_comment_ids = [
                    r[0] for r in conn.execute(
                        'SELECT id FROM comments WHERE user_id = ?', (user_id,)
                    ).fetchall()
                ]
                for cid in my_comment_ids:
                    conn.execute('DELETE FROM comment_likes WHERE comment_id = ?', (cid,))
                conn.execute('DELETE FROM comments WHERE user_id = ?', (user_id,))
            except Exception:
                pass

            # Engagement
            conn.execute('DELETE FROM likes WHERE user_id = ?', (user_id,))
            try:
                conn.execute('DELETE FROM comment_likes WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            conn.execute('DELETE FROM saved_posts WHERE user_id = ?', (user_id,))
            try:
                conn.execute('DELETE FROM reposts WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM post_views WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM reports WHERE reporter_id = ?', (user_id,))
            except Exception:
                pass

            # Social graph
            try:
                conn.execute(
                    'DELETE FROM follows WHERE follower_id = ? OR following_id = ?',
                    (user_id, user_id)
                )
            except Exception:
                pass
            try:
                conn.execute(
                    'DELETE FROM blocks WHERE blocker_id = ? OR blocked_id = ?',
                    (user_id, user_id)
                )
            except Exception:
                pass

            # Messages & calls
            try:
                conn.execute(
                    'DELETE FROM private_messages WHERE sender_id = ? OR receiver_id = ?',
                    (user_id, user_id)
                )
            except Exception:
                pass
            try:
                conn.execute(
                    'DELETE FROM pinned_chats WHERE user_id = ? OR other_user_id = ?',
                    (user_id, user_id)
                )
            except Exception:
                pass
            try:
                conn.execute(
                    'DELETE FROM call_signals WHERE from_user_id = ? OR to_user_id = ?',
                    (user_id, user_id)
                )
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM push_subscriptions WHERE user_id = ?', (user_id,))
            except Exception:
                pass

            # Notifications, history, badges
            conn.execute(
                'DELETE FROM notifications WHERE user_id = ? OR sender_id = ?',
                (user_id, user_id)
            )
            try:
                conn.execute('DELETE FROM history WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM badge_requests WHERE user_id = ?', (user_id,))
            except Exception:
                pass

            # Community / groups
            try:
                conn.execute('DELETE FROM community_posts WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM group_messages WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                conn.execute('DELETE FROM group_members WHERE user_id = ?', (user_id,))
            except Exception:
                pass
            try:
                group_ids = [
                    r[0] for r in conn.execute(
                        'SELECT id FROM community_groups WHERE creator_id = ?', (user_id,)
                    ).fetchall()
                ]
                for gid in group_ids:
                    conn.execute('DELETE FROM group_messages WHERE group_id = ?', (gid,))
                    conn.execute('DELETE FROM group_members WHERE group_id = ?', (gid,))
                conn.execute('DELETE FROM community_groups WHERE creator_id = ?', (user_id,))
            except Exception as e:
                print('[delete_account] groups:', e)

            # OTPs / pending
            try:
                email = user['email'] if 'email' in user.keys() else None
                if email:
                    conn.execute('DELETE FROM otps WHERE email = ?', (email,))
                    conn.execute('DELETE FROM pending_registrations WHERE email = ?', (email,))
            except Exception:
                pass

            # User mwenyewe (mwisho)
            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))

            conn.commit()

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            print('[delete_account] ERROR:', e)
            flash('Kuna tatizo limetokea wakati wa kufuta akaunti. Jaribu tena.')
            return redirect(request.referrer or url_for('home'))

        try:
            conn.close()
        except Exception:
            pass

        # Futa faili za disk (baada ya commit)
        for fpath in files_to_remove:
            try:
                if fpath and os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception as fe:
                print('[delete_account] file remove:', fpath, fe)

        session.clear()
        flash('Akaunti yako imefutwa kabisa kwenye mfumo. Huwezi kuirejesha.')
        return redirect(url_for('register'))


    # ----- original lines 9390-10059 -----
    # ====================== PUSH NOTIFICATIONS / PWA ======================

    @app.route("/api/push/public-key")
    def push_public_key():
        return jsonify({"publicKey": VAPID_PUBLIC_KEY})

    @app.route("/api/push/subscribe", methods=["POST"])
    def push_subscribe():
        if "user_id" not in session:
            return jsonify({"success": False, "message": "Login required"}), 401

        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")

        if not endpoint or not p256dh or not auth:
            return jsonify({"success": False, "message": "Invalid subscription"}), 400

        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE push_subscriptions
                   SET user_id = ?, p256dh = ?, auth = ?
                   WHERE endpoint = ?""",
                (session["user_id"], p256dh, auth, endpoint)
            )
        else:
            conn.execute(
                """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                   VALUES (?, ?, ?, ?)""",
                (session["user_id"], endpoint, p256dh, auth)
            )

        conn.commit()
        conn.close()
        return jsonify({"success": True})

    @app.route("/api/push/unsubscribe", methods=["POST"])
    def push_unsubscribe():
        if "user_id" not in session:
            return jsonify({"success": False}), 401

        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")

        conn = get_db_connection()
        if endpoint:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
                (endpoint, session["user_id"])
            )
        else:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ?",
                (session["user_id"],)
            )
        conn.commit()
        conn.close()
        return jsonify({"success": True})


    # ========== TEST PUSH (kwa debug) ==========

    @app.route("/api/push/test", methods=["POST", "GET"])
    def push_test():
        """Tuma push ya majaribio kwa user aliyelogin. Fungua /api/push/test ukiwa umeingia."""
        if "user_id" not in session:
            return jsonify({"success": False, "message": "Login first"}), 401

        uid = session["user_id"]
        conn = get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (uid,)
        ).fetchone()[0]
        conn.close()

        if count == 0:
            return jsonify({
                "success": False,
                "message": "Hakuna subscription. Bonyeza Allow notifications kwenye browser kisha refresh.",
                "subscriptions": 0
            })

        try:
            send_web_push(
                uid,
                "Kijiji Tanzania - Test",
                "Push inafanya kazi! ✅",
                url="/notifications",
                icon=None
            )
            return jsonify({
                "success": True,
                "message": "Push imetumwa. Angalia notification kwenye kifaa chako.",
                "subscriptions": count
            })
        except Exception as e:
            return jsonify({
                "success": False,
                "message": str(e),
                "subscriptions": count
            }), 500

    @app.route('/offline')
    def offline_page():
        """Ukurasa wa offline — service worker fallback."""
        return app.send_static_file('offline.html')

    @app.route("/sw.js")
    def serve_service_worker():
        return app.send_static_file("sw.js"), 200, {
            "Content-Type": "application/javascript; charset=utf-8",
            "Service-Worker-Allowed": "/"
        }

    @app.route('/manifest.json')
    def serve_manifest():
        resp = app.send_static_file('manifest.json')
        resp.headers['Content-Type'] = 'application/manifest+json; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.route('/share', methods=['GET', 'POST'])
    def share_target():
        """PWA Share Target — kupokea share kutoka apps zingine (picha/video/text).
        Inahifadhi media kisha inaelekeza feed na fomu ya post tayari (kama WhatsApp/IG).
        """
        title = (request.form.get('title') or request.args.get('title') or '').strip()
        text = (request.form.get('text') or request.args.get('text') or '').strip()
        url = (request.form.get('url') or request.args.get('url') or '').strip()

        # Collect files from 'media' (manifest) and any other file fields
        files = []
        files.extend(request.files.getlist('media'))
        for key in request.files:
            if key == 'media':
                continue
            files.extend(request.files.getlist(key))

        parts = []
        if title:
            parts.append(title)
        if text:
            parts.append(text)
        if url and url not in text:
            parts.append(url)
        share_content = '\n'.join(parts).strip()

        saved_files = []
        for f in files:
            if not f or not getattr(f, 'filename', None):
                continue
            raw = secure_filename(f.filename) or 'shared_media'
            stamp = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y%m%d%H%M%S')
            token = secrets.token_hex(4)
            fname = f'share_{stamp}_{token}_{raw}'
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
            try:
                f.save(save_path)
                ext = raw.rsplit('.', 1)[-1].lower() if '.' in raw else ''
                mtype = 'video' if ext in VIDEO_EXTS else 'image'
                saved_files.append({'filename': fname, 'media_type': mtype})
                print('[share_target] saved', fname, mtype)
            except Exception as e:
                print('[share_target] save error:', e)

        session['pending_share'] = {
            'content': share_content,
            'files': saved_files,
        }
        session.modified = True
        print('[share_target] pending_share files=', len(saved_files), 'content_len=', len(share_content))

        if 'user_id' not in session:
            session['next_after_login'] = url_for('home') + '?share=1'
            flash('Ingia ili uchapishe kitu ulichoshare', 'info')
            return redirect(url_for('login'))

        if saved_files:
            flash('Media imepokelewa — kagua na bonyeza Post', 'success')
        elif share_content:
            flash('Maandishi yamepokelewa — kagua na bonyeza Post', 'success')
        return redirect(url_for('home') + '?share=1')


    # ######################################################################
    # #########################  GROUP A ROUTES  ###########################
    # ######################################################################

    @app.route('/explore')
    @login_required
    def explore():
        publish_due_scheduled_posts()
        user_id = session['user_id']
        conn = get_db_connection()
        muted = muted_ids_for(conn, user_id)
        try:
            trending = [dict(r) for r in conn.execute(
                "SELECT tag, use_count FROM hashtags ORDER BY use_count DESC, id DESC LIMIT 20"
            ).fetchall()]
        except Exception:
            trending = []
        posts = conn.execute(
            "SELECT p.*, u.username, u.full_name, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id AND user_id = ?) AS user_liked "
            "FROM posts p JOIN users u ON u.id = p.user_id "
            "WHERE COALESCE(p.is_draft, 0) = 0 "
            "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
            "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
            "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
            "AND p.created_at >= datetime('now', '-7 days') "
            "ORDER BY likes_count DESC, comments_count DESC, p.id DESC LIMIT 40",
            (user_id,)
        ).fetchall()
        posts_data = [dict(r) for r in posts if r["user_id"] not in muted]
        suggested = conn.execute(
            "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM follows WHERE following_id = u.id) AS followers_count "
            "FROM users u WHERE u.id != ? AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
            "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
            "AND COALESCE(u.warning_count, 0) = 0 "
            "AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id = ?) "
            "AND u.id NOT IN (SELECT muted_id FROM mutes WHERE muter_id = ?) "
            "ORDER BY COALESCE(u.village_mode,0) DESC, COALESCE(u.is_verified,0) DESC, followers_count DESC LIMIT 20",
            (user_id, user_id, user_id)
        ).fetchall()
        suggested_users = [dict(r) for r in suggested]
        conn.close()
        try:
            return render_template(
                "explore/explore.html", posts_data=posts_data, trending=trending,
                suggested_users=suggested_users, username=session.get("username")
            )
        except Exception:
            return render_template(
                "feed/feed.html", posts_data=posts_data, unread_notifs=0, status_list=[],
                my_status_info={"user_id": user_id, "has_status": False},
                pending_share=None, suggested_users=suggested_users
            )


    @app.route('/api/explore')
    @login_required
    def api_explore():
        publish_due_scheduled_posts()
        user_id = session['user_id']
        cursor = request.args.get('cursor', 0, type=int)
        limit = min(request.args.get('limit', 20, type=int), 50)
        conn = get_db_connection()
        muted = muted_ids_for(conn, user_id)
        trending = []
        try:
            trending = [dict(r) for r in conn.execute(
                "SELECT tag, use_count FROM hashtags ORDER BY use_count DESC LIMIT 15"
            ).fetchall()]
        except Exception:
            pass
        rows = conn.execute(
            "SELECT p.id, p.content, p.file_path, p.media_type, p.created_at, p.user_id, "
            "u.username, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count "
            "FROM posts p JOIN users u ON u.id = p.user_id "
            "WHERE COALESCE(p.is_draft, 0) = 0 "
            "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
            "AND p.id < ? "
            "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
            "ORDER BY likes_count DESC, p.id DESC LIMIT ?",
            (cursor if cursor > 0 else 10**12, limit)
        ).fetchall()
        items = []
        for r in rows:
            if r['user_id'] in muted:
                continue
            items.append({
                'id': r['id'], 'content': r['content'], 'file_path': r['file_path'],
                'media_type': r['media_type'], 'created_at': str(r['created_at'] or ''),
                'user_id': r['user_id'], 'username': r['username'], 'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'], 'likes_count': r['likes_count'],
                'comments_count': r['comments_count'],
            })
        conn.close()
        return jsonify({'success': True, 'trending': trending, 'posts': items,
                        'next_cursor': items[-1]['id'] if items else None})


    @app.route('/trending')
    @login_required
    def trending():
        conn = get_db_connection()
        try:
            tags = [dict(r) for r in conn.execute(
                "SELECT tag, use_count FROM hashtags ORDER BY use_count DESC LIMIT 50"
            ).fetchall()]
        except Exception:
            tags = []
        conn.close()
        try:
            return render_template('explore/trending.html', tags=tags)
        except Exception:
            return jsonify({'success': True, 'tags': tags})


    @app.route('/hashtag/<tag>')
    @login_required
    def hashtag_posts(tag):
        tag = (tag or '').strip().lstrip('#').lower()
        if not tag:
            return redirect(url_for('explore'))
        user_id = session['user_id']
        conn = get_db_connection()
        muted = muted_ids_for(conn, user_id)
        rows = conn.execute(
            "SELECT p.*, u.username, u.full_name, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id AND user_id = ?) AS user_liked, "
            "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count "
            "FROM posts p JOIN users u ON u.id = p.user_id "
            "JOIN post_hashtags ph ON ph.post_id = p.id JOIN hashtags h ON h.id = ph.hashtag_id "
            "WHERE h.tag = ? COLLATE NOCASE AND COALESCE(p.is_draft, 0) = 0 "
            "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
            "ORDER BY p.id DESC LIMIT 50",
            (user_id, tag)
        ).fetchall()
        posts_data = [dict(r) for r in rows if r['user_id'] not in muted]
        use_count = 0
        hr = conn.execute("SELECT use_count FROM hashtags WHERE tag = ? COLLATE NOCASE", (tag,)).fetchone()
        if hr:
            use_count = hr['use_count'] or 0
        conn.close()
        try:
            return render_template('explore/hashtag.html', tag=tag, use_count=use_count,
                                   posts_data=posts_data, username=session.get('username'))
        except Exception:
            return render_template('feed/feed.html', posts_data=posts_data, unread_notifs=0, status_list=[],
                                   my_status_info={'user_id': user_id, 'has_status': False},
                                   pending_share=None, suggested_users=[])


    @app.route('/api/hashtags/suggest')
    @login_required
    def api_hashtag_suggest():
        q = (request.args.get('q') or '').strip().lstrip('#').lower()
        if len(q) < 1:
            return jsonify({'tags': []})
        conn = get_db_connection()
        try:
            tags = [dict(r) for r in conn.execute(
                "SELECT tag, use_count FROM hashtags WHERE tag LIKE ? COLLATE NOCASE "
                "ORDER BY use_count DESC LIMIT 10", (q + '%',)
            ).fetchall()]
        except Exception:
            tags = []
        conn.close()
        return jsonify({'tags': tags})


    @app.route('/api/search/posts')
    @login_required
    def api_search_posts():
        q = (request.args.get('q') or '').strip()
        if len(q) < 2:
            return jsonify({'success': True, 'posts': []})
        user_id = session['user_id']
        conn = get_db_connection()
        muted = muted_ids_for(conn, user_id)
        rows = conn.execute(
            "SELECT p.id, p.content, p.file_path, p.media_type, p.created_at, p.user_id, "
            "u.username, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count "
            "FROM posts p JOIN users u ON u.id = p.user_id "
            "WHERE COALESCE(p.is_draft, 0) = 0 "
            "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
            "AND p.content LIKE ? "
            "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
            "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
            "ORDER BY p.id DESC LIMIT 30",
            ('%' + q + '%',)
        ).fetchall()
        posts = []
        for r in rows:
            if r['user_id'] in muted:
                continue
            posts.append({
                'id': r['id'], 'content': r['content'], 'file_path': r['file_path'],
                'media_type': r['media_type'], 'created_at': str(r['created_at'] or ''),
                'username': r['username'], 'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'], 'likes_count': r['likes_count'],
            })
        conn.close()
        return jsonify({'success': True, 'posts': posts, 'query': q})


    @app.route('/mute/<int:user_id>', methods=['POST'])
    @login_required
    def mute_user(user_id):
        me = session['user_id']
        if me == user_id:
            return jsonify({'success': False, 'message': 'Huwezi kujimute'}), 400
        conn = get_db_connection()
        existing = conn.execute(
            'SELECT id FROM mutes WHERE muter_id = ? AND muted_id = ?', (me, user_id)
        ).fetchone()
        if existing:
            conn.execute('DELETE FROM mutes WHERE muter_id = ? AND muted_id = ?', (me, user_id))
            muted, msg = False, 'Umeondoa mute'
        else:
            conn.execute(
                'INSERT OR IGNORE INTO mutes (muter_id, muted_id, created_at) VALUES (?, ?, ?)',
                (me, user_id, now_tz())
            )
            muted, msg = True, 'Mtumiaji amemutewa'
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'muted': muted, 'message': msg})


    @app.route('/muted')
    @login_required
    def muted_list():
        me = session['user_id']
        conn = get_db_connection()
        users = [dict(r) for r in conn.execute(
            "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, m.created_at "
            "FROM mutes m JOIN users u ON u.id = m.muted_id WHERE m.muter_id = ? ORDER BY m.id DESC",
            (me,)
        ).fetchall()]
        conn.close()
        try:
            return render_template('account/muted.html', users=users)
        except Exception:
            return jsonify({'success': True, 'users': [
                {'id': u['id'], 'username': u['username'], 'full_name': u.get('full_name')} for u in users
            ]})


    @app.route('/close-friends', methods=['GET', 'POST'])
    @login_required
    def close_friends():
        me = session['user_id']
        conn = get_db_connection()
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            action = (data.get('action') or request.form.get('action') or 'toggle').strip()
            try:
                friend_id = int(data.get('friend_id') or request.form.get('friend_id'))
            except (TypeError, ValueError):
                conn.close()
                return jsonify({'success': False, 'message': 'friend_id required'}), 400
            if friend_id == me:
                conn.close()
                return jsonify({'success': False, 'message': 'Invalid'}), 400
            existing = conn.execute(
                'SELECT id FROM close_friends WHERE user_id = ? AND friend_id = ?', (me, friend_id)
            ).fetchone()
            if action == 'remove' or (action == 'toggle' and existing):
                conn.execute('DELETE FROM close_friends WHERE user_id = ? AND friend_id = ?', (me, friend_id))
                is_cf = False
            else:
                conn.execute(
                    'INSERT OR IGNORE INTO close_friends (user_id, friend_id, created_at) VALUES (?, ?, ?)',
                    (me, friend_id, now_tz())
                )
                is_cf = True
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'is_close_friend': is_cf})
        friends = [dict(r) for r in conn.execute(
            "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified "
            "FROM close_friends cf JOIN users u ON u.id = cf.friend_id "
            "WHERE cf.user_id = ? ORDER BY u.username COLLATE NOCASE", (me,)
        ).fetchall()]
        candidates = [dict(r) for r in conn.execute(
            "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM close_friends cf WHERE cf.user_id = ? AND cf.friend_id = u.id) AS is_cf "
            "FROM follows f JOIN users u ON u.id = f.following_id WHERE f.follower_id = ? "
            "ORDER BY u.username COLLATE NOCASE LIMIT 100", (me, me)
        ).fetchall()]
        conn.close()
        try:
            return render_template('account/close_friends.html', friends=friends, candidates=candidates)
        except Exception:
            return jsonify({'success': True, 'friends': friends, 'candidates': candidates})


    @app.route('/status/privacy/<int:status_id>', methods=['POST'])
    @login_required
    def status_privacy(status_id):
        data = request.get_json(silent=True) or {}
        privacy = (data.get('privacy') or request.form.get('privacy') or 'public').strip().lower()
        if privacy not in ('public', 'followers', 'close_friends'):
            return jsonify({'success': False, 'message': 'privacy si sahihi'}), 400
        conn = get_db_connection()
        row = conn.execute('SELECT user_id FROM statuses WHERE id = ?', (status_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        if int(row['user_id']) != int(session['user_id']):
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403
        try:
            conn.execute('UPDATE statuses SET privacy = ? WHERE id = ?', (privacy, status_id))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'message': str(e)}), 500
        conn.close()
        return jsonify({'success': True, 'privacy': privacy})


    @app.route('/chat/edit/<int:msg_id>', methods=['POST'])
    @login_required
    def chat_edit(msg_id):
        data = request.get_json(silent=True) or {}
        new_text = (data.get('message') or data.get('content') or '').strip()
        if not new_text:
            return jsonify({'success': False, 'message': 'Ujumbe hauwezi kuwa tupu'}), 400
        if len(new_text) > 5000:
            new_text = new_text[:5000]
        me = session['user_id']
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or int(msg['sender_id']) != int(me):
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403
        try:
            conn.execute(
                'UPDATE private_messages SET message = ?, edited_at = ? WHERE id = ?',
                (new_text, now_tz(), msg_id)
            )
        except sqlite3.OperationalError:
            conn.execute('UPDATE private_messages SET message = ? WHERE id = ?', (new_text, msg_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': new_text, 'edited_at': now_tz()})


    @app.route('/chat/forward', methods=['POST'])
    @login_required
    def chat_forward():
        data = request.get_json(silent=True) or {}
        try:
            msg_id = int(data.get('message_id'))
            to_user_id = int(data.get('to_user_id'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'message_id na to_user_id vinahitajika'}), 400
        me = session['user_id']
        if to_user_id == me:
            return jsonify({'success': False, 'message': 'Huwezi kujiforward'}), 400
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or me not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403
        target = conn.execute('SELECT id, username FROM users WHERE id = ?', (to_user_id,)).fetchone()
        if not target:
            conn.close()
            return jsonify({'success': False, 'message': 'Mpokeaji hajapatikana'}), 404
        body = msg['message'] or ''
        if not body.startswith('Forwarded') and '↪️' not in body[:10]:
            body = '↪️ Forwarded\n' + body
        fp = msg['file_path'] if 'file_path' in msg.keys() else None
        mt = msg['media_type'] if 'media_type' in msg.keys() else None
        cur = conn.execute(
            "INSERT INTO private_messages "
            "(sender_id, receiver_id, message, is_read, is_delivered, file_path, media_type, created_at) "
            "VALUES (?, ?, ?, 0, 0, ?, ?, ?)",
            (me, to_user_id, body, fp, mt, now_tz())
        )
        new_id = cur.lastrowid
        conn.commit()
        try:
            notify_user(to_user_id, me, 'message', body[:80])
        except Exception:
            pass
        conn.close()
        return jsonify({'success': True, 'message_id': new_id, 'to_username': target['username']})


    @app.route('/chat/star/<int:msg_id>', methods=['POST'])
    @login_required
    def chat_star(msg_id):
        me = session['user_id']
        conn = get_db_connection()
        msg = conn.execute(
            'SELECT id, sender_id, receiver_id FROM private_messages WHERE id = ?', (msg_id,)
        ).fetchone()
        if not msg or me not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False}), 403
        existing = conn.execute(
            'SELECT id FROM starred_messages WHERE user_id = ? AND message_id = ?', (me, msg_id)
        ).fetchone()
        if existing:
            conn.execute('DELETE FROM starred_messages WHERE user_id = ? AND message_id = ?', (me, msg_id))
            starred = False
        else:
            conn.execute(
                'INSERT OR IGNORE INTO starred_messages (user_id, message_id, created_at) VALUES (?, ?, ?)',
                (me, msg_id, now_tz())
            )
            starred = True
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'starred': starred})


    @app.route('/chat/starred')
    @login_required
    def chat_starred():
        me = session['user_id']
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT pm.*, u.username AS sender_username FROM starred_messages sm "
            "JOIN private_messages pm ON pm.id = sm.message_id "
            "JOIN users u ON u.id = pm.sender_id WHERE sm.user_id = ? "
            "ORDER BY sm.id DESC LIMIT 100", (me,)
        ).fetchall()
        items = [dict(r) for r in rows]
        for m in items:
            if m.get('created_at') is not None:
                m['created_at'] = str(m['created_at'])
        conn.close()
        return jsonify({'success': True, 'messages': items})


    @app.route('/inbox/archive', methods=['POST'])
    @login_required
    def inbox_archive():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        want = data.get('archived', None)
        if not username:
            return jsonify({'success': False, 'message': 'username required'}), 400
        me = session['user_id']
        conn = get_db_connection()
        other = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if not other:
            conn.close()
            return jsonify({'success': False, 'message': 'User not found'}), 404
        other_id = other['id']
        existing = conn.execute(
            'SELECT id FROM archived_chats WHERE user_id = ? AND other_user_id = ?', (me, other_id)
        ).fetchone()
        if want is True or (want is None and not existing):
            if not existing:
                conn.execute(
                    'INSERT INTO archived_chats (user_id, other_user_id, created_at) VALUES (?, ?, ?)',
                    (me, other_id, now_tz())
                )
            archived = True
        else:
            conn.execute(
                'DELETE FROM archived_chats WHERE user_id = ? AND other_user_id = ?', (me, other_id)
            )
            archived = False
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'archived': archived})


    # ----- original lines 10060-10804 -----
    @app.route('/drafts', methods=['GET'])
    @login_required
    def drafts_list():
        me = session['user_id']
        try:
            publish_due_scheduled_posts()
        except Exception:
            pass
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT id, content, file_path, media_type, scheduled_at, created_at, category, published_at "
            "FROM posts WHERE user_id = ? AND COALESCE(is_draft, 0) = 1 ORDER BY id DESC LIMIT 200",
            (me,)
        ).fetchall()
        items = []
        now = now_tz()
        for r in rows:
            sched = str(r['scheduled_at'] or '') if r['scheduled_at'] else None
            status = 'draft'
            if sched:
                status = 'scheduled'
                try:
                    if sched <= now:
                        status = 'due'
                except Exception:
                    pass
            items.append({
                'id': r['id'],
                'content': r['content'] or '',
                'file_path': r['file_path'],
                'media_type': r['media_type'],
                'scheduled_at': sched,
                'created_at': str(r['created_at'] or ''),
                'category': r['category'] if 'category' in r.keys() else 'general',
                'status': status,
            })
        conn.close()
        try:
            return render_template('account/drafts.html', drafts=items, now_server=now)
        except Exception:
            return jsonify({'success': True, 'drafts': items})


    def _parse_scheduled_at(raw):
        scheduled_at = (raw or '').strip() or None
        if not scheduled_at:
            return None, None
        try:
            datetime.strptime(scheduled_at[:19], '%Y-%m-%d %H:%M:%S')
            return scheduled_at[:19], None
        except ValueError:
            pass
        try:
            datetime.strptime(scheduled_at[:16], '%Y-%m-%dT%H:%M')
            return scheduled_at[:16].replace('T', ' ') + ':00', None
        except ValueError:
            return None, 'scheduled_at si sahihi'


    def _guess_category(content):
        category = 'general'
        text_l = (content or '').lower()
        for words, cat in [
            (['mpira', 'football', 'goal', 'simba', 'yanga'], 'football'),
            (['allah', 'islam', 'quran', 'swalah'], 'islamic'),
            (['comedy', 'haha', 'ucheshi'], 'comedy'),
            (['music', 'muziki', 'wimbo'], 'music'),
            (['tech', 'python', 'coding', 'ai'], 'technology'),
            (['habari', 'news'], 'news'),
            (['biashara', 'bei', 'uza'], 'business'),
        ]:
            if any(w in text_l for w in words):
                category = cat
                break
        return category


    def _save_draft_media(file):
        if not file or not file.filename or not allowed_file(file.filename):
            return None, None, None
        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[1].lower()
        is_video = ext in VIDEO_EXTS
        is_image = ext in IMAGE_EXTS
        is_doc = ext in getattr(__import__('builtins'), 'DOCUMENT_EXTS', set()) if False else ext in (
            'pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx', 'ppt', 'pptx', 'zip'
        )
        if not is_video and not is_image and not is_doc:
            return None, None, 'Aina ya file hairuhusiwi'
        unique_filename = f"draft_{session['user_id']}_{int(time.time())}_{filename}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
        if is_video:
            media_type = 'video'
        elif is_image:
            media_type = 'image'
        else:
            media_type = 'document'
        return unique_filename, media_type, None


    @app.route('/drafts/save', methods=['POST'])
    @login_required
    def drafts_save():
        content = (request.form.get('content') or '').strip()
        scheduled_at, sched_err = _parse_scheduled_at(request.form.get('scheduled_at'))
        if sched_err:
            return jsonify({'success': False, 'message': sched_err}), 400
        draft_id = request.form.get('draft_id', type=int)
        remove_media = (request.form.get('remove_media') or '') in ('1', 'true', 'yes')
        category = (request.form.get('category') or '').strip() or _guess_category(content)
        file = request.files.get('file')
        file_path, media_type, media_err = _save_draft_media(file)
        if media_err:
            return jsonify({'success': False, 'message': media_err}), 400

        conn = get_db_connection()
        me = session['user_id']

        if draft_id:
            row = conn.execute(
                'SELECT * FROM posts WHERE id = ? AND user_id = ? AND COALESCE(is_draft,0)=1',
                (draft_id, me)
            ).fetchone()
            if not row:
                conn.close()
                return jsonify({'success': False, 'message': 'Draft haipatikani'}), 404
            new_content = content if content or file_path or remove_media else (row['content'] or '')
            new_fp = row['file_path']
            new_mt = row['media_type']
            if remove_media and row['file_path']:
                try:
                    fp = os.path.join(app.config['UPLOAD_FOLDER'], row['file_path'])
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass
                new_fp, new_mt = None, None
            if file_path:
                if row['file_path'] and row['file_path'] != file_path:
                    try:
                        fp = os.path.join(app.config['UPLOAD_FOLDER'], row['file_path'])
                        if os.path.isfile(fp):
                            os.remove(fp)
                    except Exception:
                        pass
                new_fp, new_mt = file_path, media_type
            if not (new_content or '').strip() and not new_fp:
                conn.close()
                return jsonify({'success': False, 'message': 'Andika kitu au weka media'}), 400
            # scheduled_at: form empty string means clear; missing key keep existing
            if 'scheduled_at' in request.form:
                final_sched = scheduled_at
            else:
                final_sched = row['scheduled_at']
            conn.execute(
                '''UPDATE posts SET content=?, file_path=?, media_type=?, category=?, scheduled_at=?
                   WHERE id=? AND user_id=?''',
                (new_content, new_fp, new_mt, category, final_sched, draft_id, me)
            )
            if new_content:
                try:
                    save_post_hashtags(conn, draft_id, new_content)
                except Exception:
                    pass
            conn.commit()
            conn.close()
            return jsonify({
                'success': True, 'draft_id': draft_id, 'scheduled_at': final_sched,
                'file_path': new_fp, 'media_type': new_mt, 'category': category,
                'message': 'Draft imesasishwa' + (' · imepangwa' if final_sched else ''),
            })

        if not content and not file_path:
            conn.close()
            return jsonify({'success': False, 'message': 'Andika kitu au weka media'}), 400
        cur = conn.execute(
            "INSERT INTO posts (user_id, content, file_path, media_type, shares, created_at, category, is_draft, scheduled_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?)",
            (me, content, file_path, media_type, now_tz(), category, scheduled_at)
        )
        new_id = cur.lastrowid
        if content:
            try:
                save_post_hashtags(conn, new_id, content)
            except Exception:
                pass
        conn.commit()
        conn.close()
        return jsonify({
            'success': True, 'draft_id': new_id, 'scheduled_at': scheduled_at,
            'file_path': file_path, 'media_type': media_type, 'category': category,
            'message': 'Draft imehifadhiwa' + (' na kupangwa' if scheduled_at else ''),
        })


    @app.route('/drafts/publish/<int:post_id>', methods=['POST'])
    @login_required
    def drafts_publish(post_id):
        me = session['user_id']
        conn = get_db_connection()
        row = conn.execute(
            'SELECT * FROM posts WHERE id = ? AND user_id = ? AND COALESCE(is_draft,0)=1',
            (post_id, me)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'message': 'Draft haipatikani'}), 404
        if not (row['content'] or '').strip() and not row['file_path']:
            conn.close()
            return jsonify({'success': False, 'message': 'Draft tupu haiwezi kuchapishwa'}), 400
        now = now_tz()
        conn.execute(
            'UPDATE posts SET is_draft = 0, published_at = ?, scheduled_at = NULL WHERE id = ?',
            (now, post_id)
        )
        if row['content']:
            try:
                save_post_hashtags(conn, post_id, row['content'])
            except Exception:
                pass
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Post imechapishwa', 'post_id': post_id})


    @app.route('/drafts/delete/<int:post_id>', methods=['POST'])
    @login_required
    def drafts_delete(post_id):
        me = session['user_id']
        conn = get_db_connection()
        row = conn.execute(
            'SELECT file_path FROM posts WHERE id = ? AND user_id = ? AND COALESCE(is_draft,0) = 1',
            (post_id, me)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))
        conn.commit()
        conn.close()
        if row['file_path']:
            try:
                fp = os.path.join(app.config['UPLOAD_FOLDER'], row['file_path'])
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass
        return jsonify({'success': True})


    @app.route('/drafts/duplicate/<int:post_id>', methods=['POST'])
    @login_required
    def drafts_duplicate(post_id):
        me = session['user_id']
        conn = get_db_connection()
        row = conn.execute(
            'SELECT * FROM posts WHERE id = ? AND user_id = ? AND COALESCE(is_draft,0)=1',
            (post_id, me)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'message': 'Draft haipatikani'}), 404
        new_fp = row['file_path']
        # optional: copy file so each draft owns media
        if row['file_path']:
            try:
                src = os.path.join(app.config['UPLOAD_FOLDER'], row['file_path'])
                if os.path.isfile(src):
                    base = f"draft_{me}_{int(time.time())}_copy_{row['file_path']}"
                    dst = os.path.join(app.config['UPLOAD_FOLDER'], base)
                    import shutil
                    shutil.copy2(src, dst)
                    new_fp = base
            except Exception:
                new_fp = row['file_path']
        cur = conn.execute(
            "INSERT INTO posts (user_id, content, file_path, media_type, shares, created_at, category, is_draft, scheduled_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, 1, NULL)",
            (me, row['content'], new_fp, row['media_type'], now_tz(),
             row['category'] if 'category' in row.keys() else 'general')
        )
        new_id = cur.lastrowid
        if row['content']:
            try:
                save_post_hashtags(conn, new_id, row['content'])
            except Exception:
                pass
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'draft_id': new_id, 'message': 'Draft imenakiliwa'})


    @app.route('/drafts/unschedule/<int:post_id>', methods=['POST'])
    @login_required
    def drafts_unschedule(post_id):
        me = session['user_id']
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id FROM posts WHERE id = ? AND user_id = ? AND COALESCE(is_draft,0)=1',
            (post_id, me)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        conn.execute('UPDATE posts SET scheduled_at = NULL WHERE id = ?', (post_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Muda umefutwa'})


    @app.route('/insights')
    @login_required
    def creator_insights():
        me = session['user_id']

        # Dashboard (Insights) ni sehemu ya Kijiji Mode — inaonekana tu kwa
        # watumiaji waliowasha Kijiji Mode.
        gate_conn = get_db_connection()
        gate_row = gate_conn.execute(
            'SELECT village_mode FROM users WHERE id = ?', (me,)
        ).fetchone()
        gate_conn.close()
        if not gate_row or not gate_row['village_mode']:
            flash('Washa Kijiji Mode kwanza ili kuona Dashboard (Insights).', 'info')
            return redirect(url_for('settings_kijiji_mode'))

        period = (request.args.get('period') or '30').strip().lower()
        period_map = {'1': 1, '7': 7, '30': 30, '90': 90, 'all': None}
        days = period_map.get(period, 30)

        conn = get_db_connection()
        cutoff = None
        if days:
            try:
                from datetime import datetime, timedelta
                from zoneinfo import ZoneInfo
                tz = ZoneInfo('Africa/Nairobi')
                cutoff = (datetime.now(tz) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                cutoff = None

        def q1(sql, params=()):
            try:
                return conn.execute(sql, params).fetchone()[0] or 0
            except Exception:
                return 0

        def qall(sql, params=()):
            try:
                return conn.execute(sql, params).fetchall()
            except Exception:
                return []

        posts_count = q1(
            "SELECT COUNT(*) FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0", (me,))
        likes_count = q1(
            "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id WHERE p.user_id = ?", (me,))
        comments_count = q1(
            "SELECT COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id WHERE p.user_id = ? AND COALESCE(c.is_hidden,0)=0", (me,))
        views_count = q1(
            "SELECT COUNT(DISTINCT pv.user_id) FROM post_views pv JOIN posts p ON p.id = pv.post_id WHERE p.user_id = ?", (me,))
        views_total = q1(
            "SELECT COUNT(*) FROM post_views pv JOIN posts p ON p.id = pv.post_id WHERE p.user_id = ?", (me,))
        shares_count = q1(
            "SELECT COALESCE(SUM(p.shares),0) FROM posts p WHERE p.user_id = ? AND COALESCE(p.is_draft,0)=0", (me,))
        saves_count = q1(
            "SELECT COUNT(*) FROM saved_posts s JOIN posts p ON p.id = s.post_id WHERE p.user_id = ?", (me,))
        followers = q1("SELECT COUNT(*) FROM follows WHERE following_id = ?", (me,))
        following = q1("SELECT COUNT(*) FROM follows WHERE follower_id = ?", (me,))

        if cutoff:
            posts_period = q1(
                "SELECT COUNT(*) FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 AND created_at >= ?",
                (me, cutoff))
            likes_period = q1(
                "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id "
                "WHERE p.user_id = ? AND COALESCE(p.is_draft,0)=0 AND p.created_at >= ?",
                (me, cutoff))
            comments_period = q1(
                "SELECT COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id "
                "WHERE p.user_id = ? AND COALESCE(c.is_hidden,0)=0 AND COALESCE(p.is_draft,0)=0 "
                "AND (c.created_at >= ? OR p.created_at >= ?)",
                (me, cutoff, cutoff))
            views_period = q1(
                "SELECT COUNT(*) FROM post_views pv JOIN posts p ON p.id = pv.post_id "
                "WHERE p.user_id = ? AND pv.created_at >= ?",
                (me, cutoff))
            shares_period = q1(
                "SELECT COALESCE(SUM(shares),0) FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 AND created_at >= ?",
                (me, cutoff))
            saves_period = q1(
                "SELECT COUNT(*) FROM saved_posts s JOIN posts p ON p.id = s.post_id "
                "WHERE p.user_id = ? AND p.created_at >= ?",
                (me, cutoff))
            try:
                from datetime import datetime, timedelta
                from zoneinfo import ZoneInfo
                tz = ZoneInfo('Africa/Nairobi')
                prev_end = cutoff
                prev_start = (datetime.now(tz) - timedelta(days=days * 2)).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                prev_start, prev_end = None, None
            if prev_start and prev_end:
                likes_prev = q1(
                    "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id "
                    "WHERE p.user_id = ? AND p.created_at >= ? AND p.created_at < ?",
                    (me, prev_start, prev_end))
                views_prev = q1(
                    "SELECT COUNT(*) FROM post_views pv JOIN posts p ON p.id = pv.post_id "
                    "WHERE p.user_id = ? AND pv.created_at >= ? AND pv.created_at < ?",
                    (me, prev_start, prev_end))
                posts_prev = q1(
                    "SELECT COUNT(*) FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 "
                    "AND created_at >= ? AND created_at < ?",
                    (me, prev_start, prev_end))
            else:
                likes_prev = views_prev = posts_prev = 0
        else:
            posts_period, likes_period, comments_period = posts_count, likes_count, comments_count
            views_period, shares_period, saves_period = views_total, shares_count, saves_count
            likes_prev = views_prev = posts_prev = 0

        def pct_change(cur, prev):
            if prev and prev > 0:
                return round(((cur - prev) / prev) * 100, 1)
            if cur > 0:
                return 100.0
            return 0.0

        engagement_actions = likes_period + comments_period + shares_period + saves_period
        eng_rate = round((engagement_actions / views_period) * 100, 2) if views_period else 0.0

        series_days = days or 30
        daily = []
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            tz = ZoneInfo('Africa/Nairobi')
            base = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            for i in range(series_days - 1, -1, -1):
                day = base - timedelta(days=i)
                day_s = day.strftime('%Y-%m-%d')
                day_start = day_s + ' 00:00:00'
                day_end = day_s + ' 23:59:59'
                likes_d = q1(
                    "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id "
                    "WHERE p.user_id = ? AND p.created_at >= ? AND p.created_at <= ?",
                    (me, day_start, day_end))
                views_d = q1(
                    "SELECT COUNT(*) FROM post_views pv JOIN posts p ON p.id = pv.post_id "
                    "WHERE p.user_id = ? AND pv.created_at >= ? AND pv.created_at <= ?",
                    (me, day_start, day_end))
                comments_d = q1(
                    "SELECT COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id "
                    "WHERE p.user_id = ? AND COALESCE(c.is_hidden,0)=0 "
                    "AND c.created_at >= ? AND c.created_at <= ?",
                    (me, day_start, day_end))
                posts_d = q1(
                    "SELECT COUNT(*) FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 "
                    "AND created_at >= ? AND created_at <= ?",
                    (me, day_start, day_end))
                daily.append({
                    'date': day_s, 'label': day.strftime('%d/%m'),
                    'likes': likes_d, 'views': views_d, 'comments': comments_d, 'posts': posts_d,
                })
        except Exception as e:
            print('[insights] daily', e)
            daily = []

        hour_rows = qall(
            "SELECT CAST(strftime('%H', pv.created_at) AS INTEGER) AS h, COUNT(*) AS c "
            "FROM post_views pv JOIN posts p ON p.id = pv.post_id "
            "WHERE p.user_id = ? AND pv.created_at IS NOT NULL GROUP BY h ORDER BY h",
            (me,))
        hours = {int(r['h']): r['c'] for r in hour_rows if r['h'] is not None}
        best_hours = sorted(hours.items(), key=lambda x: -x[1])[:3]

        weekday_names = ['Jumapili', 'Jumatatu', 'Jumanne', 'Jumatano', 'Alhamisi', 'Ijumaa', 'Jumamosi']
        weekdays = []
        for r in qall(
            "SELECT CAST(strftime('%w', created_at) AS INTEGER) AS d, COUNT(*) AS posts "
            "FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 GROUP BY d",
            (me,)):
            weekdays.append({'day': weekday_names[int(r['d']) % 7], 'posts': r['posts'], 'dow': int(r['d'])})

        weekday_views = []
        for r in qall(
            "SELECT CAST(strftime('%w', pv.created_at) AS INTEGER) AS d, COUNT(*) AS c "
            "FROM post_views pv JOIN posts p ON p.id = pv.post_id WHERE p.user_id = ? GROUP BY d",
            (me,)):
            weekday_views.append({'day': weekday_names[int(r['d']) % 7], 'views': r['c'], 'dow': int(r['d'])})

        media = []
        for r in qall(
            "SELECT COALESCE(NULLIF(media_type,''), 'text') AS mt, COUNT(*) AS posts "
            "FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 GROUP BY mt",
            (me,)):
            mt = r['mt'] or 'text'
            likes_m = q1(
                "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id "
                "WHERE p.user_id = ? AND COALESCE(NULLIF(p.media_type,''), 'text') = ?",
                (me, mt))
            views_m = q1(
                "SELECT COUNT(*) FROM post_views pv JOIN posts p ON p.id = pv.post_id "
                "WHERE p.user_id = ? AND COALESCE(NULLIF(p.media_type,''), 'text') = ?",
                (me, mt))
            media.append({'type': mt, 'posts': r['posts'], 'likes': likes_m, 'views': views_m})

        top_tags = []
        for r in qall(
            "SELECT h.tag, COUNT(*) AS c FROM hashtags h "
            "JOIN post_hashtags ph ON ph.hashtag_id = h.id "
            "JOIN posts p ON p.id = ph.post_id "
            "WHERE p.user_id = ? AND COALESCE(p.is_draft,0)=0 "
            "GROUP BY h.tag ORDER BY c DESC LIMIT 12",
            (me,)):
            top_tags.append({'tag': r['tag'], 'count': r['c']})

        def post_rows(order_sql, limit=8):
            rows = qall(
                "SELECT p.id, p.content, p.file_path, p.media_type, p.created_at, p.shares, p.category, "
                "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
                "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count, "
                "(SELECT COUNT(DISTINCT user_id) FROM post_views WHERE post_id = p.id) AS views_count, "
                "(SELECT COUNT(*) FROM saved_posts WHERE post_id = p.id) AS saves_count, "
                "(SELECT COUNT(*) FROM follows WHERE source_post_id = p.id) AS new_followers "
                "FROM posts p WHERE p.user_id = ? AND COALESCE(p.is_draft,0)=0 "
                "ORDER BY " + order_sql + " LIMIT ?",
                (me, limit))
            out = []
            for r in rows:
                eng = (r['likes_count'] or 0) + (r['comments_count'] or 0) + (r['shares'] or 0) + (r['saves_count'] or 0)
                out.append({
                    'id': r['id'],
                    'content': (r['content'] or '')[:140],
                    'file_path': r['file_path'] or '',
                    'media_type': r['media_type'] or 'text',
                    'likes_count': r['likes_count'] or 0,
                    'comments_count': r['comments_count'] or 0,
                    'views_count': r['views_count'] or 0,
                    'shares': r['shares'] or 0,
                    'saves_count': r['saves_count'] or 0,
                    'new_followers': r['new_followers'] or 0,
                    'engagement': eng,
                    'created_at': str(r['created_at'] or ''),
                    'category': r['category'] if 'category' in r.keys() else 'general',
                })
            return out

        top_posts = post_rows("(likes_count + comments_count + COALESCE(shares,0)) DESC, views_count DESC", 10)
        bottom_posts = post_rows("(likes_count + comments_count + COALESCE(shares,0)) ASC, views_count ASC", 5)

        categories = []
        for r in qall(
            "SELECT COALESCE(category,'general') AS cat, COUNT(*) AS posts "
            "FROM posts WHERE user_id = ? AND COALESCE(is_draft,0)=0 GROUP BY cat ORDER BY posts DESC",
            (me,)):
            cat = r['cat']
            lk = q1(
                "SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id "
                "WHERE p.user_id = ? AND COALESCE(p.category,'general') = ?",
                (me, cat))
            categories.append({'category': cat, 'posts': r['posts'], 'likes': lk})

        engaged = []
        for r in qall(
            "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id WHERE p.user_id = ? AND l.user_id = u.id) + "
            "(SELECT COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id WHERE p.user_id = ? AND c.user_id = u.id) AS score "
            "FROM users u WHERE u.id != ? AND ("
            "EXISTS (SELECT 1 FROM likes l JOIN posts p ON p.id = l.post_id WHERE p.user_id = ? AND l.user_id = u.id) "
            "OR EXISTS (SELECT 1 FROM comments c JOIN posts p ON p.id = c.post_id WHERE p.user_id = ? AND c.user_id = u.id)"
            ") ORDER BY score DESC LIMIT 10",
            (me, me, me, me, me)):
            engaged.append({
                'id': r['id'], 'username': r['username'],
                'full_name': r['full_name'] if 'full_name' in r.keys() else None,
                'profile_pic': r['profile_pic'], 'is_verified': r['is_verified'] or 0,
                'score': r['score'] or 0,
            })

        tips = []
        if media:
            best_m = max(media, key=lambda x: (x['likes'] / x['posts']) if x['posts'] else 0)
            if best_m['posts'] >= 1 and best_m['likes'] > 0:
                tips.append("Aina '%s' inapata likes nyingi kwa post — zingatia kuchapisha zaidi." % best_m['type'])
        if best_hours:
            tips.append("Saa bora ya views ni saa %02d:00 EAT — panga drafts karibu na muda huo." % best_hours[0][0])
        if weekday_views:
            best_d = max(weekday_views, key=lambda x: x['views'])
            tips.append("Siku yenye views nyingi: %s." % best_d['day'])
        if eng_rate < 2 and views_period > 20:
            tips.append("Engagement rate ni chini — jaribu maswali, hashtags, au video fupi.")
        if posts_period == 0 and days:
            tips.append("Hujachapisha katika kipindi hiki — endelea kuwa active.")
        if not tips:
            tips.append("Endelea kuchapisha content thabiti. Insights zitajaa kadri data inavyoongezeka.")

        conn.close()

        stats = {
            'posts_count': posts_count, 'likes_count': likes_count, 'comments_count': comments_count,
            'views_count': views_count, 'views_total': views_total, 'shares_count': shares_count,
            'saves_count': saves_count, 'followers': followers, 'following': following,
            'period': period, 'period_days': days,
            'posts_period': posts_period, 'likes_period': likes_period,
            'comments_period': comments_period, 'views_period': views_period,
            'shares_period': shares_period, 'saves_period': saves_period,
            'engagement_rate': eng_rate,
            'likes_change': pct_change(likes_period, likes_prev),
            'views_change': pct_change(views_period, views_prev),
            'posts_change': pct_change(posts_period, posts_prev),
            'daily': daily,
            'daily_views_sum': sum((d.get('views') or 0) for d in daily),
            'daily_likes_sum': sum((d.get('likes') or 0) for d in daily),
            'hours': [{'hour': h, 'count': c} for h, c in sorted(hours.items())],
            'best_hours': [{'hour': h, 'count': c} for h, c in best_hours],
            'weekdays': weekdays, 'weekday_views': weekday_views,
            'media': media, 'top_tags': top_tags,
            'top_posts': top_posts, 'bottom_posts': bottom_posts,
            'categories': categories, 'engaged_followers': engaged, 'tips': tips,
            'new_followers_total': q1(
                "SELECT COUNT(*) FROM follows f JOIN posts p ON p.id = f.source_post_id "
                "WHERE p.user_id = ?", (me,)),
        }

        try:
            return render_template('account/insights.html', stats=stats, username=session.get('username'), period=period)
        except Exception as e:
            print('[insights] template', e)
            return jsonify({'success': True, **stats})


    @app.route('/api/insights')
    @login_required
    def api_insights():
        return creator_insights()


    @app.route('/settings/sessions')
    @login_required
    def settings_sessions():
        me = session['user_id']
        current_token = session.get('_sid')
        conn = get_db_connection()
        rows = conn.execute(
            'SELECT id, device_info, ip_address, created_at, last_active, session_token '
            'FROM user_sessions WHERE user_id = ? ORDER BY last_active DESC LIMIT 30',
            (me,)
        ).fetchall()
        sessions_list = [{
            'id': r['id'], 'device_info': r['device_info'], 'ip_address': r['ip_address'],
            'created_at': str(r['created_at'] or ''), 'last_active': str(r['last_active'] or ''),
            'is_current': bool(current_token and r['session_token'] == current_token),
        } for r in rows]
        conn.close()
        try:
            return render_template('account/sessions.html', sessions=sessions_list)
        except Exception:
            return jsonify({'success': True, 'sessions': sessions_list})


    @app.route('/settings/sessions/revoke/<int:session_id>', methods=['POST'])
    @login_required
    def settings_sessions_revoke(session_id):
        me = session['user_id']
        current_token = session.get('_sid')
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, session_token FROM user_sessions WHERE id = ? AND user_id = ?',
            (session_id, me)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'message': 'Session haipatikani'}), 404
        if current_token and row['session_token'] == current_token:
            conn.close()
            return jsonify({'success': False, 'message': 'Huwezi kufuta session ya sasa. Tumia Logout.'}), 400
        conn.execute('DELETE FROM user_sessions WHERE id = ?', (session_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Session imefutwa'})


    @app.route('/settings/sessions/revoke-others', methods=['POST'])
    @login_required
    def settings_sessions_revoke_others():
        me = session['user_id']
        current_token = session.get('_sid')
        conn = get_db_connection()
        if current_token:
            conn.execute(
                'DELETE FROM user_sessions WHERE user_id = ? AND session_token != ?',
                (me, current_token)
            )
        else:
            conn.execute('DELETE FROM user_sessions WHERE user_id = ?', (me,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Sessions zingine zimefutwa'})

    @app.route('/settings/kijiji-mode', methods=['GET', 'POST'])
    @login_required
    def settings_kijiji_mode():
        me = session['user_id']
        conn = get_db_connection()

        if request.method == 'POST':
            new_state = 1 if request.form.get('village_mode') == 'on' else 0
            conn.execute('UPDATE users SET village_mode = ? WHERE id = ?', (new_state, me))
            conn.commit()
            if new_state == 0:
                # Kijiji Mode off → rudi profile mode
                set_active_linkup_id(None)
            conn.close()
            return jsonify({'success': True, 'village_mode': bool(new_state)})

        user = conn.execute('SELECT * FROM users WHERE id = ?', (me,)).fetchone()
        village_mode = bool(user['village_mode']) if user and 'village_mode' in user.keys() else False
        recommendable = ((user['warning_count'] or 0) == 0) if user and 'warning_count' in user.keys() else True

        followers_count = conn.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (me,)).fetchone()[0]
        following_count = conn.execute('SELECT COUNT(*) FROM follows WHERE follower_id = ?', (me,)).fetchone()[0]
        friends_count = conn.execute(
            "SELECT COUNT(*) FROM follows f1 WHERE f1.follower_id = ? AND EXISTS ("
            "  SELECT 1 FROM follows f2 WHERE f2.follower_id = f1.following_id AND f2.following_id = f1.follower_id"
            ")", (me,)
        ).fetchone()[0]
        my_linkups = get_user_linkups(conn, me) if village_mode else []
        active_lu = resolve_active_linkup(conn, me) if village_mode else None
        conn.close()

        return render_template(
            'kijiji/kijiji_mode.html',
            village_mode=village_mode,
            recommendable=recommendable,
            followers_count=followers_count,
            following_count=following_count,
            friends_count=friends_count,
            my_linkups=my_linkups,
            active_linkup=active_lu,
            max_linkups=MAX_LINKUPS_PER_USER,
        )




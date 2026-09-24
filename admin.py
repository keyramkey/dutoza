"""
admin.py
========
Route zote za Admin:
  /admin, /admin/messages, verify, block, warn, broadcast,
  delete_user, badge approve/reject, reports, moderation (NSFW)

Unganisha kwenye app.py:
    from admin import register_admin_routes
    register_admin_routes(app)
"""

import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import (
    session, redirect, url_for, render_template, request,
    flash, jsonify, send_from_directory
)
from functools import wraps
from werkzeug.utils import secure_filename

# Fallback kama app haijatoa
try:
    from app import allowed_file, IMAGE_EXTS
except ImportError:
    IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    def allowed_file(filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in IMAGE_EXTS


def register_admin_routes(app):
    """Sajili route za admin. Endpoint names zinabaki sawa."""
    from app import (
        get_db_connection, now_tz, login_required,
        register_nsfw_violation, UPLOAD_BADGES_FOLDER,
        UPLOAD_FOLDER, BASE_DIR, allowed_badge_file,
    )
    try:
        from app import create_notification
    except ImportError:
        def create_notification(*args, **kwargs):
            pass

    ADMIN_EMAIL = 'matondomaduhu135@gmail.com'

    def require_admin():
        """Rudi (None, redirect) kama si admin, au (conn, None) kama ni admin."""
        if 'user_id' not in session:
            flash("Tafadhali ingia kwanza!")
            return None, redirect(url_for('login'))
        conn = get_db_connection()
        admin = conn.execute(
            'SELECT email FROM users WHERE id = ?', (session['user_id'],)
        ).fetchone()
        if not admin or admin['email'] != ADMIN_EMAIL:
            conn.close()
            flash("Huruhusiwi kuingia hapa!")
            return None, redirect(url_for('home'))
        return conn, None

    # ====================== ADMIN ======================

    @app.route('/admin/messages')
    def admin_messages():
        conn, err = require_admin()
        if err:
            return err
        try:
            rows = conn.execute(
                "SELECT * FROM admin_messages ORDER BY created_at DESC"
            ).fetchall()
            messages = [dict(r) for r in rows]
        finally:
            conn.close()
        return render_template('admin/admin_messages.html', admin_messages=messages)

    @app.route('/admin')
    def admin_dashboard():
        if 'user_id' not in session:
            flash("Tafadhali ingia kwanza kwenye akaunti yako!")
            return redirect(url_for('login'))

        conn = get_db_connection()
        try:
            user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()

            if not user or user['email'] != 'matondomaduhu135@gmail.com':
                flash("Huruhusiwi kuingia kwenye ukurasa huu wa Admin!")
                return redirect(url_for('home'))

            users_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            moderation_pending_count = 0
            posts_count = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
            users_raw = conn.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
            posts = conn.execute('''
                SELECT posts.*, users.username, users.profile_pic FROM posts
                JOIN users ON posts.user_id = users.id
                ORDER BY posts.id DESC
            ''').fetchall()

            badge_requests = conn.execute('''
                SELECT badge_requests.*, users.username, users.profile_pic
                FROM badge_requests
                JOIN users ON badge_requests.user_id = users.id
                WHERE badge_requests.status = 'pending'
                ORDER BY badge_requests.id DESC
            ''').fetchall()

            approved_badge_requests = conn.execute('''
                SELECT badge_requests.*, users.username, users.profile_pic
                FROM badge_requests
                JOIN users ON badge_requests.user_id = users.id
                WHERE badge_requests.status = 'approved'
                ORDER BY badge_requests.id DESC
                LIMIT 50
            ''').fetchall()

            admin_messages = conn.execute('SELECT * FROM admin_messages ORDER BY created_at DESC').fetchall()

            # ====================== DATA DELETION REQUESTS ======================

            deletion_requests = conn.execute('''
                SELECT
                    data_deletion_requests.*,
                    users.username,
                    users.is_verified
                FROM data_deletion_requests
                LEFT JOIN users
                    ON data_deletion_requests.user_id = users.id
                WHERE data_deletion_requests.status = 'pending'
                ORDER BY data_deletion_requests.created_at ASC
            ''').fetchall()

            post_reports = conn.execute('''
                SELECT r.id, r.post_id, r.reason, r.created_at,
                       u.username AS reporter_username,
                       p.content AS post_content,
                       pu.username AS post_owner
                FROM reports r
                JOIN users u ON u.id = r.reporter_id
                LEFT JOIN posts p ON p.id = r.post_id
                LEFT JOIN users pu ON pu.id = p.user_id
                ORDER BY r.id DESC
                LIMIT 100
            ''').fetchall()
            reports_count = conn.execute('SELECT COUNT(*) FROM reports').fetchone()[0]
            try:
                moderation_pending_count = conn.execute(
                    "SELECT COUNT(*) FROM moderation_flags WHERE status = 'pending'"
                ).fetchone()[0]
            except Exception:
                moderation_pending_count = 0

            # Enrich users: join duration + auth provider
            now = datetime.now(ZoneInfo("Africa/Dar_es_Salaam"))
            users = []
            for u in users_raw:
                d = dict(u)
                created = d.get('created_at')
                joined_label = '—'
                if created:
                    try:
                        if isinstance(created, str):
                            cdt = datetime.strptime(str(created)[:19], '%Y-%m-%d %H:%M:%S')
                        else:
                            cdt = created
                        delta = now.replace(tzinfo=None) - cdt
                        days = max(0, delta.days)
                        if days < 1:
                            hours = max(0, int(delta.total_seconds() // 3600))
                            joined_label = f'{hours}h kwenye app' if hours else 'Chini ya saa 1'
                        elif days < 7:
                            joined_label = f'{days}d kwenye app'
                        elif days < 30:
                            joined_label = f'{days // 7}w kwenye app'
                        elif days < 365:
                            joined_label = f'{days // 30}mo kwenye app'
                        else:
                            joined_label = f'{days // 365}y kwenye app'
                    except Exception:
                        joined_label = str(created)[:16]
                d['joined_label'] = joined_label
                d['joined_date'] = str(created)[:16] if created else '—'
                provider = (d.get('auth_provider') or '').lower()
                if not provider or provider == 'local':
                    if (d.get('bio') or '') == 'Joined with Google' or str(d.get('profile_pic') or '').startswith('http'):
                        provider = 'google'
                    else:
                        provider = 'local'
                d['auth_provider'] = provider
                users.append(d)

        finally:
            conn.close()

        return render_template(
            'admin/admin.html',
            users_count=users_count,
            posts_count=posts_count,
            users=users,
            posts=posts,
            pending_badge_requests=badge_requests,
            approved_badge_requests=approved_badge_requests,
            admin_messages=admin_messages,
            deletion_requests=deletion_requests,
            post_reports=post_reports,
            reports_count=reports_count,
            moderation_pending_count=moderation_pending_count
        )

    # ====================== PAGE ROUTES (SPLIT TEMPLATES) ======================

    @app.route('/admin/badges')
    def admin_badges():
        conn, err = require_admin()
        if err:
            return err
        try:
            badge_requests = conn.execute('''
                SELECT badge_requests.*, users.username, users.profile_pic
                FROM badge_requests
                JOIN users ON badge_requests.user_id = users.id
                WHERE badge_requests.status = 'pending'
                ORDER BY badge_requests.id DESC
            ''').fetchall()
            approved_badge_requests = conn.execute('''
                SELECT badge_requests.*, users.username, users.profile_pic
                FROM badge_requests
                JOIN users ON badge_requests.user_id = users.id
                WHERE badge_requests.status = 'approved'
                ORDER BY badge_requests.id DESC
                LIMIT 50
            ''').fetchall()
        finally:
            conn.close()
        return render_template(
            'admin/admin_badges.html',
            pending_badge_requests=badge_requests,
            approved_badge_requests=approved_badge_requests
        )

    @app.route('/admin/users')
    def admin_users():
        conn, err = require_admin()
        if err:
            return err
        try:
            users_raw = conn.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
            now = datetime.now(ZoneInfo("Africa/Dar_es_Salaam"))
            users = []
            for u in users_raw:
                d = dict(u)
                created = d.get('created_at')
                joined_label = '—'
                if created:
                    try:
                        if isinstance(created, str):
                            cdt = datetime.strptime(str(created)[:19], '%Y-%m-%d %H:%M:%S')
                        else:
                            cdt = created
                        delta = now.replace(tzinfo=None) - cdt
                        days = max(0, delta.days)
                        if days < 1:
                            hours = max(0, int(delta.total_seconds() // 3600))
                            joined_label = f'{hours}h kwenye app' if hours else 'Chini ya saa 1'
                        elif days < 7:
                            joined_label = f'{days}d kwenye app'
                        elif days < 30:
                            joined_label = f'{days // 7}w kwenye app'
                        elif days < 365:
                            joined_label = f'{days // 30}mo kwenye app'
                        else:
                            joined_label = f'{days // 365}y kwenye app'
                    except Exception:
                        joined_label = str(created)[:16]
                d['joined_label'] = joined_label
                d['joined_date'] = str(created)[:16] if created else '—'
                provider = (d.get('auth_provider') or '').lower()
                if not provider or provider == 'local':
                    if (d.get('bio') or '') == 'Joined with Google' or str(d.get('profile_pic') or '').startswith('http'):
                        provider = 'google'
                    else:
                        provider = 'local'
                d['auth_provider'] = provider
                users.append(d)
        finally:
            conn.close()
        return render_template('admin/admin_users.html', users=users)

    @app.route('/admin/posts')
    def admin_posts():
        conn, err = require_admin()
        if err:
            return err
        try:
            posts = conn.execute('''
                SELECT posts.*, users.username, users.profile_pic FROM posts
                JOIN users ON posts.user_id = users.id
                ORDER BY posts.id DESC
            ''').fetchall()
        finally:
            conn.close()
        return render_template('admin/admin_posts.html', posts=posts)

    @app.route('/admin/broadcast', methods=['GET'])
    def admin_broadcast_page():
        conn, err = require_admin()
        if err:
            return err
        conn.close()
        return render_template('admin/admin_broadcast.html')

    @app.route('/admin/deletion')
    def admin_deletion():
        conn, err = require_admin()
        if err:
            return err
        try:
            deletion_requests = conn.execute('''
                SELECT
                    data_deletion_requests.*,
                    users.username,
                    users.is_verified
                FROM data_deletion_requests
                LEFT JOIN users
                    ON data_deletion_requests.user_id = users.id
                WHERE data_deletion_requests.status = 'pending'
                ORDER BY data_deletion_requests.created_at ASC
            ''').fetchall()
        finally:
            conn.close()
        return render_template('admin/admin_deletion.html', deletion_requests=deletion_requests)

    # ====================== ACTION ROUTES ======================

    @app.route('/admin/reply/<int:message_id>', methods=['POST'])
    def reply_message(message_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_check = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_check or admin_check['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            return redirect(url_for('home'))

        reply = request.form.get('reply')
        cursor = conn.cursor()
        cursor.execute("UPDATE admin_messages SET admin_reply = ?, status = 'replied' WHERE id = ?", (reply, message_id))
        conn.commit()
        conn.close()

        flash("Jibu limetumwa kwa mafanikio!")
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/search_live', methods=['GET'])
    def admin_search_live():
        if 'user_id' not in session:
            return jsonify({'success': False}), 401

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            return jsonify({'success': False}), 403

        query = request.args.get('q', '').strip()

        users = conn.execute(
            "SELECT id, username, email, is_verified, is_blocked, warning_message FROM users WHERE username LIKE ? OR email LIKE ? LIMIT 20",
            (f'%{query}%', f'%{query}%')
        ).fetchall()

        posts = conn.execute(
            """SELECT posts.id, posts.content, posts.created_at, users.username
               FROM posts JOIN users ON posts.user_id = users.id
               WHERE posts.content LIKE ? OR users.username LIKE ? LIMIT 20""",
            (f'%{query}%', f'%{query}%')
        ).fetchall()

        conn.close()

        return jsonify({
            'success': True,
            'users': [dict(u) for u in users],
            'posts': [dict(p) for p in posts]
        })

    @app.route('/admin/verify/<int:user_id>', methods=['POST'])
    def admin_verify(user_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash("Huruhusiwi kufanya kitendo hiki!")
            return redirect(url_for('home'))

        target = conn.execute('SELECT is_verified FROM users WHERE id = ?', (user_id,)).fetchone()
        if target:
            new_status = 0 if target['is_verified'] == 1 else 1
            conn.execute('UPDATE users SET is_verified = ? WHERE id = ?', (new_status, user_id))
            conn.commit()
            if new_status == 1:
                try:
                    create_notification(
                        user_id,
                        session['user_id'],
                        'badge',
                        'Hongera! Umepewa Verified Badge na Admin ✓',
                        url='/notifications'
                    )
                except Exception:
                    pass
                flash("Badge imetolewa. Notification imetumwa kwa mtumiaji!")
            else:
                flash("Badge imeondolewa.")

        conn.close()
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/block/<int:user_id>', methods=['POST'])
    def admin_block(user_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash("Huruhusiwi kufanya kitendo hiki!")
            return redirect(url_for('home'))

        target = conn.execute('SELECT is_blocked FROM users WHERE id = ?', (user_id,)).fetchone()
        if target:
            new_block_status = 0 if target['is_blocked'] == 1 else 1
            conn.execute('UPDATE users SET is_blocked = ? WHERE id = ?', (new_block_status, user_id))
            conn.commit()
            flash("Hali ya uzuiaji (Block/Unblock) imebadilishwa!")

        conn.close()
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/delete_post/<int:post_id>', methods=['POST'])
    def admin_delete_post(post_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash("Huruhusiwi kufanya kitendo hiki!")
            return redirect(url_for('home'))

        conn.execute('DELETE FROM likes WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM saved_posts WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))
        conn.commit()
        conn.close()

        flash("Chapisho (Post) limefutwa kikamilifu!")
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/warn/<int:user_id>', methods=['POST'])
    def admin_warn(user_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash("Huruhusiwi kufanya kitendo hiki!")
            return redirect(url_for('home'))

        warning_text = request.form.get('warning_text', '').strip()
        conn.execute('UPDATE users SET warning_message = ? WHERE id = ?', (warning_text, user_id))
        conn.commit()
        conn.close()

        flash("Onyo limetumwa kwa mtumiaji!")
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/broadcast', methods=['POST'])
    def admin_broadcast():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        admin_user = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin_user or admin_user['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash("Huruhusiwi kufanya kitendo hiki!")
            return redirect(url_for('home'))

        announcement = request.form.get('announcement', '').strip()
        image_path = None
        f = request.files.get('broadcast_image')
        if f and f.filename and allowed_file(f.filename):
            ext = f.filename.rsplit('.', 1)[-1].lower()
            if ext in IMAGE_EXTS:
                fname = secure_filename(f.filename) or 'broadcast.jpg'
                stamp = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y%m%d%H%M%S')
                unique = f'broadcast_{stamp}_{secrets.token_hex(3)}_{fname}'
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], unique))
                image_path = unique

        if not announcement and not image_path:
            conn.close()
            flash('Andika ujumbe au weka picha.')
            return redirect(url_for('admin_dashboard'))

        msg = announcement or 'Taarifa mpya kutoka Admin'
        if image_path:
            msg = msg + f'\n[Picha: /static/uploads/{image_path}]'

        users = conn.execute('SELECT id FROM users WHERE id != ?', (session['user_id'],)).fetchall()
        for u in users:
            create_notification(
                u['id'], session['user_id'], 'broadcast',
                msg,
                url='/notifications'
            )
        flash(f'Taarifa imetumwa kwa watumiaji {len(users)}!')

        conn.close()
        return redirect(url_for('admin_dashboard'))

    # ====================== ROUTE YA BADGE REQUEST ======================

    @app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
    def delete_user(user_id):
        """Futa akaunti na data zake zote kabisa (AJAX)."""
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        conn = get_db_connection()

        try:
            admin = conn.execute(
                'SELECT email FROM users WHERE id = ?',
                (session['user_id'],)
            ).fetchone()
            if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
                conn.close()
                return jsonify({'success': False, 'message': 'Huruhusiwi kufanya kitendo hiki.'}), 403

            user = conn.execute(
                'SELECT * FROM users WHERE id = ?', (user_id,)
            ).fetchone()
            if not user:
                conn.close()
                return jsonify({'success': False, 'message': 'Mtumiaji hajapatikana.'}), 404

            user_email = user['email'] if 'email' in user.keys() else None
            files_to_remove = []

            def add_file(path):
                if not path:
                    return
                path = str(path)
                if path.startswith('http://') or path.startswith('https://'):
                    return
                if os.path.isabs(path):
                    full = path
                else:
                    candidates = [
                        os.path.join(BASE_DIR, path),
                        os.path.join(app.config['UPLOAD_FOLDER'], path),
                        os.path.join(UPLOAD_BADGES_FOLDER, path),
                        os.path.join(BASE_DIR, 'static', 'uploads', path),
                        os.path.join(BASE_DIR, 'uploads', 'badges', path),
                    ]
                    full = None
                    for c in candidates:
                        if os.path.isfile(c):
                            full = c
                            break
                    if not full:
                        full = os.path.join(BASE_DIR, path)
                if full and full not in files_to_remove:
                    files_to_remove.append(full)

            try:
                add_file(user['profile_pic'] if 'profile_pic' in user.keys() else None)
            except Exception:
                pass
            try:
                add_file(user['cover_photo'] if 'cover_photo' in user.keys() else None)
            except Exception:
                pass
            try:
                for row in conn.execute(
                    'SELECT id_document_path FROM badge_requests WHERE user_id = ? AND id_document_path IS NOT NULL',
                    (user_id,)
                ).fetchall():
                    add_file(row['id_document_path'])
            except Exception:
                pass
            try:
                for row in conn.execute(
                    'SELECT file_path FROM posts WHERE user_id = ? AND file_path IS NOT NULL',
                    (user_id,)
                ).fetchall():
                    add_file(row['file_path'])
            except Exception:
                pass
            try:
                for row in conn.execute(
                    'SELECT file_path, music_path FROM statuses WHERE user_id = ?',
                    (user_id,)
                ).fetchall():
                    add_file(row['file_path'] if 'file_path' in row.keys() else None)
                    try:
                        add_file(row['music_path'] if 'music_path' in row.keys() else None)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                for row in conn.execute(
                    'SELECT file_path FROM community_posts WHERE user_id = ? AND file_path IS NOT NULL',
                    (user_id,)
                ).fetchall():
                    add_file(row['file_path'])
            except Exception:
                pass
            try:
                for row in conn.execute(
                    'SELECT file_path FROM private_messages WHERE (sender_id = ? OR receiver_id = ?) AND file_path IS NOT NULL',
                    (user_id, user_id)
                ).fetchall():
                    add_file(row['file_path'])
            except Exception:
                pass

            try:
                conn.execute('PRAGMA foreign_keys = OFF')
            except Exception:
                pass

            def safe_exec(sql, params=()):
                try:
                    conn.execute(sql, params)
                except Exception as e:
                    print('[delete_user] safe_exec skip:', e, '|', sql[:60])

            try:
                status_ids = [r[0] for r in conn.execute(
                    'SELECT id FROM statuses WHERE user_id = ?', (user_id,)
                ).fetchall()]
                for sid in status_ids:
                    safe_exec('DELETE FROM status_reactions WHERE status_id = ?', (sid,))
                    safe_exec('DELETE FROM status_views WHERE status_id = ?', (sid,))
                safe_exec('DELETE FROM status_reactions WHERE user_id = ?', (user_id,))
                safe_exec('DELETE FROM status_views WHERE viewer_id = ?', (user_id,))
                safe_exec('DELETE FROM status_views WHERE user_id = ?', (user_id,))
                safe_exec('DELETE FROM statuses WHERE user_id = ?', (user_id,))
            except Exception as e:
                print('[delete_user] statuses:', e)

            try:
                post_ids = [r[0] for r in conn.execute(
                    'SELECT id FROM posts WHERE user_id = ?', (user_id,)
                ).fetchall()]
                for pid in post_ids:
                    cids = [r[0] for r in conn.execute(
                        'SELECT id FROM comments WHERE post_id = ?', (pid,)
                    ).fetchall()]
                    for cid in cids:
                        safe_exec('DELETE FROM comment_likes WHERE comment_id = ?', (cid,))
                    safe_exec('DELETE FROM comments WHERE post_id = ?', (pid,))
                    safe_exec('DELETE FROM likes WHERE post_id = ?', (pid,))
                    safe_exec('DELETE FROM saved_posts WHERE post_id = ?', (pid,))
                    safe_exec('DELETE FROM reposts WHERE original_post_id = ?', (pid,))
                    safe_exec('DELETE FROM reports WHERE post_id = ?', (pid,))
                    safe_exec('DELETE FROM post_views WHERE post_id = ?', (pid,))
                    safe_exec('DELETE FROM notifications WHERE post_id = ?', (pid,))
                safe_exec('DELETE FROM posts WHERE user_id = ?', (user_id,))
            except Exception as e:
                print('[delete_user] posts:', e)

            try:
                cids = [r[0] for r in conn.execute(
                    'SELECT id FROM comments WHERE user_id = ?', (user_id,)
                ).fetchall()]
                for cid in cids:
                    safe_exec('DELETE FROM comment_likes WHERE comment_id = ?', (cid,))
                safe_exec('DELETE FROM comments WHERE user_id = ?', (user_id,))
            except Exception as e:
                print('[delete_user] comments:', e)

            safe_exec('DELETE FROM likes WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM comment_likes WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM saved_posts WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM reposts WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM reports WHERE reporter_id = ?', (user_id,))
            safe_exec('DELETE FROM post_views WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM notifications WHERE user_id = ? OR sender_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM history WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM follows WHERE follower_id = ? OR following_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM blocks WHERE blocker_id = ? OR blocked_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM badge_requests WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM private_messages WHERE sender_id = ? OR receiver_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM pinned_chats WHERE user_id = ? OR other_user_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM call_signals WHERE from_user_id = ? OR to_user_id = ?', (user_id, user_id))
            safe_exec('DELETE FROM push_subscriptions WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM community_posts WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM group_messages WHERE user_id = ?', (user_id,))
            safe_exec('DELETE FROM group_members WHERE user_id = ?', (user_id,))

            try:
                gids = [r[0] for r in conn.execute(
                    'SELECT id FROM community_groups WHERE creator_id = ?', (user_id,)
                ).fetchall()]
                for gid in gids:
                    safe_exec('DELETE FROM group_messages WHERE group_id = ?', (gid,))
                    safe_exec('DELETE FROM group_members WHERE group_id = ?', (gid,))
                safe_exec('DELETE FROM community_groups WHERE creator_id = ?', (user_id,))
            except Exception as e:
                print('[delete_user] groups:', e)

            if user_email:
                safe_exec('DELETE FROM otps WHERE email = ?', (user_email,))
                safe_exec('DELETE FROM pending_registrations WHERE email = ?', (user_email,))
                safe_exec(
                    'DELETE FROM data_deletion_requests WHERE user_id = ? OR email = ?',
                    (user_id, user_email)
                )
            else:
                safe_exec('DELETE FROM data_deletion_requests WHERE user_id = ?', (user_id,))

            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()

            try:
                conn.execute('PRAGMA foreign_keys = ON')
            except Exception:
                pass

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print('========= ERROR DELETING USER =========')
            print(e)
            import traceback
            traceback.print_exc()
            try:
                conn.close()
            except Exception:
                pass
            return jsonify({
                'success': False,
                'message': 'Kuna tatizo limetokea wakati wa kufuta akaunti: ' + str(e)[:120]
            }), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for file_path in files_to_remove:
            try:
                if file_path and os.path.isfile(file_path):
                    os.remove(file_path)
                    print('[delete_user] deleted file:', file_path)
            except Exception as e:
                print('[delete_user] file deletion error:', file_path, e)

        return jsonify({
            'success': True,
            'message': 'Akaunti na data zake zimefutwa kabisa.'
        })


    # ====================== APPROVE DATA DELETION REQUEST ======================

    @app.route('/admin/approve_deletion/<int:request_id>', methods=['POST'])
    def approve_deletion(request_id):
        """Kubali ombi la kufuta akaunti: futa user + request kabisa."""
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        conn = get_db_connection()

        try:
            admin = conn.execute(
                'SELECT email FROM users WHERE id = ?',
                (session['user_id'],)
            ).fetchone()
            if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
                conn.close()
                return jsonify({'success': False, 'message': 'Huruhusiwi kufanya kitendo hiki.'}), 403

            deletion_request = conn.execute(
                "SELECT id, user_id, email, status FROM data_deletion_requests WHERE id = ? AND status = 'pending'",
                (request_id,)
            ).fetchone()

            if not deletion_request:
                conn.close()
                return jsonify({
                    'success': False,
                    'message': 'Ombi hili halijapatikana au tayari limeshughulikiwa.'
                }), 404

            user_id = deletion_request['user_id']
            req_email = deletion_request['email']

            # Mark processing so it leaves the pending list immediately
            conn.execute(
                "UPDATE data_deletion_requests SET status = 'processing', processed_at = ? WHERE id = ?",
                (now_tz(), request_id)
            )
            conn.commit()
            conn.close()

            if not user_id:
                conn2 = get_db_connection()
                conn2.execute('DELETE FROM data_deletion_requests WHERE id = ?', (request_id,))
                if req_email:
                    conn2.execute('DELETE FROM data_deletion_requests WHERE email = ?', (req_email,))
                conn2.commit()
                conn2.close()
                return jsonify({
                    'success': True,
                    'message': 'Ombi limeshughulikiwa (hakuna akaunti iliyounganishwa).'
                })

            result = delete_user(user_id)

            # Extra safety cleanup of this request
            try:
                conn3 = get_db_connection()
                conn3.execute('DELETE FROM data_deletion_requests WHERE id = ?', (request_id,))
                if req_email:
                    conn3.execute(
                        "DELETE FROM data_deletion_requests WHERE email = ? OR status IN ('processing','pending')",
                        (req_email,)
                    )
                conn3.execute(
                    "DELETE FROM data_deletion_requests WHERE user_id = ? OR (email = ? AND status != 'completed')",
                    (user_id, req_email)
                )
                conn3.commit()
                conn3.close()
            except Exception as e:
                print('[approve_deletion] cleanup request:', e)

            return result

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            print('========= ERROR APPROVING DELETION REQUEST =========')
            print(e)
            import traceback
            traceback.print_exc()
            return jsonify({
                'success': False,
                'message': 'Kuna tatizo limetokea wakati wa kushughulikia ombi.'
            }), 500


    @app.route('/admin/badge_file/<path:filename>')
    def admin_badge_file(filename):
        """Serve badge ID document for admin only."""
        if 'user_id' not in session:
            return redirect(url_for('login'))
        conn = get_db_connection()
        try:
            admin_check = conn.execute(
                'SELECT email FROM users WHERE id = ?', (session['user_id'],)
            ).fetchone()
            if not admin_check or admin_check['email'] != 'matondomaduhu135@gmail.com':
                conn.close()
                flash('Huruhusiwi.')
                return redirect(url_for('home'))
        finally:
            try:
                conn.close()
            except Exception:
                pass
        safe = os.path.basename(str(filename).replace('\\', '/'))
        folder = app.config.get('UPLOAD_BADGES_FOLDER') or UPLOAD_BADGES_FOLDER
        return send_from_directory(folder, safe)

    @app.route('/admin/approve_badge/<int:user_id>', methods=['POST'])
    def approve_badge(user_id):
        if 'user_id' not in session:
            flash("Tafadhali ingia kwanza!")
            return redirect(url_for('login'))

        conn = get_db_connection()
        try:
            admin_check = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            if not admin_check or admin_check['email'] != 'matondomaduhu135@gmail.com':
                flash("Huruhusiwi kufanya kitendo hiki!")
                return redirect(url_for('home'))

            conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            conn.execute("UPDATE badge_requests SET status = 'approved' WHERE user_id = ?", (user_id,))
            conn.commit()

            try:
                create_notification(
                    user_id,
                    session['user_id'],
                    'badge',
                    'Hongera! Ombi lako la Verified Badge limekubaliwa. Sasa una badge ✓',
                    url='/notifications'
                )
            except Exception as ne:
                print('badge notify error:', ne)

            flash("Mtumiaji amethibitishwa kikamilifu! Notification imetumwa.")
        except Exception as e:
            print("Error approving badge:", e)
            flash("Kuna tatizo limetokea wakati wa kuthibisha badge.")
        finally:
            conn.close()

        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/reject_badge/<int:request_id>', methods=['POST'])
    def reject_badge(request_id):
        if 'user_id' not in session:
            flash("Tafadhali ingia kwanza!")
            return redirect(url_for('login'))

        conn = get_db_connection()
        try:
            admin_check = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            if not admin_check or admin_check['email'] != 'matondomaduhu135@gmail.com':
                flash("Huruhusiwi kufanya kitendo hiki!")
                return redirect(url_for('home'))

            row = conn.execute(
                'SELECT user_id FROM badge_requests WHERE id = ?', (request_id,)
            ).fetchone()
            conn.execute("UPDATE badge_requests SET status = 'rejected' WHERE id = ?", (request_id,))
            conn.commit()
            if row:
                try:
                    create_notification(
                        row['user_id'],
                        session['user_id'],
                        'badge',
                        'Ombi lako la Verified Badge limekataliwa. Unaweza kuomba tena baadaye.',
                        url='/notifications'
                    )
                except Exception:
                    pass

            flash("Ombi la badge limekataliwa. Notification imetumwa.")
        except Exception as e:
            print("Error rejecting badge:", e)
            flash("Kuna tatizo limetokea.")
        finally:
            conn.close()

        return redirect(url_for('admin_dashboard'))



    @app.route('/admin/reports')
    @login_required
    def admin_reports():
        conn = get_db_connection()
        admin = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash('Huruhusiwi')
            return redirect(url_for('home'))
        status_filter = (request.args.get('status') or 'pending').strip()
        base = (
            "SELECT r.*, p.content AS post_content, p.file_path, p.media_type, "
            "u.username AS reporter_name, ou.username AS owner_name, p.user_id AS owner_id "
            "FROM reports r JOIN posts p ON p.id = r.post_id "
            "JOIN users u ON u.id = r.reporter_id JOIN users ou ON ou.id = p.user_id "
        )
        if status_filter == 'all':
            rows = conn.execute(base + "ORDER BY r.id DESC LIMIT 100").fetchall()
        else:
            rows = conn.execute(
                base + "WHERE COALESCE(r.status, 'pending') = ? ORDER BY r.id DESC LIMIT 100",
                (status_filter,)
            ).fetchall()
        reports = [dict(r) for r in rows]
        for r in reports:
            r['created_at'] = str(r.get('created_at') or '')
            r['post_content'] = (r.get('post_content') or '')[:200]
        conn.close()
        try:
            return render_template('admin/admin_reports.html', reports=reports, status_filter=status_filter)
        except Exception:
            return jsonify({'success': True, 'reports': reports, 'status_filter': status_filter})


    @app.route('/admin/reports/<int:report_id>', methods=['POST'])
    @login_required
    def admin_report_action(report_id):
        conn = get_db_connection()
        admin = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            return jsonify({'success': False, 'message': 'Forbidden'}), 403
        data = request.get_json(silent=True) or {}
        action = (data.get('action') or request.form.get('action') or '').strip()
        note = (data.get('note') or request.form.get('note') or '').strip()
        row = conn.execute('SELECT * FROM reports WHERE id = ?', (report_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'message': 'Report haipatikani'}), 404
        if action == 'resolve':
            new_status = 'resolved'
        elif action == 'dismiss':
            new_status = 'dismissed'
        elif action == 'review':
            new_status = 'reviewed'
        elif action == 'delete_post':
            new_status = 'resolved'
            pid = row['post_id']
            try:
                conn.execute('DELETE FROM likes WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM comments WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM saved_posts WHERE post_id = ?', (pid,))
                try:
                    conn.execute('DELETE FROM post_hashtags WHERE post_id = ?', (pid,))
                except Exception:
                    pass
                conn.execute('DELETE FROM posts WHERE id = ?', (pid,))
            except Exception as e:
                print('[admin_report] delete post:', e)
        else:
            conn.close()
            return jsonify({'success': False, 'message': 'action si sahihi'}), 400
        try:
            conn.execute(
                'UPDATE reports SET status = ?, admin_note = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?',
                (new_status, note or None, now_tz(), session['user_id'], report_id)
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'status': new_status})


    @app.route('/admin/moderation')
    @login_required
    def admin_moderation():
        conn = get_db_connection()
        admin = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            flash('Huruhusiwi')
            return redirect(url_for('home'))
        status_filter = (request.args.get('status') or 'pending').strip()
        base = (
            "SELECT mf.*, p.content AS post_content, p.file_path, p.media_type, "
            "p.moderation_status, ou.username AS owner_name, ou.warning_count, "
            "ou.restricted_until, ou.is_blocked AS owner_blocked "
            "FROM moderation_flags mf "
            "JOIN posts p ON p.id = mf.post_id "
            "JOIN users ou ON ou.id = mf.user_id "
        )
        if status_filter == 'all':
            rows = conn.execute(base + "ORDER BY mf.id DESC LIMIT 100").fetchall()
        else:
            rows = conn.execute(
                base + "WHERE mf.status = ? ORDER BY mf.id DESC LIMIT 100",
                (status_filter,)
            ).fetchall()
        flags = [dict(r) for r in rows]
        for f in flags:
            f['created_at'] = str(f.get('created_at') or '')
            f['post_content'] = (f.get('post_content') or '')[:200]
        conn.close()
        try:
            return render_template('admin/admin_moderation.html', flags=flags, status_filter=status_filter)
        except Exception:
            return jsonify({'success': True, 'flags': flags, 'status_filter': status_filter})


    @app.route('/admin/moderation/<int:flag_id>', methods=['POST'])
    @login_required
    def admin_moderation_action(flag_id):
        conn = get_db_connection()
        admin = conn.execute('SELECT email FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not admin or admin['email'] != 'matondomaduhu135@gmail.com':
            conn.close()
            return jsonify({'success': False, 'message': 'Forbidden'}), 403
        data = request.get_json(silent=True) or {}
        action = (data.get('action') or request.form.get('action') or '').strip()
        note = (data.get('note') or request.form.get('note') or '').strip()
        row = conn.execute('SELECT * FROM moderation_flags WHERE id = ?', (flag_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'message': 'Flag haipatikani'}), 404

        pid = row['post_id']
        owner_id = row['user_id']
        new_status = 'resolved'

        admin_id = session['user_id']
        notif_msg = None

        if action == 'approve':
            conn.execute("UPDATE posts SET moderation_status = 'approved' WHERE id = ?", (pid,))
            notif_msg = (
                '✅ Admin ameidhinisha chapisho lako. Sasa linaonekana hadharani.'
            )

        elif action == 'reject':
            conn.execute("UPDATE posts SET moderation_status = 'rejected' WHERE id = ?", (pid,))
            register_nsfw_violation(conn, pid, owner_id, row['nsfw_score'] or 0, [])
            # register_nsfw_violation tayari inaweka notification ya warning
            notif_msg = None

        elif action == 'delete_post':
            try:
                conn.execute('DELETE FROM likes WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM comments WHERE post_id = ?', (pid,))
                conn.execute('DELETE FROM saved_posts WHERE post_id = ?', (pid,))
                try:
                    conn.execute('DELETE FROM post_hashtags WHERE post_id = ?', (pid,))
                except Exception:
                    pass
                try:
                    conn.execute('DELETE FROM moderation_flags WHERE post_id = ?', (pid,))
                except Exception:
                    pass
                conn.execute('DELETE FROM posts WHERE id = ?', (pid,))
            except Exception as e:
                print('[admin_moderation] delete post:', e)
            reason = (note or '').strip()
            notif_msg = (
                '🗑️ Admin amefuta chapisho lako. '
                + (f'Sababu: {reason}' if reason else
                   'Sababu: kukiuka sheria za maudhui (uchi/ngono au vingine).')
            )

        elif action == 'block_user':
            conn.execute(
                'UPDATE users SET is_blocked = 1, restricted_until = NULL WHERE id = ?',
                (owner_id,)
            )
            notif_msg = (
                '🚫 Akaunti yako IMEZUIWA na Admin kwa sababu ya kukiuka sheria za maudhui. '
                'Huwezi kuchapisha wala kutumia baadhi ya huduma.'
            )

        elif action == 'unblock_restriction':
            conn.execute(
                'UPDATE users SET restricted_until = NULL WHERE id = ?', (owner_id,)
            )
            notif_msg = (
                '✅ Admin ameondoa restriction yako ya muda. Unaweza kuchapisha tena — '
                'heshimu sheria za maudhui.'
            )

        elif action == 'dismiss':
            new_status = 'dismissed'
            notif_msg = None

        else:
            conn.close()
            return jsonify({'success': False, 'message': 'action si sahihi'}), 400

        # Usifute flag row kama post imefutwa - status update inaweza kushindwa
        try:
            conn.execute(
                'UPDATE moderation_flags SET status = ?, admin_note = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?',
                (new_status, note or None, now_tz(), admin_id, flag_id)
            )
        except Exception as e:
            print('[admin_moderation] flag update:', e)

        if notif_msg and owner_id and owner_id != admin_id:
            try:
                conn.execute(
                    """INSERT INTO notifications
                       (user_id, sender_id, type, post_id, message, is_read, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?)""",
                    (owner_id, admin_id, 'moderation', pid if action != 'delete_post' else None,
                     notif_msg, now_tz())
                )
            except Exception as e:
                print('[admin_moderation] notification error:', e)
                try:
                    create_notification(owner_id, admin_id, 'moderation', message=notif_msg, post_id=None)
                except Exception as e2:
                    print('[admin_moderation] create_notification error:', e2)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'status': new_status})




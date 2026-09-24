"""
kijiji.py — Kijiji media feed and related AJAX actions
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
import requests

from db import get_db_connection, UPLOAD_FOLDER, BASE_DIR
from helpers import (
    login_required, now_tz, allowed_file, notify_user, create_notification,
    publish_due_scheduled_posts, muted_ids_for, save_post_hashtags,
    enrich_posts_with_linkup, get_active_linkup_id, resolve_active_linkup,
    set_active_linkup_id, user_has_kijiji_mode, get_user_linkups, get_linkup_by_id,
    get_linkup_by_username, linkup_username_taken, is_following_linkup,
    count_linkup_followers, sanitize_username, IMAGE_EXTS, VIDEO_EXTS,
    ALLOWED_EXTENSIONS, MAX_LINKUPS_PER_USER, LINKUP_CATEGORIES,
    VAPID_PUBLIC_KEY, send_web_push
)


def register_kijiji_routes(app):
    # ====================== KIJIJI (MEDIA FEED) ======================

    @app.route('/kijiji')
    def kijiji():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        current_user_id = session.get('user_id')

        # ============ CHUKUA POSTS (Video + Picha) ============
        if current_user_id:
            # Mtu aliyelogin → ondoa watu aliozuia
            cursor.execute('''
                SELECT
                    p.id,
                    p.content AS caption,
                    p.file_path AS media_path,
                    p.media_type,
                    p.shares,
                    p.created_at,
                    u.id AS user_id,
                    u.username,
                    u.full_name,
                    u.profile_pic,
                    u.is_verified
                FROM posts p
                JOIN users u ON p.user_id = u.id
                WHERE p.file_path IS NOT NULL
                  AND p.media_type IN ('video', 'image')
                  AND COALESCE(p.moderation_status, 'approved') = 'approved'
                  AND (u.is_blocked = 0 OR u.is_blocked IS NULL)
                  AND u.id NOT IN (
                      SELECT blocked_id FROM blocks WHERE blocker_id = ?
                  )
                ORDER BY p.created_at DESC
                LIMIT 100
            ''', (current_user_id,))
        else:
            # Mtu hajalogin
            cursor.execute('''
                SELECT
                    p.id,
                    p.content AS caption,
                    p.file_path AS media_path,
                    p.media_type,
                    p.shares,
                    p.created_at,
                    u.id AS user_id,
                    u.username,
                    u.full_name,
                    u.profile_pic,
                    u.is_verified
                FROM posts p
                JOIN users u ON p.user_id = u.id
                WHERE p.file_path IS NOT NULL
                  AND p.media_type IN ('video', 'image')
                  AND COALESCE(p.moderation_status, 'approved') = 'approved'
                  AND (u.is_blocked = 0 OR u.is_blocked IS NULL)
                ORDER BY p.created_at DESC
                LIMIT 100
            ''')

        kijiji_list = cursor.fetchall()

        # ============ ANDAA DATA KWA KILA POST ============
        posts_data = []

        for item in kijiji_list:
            post_id = item['id']

            # Likes count
            cursor.execute('SELECT COUNT(*) FROM likes WHERE post_id = ?', (post_id,))
            likes_count = cursor.fetchone()[0]

            # Comments count
            cursor.execute('''
                SELECT COUNT(*) FROM comments
                WHERE post_id = ? AND (is_hidden = 0 OR is_hidden IS NULL)
            ''', (post_id,))
            comments_count = cursor.fetchone()[0]

            # User already liked?
            user_liked = False
            if current_user_id:
                cursor.execute(
                    'SELECT 1 FROM likes WHERE user_id = ? AND post_id = ?',
                    (current_user_id, post_id)
                )
                user_liked = cursor.fetchone() is not None

            # User already saved?
            user_saved = False
            if current_user_id:
                cursor.execute(
                    'SELECT 1 FROM saved_posts WHERE user_id = ? AND post_id = ?',
                    (current_user_id, post_id)
                )
                user_saved = cursor.fetchone() is not None

            # User already reposted? (optional)
            user_reposted = False
            if current_user_id:
                cursor.execute(
                    'SELECT 1 FROM reposts WHERE user_id = ? AND original_post_id = ?',
                    (current_user_id, post_id)
                )
                user_reposted = cursor.fetchone() is not None

            posts_data.append({
                'id': post_id,
                'caption': item['caption'],
                'media_path': item['media_path'],
                'media_type': item['media_type'],
                'shares': item['shares'] or 0,
                'username': item['username'],
                'full_name': item['full_name'] if 'full_name' in item.keys() else None,
                'profile_pic': item['profile_pic'],
                'user_id': item['user_id'],
                'is_verified': item['is_verified'],
                'likes_count': likes_count,
                'comments_count': comments_count,
                'user_liked': user_liked,
                'user_saved': user_saved,
                'user_reposted': user_reposted,
                'created_at': item['created_at']
            })

        conn.close()
        return render_template('kijiji/kijiji.html', kijiji_list=posts_data)


    # ========== LIKE / UNLIKE ==========

    @app.route('/kijiji/like/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_like(post_id):
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if already liked
        cursor.execute('SELECT id FROM likes WHERE user_id = ? AND post_id = ?', (user_id, post_id))
        existing = cursor.fetchone()

        if existing:
            cursor.execute('DELETE FROM likes WHERE user_id = ? AND post_id = ?', (user_id, post_id))
            action = 'unliked'
        else:
            cursor.execute('INSERT INTO likes (user_id, post_id) VALUES (?, ?)', (user_id, post_id))
            action = 'liked'

            # Notification + Push
            cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
            owner = cursor.fetchone()
            if owner and owner[0] != user_id:
                create_notification(
                    owner[0], user_id, 'like',
                    'amependa Kijiji yako',
                    post_id=post_id,
                    url=f'/post/{post_id}'
                )

        conn.commit()

        # Return new count
        cursor.execute('SELECT COUNT(*) FROM likes WHERE post_id = ?', (post_id,))
        count = cursor.fetchone()[0]
        conn.close()

        return jsonify({'status': action, 'likes_count': count})


    # ========== SAVE / UNSAVE ==========

    @app.route('/kijiji/save/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_save(post_id):
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT id FROM saved_posts WHERE user_id = ? AND post_id = ?', (user_id, post_id))
        existing = cursor.fetchone()

        if existing:
            cursor.execute('DELETE FROM saved_posts WHERE user_id = ? AND post_id = ?', (user_id, post_id))
            action = 'unsaved'
        else:
            cursor.execute('INSERT INTO saved_posts (user_id, post_id) VALUES (?, ?)', (user_id, post_id))
            action = 'saved'
            # Notification + Push
            cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
            owner = cursor.fetchone()
            if owner and owner[0] != user_id:
                create_notification(
                    owner[0], user_id, 'save',
                    'amehifadhi Kijiji yako',
                    post_id=post_id,
                    url=f'/post/{post_id}'
                )

        conn.commit()
        conn.close()
        return jsonify({'status': action})


    # ========== SHARE ==========

    @app.route('/kijiji/share/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_share(post_id):
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('UPDATE posts SET shares = COALESCE(shares, 0) + 1 WHERE id = ?', (post_id,))
        conn.commit()

        # Notification + Push
        cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
        owner = cursor.fetchone()
        if owner and owner[0] != user_id:
            create_notification(
                owner[0], user_id, 'share',
                'ameshare Kijiji yako',
                post_id=post_id,
                url=f'/post/{post_id}'
            )

        cursor.execute('SELECT shares FROM posts WHERE id = ?', (post_id,))
        shares = cursor.fetchone()[0]
        conn.close()

        return jsonify({'shares': shares})


    # ========== RECORD VIEW (optional lakini muhimu) ==========

    @app.route('/kijiji/view/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_view(post_id):
        """Rekodi view ya post — mara moja tu kwa kila user."""
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        existing = cursor.execute(
            'SELECT id FROM post_views WHERE user_id = ? AND post_id = ? LIMIT 1',
            (user_id, post_id)
        ).fetchone()
        recorded = False
        if not existing:
            try:
                data = request.get_json(silent=True) or {}
                watch_seconds = float(data.get('watch_seconds', 0) or 0)
            except Exception:
                watch_seconds = 0.0
            cursor.execute(
                '''
                INSERT INTO post_views (user_id, post_id, watch_seconds)
                VALUES (?, ?, ?)
                ''',
                (user_id, post_id, watch_seconds)
            )
            conn.commit()
            recorded = True
        try:
            views_count = int(cursor.execute(
                'SELECT COUNT(DISTINCT user_id) FROM post_views WHERE post_id = ?',
                (post_id,)
            ).fetchone()[0] or 0)
        except Exception:
            views_count = 0
        conn.close()
        return jsonify({
            'status': 'ok',
            'recorded': recorded,
            'views_count': views_count
        })


    # ========== DELETE COMMENT ==========

    @app.route('/kijiji/comment/delete/<int:comment_id>', methods=['POST'])
    @login_required
    def kijiji_delete_comment(comment_id):
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        # Ruhusu mmiliki wa comment au admin kufuta
        cursor.execute('SELECT user_id FROM comments WHERE id = ?', (comment_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Comment haipo'}), 404

        # Check if admin
        cursor.execute('SELECT role FROM users WHERE id = ?', (user_id,))
        role = cursor.fetchone()[0]

        if row[0] != user_id and role != 'admin':
            conn.close()
            return jsonify({'error': 'Huna ruhusa'}), 403

        cursor.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'deleted'})


    # ========== REPORT POST ==========

    @app.route('/kijiji/report/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_report(post_id):
        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        reason = (data.get('reason') or 'Hakuna sababu').strip()

        if not reason:
            return jsonify({'error': 'Andika sababu'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT id FROM reports WHERE post_id = ? AND reporter_id = ?', (post_id, user_id))
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Umesharipoti tayari'}), 400

        cursor.execute('''
            INSERT INTO reports (post_id, reporter_id, reason)
            VALUES (?, ?, ?)
        ''', (post_id, user_id, reason))
        conn.commit()

        reporter = cursor.execute(
            'SELECT username FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        reporter_name = reporter['username'] if reporter else 'Mtumiaji'
        admin_rows = cursor.execute(
            "SELECT id FROM users WHERE email = ?",
            ('matondomaduhu135@gmail.com',)
        ).fetchall()
        conn.close()

        msg = f'🚨 Report: @{reporter_name} ameripoti post #{post_id}. Sababu: {reason}'
        for adm in admin_rows:
            try:
                create_notification(
                    adm['id'], user_id, 'report', msg,
                    post_id=post_id, url='/admin'
                )
            except Exception:
                pass

        return jsonify({'status': 'reported'})


    # ========== BLOCK USER ==========

    @app.route('/kijiji/block/<int:user_id>', methods=['POST'])
    @login_required
    def kijiji_block(user_id):
        blocker_id = session['user_id']

        if blocker_id == user_id:
            return jsonify({'error': 'Huwezi kujizuia mwenyewe'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT INTO blocks (blocker_id, blocked_id)
                VALUES (?, ?)
            ''', (blocker_id, user_id))
            conn.commit()
            status = 'blocked'
        except sqlite3.IntegrityError:
            # Already blocked → unblock
            cursor.execute('DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?', (blocker_id, user_id))
            conn.commit()
            status = 'unblocked'

        conn.close()
        return jsonify({'status': status})


    # ========== REPOST ==========

    @app.route('/kijiji/repost/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_repost(post_id):
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if already reposted
        cursor.execute('SELECT id FROM reposts WHERE user_id = ? AND original_post_id = ?', (user_id, post_id))
        existing = cursor.fetchone()

        if existing:
            cursor.execute('DELETE FROM reposts WHERE user_id = ? AND original_post_id = ?', (user_id, post_id))
            action = 'unreposted'
        else:
            cursor.execute('''
                INSERT INTO reposts (user_id, original_post_id)
                VALUES (?, ?)
            ''', (user_id, post_id))
            action = 'reposted'

            # Optional: increase shares count
            cursor.execute('UPDATE posts SET shares = COALESCE(shares, 0) + 1 WHERE id = ?', (post_id,))

            # Notification + Push
            cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
            owner = cursor.fetchone()
            if owner and owner[0] != user_id:
                create_notification(
                    owner[0], user_id, 'repost',
                    'amerepost Kijiji yako',
                    post_id=post_id,
                    url=f'/post/{post_id}'
                )

        conn.commit()
        conn.close()
        return jsonify({'status': action})


    # ========== GET COMMENTS (AJAX) ==========

    @app.route('/kijiji/comments/<int:post_id>')
    @login_required
    def kijiji_get_comments(post_id):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT c.id, c.content, c.created_at, c.user_id,
                   u.username, u.profile_pic
            FROM comments c
            JOIN users u ON c.user_id = u.id
            WHERE c.post_id = ? AND (c.is_hidden = 0 OR c.is_hidden IS NULL)
            ORDER BY c.created_at ASC
        ''', (post_id,))
        comments = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify(comments)


    # ========== POST COMMENT (AJAX) ==========

    @app.route('/kijiji/comment/<int:post_id>', methods=['POST'])
    @login_required
    def kijiji_post_comment(post_id):
        user_id = session['user_id']
        content = request.json.get('content', '').strip()

        if not content:
            return jsonify({'error': 'Andika comment'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO comments (post_id, user_id, content)
            VALUES (?, ?, ?)
        ''', (post_id, user_id, content))
        comment_id = cursor.lastrowid
        conn.commit()

        # Notification + Push
        cursor.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
        owner = cursor.fetchone()
        if owner and owner[0] != user_id:
            create_notification(
                owner[0], user_id, 'comment',
                'amecomment Kijiji yako',
                post_id=post_id,
                url=f'/post/{post_id}'
            )

        # Return the new comment
        cursor.execute('''
            SELECT c.id, c.content, c.created_at, c.user_id,
                   u.username, u.profile_pic
            FROM comments c
            JOIN users u ON c.user_id = u.id
            WHERE c.id = ?
        ''', (comment_id,))
        new_comment = dict(cursor.fetchone())
        conn.close()
        return jsonify(new_comment)


    # (Admin routes zimehamishwa → admin.py  lines ~8332-9014)
    @app.route('/request_badge', methods=['POST'])
    def request_badge():

        # ====================== 1. HAKIKI LOGIN ======================
        if 'user_id' not in session:
            return jsonify({
                'success': False,
                'message': 'Unatakiwa uingie kwanza (Login).'
            }), 401

        user_id = session.get('user_id')

        # Ombi hili ni kwa ajili ya PROFILE ya mtu binafsi (si Linkup),
        # kwa hiyo halihitaji Kijiji Mode iwe imewashwa.

        # ====================== 2. POKEA TAARIFA ZA FORM ======================
        account_type = request.form.get('account_type', '').strip()
        full_name = request.form.get('full_name', '').strip()
        alias_name = request.form.get('alias_name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        category = request.form.get('category', '').strip()
        id_type = request.form.get('id_type', '').strip()
        id_number = request.form.get('id_number', '').strip()
        website_link = request.form.get('website_link', '').strip()
        media_links = request.form.get('media_links', '').strip()
        other_socials = request.form.get('other_socials', '').strip()
        reason = request.form.get('reason', '').strip()

        # ====================== 3. HAKIKI TAARIFA MUHIMU ======================
        required_fields = {
            'Aina ya akaunti': account_type,
            'Jina kamili': full_name,
            'Email': email,
            'Nambari ya simu': phone,
            'Kategori': category,
            'Aina ya kitambulisho': id_type,
            'Namba ya kitambulisho': id_number,
            'Sababu ya kuomba badge': reason
        }

        for field_name, field_value in required_fields.items():
            if not field_value:
                return jsonify({
                    'success': False,
                    'message': f'Tafadhali jaza: {field_name}.'
                }), 400

        # ====================== 4. HAKIKI FILE ======================
        if 'id_document' not in request.files:
            return jsonify({
                'success': False,
                'message': 'Tafadhali pakia picha au nakala ya kitambulisho chako.'
            }), 400

        file = request.files['id_document']
        if not file or file.filename == '':
            return jsonify({
                'success': False,
                'message': 'Hujachagua faili lolote la kitambulisho.'
            }), 400

        # ====================== 5. HAKIKI AINA YA FILE ======================
        allowed_extensions = {'jpg', 'jpeg', 'png', 'pdf'}
        original_filename = secure_filename(file.filename)

        if not original_filename or '.' not in original_filename:
            return jsonify({
                'success': False,
                'message': 'Faili hili halina extension inayotambulika.'
            }), 400

        extension = original_filename.rsplit('.', 1)[1].lower()
        if extension not in allowed_extensions:
            return jsonify({
                'success': False,
                'message': 'Aina ya faili haikubaliwi. Pakia JPG, JPEG, PNG au PDF pekee.'
            }), 400

        # ====================== 6. HAKIKI UKUBWA WA FILE ======================
        MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({
                'success': False,
                'message': 'Faili ni kubwa sana. Ukubwa wa juu ni 5MB.'
            }), 400

        # ====================== 7. DATABASE ======================
        conn = get_db_connection()
        id_document_path = None

        try:
            # ====================== 8. HAKIKI USER ======================
            user = conn.execute(
                '''
                SELECT id, username, email FROM users WHERE id = ?
                ''',
                (user_id,)
            ).fetchone()

            if not user:
                return jsonify({
                    'success': False,
                    'message': 'Mtumiaji hajapatikana.'
                }), 404

            username = user['username']

            # ====================== 9. HAKIKI OMBI LA AWALI ======================
            existing_req = conn.execute(
                '''
                SELECT id FROM badge_requests WHERE user_id = ? AND status = 'pending' LIMIT 1
                ''',
                (user_id,)
            ).fetchone()

            if existing_req:
                return jsonify({
                    'success': False,
                    'message': 'Ombi lako la awali bado linashughulikiwa na Admins.'
                }), 400

            # ====================== 10. HAKIKI FOLDER ======================
            upload_folder = app.config.get('UPLOAD_BADGES_FOLDER')
            if not upload_folder:
                return jsonify({
                    'success': False,
                    'message': 'UPLOAD_BADGES_FOLDER haijawekwa kwenye server.'
                }), 500

            os.makedirs(upload_folder, exist_ok=True)

            # ====================== 11. TENGENEZA JINA SALAMA LA FILE ======================
            unique_filename = f"user_{user_id}_{uuid.uuid4().hex}.{extension}"
            id_document_path = os.path.join(upload_folder, unique_filename)

            # ====================== 12. HIFADHI FILE ======================
            file.save(id_document_path)

            # ====================== 13. HAKIKI FILE IMEHIFADHIWA ======================
            if not os.path.exists(id_document_path):
                return jsonify({
                    'success': False,
                    'message': 'Faili halikuweza kuhifadhiwa kwenye server.'
                }), 500

            # ====================== 14. HIFADHI OMBI DATABASE ======================
            conn.execute(
                '''
                INSERT INTO badge_requests (
                    user_id, username, account_type, full_name, alias_name,
                    email, phone, category, id_type, id_number,
                    id_document_path, website_link, media_links, other_socials, reason, status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending'
                )
                ''',
                (
                    user_id, username, account_type, full_name, alias_name,
                    email, phone, category, id_type, id_number,
                    id_document_path, website_link, media_links, other_socials, reason
                )
            )

            # ====================== 15. COMMIT ======================
            conn.commit()

            # ====================== 16. SUCCESS ======================
            return jsonify({
                'success': True,
                'message': 'Ombi lako la Verified Badge limetumwa kikamilifu!'
            }), 200

        # ====================== DATABASE ERROR ======================
        except sqlite3.Error as e:
            conn.rollback()
            if id_document_path and os.path.exists(id_document_path):
                try:
                    os.remove(id_document_path)
                except Exception:
                    pass
            print('DATABASE ERROR ON REQUEST BADGE:', str(e))
            return jsonify({
                'success': False,
                'message': 'Kuna tatizo kwenye database. Tafadhali jaribu tena.'
            }), 500

        # ====================== GENERAL ERROR ======================
        except Exception as e:
            conn.rollback()
            if id_document_path and os.path.exists(id_document_path):
                try:
                    os.remove(id_document_path)
                except Exception:
                    pass
            print('ERROR ON REQUEST BADGE:', str(e))
            return jsonify({
                'success': False,
                'message': 'Kuna tatizo kwenye server. Tafadhali jaribu tena.'
            }), 500

        # ====================== FUNGA DATABASE ======================
        finally:
            conn.close()

    # (Admin routes zimehamishwa → admin.py  lines ~9221-9320)


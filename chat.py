"""
chat.py — Private messages, inbox, calls, star, archive, react
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


def register_chat_routes(app):
    # ============================================================
    # ONGEZA HIZI ROUTES KWENYE app.py YAKO
    # ============================================================

    @app.route('/send_message/<int:user_id>', methods=['POST'])
    def send_message(user_id):
        if 'user_id' not in session:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({
                    'success': False,
                    'error': 'login_required'
                }), 401
            return redirect(url_for('login'))

        message = request.form.get('message', '').strip()
        file = request.files.get('file')

        # ========== REPLY (ongeza hii) ==========
        reply_to = request.form.get('reply_to')  # id ya ujumbe unaojibiwa
        try:
            reply_to = int(reply_to) if reply_to else None
        except (TypeError, ValueError):
            reply_to = None
        # =======================================

        if user_id == session['user_id']:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({
                    'success': False,
                    'error': 'Huwezi kujituma ujumbe'
                }), 400
            flash('Huwezi kujituma ujumbe')
            return redirect(url_for('home'))

        conn = get_db_connection()

        receiver = conn.execute(
            'SELECT id, username FROM users WHERE id = ?',
            (user_id,)
        ).fetchone()

        if not receiver:
            conn.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({
                    'success': False,
                    'error': 'Mtumiaji hajapatikana'
                }), 404
            flash('Mtumiaji hajapatikana')
            return redirect(url_for('home'))

        file_path = None
        media_type = None

        # ==============================
        # HANDLE FILE / MEDIA
        # ==============================
        if file and file.filename:
            ext = file.filename.rsplit('.', 1)[-1].lower()

            if ext in ALLOWED_EXTENSIONS:
                filename = secure_filename(file.filename)

                unique = (
                    f"chat_{session['user_id']}_"
                    f"{int(time.time())}_{filename}"
                )

                file.save(
                    os.path.join(
                        app.config['UPLOAD_FOLDER'],
                        unique
                    )
                )

                file_path = unique

                if filename.startswith('voice_') or ext in ('mp3', 'wav', 'aac', 'm4a', 'opus'):
                    media_type = 'audio'
                elif ext in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif'):
                    media_type = 'image'
                elif ext in ('mp4', 'webm', 'ogg', 'mov', 'avi', '3gp', 'mkv'):
                    media_type = 'video'
                else:
                    media_type = 'file'

        # ==============================
        # CHECK EMPTY MESSAGE
        # ==============================
        if not message and not file_path:
            conn.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({
                    'success': False,
                    'error': 'Andika ujumbe au ambatisha faili'
                }), 400
            flash('Andika ujumbe au ambatisha faili')
            return redirect(url_for('chat', username=receiver['username']))

        # ========== REPLY: pata text + name ya ujumbe wa zamani ==========
        reply_to_text = None
        reply_to_name = None
        if reply_to:
            original = conn.execute(
                '''
                SELECT pm.message, pm.media_type, u.username
                FROM private_messages pm
                JOIN users u ON u.id = pm.sender_id
                WHERE pm.id = ?
                  AND (
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                    OR
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                  )
                ''',
                (reply_to, session['user_id'], user_id, user_id, session['user_id'])
            ).fetchone()

            if original:
                reply_to_name = original['username']
                if original['message']:
                    reply_to_text = original['message'][:120]
                else:
                    reply_to_text = (original['media_type'] or 'media').upper()
            else:
                # kama ujumbe haujapatikana, usihifadhi reply_to
                reply_to = None
        # ==================================================================

        # ==============================
        # SAVE MESSAGE
        # ==============================
        # KAMA bado huna column reply_to_id, kimbiza SQL hii mara moja:
        # ALTER TABLE private_messages ADD COLUMN reply_to_id INTEGER;

        cur = conn.execute(
            '''
            INSERT INTO private_messages
            (
                sender_id,
                receiver_id,
                message,
                is_read,
                is_delivered,
                file_path,
                media_type,
                reply_to_id,
                created_at
            )
            VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?)
            ''',
            (
                session['user_id'],
                user_id,
                message or '',
                file_path,
                media_type,
                reply_to,          # <-- REPLY ID
                now_tz()
            )
        )

        new_id = cur.lastrowid

        # ==============================
        # COMMIT MAPEMA (KUEPUKA "database is locked")
        # Lazima tu-commit KABLA ya notify_user, kwa sababu notify_user
        # inafungua connection nyingine ya kuandika. Tukiiacha hapa bila
        # commit, connection hii inashikilia write lock wakati wote wa
        # push notification (network request) -> DB inalock kwa 30+ sekunde.
        # ==============================
        conn.commit()

        # ==============================
        # MESSAGE PREVIEW
        # ==============================
        preview = (
            message[:80]
            if message
            else ('📎 ' + (media_type or 'file'))
        )
        if len(message or '') > 80:
            preview += '...'

        # ==============================
        # SAVE NOTIFICATION + PUSH (nje ya transaction)
        # ==============================
        try:
            notify_user(
                user_id,
                session['user_id'],
                'message',
                preview
            )
        except Exception as e:
            print('[PUSH] message notify error:', e)
            try:
                conn.execute(
                    "INSERT INTO notifications (user_id, sender_id, type, message, is_read) VALUES (?, ?, 'message', ?, 0)",
                    (user_id, session['user_id'], preview)
                )
                conn.commit()
            except Exception:
                pass

        # ==============================
        # GET CREATED MESSAGE DATA
        # ==============================
        created_message = conn.execute(
            '''
            SELECT
                id,
                sender_id,
                receiver_id,
                message,
                file_path,
                media_type,
                created_at,
                is_read,
                is_delivered,
                reaction,
                is_hidden,
                reply_to_id
            FROM private_messages
            WHERE id = ?
            ''',
            (new_id,)
        ).fetchone()

        conn.close()

        # ==============================
        # AJAX RESPONSE - REAL TIME
        # ==============================
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'success': True,
                'message': {
                    'id': created_message['id'],
                    'sender_id': created_message['sender_id'],
                    'receiver_id': created_message['receiver_id'],
                    'message': created_message['message'] or '',
                    'file_path': created_message['file_path'],
                    'media_type': created_message['media_type'],
                    'created_at': (
                        str(created_message['created_at'])
                        if created_message['created_at']
                        else ''
                    ),
                    'is_read': created_message['is_read'] or 0,
                    'is_delivered': created_message['is_delivered'] or 0,
                    'reaction': (
                        created_message['reaction']
                        if 'reaction' in created_message.keys()
                        else None
                    ),
                    'is_hidden': (
                        created_message['is_hidden']
                        if 'is_hidden' in created_message.keys()
                        else 0
                    ),
                    # ========== REPLY fields (frontend inahitaji hizi) ==========
                    'reply_to_id': reply_to,
                    'reply_to_text': reply_to_text,
                    'reply_to_name': reply_to_name,
                    # ============================================================
                }
            })

        # ==============================
        # NORMAL REQUEST
        # ==============================
        return redirect(
            url_for(
                'chat',
                username=receiver['username']
            )
        )


    # ============================================================
    # ENDPOINT MPYA: Inatumika na polling (real-time)
    # ============================================================

    @app.route('/chat/messages/<int:user_id>')
    def get_chat_messages(user_id):
        if 'user_id' not in session:
            return jsonify({'messages': []}), 401

        after_id = request.args.get('after_id', 0, type=int)
        me = session['user_id']

        conn = get_db_connection()

        # Mark as delivered + read (mpokeaji anapoll / yuko kwenye chat)
        conn.execute(
            "UPDATE private_messages SET is_delivered = 1, is_read = 1 "
            "WHERE sender_id = ? AND receiver_id = ? AND is_read = 0",
            (user_id, me)
        )
        conn.commit()

        # Ujumbe mpya (pamoja na reply quote)
        rows = conn.execute(
            '''
            SELECT
                pm.id,
                pm.sender_id,
                pm.receiver_id,
                pm.message,
                pm.file_path,
                pm.media_type,
                pm.is_read,
                pm.is_delivered,
                pm.created_at,
                pm.reaction,
                pm.is_hidden,
                pm.reply_to_id,
                rpm.message AS reply_to_text_raw,
                rpm.media_type AS reply_to_media,
                ru.username AS reply_to_name
            FROM private_messages pm
            LEFT JOIN private_messages rpm ON rpm.id = pm.reply_to_id
            LEFT JOIN users ru ON ru.id = rpm.sender_id
            WHERE
                (
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                    OR
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                )
                AND pm.id > ?
                AND NOT (
                    (pm.sender_id = ? AND COALESCE(pm.deleted_for_sender, 0) = 1)
                    OR
                    (pm.receiver_id = ? AND COALESCE(pm.deleted_for_receiver, 0) = 1)
                )
            ORDER BY pm.id ASC
            ''',
            (me, user_id, user_id, me, after_id, me, me)
        ).fetchall()

        # Status ya ujumbe wangu (ticks: delivered/read) — bila kuharibu logic yako
        my_status = conn.execute(
            '''
            SELECT
                pm.id,
                pm.sender_id,
                pm.receiver_id,
                pm.message,
                pm.file_path,
                pm.media_type,
                pm.is_read,
                pm.is_delivered,
                pm.created_at,
                pm.reaction,
                pm.is_hidden,
                pm.reply_to_id,
                rpm.message AS reply_to_text_raw,
                rpm.media_type AS reply_to_media,
                ru.username AS reply_to_name
            FROM private_messages pm
            LEFT JOIN private_messages rpm ON rpm.id = pm.reply_to_id
            LEFT JOIN users ru ON ru.id = rpm.sender_id
            WHERE pm.sender_id = ? AND pm.receiver_id = ? AND pm.id > ?
                AND COALESCE(pm.deleted_for_sender, 0) = 0
            ORDER BY pm.id ASC
            ''',
            (me, user_id, max(0, after_id - 80))
        ).fetchall()

        conn.close()

        seen = set()
        messages = []

        def _add(r):
            if r['id'] in seen:
                return
            seen.add(r['id'])

            # Reply fields
            reply_to_id = r['reply_to_id'] if 'reply_to_id' in r.keys() else None
            reply_to_text = None
            reply_to_name = None
            if reply_to_id:
                raw = r['reply_to_text_raw'] if 'reply_to_text_raw' in r.keys() else None
                media = r['reply_to_media'] if 'reply_to_media' in r.keys() else None
                if raw:
                    reply_to_text = (raw or '')[:120]
                else:
                    reply_to_text = (media or 'media').upper()
                reply_to_name = (
                    r['reply_to_name']
                    if 'reply_to_name' in r.keys() and r['reply_to_name']
                    else 'Ujumbe'
                )

            messages.append({
                'id': r['id'],
                'sender_id': r['sender_id'],
                'receiver_id': r['receiver_id'],
                'message': r['message'] or '',
                'file_path': r['file_path'] if r['file_path'] else None,
                'media_type': r['media_type'] if r['media_type'] else None,
                'is_read': r['is_read'] or 0,
                'is_delivered': r['is_delivered'] or 0,
                'created_at': str(r['created_at']) if r['created_at'] else '',
                'reaction': r['reaction'] if 'reaction' in r.keys() else None,
                'is_hidden': r['is_hidden'] if 'is_hidden' in r.keys() else 0,
                # Reply (mpya)
                'reply_to_id': reply_to_id,
                'reply_to_text': reply_to_text,
                'reply_to_name': reply_to_name,
            })

        for r in rows:
            _add(r)
        for r in my_status:
            _add(r)

        messages.sort(key=lambda m: m['id'])

        return jsonify({
            'success': True,
            'messages': messages
        })

    @app.route('/chat/react/<int:msg_id>', methods=['POST'])
    def chat_react(msg_id):
        if 'user_id' not in session:
            return jsonify({'success': False}), 401
        data = request.get_json() or {}
        reaction = (data.get('reaction') or '')[:8]
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or session['user_id'] not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False}), 403
        conn.execute('UPDATE private_messages SET reaction = ? WHERE id = ?', (reaction, msg_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/chat/hide/<int:msg_id>', methods=['POST'])
    def chat_hide(msg_id):
        if 'user_id' not in session:
            return jsonify({'success': False}), 401
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or session['user_id'] not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False}), 403
        conn.execute('UPDATE private_messages SET is_hidden = 1 WHERE id = ?', (msg_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/chat/unhide/<int:msg_id>', methods=['POST'])
    def chat_unhide(msg_id):
        """Rudisha ujumbe uliofichwa uonekane tena (bonyeza ili kuufichua)."""
        if 'user_id' not in session:
            return jsonify({'success': False}), 401
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or session['user_id'] not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False}), 403
        conn.execute('UPDATE private_messages SET is_hidden = 0 WHERE id = ?', (msg_id,))
        conn.commit()
        row = conn.execute(
            'SELECT message, file_path, media_type, reply_to_id FROM private_messages WHERE id = ?',
            (msg_id,)
        ).fetchone()
        conn.close()
        return jsonify({
            'success': True,
            'message': row['message'] or '',
            'file_path': row['file_path'],
            'media_type': row['media_type'],
        })

    @app.route('/chat/delete/<int:msg_id>', methods=['POST'])
    def chat_delete(msg_id):
        """'Futa kwangu' - ujumbe unaondoka kwenye chat yako pekee.
        Kama pande zote mbili (aliyetuma na aliyepokea) wamefuta, ndipo unafutwa kabisa DB."""
        if 'user_id' not in session:
            return jsonify({'success': False}), 401
        me = session['user_id']
        conn = get_db_connection()
        msg = conn.execute('SELECT * FROM private_messages WHERE id = ?', (msg_id,)).fetchone()
        if not msg or me not in (msg['sender_id'], msg['receiver_id']):
            conn.close()
            return jsonify({'success': False}), 403

        if me == msg['sender_id']:
            conn.execute('UPDATE private_messages SET deleted_for_sender = 1 WHERE id = ?', (msg_id,))
        else:
            conn.execute('UPDATE private_messages SET deleted_for_receiver = 1 WHERE id = ?', (msg_id,))
        conn.commit()

        refreshed = conn.execute(
            'SELECT deleted_for_sender, deleted_for_receiver FROM private_messages WHERE id = ?',
            (msg_id,)
        ).fetchone()
        if refreshed and refreshed['deleted_for_sender'] and refreshed['deleted_for_receiver']:
            conn.execute('DELETE FROM private_messages WHERE id = ?', (msg_id,))
            conn.commit()

        conn.close()
        return jsonify({'success': True})

    @app.route('/inbox')
    def inbox():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        me = session['user_id']
        conn = get_db_connection()

        # Watu uliozungumza nao + last message + unread + pin
        rows = conn.execute('''
            SELECT
                u.id,
                u.username,
                u.full_name,
                u.profile_pic,
                (
                    SELECT message FROM private_messages pm2
                    WHERE (pm2.sender_id = me_id.uid AND pm2.receiver_id = u.id)
                       OR (pm2.sender_id = u.id AND pm2.receiver_id = me_id.uid)
                    ORDER BY pm2.id DESC LIMIT 1
                ) AS last_message,
                (
                    SELECT created_at FROM private_messages pm3
                    WHERE (pm3.sender_id = me_id.uid AND pm3.receiver_id = u.id)
                       OR (pm3.sender_id = u.id AND pm3.receiver_id = me_id.uid)
                    ORDER BY pm3.id DESC LIMIT 1
                ) AS last_time,
                (
                    SELECT COUNT(*) FROM private_messages pm4
                    WHERE pm4.sender_id = u.id
                      AND pm4.receiver_id = me_id.uid
                      AND pm4.is_read = 0
                ) AS unread_count,
                (
                    SELECT COUNT(*) FROM pinned_chats pc
                    WHERE pc.user_id = me_id.uid AND pc.other_user_id = u.id
                ) AS is_pinned
            FROM users u
            JOIN (SELECT ? AS uid) me_id
            WHERE u.id IN (
                SELECT sender_id FROM private_messages WHERE receiver_id = ?
                UNION
                SELECT receiver_id FROM private_messages WHERE sender_id = ?
            )
            ORDER BY is_pinned DESC, last_time DESC
        ''', (me, me, me)).fetchall()

        # Watu unaweza pia kuchati nao (sio wewe, sio blocked, sio waliokwisha chat)
        suggested_chat_users = conn.execute('''
            SELECT
                u.id,
                u.username,
                u.full_name,
                u.profile_pic,
                u.is_verified,
                (
                    SELECT COUNT(*) FROM follows f
                    WHERE f.following_id = u.id
                ) AS followers_count
            FROM users u
            WHERE u.id != ?
              AND (u.is_blocked = 0 OR u.is_blocked IS NULL)
              AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL)
              AND u.id NOT IN (
                  SELECT sender_id FROM private_messages WHERE receiver_id = ?
                  UNION
                  SELECT receiver_id FROM private_messages WHERE sender_id = ?
              )
              AND u.id NOT IN (
                  SELECT blocked_id FROM blocks WHERE blocker_id = ?
              )
              AND u.id NOT IN (
                  SELECT blocker_id FROM blocks WHERE blocked_id = ?
              )
            ORDER BY
                COALESCE(u.is_verified, 0) DESC,
                followers_count DESC,
                u.id DESC
            LIMIT 30
        ''', (me, me, me, me, me)).fetchall()

        suggested_chat_users = [dict(r) for r in suggested_chat_users]

        conn.close()
        return render_template(
            'chat/inbox.html',
            conversations=rows,
            suggested_chat_users=suggested_chat_users,
        )


    # ========== INBOX PEOPLE SEARCH (watu wa kuchati nao) ==========

    @app.route('/inbox/people_search')
    @login_required
    def inbox_people_search():
        """Tafuta watumiaji wa kuchati nao."""
        q = (request.args.get('q') or '').strip()
        if not q or len(q) < 1:
            return jsonify({'users': []})

        me = session['user_id']
        like = '%' + q + '%'
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT
                u.id,
                u.username,
                u.full_name,
                u.profile_pic,
                u.is_verified
            FROM users u
            WHERE u.id != ?
              AND (u.is_blocked = 0 OR u.is_blocked IS NULL)
              AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL)
              AND u.id NOT IN (
                  SELECT blocked_id FROM blocks WHERE blocker_id = ?
              )
              AND u.id NOT IN (
                  SELECT blocker_id FROM blocks WHERE blocked_id = ?
              )
              AND (
                  u.username LIKE ? COLLATE NOCASE
                  OR IFNULL(u.full_name, '') LIKE ? COLLATE NOCASE
              )
            ORDER BY
                CASE WHEN u.username LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
                COALESCE(u.is_verified, 0) DESC,
                u.username COLLATE NOCASE
            LIMIT 25
        ''', (me, me, me, like, like, q + '%')).fetchall()
        conn.close()

        users = []
        for r in rows:
            users.append({
                'id': r['id'],
                'username': r['username'],
                'full_name': r['full_name'] or r['username'],
                'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'] or 0,
            })
        return jsonify({'users': users})


    # ========== INBOX SEARCH (ndani ya ujumbe wote) ==========

    @app.route('/inbox/search')
    @login_required
    def inbox_search():
        """Tafuta jina au neno lolote ndani ya private messages."""
        q = (request.args.get('q') or '').strip()
        if not q:
            return jsonify({'usernames': []})

        my_id = session['user_id']
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT DISTINCT u.username
            FROM private_messages m
            JOIN users u ON u.id = CASE
                WHEN m.sender_id = ? THEN m.receiver_id
                ELSE m.sender_id
            END
            WHERE (m.sender_id = ? OR m.receiver_id = ?)
              AND IFNULL(m.is_hidden, 0) = 0
              AND (
                    m.message LIKE ?
                 OR u.username LIKE ?
              )
            ORDER BY u.username COLLATE NOCASE
        ''', (my_id, my_id, my_id, '%' + q + '%', '%' + q + '%')).fetchall()
        conn.close()
        return jsonify({'usernames': [r['username'] for r in rows]})


    # ========== INBOX DELETE (futa chat nzima) ==========

    @app.route('/inbox/delete', methods=['POST'])
    @login_required
    def inbox_delete():
        """Futa ujumbe wote kati yako na user(s) uliochagua."""
        my_id = session['user_id']
        data = request.get_json(silent=True) or {}
        usernames = data.get('usernames') or []
        if isinstance(usernames, str):
            usernames = [usernames]
        if not usernames:
            return jsonify({'success': False, 'error': 'No usernames'}), 400

        conn = get_db_connection()
        deleted = 0
        for username in usernames:
            username = (username or '').strip()
            if not username:
                continue
            row = conn.execute(
                'SELECT id FROM users WHERE username = ?', (username,)
            ).fetchone()
            if not row:
                continue
            other_id = row['id']
            cur = conn.execute('''
                DELETE FROM private_messages
                WHERE (sender_id = ? AND receiver_id = ?)
                   OR (sender_id = ? AND receiver_id = ?)
            ''', (my_id, other_id, other_id, my_id))
            deleted += cur.rowcount
            # Ondoa pin pia kama ilikuwepo
            conn.execute(
                'DELETE FROM pinned_chats WHERE user_id = ? AND other_user_id = ?',
                (my_id, other_id)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'deleted_messages': deleted})


    # ========== INBOX PIN / UNPIN (server-side) ==========

    @app.route('/inbox/pin', methods=['POST'])
    @login_required
    def inbox_pin():
        """Pin au ondoa pin ya chat — inahifadhiwa kwenye database."""
        my_id = session['user_id']
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        # pinned=True → pin, pinned=False → unpin; kama haipo → toggle
        want_pinned = data.get('pinned', None)

        if not username:
            return jsonify({'success': False, 'error': 'username required'}), 400

        conn = get_db_connection()
        row = conn.execute(
            'SELECT id FROM users WHERE username = ?', (username,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'}), 404

        other_id = row['id']
        if other_id == my_id:
            conn.close()
            return jsonify({'success': False, 'error': 'Cannot pin self'}), 400

        existing = conn.execute(
            'SELECT id FROM pinned_chats WHERE user_id = ? AND other_user_id = ?',
            (my_id, other_id)
        ).fetchone()

        if want_pinned is True or (want_pinned is None and not existing):
            if not existing:
                conn.execute(
                    'INSERT INTO pinned_chats (user_id, other_user_id) VALUES (?, ?)',
                    (my_id, other_id)
                )
            pinned = True
        else:
            conn.execute(
                'DELETE FROM pinned_chats WHERE user_id = ? AND other_user_id = ?',
                (my_id, other_id)
            )
            pinned = False

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'pinned': pinned})


    # ========== KIJIJI FEED (Video + Picha) ==========

    @app.route('/call/signal', methods=['POST'])
    def call_signal():
        if 'user_id' not in session:
            return jsonify({'success': False}), 401

        data = request.get_json(silent=True) or {}
        to_user_id = data.get('to_user_id')
        sig_type = data.get('type')  # offer | answer | ice | hangup | reject | busy
        payload = data.get('payload') or {}

        if not to_user_id or not sig_type:
            return jsonify({'success': False, 'error': 'missing fields'}), 400

        me = session['user_id']
        payload_str = json.dumps(payload)

        conn = get_db_connection()

        # Ikiwa ni hangup/reject/busy → mark offers za zamani kati ya users hawa kama read
        if sig_type in ('hangup', 'reject', 'busy'):
            conn.execute(
                '''UPDATE call_signals SET is_read = 1
                   WHERE ((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))
                     AND type IN ('offer', 'answer', 'ice')
                     AND is_read = 0''',
                (me, to_user_id, to_user_id, me)
            )

        conn.execute(
            '''INSERT INTO call_signals (from_user_id, to_user_id, type, payload)
               VALUES (?, ?, ?, ?)''',
            (me, to_user_id, sig_type, payload_str)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/call/signals')
    def get_call_signals():
        if 'user_id' not in session:
            return jsonify({'signals': []}), 401

        after_id = request.args.get('after_id', 0, type=int)
        me = session['user_id']

        conn = get_db_connection()

        # MUHIMU: is_read = 0 tu + za dakika 1 zilizopita (epuka stale offers)
        rows = conn.execute(
            '''SELECT id, from_user_id, to_user_id, type, payload, created_at
               FROM call_signals
               WHERE to_user_id = ?
                 AND id > ?
                 AND is_read = 0
                 AND created_at >= datetime('now', '-60 seconds')
               ORDER BY id ASC
               LIMIT 50''',
            (me, after_id)
        ).fetchall()

        if rows:
            ids = [r['id'] for r in rows]
            placeholders = ','.join('?' * len(ids))
            conn.execute(
                f'UPDATE call_signals SET is_read = 1 WHERE id IN ({placeholders})',
                ids
            )
            conn.commit()

        conn.close()

        signals = []
        for r in rows:
            signals.append({
                'id': r['id'],
                'from_user_id': r['from_user_id'],
                'to_user_id': r['to_user_id'],
                'type': r['type'],
                'payload': r['payload'],
                'created_at': str(r['created_at']) if r['created_at'] else ''
            })

        return jsonify({'signals': signals})


    # ====================== PRIVATE CHAT ======================




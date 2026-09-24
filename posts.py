"""
posts.py — Feed, create/edit/delete posts, likes, comments, shares, status/stories
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
import requests
from io import BytesIO

from db import get_db_connection, UPLOAD_FOLDER, UPLOAD_BADGES_FOLDER, BASE_DIR
from helpers import (
    login_required, now_tz, allowed_file, avatar_url, notify_user, create_notification,
    publish_due_scheduled_posts, muted_ids_for, save_post_hashtags, extract_hashtags,
    enrich_posts_with_linkup, attach_original_posts, get_active_linkup_id, resolve_active_linkup,
    set_active_linkup_id, user_has_kijiji_mode, get_user_linkups, get_linkup_by_id,
    get_linkup_by_username, linkup_username_taken, is_following_linkup,
    count_linkup_followers, sanitize_username, IMAGE_EXTS, VIDEO_EXTS,
    ALLOWED_EXTENSIONS, MAX_LINKUPS_PER_USER, LINKUP_CATEGORIES,
    MAX_IMAGE_BYTES, MAX_VIDEO_BYTES,
    get_user_restriction_status, register_nsfw_violation, flag_post_for_admin,
    record_status_view, count_status_views, can_view_status, send_web_push,
    VAPID_PUBLIC_KEY, generate_profile_qr_card, get_profile_public_url,
    SUPPORTED_LANGUAGES, save_user_language, load_translations
)



def register_posts_routes(app):
    # ====================== HOME / FEED / POSTS ======================

    @app.route('/')
    @app.route('/home')
    @app.route('/feed')
    def home():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        user_id = session['user_id']

        conn = get_db_connection()
        cursor = conn.cursor()

        # ================= USER CHECK =================

        user_check = cursor.execute(
            "SELECT is_blocked FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if user_check and user_check['is_blocked'] == 1:
            conn.close()
            session.clear()
            flash('Akaunti yako imezuiwa na Admin!')
            return redirect(url_for('login'))

        # Feed inabaki FEED hata ukiwa switched kwenye Linkup.
        # Identity (nani anachapisha) inabadilika; ukurasa wa Home haubadiliki.

        # ================= GET POSTS =================

        cursor.execute("""
            SELECT
                posts.*,
                users.username,
                users.full_name,
                users.role,
                users.profile_pic,
                users.is_verified,

                -- Likes zote
                (
                    SELECT COUNT(*)
                    FROM likes
                    WHERE likes.post_id = posts.id
                ) AS likes_count,

                -- User ame-like?
                (
                    SELECT COUNT(*)
                    FROM likes
                    WHERE likes.post_id = posts.id
                    AND likes.user_id = ?
                ) AS user_liked,

                -- Saves zote
                (
                    SELECT COUNT(*)
                    FROM saved_posts
                    WHERE saved_posts.post_id = posts.id
                ) AS saved_count,

                -- User ame-save?
                (
                    SELECT COUNT(*)
                    FROM saved_posts
                    WHERE saved_posts.post_id = posts.id
                    AND saved_posts.user_id = ?
                ) AS user_saved,

                -- User anamfollow owner?
                (
                    SELECT COUNT(*)
                    FROM follows
                    WHERE follows.follower_id = ?
                    AND follows.following_id = posts.user_id
                ) AS is_following,

                -- Owner anamfollow user (follows you)?
                (
                    SELECT COUNT(*)
                    FROM follows
                    WHERE follows.follower_id = posts.user_id
                    AND follows.following_id = ?
                ) AS follows_you,

                -- User amesha-view?
                (
                    SELECT COUNT(*)
                    FROM post_views
                    WHERE post_views.post_id = posts.id
                    AND post_views.user_id = ?
                ) AS user_viewed,

                -- Comments
                (
                    SELECT COUNT(*)
                    FROM comments
                    WHERE comments.post_id = posts.id
                    AND comments.is_hidden = 0
                ) AS comments_count,

                -- Reposts
                (
                    SELECT COUNT(*)
                    FROM reposts
                    WHERE reposts.original_post_id = posts.id
                ) AS reposts_count,

                -- Followers wa owner
                (
                    SELECT COUNT(*)
                    FROM follows
                    WHERE follows.following_id = posts.user_id
                ) AS followers_count,

                -- Views za post (unique viewers)
                (
                    SELECT COUNT(DISTINCT post_views.user_id)
                    FROM post_views
                    WHERE post_views.post_id = posts.id
                ) AS views_count

            FROM posts
            JOIN users
            ON posts.user_id = users.id

            WHERE COALESCE(posts.is_draft, 0) = 0
              AND COALESCE(posts.moderation_status, 'approved') = 'approved'
              AND (users.is_blocked = 0 OR users.is_blocked IS NULL)
              AND (users.is_deactivated = 0 OR users.is_deactivated IS NULL)
              AND posts.user_id NOT IN (
                  SELECT blocked_id FROM blocks WHERE blocker_id = ?
              )
              AND posts.user_id NOT IN (
                  SELECT blocker_id FROM blocks WHERE blocked_id = ?
              )
              AND (
                  COALESCE(users.post_visibility, 'public') = 'public'
                  OR posts.user_id = ?
                  OR (
                      COALESCE(users.post_visibility, 'public') = 'followers'
                      AND EXISTS (
                          SELECT 1 FROM follows
                          WHERE follows.follower_id = ?
                            AND follows.following_id = posts.user_id
                      )
                  )
              )
              -- Linkup posts: PUBLIC kwa kila mtu (kama Facebook Page)

            ORDER BY posts.id DESC

        """, (
            user_id,  # likes
            user_id,  # saved
            user_id,  # is_following
            user_id,  # follows_you
            user_id,  # user_viewed
            user_id,  # blocker
            user_id,  # blocked
            user_id,  # visibility self
            user_id,  # visibility followers
        ))

        posts_data = [dict(row) for row in cursor.fetchall()]
        for _p in posts_data:
            _p['is_following'] = bool(_p.get('is_following'))
            _p['follows_you'] = bool(_p.get('follows_you'))
            _p['is_friend'] = bool(_p.get('is_following') and _p.get('follows_you'))
            try:
                _p['is_verified'] = int(_p.get('is_verified') or 0)
            except Exception:
                _p['is_verified'] = 0
        try:
            enrich_posts_with_linkup(conn, posts_data)
        except Exception as e:
            print('[home] enrich linkup:', e)

        try:
            attach_original_posts(conn, posts_data)
        except Exception as e:
            print('[home] attach orig:', e)

        # ================= USER INTERESTS =================
        #
        # Tunajifunza category ambazo user
        # ana-interact nazo.
        #

        category_scores = {}

        # ---------- LIKES ----------

        liked_categories = cursor.execute("""
            SELECT
                COALESCE(posts.category, 'general') AS category,
                COUNT(*) AS total
            FROM likes
            JOIN posts
            ON likes.post_id = posts.id
            WHERE likes.user_id = ?
            GROUP BY category
        """, (user_id,)).fetchall()

        for row in liked_categories:
            category = row['category']
            category_scores[category] = \
                category_scores.get(category, 0) + (row['total'] * 5)

        # ---------- SAVES ----------

        saved_categories = cursor.execute("""
            SELECT
                COALESCE(posts.category, 'general') AS category,
                COUNT(*) AS total
            FROM saved_posts
            JOIN posts
            ON saved_posts.post_id = posts.id
            WHERE saved_posts.user_id = ?
            GROUP BY category
        """, (user_id,)).fetchall()

        for row in saved_categories:
            category = row['category']
            category_scores[category] = \
                category_scores.get(category, 0) + (row['total'] * 7)

        # ---------- COMMENTS ----------

        comment_categories = cursor.execute("""
            SELECT
                COALESCE(posts.category, 'general') AS category,
                COUNT(*) AS total
            FROM comments
            JOIN posts
            ON comments.post_id = posts.id
            WHERE comments.user_id = ?
            GROUP BY category
        """, (user_id,)).fetchall()

        for row in comment_categories:
            category = row['category']
            category_scores[category] = \
                category_scores.get(category, 0) + (row['total'] * 6)

        # ---------- VIEWS ----------

        view_categories = cursor.execute("""
            SELECT
                COALESCE(posts.category, 'general') AS category,
                COUNT(*) AS total
            FROM post_views
            JOIN posts
            ON post_views.post_id = posts.id
            WHERE post_views.user_id = ?
            GROUP BY category
        """, (user_id,)).fetchall()

        for row in view_categories:
            category = row['category']
            category_scores[category] = \
                category_scores.get(category, 0) + (row['total'] * 2)

        # ================= FOLLOWED CREATORS =================

        followed_users = cursor.execute("""
            SELECT following_id
            FROM follows
            WHERE follower_id = ?
        """, (user_id,)).fetchall()

        followed_ids = {
            row['following_id']
            for row in followed_users
        }

        # ================= SCORE POSTS =================

        import random
        from datetime import datetime

        now = datetime.now()

        for post in posts_data:

            score = 0

            category = post.get('category') or 'general'

            # ==========================================
            # 1. USER INTEREST
            # ==========================================

            interest = category_scores.get(category, 0)

            score += interest

            # ==========================================
            # 2. FOLLOWED CREATOR
            # ==========================================

            if post['is_following']:
                score += 25

            # ==========================================
            # 3. USER ALREADY LIKED
            # ==========================================

            if post['user_liked']:
                score += 8

            # ==========================================
            # 4. USER SAVED
            # ==========================================

            if post['user_saved']:
                score += 10

            # ==========================================
            # 5. NEW / UNSEEN POSTS
            # ==========================================

            if post['user_viewed']:
                score -= 12
            else:
                score += 15

            # ==========================================
            # 6. POPULARITY
            # ==========================================

            score += min(post['likes_count'], 50) * 0.4

            score += min(post['comments_count'], 30) * 0.8

            score += min(post['reposts_count'], 30) * 1.2

            score += min(post.get('shares', 0), 30) * 0.8

            # ==========================================
            # 7. FRESHNESS
            # ==========================================

            try:
                created = datetime.strptime(
                    post['created_at'],
                    '%Y-%m-%d %H:%M:%S'
                )

                age_hours = max(
                    0,
                    (now - created).total_seconds() / 3600
                )

                if age_hours < 1:
                    score += 25

                elif age_hours < 6:
                    score += 18

                elif age_hours < 24:
                    score += 12

                elif age_hours < 72:
                    score += 6

            except Exception:
                pass

            # ==========================================
            # 8. VERIFIED CREATOR
            # ==========================================

            if post['is_verified']:
                score += 2

            # ==========================================
            # 9. DISCOVERY
            # ==========================================
            # Hii inazuia feed kuwa category moja tu.

            score += random.uniform(0, 8)

            post['feed_score'] = score

        # ================= SORT =================

        posts_data.sort(
            key=lambda post: post['feed_score'],
            reverse=True
        )

        # ================= COMMENTS =================

        for post in posts_data:

            try:
                cursor.execute("""
                    SELECT
                        comments.*,
                        users.username,
                        users.full_name,
                        users.role,
                        users.profile_pic,
                        users.is_verified,
                        l.id AS linkup_id_join,
                        l.username AS linkup_username,
                        l.display_name AS linkup_display_name,
                        l.profile_pic AS linkup_profile_pic,
                        l.is_verified AS linkup_verified,
                        (
                            SELECT COUNT(*)
                            FROM comment_likes
                            WHERE comment_likes.comment_id = comments.id
                        ) AS comment_likes_count,
                        (
                            SELECT COUNT(*)
                            FROM comment_likes
                            WHERE comment_likes.comment_id = comments.id
                            AND comment_likes.user_id = ?
                        ) AS user_comment_liked
                    FROM comments
                    JOIN users ON comments.user_id = users.id
                    LEFT JOIN linkups l ON l.id = comments.linkup_id
                    WHERE comments.post_id = ?
                      AND comments.is_hidden = 0
                    ORDER BY comments.id ASC
                """, (user_id, post['id']))
            except Exception:
                # Schema bila linkup_id kwenye comments
                cursor.execute("""
                    SELECT
                        comments.*,
                        users.username,
                        users.full_name,
                        users.role,
                        users.profile_pic,
                        users.is_verified,
                        (
                            SELECT COUNT(*)
                            FROM comment_likes
                            WHERE comment_likes.comment_id = comments.id
                        ) AS comment_likes_count,
                        (
                            SELECT COUNT(*)
                            FROM comment_likes
                            WHERE comment_likes.comment_id = comments.id
                            AND comment_likes.user_id = ?
                        ) AS user_comment_liked
                    FROM comments
                    JOIN users ON comments.user_id = users.id
                    WHERE comments.post_id = ?
                      AND comments.is_hidden = 0
                    ORDER BY comments.id ASC
                """, (user_id, post['id']))

            comments_list = [dict(row) for row in cursor.fetchall()]
            for c in comments_list:
                # Display kama Linkup ikiwa comment iliwekwa kama Linkup
                lu_user = c.get('linkup_username')
                lid = c.get('linkup_id') or c.get('linkup_id_join')
                if lid and lu_user:
                    c['is_linkup_comment'] = True
                    c['display_name'] = c.get('linkup_display_name') or lu_user
                    c['username'] = lu_user
                    if c.get('linkup_profile_pic'):
                        c['profile_pic'] = c['linkup_profile_pic']
                    if c.get('linkup_verified'):
                        c['is_verified'] = 1
                        c['linkup_verified'] = 1
                    c['profile_url'] = '/linkup/' + str(lu_user)
                else:
                    c['is_linkup_comment'] = False
                    c['display_name'] = c.get('full_name') or c.get('username')
                    c['profile_url'] = '/profile/' + str(c.get('username') or '')
            post['comments'] = comments_list

        # ================= NOTIFICATIONS =================

        unread_notifs = cursor.execute("""
            SELECT COUNT(*)
            FROM notifications
            WHERE user_id = ?
            AND is_read = 0
        """, (user_id,)).fetchone()[0]

        # ================= ACTIVE STATUSES (last 24 hours) =================
        status_rows = cursor.execute('''
            SELECT
                u.id AS user_id,
                u.username,
                u.full_name,
                u.profile_pic,
                MAX(s.id) AS latest_id
            FROM statuses s
            JOIN users u ON u.id = s.user_id
            WHERE s.created_at >= datetime('now', '-24 hours')
              AND (u.is_blocked = 0 OR u.is_blocked IS NULL)
              AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL)
            GROUP BY u.id
            ORDER BY
                CASE WHEN u.id = ? THEN 0 ELSE 1 END,
                MAX(s.created_at) DESC
        ''', (user_id,)).fetchall()

        status_list = []
        for row in status_rows:
            d = dict(row)
            # Seen = user ametazama status zote hai za huyu (24h)
            if d['user_id'] == user_id:
                d['seen'] = True  # yako mwenyewe
            else:
                unseen = cursor.execute('''
                    SELECT COUNT(*) FROM statuses s
                    WHERE s.user_id = ?
                      AND s.created_at >= datetime('now', '-24 hours')
                      AND NOT EXISTS (
                          SELECT 1 FROM status_views v
                          WHERE v.status_id = s.id AND v.viewer_id = ?
                      )
                ''', (d['user_id'], user_id)).fetchone()[0]
                d['seen'] = (unseen == 0)
            status_list.append(d)

        # Unseen kwanza, kisha seen (bado yako mbele)
        status_list.sort(key=lambda x: (
            0 if x['user_id'] == user_id else 1,
            1 if x.get('seen') else 0,
        ))

        my_has_status = any(s['user_id'] == user_id for s in status_list)

        me_user = cursor.execute(
            'SELECT id, username, profile_pic FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        my_status_info = {
            'user_id': user_id,
            'username': me_user['username'] if me_user else session.get('username'),
            'profile_pic': me_user['profile_pic'] if me_user else None,
            'has_status': my_has_status,
        }



        # ================= SUGGESTED USERS (Watu wa kufollow) =================

        suggested_rows = cursor.execute('''
            SELECT
                u.id,
                u.username,
                u.full_name,
                u.profile_pic,
                u.is_verified,

                (
                    SELECT COUNT(*)
                    FROM follows f
                    WHERE f.following_id = u.id
                ) AS followers_count,

                -- CHECK IF THIS USER FOLLOWS ME
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM follows fm
                        WHERE fm.follower_id = u.id
                          AND fm.following_id = ?
                    )
                    THEN 1
                    ELSE 0
                END AS follows_me,

                -- CHECK IF I AM FOLLOWING THIS USER
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM follows fi
                        WHERE fi.follower_id = ?
                          AND fi.following_id = u.id
                    )
                    THEN 1
                    ELSE 0
                END AS is_following

            FROM users u

            WHERE u.id != ?

              AND (u.is_blocked = 0 OR u.is_blocked IS NULL)

              AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL)

              -- Pendekeza watumiaji wengi (priority: Kijiji Mode, verified, followers)
              -- Hatuwazuii kwa village_mode pekee — ili "Watu wa kufollow" ijaa
              AND COALESCE(u.warning_count, 0) = 0

              -- EXCLUDE USERS I ALREADY FOLLOW
              AND u.id NOT IN (
                  SELECT following_id
                  FROM follows
                  WHERE follower_id = ?
              )

              -- EXCLUDE USERS I BLOCKED
              AND u.id NOT IN (
                  SELECT blocked_id
                  FROM blocks
                  WHERE blocker_id = ?
              )

              -- EXCLUDE USERS WHO BLOCKED ME
              AND u.id NOT IN (
                  SELECT blocker_id
                  FROM blocks
                  WHERE blocked_id = ?
              )

            ORDER BY
                COALESCE(u.village_mode, 0) DESC,
                COALESCE(u.is_verified, 0) DESC,
                followers_count DESC,
                u.id DESC

            LIMIT 30

        ''', (
            user_id,  # follows_me: this user follows me
            user_id,  # is_following: I follow this user
            user_id,  # exclude current user
            user_id,  # exclude users I already follow
            user_id,  # exclude users I blocked
            user_id   # exclude users who blocked me
        )).fetchall()


        # CONVERT DATABASE ROWS TO DICTIONARIES
        suggested_users = [dict(r) for r in suggested_rows]


        conn.close()

        # ================= SEND TO HTML =================

        # Pending share from PWA Share Target (clear after read so it shows once)
        pending_share = session.pop('pending_share', None)

        if pending_share is not None:
            session.modified = True

        # Active Linkup identity (Kijiji Mode switch)
        active_linkup = None
        posting_as_linkup = False
        try:
            conn2 = get_db_connection()
            active_linkup = resolve_active_linkup(conn2, user_id)
            conn2.close()
            posting_as_linkup = bool(active_linkup)
        except Exception as e:
            print('[home] active_linkup:', e)

        return render_template(
            'feed/feed.html',
            username=session.get('username'),
            posts_data=posts_data,
            unread_notifs=unread_notifs,
            status_list=status_list,
            my_status_info=my_status_info,
            pending_share=pending_share,
            suggested_users=suggested_users,
            active_linkup=active_linkup,
            posting_as_linkup=posting_as_linkup,
        )

    @app.route('/change-language', methods=['GET', 'POST'])
    def change_language():
        if request.method == 'POST':
            language = request.form.get('language', 'sw')
            if language not in SUPPORTED_LANGUAGES:
                language = 'sw'

            session['language'] = language
            session.modified = True

            # Hifadhi lugha kwenye database
            if session.get('user_id'):
                save_user_language(session['user_id'], language)

            flash(load_translations(language).get('language_saved', 'Lugha imebadilishwa'))

            # ========== BAADA YA USAJILI → ENDELEA MOJA KWA MOJA FEED ==========
            if session.pop('from_registration', False):
                return redirect(url_for('home'))

            if session.get('user_id'):
                next_url = request.form.get('next') or request.args.get('next')
                if next_url:
                    return redirect(next_url)
                return redirect(url_for('settings') if 'settings' in app.view_functions else url_for('home'))

            return redirect(url_for('login'))

        return render_template(
            'account/change_language.html',
            current_language=session.get('language', 'sw'),
            languages=SUPPORTED_LANGUAGES
        )

    @app.route('/post/<int:post_id>')
    def post_detail(post_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT posts.*, users.username, users.full_name, users.role, users.profile_pic, users.is_verified,
                   (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS likes_count,
                   (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id AND likes.user_id = ?) AS user_liked,
                   (SELECT COUNT(*) FROM saved_posts WHERE saved_posts.post_id = posts.id) AS saved_count
            FROM posts
            JOIN users ON posts.user_id = users.id
            WHERE posts.id = ?
        """, (session['user_id'], post_id))
        post = cursor.fetchone()

        if not post:
            conn.close()
            flash('Chapisho halijapatikana!')
            return redirect(url_for('home'))

        post = dict(post)
        try:
            post['is_verified'] = int(post.get('is_verified') or 0)
        except Exception:
            post['is_verified'] = 0
        try:
            enrich_posts_with_linkup(conn, [post])
        except Exception as e:
            print('[post_detail] enrich linkup:', e)
        try:
            attach_original_posts(conn, [post])
        except Exception as e:
            print('[post_detail] attach orig:', e)

        cursor.execute("""
            SELECT comments.*, users.username, users.full_name, users.role, users.profile_pic, users.is_verified,
                   (SELECT COUNT(*) FROM comment_likes WHERE comment_likes.comment_id = comments.id) AS comment_likes_count,
                   (SELECT COUNT(*) FROM comment_likes WHERE comment_likes.comment_id = comments.id AND comment_likes.user_id = ?) AS user_comment_liked
            FROM comments
            JOIN users ON comments.user_id = users.id
            WHERE comments.post_id = ?
            ORDER BY comments.id ASC
        """, (session['user_id'], post_id))
        post['comments'] = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return render_template('posts/post_detail.html', post=post)

    @app.route('/create_post', methods=['POST'])
    def create_post():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        # ================= RESTRICTION CHECK =================
        _conn_check = get_db_connection()
        _is_restricted, _until = get_user_restriction_status(_conn_check, session['user_id'])
        _conn_check.close()
        if _is_restricted:
            flash(
                f'⛔ UMEZUIWA KUCHAPISHA. Akaunti yako imezuiwa kuchapisha maudhui '
                f'mpaka {_until} kwa sababu ya kukiuka sheria za uchi/ngono mara kadhaa. '
                f'Ukiongeza ukiukaji, akaunti inaweza kuzuiwa kabisa.',
                'error'
            )
            return redirect(url_for('home'))

        content = request.form.get('content', '').strip()
        file = request.files.get('file')
        shared_file = (request.form.get('shared_file') or '').strip()

        file_path = None
        media_type = None

        # PWA Share Target: file tayari iko kwenye uploads
        if shared_file and not (file and file.filename):
            safe = os.path.basename(shared_file)
            if safe and '..' not in safe and '/' not in safe and '\\' not in safe:
                abs_path = os.path.join(app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER), safe)
                if os.path.isfile(abs_path):
                    ext = safe.rsplit('.', 1)[-1].lower() if '.' in safe else ''
                    if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                        unique_filename = f"{int(time.time())}_{safe}"
                        try:
                            os.rename(abs_path, os.path.join(app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER), unique_filename))
                            file_path = unique_filename
                            media_type = 'video' if ext in VIDEO_EXTS else 'image'
                        except Exception as e:
                            print('[create_post] shared_file move error:', e)
                            file_path = safe
                            media_type = 'video' if ext in VIDEO_EXTS else 'image'

        if not content and not file_path and (not file or file.filename == ''):
            flash('Andika kitu au weka picha/video kabla ya kupost!')
            return redirect(url_for('home'))

        # ================= FILE UPLOAD =================

        if not file_path and file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[1].lower()

            # Size check (picha/video max 24MB)
            file.seek(0, 2)
            size = file.tell()
            file.seek(0)

            is_video = ext in VIDEO_EXTS
            is_image = ext in IMAGE_EXTS

            if is_video and size > MAX_VIDEO_BYTES:
                flash('Video isizidi 24MB. Chagua video ndogo zaidi.')
                return redirect(url_for('home'))
            if is_image and size > MAX_IMAGE_BYTES:
                flash('Picha isizidi 24MB. Chagua picha ndogo zaidi.')
                return redirect(url_for('home'))
            if not is_video and not is_image:
                flash('Aina ya file hairuhusiwi. Tumia picha au video.')
                return redirect(url_for('home'))

            unique_filename = f"{int(time.time())}_{filename}"

            file.save(
                os.path.join(
                    app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER),
                    unique_filename
                )
            )

            file_path = unique_filename

            media_type = 'video' if is_video else 'image'

        # ================= NSFW MODERATION (IMEZIMWA) =================
        # Moderate imezimwa: post zote zinaidhinishwa moja kwa moja.
        # Ili kuwasha tena, rudisha moderate_media() call hapa.
        moderation_result = {'decision': 'approved', 'score': 0.0, 'labels': []}
        detector_decision = 'approved'
        nsfw_score = 0.0
        nsfw_labels = []
        moderation_status = 'approved'

        # ================= AUTO CATEGORY =================

        text = content.lower()

        category = 'general'

        football_words = [
            'mpira', 'football', 'mchezo', 'goal', 'goli',
            'liga', 'champions', 'efootball', 'messi',
            'ronaldo', 'yanga', 'simba', 'azam'
        ]

        islamic_words = [
            'allah', 'muhammad', 'quran', 'qur’an',
            'islam', 'islamu', 'swala', 'sala', 'dua',
            'ramadhan', 'ramadan', 'msikiti', 'hadith'
        ]

        comedy_words = [
            'comedy', 'utani', 'kichekesho', 'chekesho',
            '😂', '🤣', 'haha', 'hahaha'
        ]

        music_words = [
            'music', 'muziki', 'song', 'wimbo',
            'msanii', 'singer', 'album'
        ]

        technology_words = [
            'technology', 'tech', 'computer', 'python',
            'flask', 'coding', 'programming', 'software',
            'app', 'android', 'iphone'
        ]

        news_words = [
            'habari', 'news', 'breaking', 'tukio',
            'serikali', 'rais', 'tanzania'
        ]

        if any(word in text for word in football_words):
            category = 'football'

        elif any(word in text for word in islamic_words):
            category = 'islamic'

        elif any(word in text for word in comedy_words):
            category = 'comedy'

        elif any(word in text for word in music_words):
            category = 'music'

        elif any(word in text for word in technology_words):
            category = 'technology'

        elif any(word in text for word in news_words):
            category = 'news'

        # ================= TIME =================

        current_time = datetime.now(
            ZoneInfo("Africa/Dar_es_Salaam")
        ).strftime('%Y-%m-%d %H:%M:%S')

        # ================= DATABASE =================

        conn = get_db_connection()
        cursor = conn.cursor()

        # Post kama Linkup ikiwa user amechagua active Linkup (Kijiji Mode)
        active_lu = resolve_active_linkup(conn, session['user_id'])
        linkup_id = int(active_lu['id']) if active_lu else None
        # Ikiwa anachapisha kama Linkup, tumia category ya Linkup kama default
        if active_lu and active_lu.get('category') and category == 'general':
            category = active_lu.get('category') or category

        cursor.execute("""
            INSERT INTO posts
            (
                user_id,
                content,
                file_path,
                media_type,
                shares,
                created_at,
                category,
                moderation_status,
                nsfw_score,
                linkup_id
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """, (
            session['user_id'],
            content,
            file_path,
            media_type,
            current_time,
            category,
            moderation_status,
            nsfw_score,
            linkup_id
        ))

        post_id = cursor.lastrowid


        # ================= NSFW: UAMUZI / WARNING / APPEAL =================
        # rejected (kutoka 'rejected' AU 'manual_review' ya detector) = kataa
        # MARA MOJA kabla haijaonekana popote + warning papo hapo. Haiendi
        # kwenye foleni ya admin kiotomatiki - mtumiaji mwenyewe ndiye anaomba
        # ukaguzi (/post/<id>/request-review) kama anahisi si sahihi.
        if moderation_status == 'rejected':
            _wcount, _restricted = register_nsfw_violation(
                conn, post_id, session['user_id'], nsfw_score, nsfw_labels
            )
            # register_nsfw_violation tayari inaweka flag 'escalation' kila warning ya 3
            # (hii ni tofauti na appeal ya mtumiaji - ni usalama wa ziada tu).

        # ================= HASHTAGS =================
        try:
            save_post_hashtags(conn, post_id, content)
        except Exception as e:
            print('[create_post] hashtags:', e)

        # ================= HISTORY =================

        cursor.execute("""
            INSERT INTO history
            (
                user_id,
                action_description,
                post_id,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            session['user_id'],
            (
                'Post IMEKATALIWA (NSFW) — haijachapishwa'
                if moderation_status == 'rejected'
                else 'Ameunda chapisho jipya'
            ),
            post_id,
            current_time
        ))

        conn.commit()
        conn.close()

        if moderation_status == 'rejected':
            flash(
                '🚫 POST IMEKATALIWA — HAIJACHAPISHWA. '
                'Mfumo umegundua maudhui yanayoshukiwa kuwa ya uchi/ngono. '
                'Chapisho HALIJAONEKANA hadharani na umepewa WARNING. '
                'Ukiongeza ukiukaji (warnings 3, 6, 9…), akaunti itazuiwa kwa masaa 2 '
                'au kuzuiwa kabisa na Admin. '
                'Kama unaona hii si sahihi, tumia kitufe cha "Omba Admin akague" '
                'kilichoonekana chini ya ujumbe huu.',
                'error'
            )
            return redirect(url_for('home', nsfw_rejected=1, rejected_post_id=post_id))
        else:
            flash('Post yako imechapishwa kikamilifu!', 'success')

        return redirect(url_for('home'))


    @app.route('/post/<int:post_id>/request-review', methods=['POST', 'GET'])
    @login_required
    def request_review(post_id):
        """
        Mmiliki wa post iliyokataliwa (NSFW) anaomba Admin aikague upya.
        Hii ndiyo njia PEKEE ya post iliyokataliwa kuingia kwenye foleni ya
        admin - haiendi huko kiotomatiki wakati wa kupost.
        """
        conn = get_db_connection()
        post = conn.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()

        if not post or post['user_id'] != session.get('user_id'):
            conn.close()
            flash('Huruhusiwi kufanya hivyo.', 'error')
            return redirect(url_for('home'))

        if post['moderation_status'] != 'rejected':
            conn.close()
            flash('Chapisho hili tayari halihitaji ukaguzi.', 'warning')
            return redirect(url_for('home'))

        # Epuka kutuma ombi mara mbili kwa post moja
        already = conn.execute(
            "SELECT id FROM moderation_flags WHERE post_id = ? AND flag_type = 'user_appeal' AND status = 'pending'",
            (post_id,)
        ).fetchone()

        if already:
            conn.close()
            flash('Tayari umeshatuma ombi la ukaguzi kwa post hii. Subiri Admin akague.', 'warning')
            return redirect(url_for('home'))

        flag_post_for_admin(
            conn, post_id, session['user_id'], 'user_appeal',
            post['nsfw_score'] or 0, []
        )
        conn.execute("UPDATE posts SET moderation_status = 'manual_review' WHERE id = ?", (post_id,))
        conn.commit()
        conn.close()

        flash('✅ Ombi lako limetumwa kwa Admin. Utaarifiwa baada ya kukaguliwa.', 'success')
        return redirect(url_for('home'))


    # ========== STATUS (Stories 24h) ==========
    # Preset status music (weka faili kwenye static/music/)
    STATUS_MUSIC_PRESETS = [
        {'id': 'none', 'label': 'Bila muziki', 'path': ''},
        {'id': 'bongo1', 'label': 'Bongo Chill', 'path': 'music/bongo_chill.mp3'},
        {'id': 'afro1', 'label': 'Afro Beat', 'path': 'music/afro_beat.mp3'},
        {'id': 'gospel1', 'label': 'Gospel Soft', 'path': 'music/gospel_soft.mp3'},
        {'id': 'romantic1', 'label': 'Romantic', 'path': 'music/romantic.mp3'},
        {'id': 'upbeat1', 'label': 'Upbeat', 'path': 'music/upbeat.mp3'},
    ]

    @app.route('/like/<int:post_id>', methods=['POST'])
    def like_post(post_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        user_id = session['user_id']
        conn = get_db_connection()

        post = conn.execute(
            'SELECT user_id FROM posts WHERE id = ?', (post_id,)
        ).fetchone()

        existing = conn.execute(
            'SELECT id FROM likes WHERE user_id = ? AND post_id = ?',
            (user_id, post_id)
        ).fetchone()

        current_time = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')

        if existing:
            conn.execute(
                'DELETE FROM likes WHERE user_id = ? AND post_id = ?',
                (user_id, post_id)
            )
            liked = False
        else:
            conn.execute(
                'INSERT INTO likes (user_id, post_id) VALUES (?, ?)',
                (user_id, post_id)
            )
            liked = True

            # History
            conn.execute(
                '''INSERT INTO history (user_id, action_description, post_id, created_at)
                   VALUES (?, ?, ?, ?)''',
                (user_id, 'Amependa (like) chapisho', post_id, current_time)
            )
            conn.commit()
            conn.close()

            # Notification + PUSH (nje ya conn ili kuepuka lock)
            if post and post['user_id'] != user_id:
                notify_user(
                    user_id=post['user_id'],
                    sender_id=user_id,
                    ntype='like',
                    post_id=post_id
                )

            likes_count = get_db_connection().execute(
                'SELECT COUNT(*) FROM likes WHERE post_id = ?', (post_id,)
            ).fetchone()[0]
            return jsonify({'success': True, 'liked': True, 'likes_count': likes_count})

        conn.commit()
        likes_count = conn.execute(
            'SELECT COUNT(*) FROM likes WHERE post_id = ?', (post_id,)
        ).fetchone()[0]
        conn.close()
        return jsonify({'success': True, 'liked': liked, 'likes_count': likes_count})

    @app.route('/save_post/<int:post_id>', methods=['POST'])
    def save_post(post_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        user_id = session['user_id']
        conn = get_db_connection()

        existing = conn.execute('SELECT * FROM saved_posts WHERE user_id = ? AND post_id = ?', (user_id, post_id)).fetchone()
        if existing:
            conn.execute('DELETE FROM saved_posts WHERE user_id = ? AND post_id = ?', (user_id, post_id))
            saved = False
        else:
            conn.execute('INSERT INTO saved_posts (user_id, post_id) VALUES (?, ?)', (user_id, post_id))
            saved = True

        conn.commit()
        saved_count = conn.execute('SELECT COUNT(*) FROM saved_posts WHERE post_id = ?', (post_id,)).fetchone()[0]
        conn.close()
        return jsonify({'success': True, 'saved': saved, 'saved_count': saved_count})

    @app.route('/share_post/<int:post_id>', methods=['POST'])
    def share_post(post_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        user_id = session['user_id']
        conn = get_db_connection()
        conn.execute('UPDATE posts SET shares = shares + 1 WHERE id = ?', (post_id,))

        current_time = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            'INSERT INTO history (user_id, action_description, post_id, created_at) VALUES (?, ?, ?, ?)',
            (user_id, 'Ameshiriki (share) chapisho', post_id, current_time)
        )

        conn.commit()
        shares_count = conn.execute('SELECT shares FROM posts WHERE id = ?', (post_id,)).fetchone()[0]
        conn.close()
        return jsonify({'success': True, 'shares_count': shares_count})

    @app.route('/comment/<int:post_id>', methods=['POST'])
    def add_comment(post_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        content = (request.form.get('content') or '').strip()
        parent_id = request.form.get('parent_id')
        user_id = session['user_id']
        username = session.get('username')

        if content:
            current_time = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')

            conn = get_db_connection()
            cursor = conn.cursor()

            # Hakikisha column linkup_id ipo kwenye comments
            try:
                cursor.execute('ALTER TABLE comments ADD COLUMN linkup_id INTEGER')
                conn.commit()
            except Exception:
                pass

            # Comment kama Linkup ikiwa user ame-switch Kijiji Mode → Linkup
            active_lu = None
            linkup_id = None
            try:
                active_lu = resolve_active_linkup(conn, user_id)
                if active_lu:
                    linkup_id = int(active_lu['id'])
            except Exception as e:
                print('[add_comment] resolve linkup:', e)

            try:
                cursor.execute(
                    "INSERT INTO comments (post_id, user_id, content, parent_id, created_at, linkup_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (post_id, user_id, content, parent_id if parent_id else None, current_time, linkup_id)
                )
            except Exception:
                cursor.execute(
                    "INSERT INTO comments (post_id, user_id, content, parent_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (post_id, user_id, content, parent_id if parent_id else None, current_time)
                )
                linkup_id = None

            cursor.execute(
                'INSERT INTO history (user_id, action_description, post_id, created_at) VALUES (?, ?, ?, ?)',
                (user_id, 'Ameweka maoni (comment) kwenye chapisho', post_id, current_time)
            )

            conn.commit()
            comment_id = cursor.lastrowid

            post = conn.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,)).fetchone()
            if post and post['user_id'] != user_id and not parent_id:
                create_notification(
                    post['user_id'], user_id, 'comment',
                    'ameweka maoni kwenye chapisho',
                    post_id=post_id,
                    url=f'/post/{post_id}'
                )

            # Display identity
            display_name = None
            profile_pic = None
            is_verified = 0
            profile_url = f'/profile/{username}'
            is_linkup_comment = False
            if linkup_id and active_lu:
                is_linkup_comment = True
                display_name = active_lu.get('display_name') or active_lu.get('username')
                username = active_lu.get('username') or username
                profile_pic = active_lu.get('profile_pic') or None
                is_verified = 1 if active_lu.get('is_verified') else 0
                profile_url = f"/linkup/{active_lu.get('username')}"
            else:
                urow = conn.execute(
                    'SELECT full_name, profile_pic, is_verified FROM users WHERE id = ?',
                    (user_id,)
                ).fetchone()
                if urow:
                    display_name = urow['full_name'] or username
                    profile_pic = urow['profile_pic']
                    is_verified = int(urow['is_verified'] or 0)

            conn.close()

            return jsonify({
                'success': True,
                'comment': {
                    'id': comment_id,
                    'username': username,
                    'full_name': display_name,
                    'display_name': display_name,
                    'profile_pic': profile_pic,
                    'is_verified': is_verified,
                    'is_linkup_comment': is_linkup_comment,
                    'profile_url': profile_url,
                    'linkup_id': linkup_id,
                    'content': content,
                    'parent_id': parent_id,
                    'created_at': current_time,
                }
            })

        return jsonify({'success': False, 'error': 'Empty content'}), 400

    @app.route('/like_comment/<int:comment_id>', methods=['POST'])
    def like_comment(comment_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        user_id = session['user_id']
        conn = get_db_connection()

        existing = conn.execute('SELECT * FROM comment_likes WHERE user_id = ? AND comment_id = ?', (user_id, comment_id)).fetchone()
        if existing:
            conn.execute('DELETE FROM comment_likes WHERE user_id = ? AND comment_id = ?', (user_id, comment_id))
            liked = False
        else:
            conn.execute('INSERT INTO comment_likes (user_id, comment_id) VALUES (?, ?)', (user_id, comment_id))
            liked = True

        conn.commit()
        likes_count = conn.execute('SELECT COUNT(*) FROM comment_likes WHERE comment_id = ?', (comment_id,)).fetchone()[0]
        conn.close()

        return jsonify({'success': True, 'liked': liked, 'likes_count': likes_count})

    @app.route('/delete_post/<int:post_id>', methods=['POST'])
    def delete_post(post_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        post = conn.execute('SELECT * FROM posts WHERE id = ? AND user_id = ?', (post_id, session['user_id'])).fetchone()

        if not post:
            conn.close()
            flash('Huruhusiwi kufuta post hii au haipo!', 'danger')
            return redirect(url_for('home'))

        conn.execute('DELETE FROM likes WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM saved_posts WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
        conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))

        current_time = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            'INSERT INTO history (user_id, action_description, post_id, created_at) VALUES (?, ?, ?, ?)',
            (session['user_id'], 'Amefuta chapisho (post)', post_id, current_time)
        )

        conn.commit()
        conn.close()

        flash('Post imefutwa kwa mafanikio!', 'success')
        return redirect(url_for('home'))

    # Route ya Ku-edit Post (Imekamilishwa)

    @app.route('/edit_post/<int:post_id>', methods=['GET', 'POST'])
    def edit_post(post_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        post = conn.execute('SELECT * FROM posts WHERE id = ? AND user_id = ?', (post_id, session['user_id'])).fetchone()

        if not post:
            conn.close()
            flash('Huruhusiwi kuedit post hii au haipo!', 'danger')
            return redirect(url_for('home'))

        if request.method == 'POST':
            new_content = request.form.get('content', '').strip()

            if not new_content:
                flash('Maudhui ya post hayawezi kuwa wazi!', 'danger')
                conn.close()
                return redirect(url_for('edit_post', post_id=post_id))

            conn.execute('UPDATE posts SET content = ? WHERE id = ?', (new_content, post_id))

            current_time = datetime.now(ZoneInfo("Africa/Dar_es_Salaam")).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                'INSERT INTO history (user_id, action_description, post_id, created_at) VALUES (?, ?, ?, ?)',
                (session['user_id'], 'Amehariri (edit) chapisho', post_id, current_time)
            )

            conn.commit()
            conn.close()

            flash('Post imebadilishwa kwa mafanikio!', 'success')
            return redirect(url_for('home'))

        conn.close()
        return render_template('posts/edit_post.html', post=dict(post))

    # Route ya Interested / Not Interested

    @app.route('/post_interest/<int:post_id>', methods=['POST'])
    def post_interest(post_id):
        if 'user_id' not in session:
            return {'success': False, 'message': 'Tafadhali ingia kwanza'}, 401

        data = request.get_json()
        action = data.get('action')

        return {'success': True}

    @app.route('/repost/<int:post_id>', methods=['POST'])
    def repost(post_id):
        """Repost = caption + frame (repost_of). No media copy. No 'Reposted from'."""
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        caption = (data.get('caption') or request.form.get('caption') or '').strip()
        if len(caption) > 2000:
            caption = caption[:2000]

        conn = get_db_connection()
        cursor = conn.cursor()

        original = cursor.execute(
            'SELECT id, user_id, content, file_path, media_type, repost_of FROM posts WHERE id = ?',
            (post_id,)
        ).fetchone()
        if not original:
            conn.close()
            return jsonify({'success': False, 'message': 'Post haipatikani'}), 404

        root_id = post_id
        try:
            if original['repost_of']:
                root_id = int(original['repost_of'])
                root = cursor.execute(
                    'SELECT id, user_id, content, file_path, media_type, repost_of FROM posts WHERE id = ?',
                    (root_id,)
                ).fetchone()
                if root:
                    original = root
                else:
                    root_id = post_id
        except Exception:
            root_id = post_id

        if int(original['user_id']) == int(user_id):
            conn.close()
            return jsonify({'success': False, 'message': 'Huwezi ku-repost post yako mwenyewe'})

        already = cursor.execute(
            'SELECT id FROM reposts WHERE user_id = ? AND original_post_id = ?',
            (user_id, root_id)
        ).fetchone()
        if already:
            conn.close()
            return jsonify({'success': False, 'message': 'Umesharepost tayari'})

        cursor.execute(
            'INSERT INTO reposts (user_id, original_post_id) VALUES (?, ?)',
            (user_id, root_id)
        )

        current_time = now_tz()
        active_lu = resolve_active_linkup(conn, user_id)
        linkup_id = int(active_lu['id']) if active_lu else None

        cursor.execute(
            "INSERT INTO posts (user_id, content, file_path, media_type, shares, created_at, "
            "category, moderation_status, repost_of, linkup_id) "
            "VALUES (?, ?, NULL, NULL, 0, ?, 'general', 'approved', ?, ?)",
            (user_id, caption or None, current_time, root_id, linkup_id)
        )
        new_post_id = cursor.lastrowid

        cursor.execute(
            'UPDATE posts SET shares = COALESCE(shares, 0) + 1 WHERE id = ?',
            (root_id,)
        )

        try:
            me = cursor.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
            who = '@' + ((me['username'] if me else 'mtu'))
            create_notification(
                int(original['user_id']), user_id, 'repost',
                who + ' amerepost posti yako',
                post_id=root_id, url='/post/' + str(new_post_id)
            )
        except Exception as e:
            print('[repost] notify error:', e)

        try:
            if caption:
                save_post_hashtags(conn, new_post_id, caption)
        except Exception:
            pass

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Ume-repost kikamilifu!', 'post_id': new_post_id})


    # ====================== REPORT POST ======================

    @app.route('/report_post/<int:post_id>', methods=['POST'])
    def report_post(post_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        data = request.get_json(silent=True) or {}
        reason = (data.get('reason') or request.form.get('reason') or '').strip()

        if not reason:
            return jsonify({'success': False, 'message': 'Sababu inahitajika'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        post = cursor.execute(
            'SELECT id, user_id, content FROM posts WHERE id = ?', (post_id,)
        ).fetchone()
        if not post:
            conn.close()
            return jsonify({'success': False, 'message': 'Post haipatikani'}), 404

        # Zuia report mara nyingi kutoka user yule yule
        existing = cursor.execute(
            'SELECT id FROM reports WHERE post_id = ? AND reporter_id = ?',
            (post_id, session['user_id'])
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({'success': False, 'message': 'Umesharipoti post hii tayari.'}), 400

        cursor.execute(
            'INSERT INTO reports (post_id, reporter_id, reason) VALUES (?, ?, ?)',
            (post_id, session['user_id'], reason)
        )
        conn.commit()

        # Notify admin(s)
        reporter = cursor.execute(
            'SELECT username FROM users WHERE id = ?', (session['user_id'],)
        ).fetchone()
        reporter_name = reporter['username'] if reporter else 'Mtumiaji'
        snippet = (post['content'] or '')[:60]
        admin_rows = cursor.execute(
            "SELECT id FROM users WHERE email = ?",
            ('matondomaduhu135@gmail.com',)
        ).fetchall()
        conn.close()

        msg = f'🚨 Report: @{reporter_name} ameripoti post #{post_id}. Sababu: {reason}'
        if snippet:
            msg += f' — "{snippet}"'
        for adm in admin_rows:
            try:
                create_notification(
                    adm['id'],
                    session['user_id'],
                    'report',
                    msg,
                    post_id=post_id,
                    url=f'/admin'
                )
            except Exception as e:
                print('report notify error:', e)

        return jsonify({'success': True, 'message': 'Ripoti imepokelewa. Asante!'})


    # ====================== DELETE COMMENT ======================

    @app.route('/delete_comment/<int:comment_id>', methods=['POST'])
    def delete_comment(comment_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        comment = cursor.execute(
            'SELECT id, user_id, post_id FROM comments WHERE id = ?',
            (comment_id,)
        ).fetchone()

        if not comment:
            conn.close()
            return jsonify({'success': False, 'message': 'Comment haipatikani'}), 404

        # Get post owner
        post = cursor.execute(
            'SELECT user_id FROM posts WHERE id = ?',
            (comment['post_id'],)
        ).fetchone()

        # Allow if: comment owner OR post owner
        if comment['user_id'] != user_id and post['user_id'] != user_id:
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403

        cursor.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
        conn.commit()
        conn.close()

        return jsonify({'success': True})


    # ====================== HIDE / UNHIDE COMMENT ======================

    @app.route('/hide_comment/<int:comment_id>', methods=['POST'])
    def hide_comment(comment_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        comment = cursor.execute(
            'SELECT id, post_id, is_hidden FROM comments WHERE id = ?',
            (comment_id,)
        ).fetchone()

        if not comment:
            conn.close()
            return jsonify({'success': False, 'message': 'Comment haipatikani'}), 404

        # Only post owner can hide
        post = cursor.execute(
            'SELECT user_id FROM posts WHERE id = ?',
            (comment['post_id'],)
        ).fetchone()

        if post['user_id'] != user_id:
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403

        new_status = 0 if comment['is_hidden'] else 1
        cursor.execute(
            'UPDATE comments SET is_hidden = ? WHERE id = ?',
            (new_status, comment_id)
        )
        conn.commit()
        conn.close()

        status_text = "imefichwa" if new_status == 1 else "imeonyeshwa"
        return jsonify({
            'success': True,
            'message': f'Comment {status_text}',
            'is_hidden': new_status
        })


    # ====================== BLOCK USER ======================

    @app.route('/edit_comment/<int:comment_id>', methods=['POST'])
    def edit_comment(comment_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        data = request.get_json()
        new_content = data.get('content', '').strip()

        if not new_content:
            return jsonify({'success': False, 'message': 'Comment haiwezi kuwa tupu'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        comment = cursor.execute(
            'SELECT id, user_id FROM comments WHERE id = ?',
            (comment_id,)
        ).fetchone()

        if not comment:
            conn.close()
            return jsonify({'success': False, 'message': 'Comment haipatikani'}), 404

        # Only comment owner can edit
        if comment['user_id'] != session['user_id']:
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa'}), 403

        cursor.execute(
            'UPDATE comments SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (new_content, comment_id)
        )
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'Comment imebadilishwa'})


    # ====================== STATUS / STORIES ======================

    @app.route('/status/create', methods=['POST'])
    @login_required
    def status_create():
        content = (request.form.get('content') or '').strip()
        file = request.files.get('file')
        music_choice = (request.form.get('music_path') or '').strip()

        # Ruhusu preset paths tu (usalama)
        allowed_music = {p['path'] for p in STATUS_MUSIC_PRESETS if p['path']}
        music_path = music_choice if music_choice in allowed_music else None

        file_path = None
        media_type = 'text'

        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ext = filename.rsplit('.', 1)[1].lower()
            file.seek(0, 2)
            size = file.tell()
            file.seek(0)
            is_video = ext in VIDEO_EXTS
            is_image = ext in IMAGE_EXTS
            if is_video and size > MAX_VIDEO_BYTES:
                flash('Video isizidi 24MB')
                return redirect(url_for('home'))
            if is_image and size > MAX_IMAGE_BYTES:
                flash('Picha isizidi 24MB')
                return redirect(url_for('home'))
            if not is_video and not is_image:
                flash('Tumia picha au video tu')
                return redirect(url_for('home'))
            unique = f"status_{int(time.time())}_{filename}"
            file.save(os.path.join(app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER), unique))
            file_path = unique
            media_type = 'video' if is_video else 'image'
        elif not content:
            flash('Andika maandishi au weka picha/video')
            return redirect(url_for('home'))

        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO statuses (user_id, content, file_path, media_type, music_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session['user_id'], content or None, file_path, media_type, music_path, now_tz())
            )
        except sqlite3.OperationalError:
            # fallback kama column haipo bado
            conn.execute(
                "INSERT INTO statuses (user_id, content, file_path, media_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (session['user_id'], content or None, file_path, media_type, now_tz())
            )
        conn.commit()
        conn.close()
        flash('Status imewekwa!')
        return redirect(url_for('home'))

    @app.route('/status/user/<int:user_id>')
    @login_required
    def status_user(user_id):
        """JSON: status zote za user zilizo hai (24h)."""
        me = int(session['user_id'])
        user_id = int(user_id)
        conn = get_db_connection()
        rows = conn.execute(
            """
            SELECT s.id, s.content, s.file_path, s.media_type, s.created_at,
                   s.user_id, u.username, u.full_name, u.profile_pic
                   , s.music_path
            FROM statuses s
            JOIN users u ON u.id = s.user_id
            WHERE s.user_id = ?
              AND s.created_at >= datetime('now', '-24 hours')
            ORDER BY s.created_at ASC
            """,
            (user_id,)
        ).fetchall()
        items = []
        for r in rows:
            owner_id = int(r['user_id'])
            is_own = (owner_id == me)
            views_count = 0
            my_reaction = None
            recent_reactions = []

            if is_own:
                views_count = count_status_views(conn, r['id'])
                # Reactions za wengine (kwa floating emojis kwa mmiliki)
                try:
                    recent_reactions = [
                        row['reaction'] for row in conn.execute(
                            '''SELECT reaction FROM status_reactions
                               WHERE status_id = ? AND user_id != ?
                               ORDER BY created_at DESC LIMIT 20''',
                            (r['id'], me)
                        ).fetchall()
                        if row['reaction']
                    ]
                except Exception:
                    recent_reactions = []
            else:
                # Rekodi view mara moja mtu anapofungua status
                record_status_view(conn, r['id'], me)

            try:
                react_row = conn.execute(
                    'SELECT reaction FROM status_reactions WHERE status_id = ? AND user_id = ?',
                    (r['id'], me)
                ).fetchone()
                if react_row:
                    my_reaction = react_row['reaction']
            except Exception:
                my_reaction = None

            music_path = ''
            try:
                music_path = r['music_path'] or ''
            except (KeyError, IndexError):
                music_path = ''

            items.append({
                'id': r['id'],
                'user_id': owner_id,
                'content': r['content'] or '',
                'file_path': r['file_path'] or '',
                'media_type': r['media_type'] or 'text',
                'music_path': music_path,
                'created_at': str(r['created_at'] or ''),
                'username': r['username'],
                'full_name': r['full_name'] if 'full_name' in r.keys() else None,
                'profile_pic': r['profile_pic'] or '',
                'views_count': views_count,
                'my_reaction': my_reaction,
                'recent_reactions': recent_reactions,
                'is_own': is_own,
            })
        try:
            conn.commit()
        except Exception:
            pass
        conn.close()
        return jsonify({'success': True, 'items': items, 'owner_id': user_id})

    @app.route('/status/view/<int:status_id>', methods=['POST'])
    @login_required
    def status_view(status_id):
        """Rekodi kwamba user ametazama status."""
        me = int(session['user_id'])
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, user_id FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        if int(row['user_id']) == me:
            count = count_status_views(conn, status_id)
            conn.close()
            return jsonify({'success': True, 'own': True, 'views_count': count})
        ok = record_status_view(conn, status_id, me)
        try:
            conn.commit()
        except Exception:
            pass
        count = count_status_views(conn, status_id)
        conn.close()
        return jsonify({'success': True, 'recorded': ok, 'views_count': count})

    @app.route('/status/viewers/<int:status_id>')
    @login_required
    def status_viewers(status_id):
        """Orodha ya waliotazama — mmiliki wa status tu."""
        me = int(session['user_id'])
        conn = get_db_connection()
        owner = conn.execute(
            'SELECT user_id FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        if not owner or int(owner['user_id']) != me:
            conn.close()
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        rows = []
        try:
            rows = conn.execute(
                '''
                SELECT u.id, u.username, u.full_name, u.profile_pic, v.viewed_at,
                       r.reaction
                FROM status_views v
                JOIN users u ON u.id = v.viewer_id
                LEFT JOIN status_reactions r ON r.status_id = v.status_id AND r.user_id = v.viewer_id
                WHERE v.status_id = ?
                ORDER BY v.viewed_at DESC
                LIMIT 200
                ''',
                (status_id,)
            ).fetchall()
        except Exception:
            # Fallback schema ya zamani (user_id badala ya viewer_id)
            try:
                rows = conn.execute(
                    '''
                    SELECT u.id, u.username, u.full_name, u.profile_pic, v.viewed_at,
                           r.reaction
                    FROM status_views v
                    JOIN users u ON u.id = v.user_id
                    LEFT JOIN status_reactions r ON r.status_id = v.status_id AND r.user_id = v.user_id
                    WHERE v.status_id = ?
                    ORDER BY v.viewed_at DESC
                    LIMIT 200
                    ''',
                    (status_id,)
                ).fetchall()
            except Exception:
                rows = []

        viewers = []
        for r in rows:
            viewers.append({
                'id': r['id'],
                'username': r['username'],
                'full_name': r['full_name'] if 'full_name' in r.keys() else None,
                'profile_pic': r['profile_pic'] or '',
                'viewed_at': str(r['viewed_at'] or ''),
                'reaction': r['reaction'] if 'reaction' in r.keys() else None,
            })
        conn.close()
        return jsonify({'success': True, 'viewers': viewers, 'count': len(viewers)})

    @app.route('/status/react/<int:status_id>', methods=['POST'])
    @login_required
    def status_react(status_id):
        """Weka / ondoa reaction kwenye status."""
        me = session['user_id']
        data = request.get_json(silent=True) or {}
        reaction = (data.get('reaction') or request.form.get('reaction') or '').strip()
        allowed = {'❤️', '😂', '😮', '😢', '🔥', '👍', '👏', '🙏'}
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, user_id FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        if row['user_id'] == me:
            conn.close()
            return jsonify({'success': False, 'error': 'Huwezi kujireact status yako'}), 400
        if not reaction or reaction not in allowed:
            # clear reaction
            conn.execute(
                'DELETE FROM status_reactions WHERE status_id = ? AND user_id = ?',
                (status_id, me)
            )
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'reaction': None})
        conn.execute(
            '''
            INSERT INTO status_reactions (status_id, user_id, reaction, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(status_id, user_id) DO UPDATE SET reaction = excluded.reaction, created_at = excluded.created_at
            ''',
            (status_id, me, reaction, now_tz())
        )
        # also count as view
        record_status_view(conn, status_id, me)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'reaction': reaction})

    @app.route('/status/reply/<int:status_id>', methods=['POST'])
    @login_required
    def status_reply(status_id):
        """Jibu status → tuma private message kwa mmiliki (status inaonekana kwenye chat)."""
        me = session['user_id']
        data = request.get_json(silent=True) or {}
        text = (data.get('message') or request.form.get('message') or '').strip()
        if not text:
            return jsonify({'success': False, 'error': 'Andika ujumbe'}), 400
        if len(text) > 1000:
            text = text[:1000]
        conn = get_db_connection()
        row = conn.execute(
            '''
            SELECT s.id, s.user_id, s.content, s.media_type, s.file_path, u.username
            FROM statuses s JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            ''',
            (status_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False}), 404
        if row['user_id'] == me:
            conn.close()
            return jsonify({'success': False, 'error': 'Huwezi kujijibu status yako'}), 400

        me_row = conn.execute(
            'SELECT username FROM users WHERE id = ?', (me,)
        ).fetchone()
        me_username = me_row['username'] if me_row else 'user'

        preview = (row['content'] or '').strip()
        st_media = (row['media_type'] or 'text').strip() or 'text'
        st_file = (row['file_path'] or '').strip()
        if not preview:
            if st_media == 'image':
                preview = '[Picha]'
            elif st_media == 'video':
                preview = '[Video]'
            else:
                preview = f"[{st_media or 'status'}]"
        else:
            preview = preview[:120]

        # Formati: [[STATUS_REPLY:status_id:replier:media_type:file_path]]
        safe_file = st_file.replace(']', '').replace('[', '')
        msg = (
            f"[[STATUS_REPLY:{status_id}:{me_username}:{st_media}:{safe_file}]]\n"
            f"↩️ Status ya @{row['username']}: {preview}\n\n"
            f"{text}"
        )

        # Hifadhi media ya status kwenye private_messages ili ionekane kwenye chat
        try:
            conn.execute(
                """
                INSERT INTO private_messages
                    (sender_id, receiver_id, message, is_read, is_delivered, created_at,
                     status_id, media_type, file_path)
                VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?)
                """,
                (me, row['user_id'], msg, now_tz(), status_id,
                 'status_reply',
                 st_file if st_file else None)
            )
        except sqlite3.OperationalError:
            try:
                conn.execute(
                    """
                    INSERT INTO private_messages
                        (sender_id, receiver_id, message, is_read, is_delivered, created_at, status_id, media_type)
                    VALUES (?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (me, row['user_id'], msg, now_tz(), status_id,
                     'status_reply')
                )
            except sqlite3.OperationalError:
                conn.execute(
                    """
                    INSERT INTO private_messages
                        (sender_id, receiver_id, message, is_read, is_delivered, created_at)
                    VALUES (?, ?, ?, 0, 0, ?)
                    """,
                    (me, row['user_id'], msg, now_tz())
                )

        record_status_view(conn, status_id, me)

        try:
            create_notification(
                row['user_id'], me, 'message',
                f'@{me_username} amejibu status yako',
                url=f'/chat/{me_username}'
            )
        except Exception:
            pass

        conn.commit()
        owner_username = row['username']
        conn.close()
        return jsonify({
            'success': True,
            'username': owner_username,
            'replier': me_username,
            'status_id': status_id,
            'media_type': st_media,
            'file_path': st_file or None
        })

    @app.route('/status/meta/<int:status_id>')
    @login_required
    def status_meta(status_id):
        """Rudi user_id wa mmiliki wa status — kwa kufungua status kutoka chat."""
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, user_id FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False}), 404
        return jsonify({
            'success': True,
            'status_id': row['id'],
            'user_id': row['user_id']
        })

    @app.route('/status/delete/<int:status_id>', methods=['POST'])
    @login_required
    def status_delete(status_id):
        """Mmiliki anafuta status yake."""
        conn = get_db_connection()
        row = conn.execute(
            'SELECT user_id, file_path FROM statuses WHERE id = ?', (status_id,)
        ).fetchone()
        if not row:
            conn.close()
            # Tayari haipo — chukulia kama imefutwa (epuka "Huna ruhusa")
            return jsonify({'success': True, 'already_deleted': True})
        if int(row['user_id']) != int(session['user_id']):
            conn.close()
            return jsonify({'success': False, 'error': 'Huna ruhusa'}), 403
        file_path = row['file_path']
        try:
            conn.execute('DELETE FROM status_views WHERE status_id = ?', (status_id,))
        except Exception:
            pass
        try:
            conn.execute('DELETE FROM status_reactions WHERE status_id = ?', (status_id,))
        except Exception:
            pass
        conn.execute('DELETE FROM statuses WHERE id = ?', (status_id,))
        conn.commit()
        # Hakikisha imefutwa
        still = conn.execute('SELECT id FROM statuses WHERE id = ?', (status_id,)).fetchone()
        conn.close()
        if still:
            return jsonify({'success': False, 'error': 'Status haijafutika'}), 500
        if file_path:
            try:
                full = os.path.join(UPLOAD_FOLDER, file_path)
                if os.path.isfile(full):
                    os.remove(full)
            except Exception as e:
                print('[status_delete] file remove:', e)
        return jsonify({'success': True})




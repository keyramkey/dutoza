"""
profile.py — Profile view, edit, follow, search, block, QR, badge request
"""
from urllib.parse import quote
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
    get_user_restriction_status, register_nsfw_violation, flag_post_for_admin,
    record_status_view, count_status_views, can_view_status, send_web_push,
    VAPID_PUBLIC_KEY, generate_profile_qr_card, get_profile_public_url
)



def register_profile_routes(app):
    # ====================== PROFILE / FOLLOW / SEARCH ======================

    @app.route('/profile/<username>')
    def profile(username):
        # Public access (QR scan). Features kamili bado zinahitaji login kwa actions.
        is_logged_in = 'user_id' in session
        current_user_id = session.get('user_id')

        conn = get_db_connection()
        cursor = conn.cursor()

        profile_user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not profile_user:
            # Labda ni username ya Linkup → elekeza kwenye ukurasa wa Linkup
            try:
                lu = get_linkup_by_username(conn, username)
                if lu:
                    conn.close()
                    return redirect(url_for('linkup_public', username=username))
            except Exception:
                pass
            conn.close()
            if is_logged_in:
                return redirect(url_for('home'))
            flash('Akaunti haipatikani.', 'warning')
            return redirect(url_for('login'))

        profile_user_id = profile_user['id']

        # Facebook Page style: ukiwa switched kwenye Linkup, "Profile" yako = Linkup page
        if is_logged_in and current_user_id and int(current_user_id) == int(profile_user_id):
            try:
                active_lu = resolve_active_linkup(conn, current_user_id)
                if active_lu:
                    uname_lu = active_lu.get('username')
                    conn.close()
                    return redirect(url_for('linkup_public', username=uname_lu))
            except Exception as e:
                print('[profile] active linkup redirect:', e)

        # Akaunti iliyodeactivate haionekani kwa watu wengine (admin anaona)
        try:
            is_deact = int(profile_user['is_deactivated'] or 0) if 'is_deactivated' in profile_user.keys() else 0
        except Exception:
            is_deact = 0
        is_admin = bool(session.get('is_admin'))
        if is_deact and profile_user_id != current_user_id and not is_admin:
            conn.close()
            flash('Akaunti hii haipatikani kwa sasa.')
            return redirect(url_for('home') if is_logged_in else url_for('login'))


        # Profile: public = approved only. Mmiliki anaona approved pekee pia
        # (rejected/manual_review HAITIONEKANI kama "imechapishwa")
        posts_count = cursor.execute(
            """SELECT COUNT(*) FROM posts
               WHERE user_id = ?
                 AND COALESCE(is_draft, 0) = 0
                 AND COALESCE(moderation_status, 'approved') = 'approved'
                 AND linkup_id IS NULL""",
            (profile_user_id,)
        ).fetchone()[0]


        try:
            followers_count = cursor.execute(
                'SELECT COUNT(*) FROM follows WHERE following_id = ?', (profile_user_id,)
            ).fetchone()[0]
            following_count = cursor.execute(
                'SELECT COUNT(*) FROM follows WHERE follower_id = ?', (profile_user_id,)
            ).fetchone()[0]
            friends_count = cursor.execute(
                "SELECT COUNT(*) FROM follows f1 "
                "WHERE f1.follower_id = ? AND EXISTS ("
                "  SELECT 1 FROM follows f2 "
                "  WHERE f2.follower_id = f1.following_id "
                "    AND f2.following_id = f1.follower_id"
                ")",
                (profile_user_id,)
            ).fetchone()[0]
        except Exception:
            followers_count = 0
            following_count = 0
            friends_count = 0

        is_following = False
        if current_user_id and current_user_id != profile_user_id:
            follow_check = cursor.execute(
                'SELECT * FROM follows WHERE follower_id = ? AND following_id = ?',
                (current_user_id, profile_user_id)
            ).fetchone()
            if follow_check:
                is_following = True


        profile_user_dict = dict(profile_user)
        profile_user_dict['followers_count'] = followers_count
        profile_user_dict['following_count'] = following_count
        profile_user_dict['friends_count'] = friends_count
        profile_user_dict['village_mode'] = bool(profile_user['village_mode']) if 'village_mode' in profile_user.keys() else False

        # Posts — approved tu (hata mmiliki: rejected haionekani kama imechapishwa)
        _viewer_id = current_user_id if current_user_id else 0
        cursor.execute("""
            SELECT posts.*, users.username, users.full_name, users.role, users.profile_pic, users.is_verified,
                   (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS likes_count,
                   (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id AND likes.user_id = ?) AS user_liked,
                   (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comments_count,
                   COALESCE(posts.shares, 0) AS shares,
                   (SELECT COUNT(*) FROM saved_posts WHERE saved_posts.post_id = posts.id) AS saved_count
            FROM posts
            JOIN users ON posts.user_id = users.id
            WHERE posts.user_id = ?
              AND COALESCE(posts.is_draft, 0) = 0
              AND COALESCE(posts.moderation_status, 'approved') = 'approved'
              AND posts.linkup_id IS NULL
            ORDER BY posts.id DESC
        """, (_viewer_id, profile_user_id))

        posts = [dict(row) for row in cursor.fetchall()]


        # ===== COMMENTS — structure SAWA na feed =====
        def load_comments_for_posts(posts_list):
            for post in posts_list:
                cursor.execute("""
                    SELECT comments.*, users.username, users.full_name, users.profile_pic, users.is_verified,
                           (SELECT COUNT(*) FROM comment_likes WHERE comment_likes.comment_id = comments.id) AS comment_likes_count,
                           (SELECT COUNT(*) FROM comment_likes WHERE comment_likes.comment_id = comments.id AND comment_likes.user_id = ?) AS user_comment_liked
                    FROM comments
                    JOIN users ON comments.user_id = users.id
                    WHERE comments.post_id = ?
                    ORDER BY comments.id ASC
                """, (_viewer_id, post['id']))
                post['comments'] = [dict(c) for c in cursor.fetchall()]
                # hakikisha parent_id ipo (NULL = comment kuu)
                for c in post['comments']:
                    if 'parent_id' not in c or c['parent_id'] is None:
                        c['parent_id'] = None
                    if 'is_hidden' not in c or c['is_hidden'] is None:
                        c['is_hidden'] = 0

        load_comments_for_posts(posts)

        try:
            enrich_posts_with_linkup(conn, posts)
        except Exception as e:
            print('[profile] enrich linkup:', e)
        try:
            attach_original_posts(conn, posts)
        except Exception as e:
            print('[profile] attach orig:', e)

        # ===== SAVED POSTS =====
        saved_posts = []
        if profile_user_id == current_user_id:
            cursor.execute("""
                SELECT posts.*, users.username, users.full_name, users.role, users.profile_pic, users.is_verified,
                       (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS likes_count,
                       (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id AND likes.user_id = ?) AS user_liked,
                       (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comments_count,
                       COALESCE(posts.shares, 0) AS shares,
                       (SELECT COUNT(*) FROM saved_posts WHERE saved_posts.post_id = posts.id) AS saved_count
                FROM saved_posts
                JOIN posts ON saved_posts.post_id = posts.id
                JOIN users ON posts.user_id = users.id
                WHERE saved_posts.user_id = ?
                ORDER BY saved_posts.id DESC
            """, (current_user_id, current_user_id))
            saved_posts = [dict(row) for row in cursor.fetchall()]
            load_comments_for_posts(saved_posts)
            try:
                enrich_posts_with_linkup(conn, saved_posts)
            except Exception as e:
                print('[profile] saved linkup:', e)
            try:
                attach_original_posts(conn, saved_posts)
            except Exception as e:
                print('[profile] saved attach orig:', e)

        # ===== LINKUPS ZA USER HUU (onyesha kwenye profile) =====
        user_linkups = []
        try:
            rows = cursor.execute('''
                SELECT l.*,
                       (SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count,
                       (SELECT COUNT(*) FROM posts WHERE linkup_id = l.id
                          AND COALESCE(is_draft,0)=0
                          AND COALESCE(moderation_status,'approved')='approved') AS posts_count
                FROM linkups l
                WHERE l.owner_user_id = ?
                  AND COALESCE(l.is_active, 1) = 1
                ORDER BY l.id DESC
            ''', (profile_user_id,)).fetchall()
            user_linkups = [dict(r) for r in rows]
        except Exception as e:
            print('[profile] linkups:', e)
            user_linkups = []

        conn.close()

        is_own_profile = (profile_user_id == current_user_id)

        follows_you = False
        is_friend = False
        if current_user_id and not is_own_profile:
            try:
                conn2 = get_db_connection()
                follows_you = conn2.execute(
                    'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
                    (profile_user_id, current_user_id)
                ).fetchone() is not None
                is_friend = bool(is_following and follows_you)
                conn2.close()
            except Exception:
                pass

        profile_share_url = get_profile_public_url(username)
        return render_template(
            'profile/profile.html',
            profile_user=profile_user_dict,
            posts=posts,
            posts_data=posts,
            saved_posts=saved_posts,
            is_own_profile=is_own_profile,
            posts_count=posts_count,
            is_following=is_following,
            follows_you=follows_you,
            is_friend=is_friend,
            profile_share_url=profile_share_url,
            user_linkups=user_linkups,
        )


    @app.route('/profile/<username>/qr.png')
    def profile_qr_png(username):
        """QR card PNG — download au <img src>. Public."""
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if not user:
            return 'User not found', 404
        try:
            is_deact = int(user['is_deactivated'] or 0) if 'is_deactivated' in user.keys() else 0
            if is_deact:
                return 'Account unavailable', 404
        except Exception:
            pass

        try:
            card = generate_profile_qr_card(user)
        except Exception as e:
            print('[qr] generate error:', e)
            return (
                'QR generation failed. Run: pip install "qrcode[pil]" Pillow',
                500,
            )

        buf = BytesIO()
        card.save(buf, format='PNG', optimize=True)
        buf.seek(0)
        download = request.args.get('download') in ('1', 'true', 'yes')
        return send_file(
            buf,
            mimetype='image/png',
            as_attachment=download,
            download_name=f'kijiji-{username}-qr.png',
        )


    @app.route('/profile/<username>/share')
    def profile_share_page(username):
        """Ukurasa wa share + QR preview + WhatsApp. Public."""
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if not user:
            flash('Akaunti haipatikani.', 'warning')
            return redirect(url_for('home') if 'user_id' in session else url_for('login'))

        share_url = get_profile_public_url(username)
        wa_text = quote(f"Angalia profile yangu kwenye Kijiji Tanzania: {share_url}")
        wa_link = f"https://wa.me/?text={wa_text}"
        qr_img_url = url_for('profile_qr_png', username=username)
        qr_download_url = url_for('profile_qr_png', username=username, download=1)
        is_own = ('user_id' in session and session['user_id'] == user['id'])

        try:
            return render_template(
                'profile/profile_share.html',
                profile_user=dict(user),
                share_url=share_url,
                wa_link=wa_link,
                qr_img_url=qr_img_url,
                qr_download_url=qr_download_url,
                is_own=is_own,
            )
        except Exception:
            name = user['full_name'] or user['username']
            html = f"""<!DOCTYPE html><html lang="sw"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Share @{user['username']} - Kijiji</title>
    <style>
    body{{font-family:system-ui,sans-serif;background:#f0f2f5;margin:0;padding:20px;text-align:center;color:#0f172a}}
    .card{{max-width:400px;margin:0 auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 4px 20px rgba(0,0,0,.06)}}
    img.qr{{max-width:100%;border-radius:12px}}
    a.btn,button.btn{{display:inline-block;margin:8px 4px;padding:12px 18px;border-radius:12px;text-decoration:none;font-weight:700;color:#fff;border:none;cursor:pointer;font-size:14px}}
    .green{{background:linear-gradient(135deg,#1EB53A,#15803d)}}
    .wa{{background:#25D366}}
    .gray{{background:#e2e8f0;color:#0f172a}}
    input{{width:100%;padding:10px;border:1px solid #e2e8f0;border-radius:10px;margin:12px 0;box-sizing:border-box}}
    </style></head><body>
    <div class="card">
      <h2>{name}</h2>
      <p>@{user['username']}</p>
      <img class="qr" src="{qr_img_url}" alt="QR Code">
      <input type="text" readonly value="{share_url}" id="shareUrl" onclick="this.select()">
      <div>
        <a class="btn green" href="{qr_download_url}">Pakua QR</a>
        <a class="btn wa" href="{wa_link}" target="_blank" rel="noopener">WhatsApp</a>
        <button type="button" class="btn gray" onclick="navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('shareUrl').value)">Nakili Link</button>
        <a class="btn gray" href="{share_url}">Fungua Profile</a>
      </div>
    </div>
    </body></html>"""
            return html


    @app.route('/edit_profile', methods=['GET', 'POST'])
    def edit_profile():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        user_id = session['user_id']
        conn = get_db_connection()


        # Hakikisha columns muhimu zipo (DB ya zamani inaweza kukosa)
        for col, typ in [
            ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
            ('full_name', 'TEXT'),
            ('birthday', 'TEXT'),
            ('join_year', 'TEXT'),
            ('sex', 'TEXT'),
            ('marital_status', 'TEXT'),
            ('cover_photo', 'TEXT'),
            ('profile_category', 'TEXT'),
            ('website', 'TEXT'),
            ('facebook', 'TEXT'),
            ('instagram', 'TEXT'),
            ('tiktok', 'TEXT'),
            ('youtube', 'TEXT'),
            ('whatsapp', 'TEXT'),
            ('telegram', 'TEXT'),
            ('x', 'TEXT'),
            ('linkedin', 'TEXT'),
            ('snapchat', 'TEXT'),
            ('pinterest', 'TEXT'),
            ('reddit', 'TEXT'),
            ('discord', 'TEXT'),
            ('github', 'TEXT'),
            ('twitch', 'TEXT'),
            ('spotify', 'TEXT'),
            ('threads', 'TEXT'),
            ('tumblr', 'TEXT'),
            ('vimeo', 'TEXT'),
            ('wordpress', 'TEXT'),
            ('medium', 'TEXT'),
            ('blogger', 'TEXT'),
        ]:
            try:
                conn.execute(f'ALTER TABLE users ADD COLUMN {col} {typ}')
                conn.commit()
            except Exception:
                pass

        if request.method == 'POST':
            try:
                bio = request.form.get('bio', '').strip()
                birthday = request.form.get('birthday', '').strip()
                join_year = request.form.get('join_year', '').strip()
                profile_category = request.form.get('profile_category', '').strip()
                sex = request.form.get('sex', '').strip()
                marital_status = request.form.get('marital_status', '').strip()
                full_name = (request.form.get('full_name') or '').strip()
                if len(full_name) > 80:
                    full_name = full_name[:80]

                website = request.form.get('website', '').strip()
                facebook = request.form.get('facebook', '').strip()
                instagram = request.form.get('instagram', '').strip()
                tiktok = request.form.get('tiktok', '').strip()
                youtube = request.form.get('youtube', '').strip()
                whatsapp = request.form.get('whatsapp', '').strip()
                telegram = request.form.get('telegram', '').strip()
                x = request.form.get('x', '').strip()
                linkedin = request.form.get('linkedin', '').strip()
                snapchat = request.form.get('snapchat', '').strip()
                pinterest = request.form.get('pinterest', '').strip()
                reddit = request.form.get('reddit', '').strip()
                discord = request.form.get('discord', '').strip()
                github = request.form.get('github', '').strip()
                twitch = request.form.get('twitch', '').strip()
                spotify = request.form.get('spotify', '').strip()
                threads = request.form.get('threads', '').strip()
                tumblr = request.form.get('tumblr', '').strip()
                vimeo = request.form.get('vimeo', '').strip()
                wordpress = request.form.get('wordpress', '').strip()
                medium = request.form.get('medium', '').strip()
                blogger = request.form.get('blogger', '').strip()

                profile_pic = None
                file = request.files.get('profile_pic')
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    unique_filename = f"profile_{user_id}_{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                    profile_pic = unique_filename

                cover_photo = None
                cover_file = request.files.get('cover_photo')
                if cover_file and cover_file.filename and allowed_file(cover_file.filename):
                    cover_filename = secure_filename(cover_file.filename)
                    unique_cover = f"cover_{user_id}_{int(time.time())}_{cover_filename}"
                    cover_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_cover))
                    cover_photo = unique_cover

                # Join year: jaribu created_at (kama ipo), vinginevyo form value
                auto_join_year = ''
                try:
                    row_created = conn.execute(
                        'SELECT created_at FROM users WHERE id = ?', (user_id,)
                    ).fetchone()
                    if row_created and row_created['created_at']:
                        auto_join_year = str(row_created['created_at'])[:4]
                except Exception:
                    pass
                final_join_year = auto_join_year or join_year or None

                updates = {
                    'bio': bio or None,
                    'birthday': birthday or None,
                    'join_year': final_join_year,
                    'sex': sex or None,
                    'marital_status': marital_status or None,
                    'full_name': full_name or None,
                    'profile_category': profile_category or None,
                    'website': website or None,
                    'facebook': facebook or None,
                    'instagram': instagram or None,
                    'tiktok': tiktok or None,
                    'youtube': youtube or None,
                    'whatsapp': whatsapp or None,
                    'telegram': telegram or None,
                    'x': x or None,
                    'linkedin': linkedin or None,
                    'snapchat': snapchat or None,
                    'pinterest': pinterest or None,
                    'reddit': reddit or None,
                    'discord': discord or None,
                    'github': github or None,
                    'twitch': twitch or None,
                    'spotify': spotify or None,
                    'threads': threads or None,
                    'tumblr': tumblr or None,
                    'vimeo': vimeo or None,
                    'wordpress': wordpress or None,
                    'medium': medium or None,
                    'blogger': blogger or None,
                }
                if profile_pic:
                    updates['profile_pic'] = profile_pic
                if cover_photo:
                    updates['cover_photo'] = cover_photo

                set_clause = ', '.join(f'{k}=?' for k in updates.keys())
                values = list(updates.values()) + [user_id]
                conn.execute(f'UPDATE users SET {set_clause} WHERE id=?', values)
                conn.commit()

                flash('Profile yako imesasishwa kwa mafanikio!')
                user_row = conn.execute(
                    'SELECT username FROM users WHERE id = ?', (user_id,)
                ).fetchone()
                conn.close()
                if user_row:
                    return redirect(url_for('profile', username=user_row['username']))
                return redirect(url_for('home'))

            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                print('[edit_profile] ERROR:', e)
                import traceback
                traceback.print_exc()
                flash('Kuna tatizo limetokea wakati wa kuhifadhi. Jaribu tena.')
                return redirect(url_for('edit_profile'))

        user = conn.execute(
            'SELECT * FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        conn.close()
        return render_template('profile/edit_profile.html', user=user)



    @app.route('/search')
    def search():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        query = (request.args.get('q') or '').strip()
        q_tag = query.lstrip('#').strip()
        people = []
        posts = []
        tags = []
        linkups = []
        me = session['user_id']
        conn = get_db_connection()

        if query:
            like = '%' + query + '%'
            try:
                people = [dict(r) for r in conn.execute(
                    "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, u.bio, "
                    "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following "
                    "FROM users u "
                    "WHERE (u.username LIKE ? COLLATE NOCASE OR u.full_name LIKE ? COLLATE NOCASE) "
                    "AND u.id != ? "
                    "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
                    "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
                    "ORDER BY COALESCE(u.is_verified, 0) DESC, u.username COLLATE NOCASE "
                    "LIMIT 30",
                    (me, like, like, me)
                ).fetchall()]
            except Exception:
                people = [dict(r) for r in conn.execute(
                    "SELECT id, username, profile_pic, is_verified, 0 AS is_following "
                    "FROM users WHERE username LIKE ? COLLATE NOCASE AND id != ? LIMIT 30",
                    (like, me)
                ).fetchall()]

            try:
                posts = [dict(r) for r in conn.execute(
                    "SELECT p.id, p.content, p.file_path, p.media_type, p.created_at, p.user_id, "
                    "u.username, u.full_name, u.profile_pic, u.is_verified, "
                    "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
                    "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count "
                    "FROM posts p "
                    "JOIN users u ON u.id = p.user_id "
                    "WHERE COALESCE(p.is_draft, 0) = 0 "
                    "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
                    "AND p.content LIKE ? "
                    "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
                    "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
                    "ORDER BY p.id DESC "
                    "LIMIT 20",
                    (like,)
                ).fetchall()]
            except Exception:
                posts = []

            try:
                tag_like = '%' + q_tag + '%'
                tags = [dict(r) for r in conn.execute(
                    "SELECT tag, use_count FROM hashtags "
                    "WHERE tag LIKE ? COLLATE NOCASE "
                    "ORDER BY use_count DESC, tag COLLATE NOCASE "
                    "LIMIT 30",
                    (tag_like,)
                ).fetchall()]
            except Exception:
                tags = []

            try:
                lu_like = '%' + query + '%'
                linkups = [dict(r) for r in conn.execute(
                    "SELECT l.id, l.username, l.display_name, l.category, l.bio, "
                    "l.profile_pic, l.is_verified, "
                    "(SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count "
                    "FROM linkups l "
                    "WHERE (l.username LIKE ? COLLATE NOCASE OR l.display_name LIKE ? COLLATE NOCASE) "
                    "AND COALESCE(l.is_active, 1) = 1 "
                    "ORDER BY COALESCE(l.is_verified, 0) DESC, followers_count DESC "
                    "LIMIT 30",
                    (lu_like, lu_like)
                ).fetchall()]
            except Exception:
                linkups = []
        else:
            try:
                people = [dict(r) for r in conn.execute(
                    "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, u.bio, "
                    "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following "
                    "FROM users u "
                    "WHERE u.id != ? "
                    "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
                    "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
                    "ORDER BY COALESCE(u.is_verified, 0) DESC, u.username COLLATE NOCASE "
                    "LIMIT 30",
                    (me, me)
                ).fetchall()]
            except Exception:
                people = [dict(r) for r in conn.execute(
                    "SELECT id, username, profile_pic, is_verified, 0 AS is_following "
                    "FROM users WHERE id != ? LIMIT 30",
                    (me,)
                ).fetchall()]

        conn.close()
        return render_template(
            'profile/search.html',
            query=query,
            people=people,
            posts=posts,
            tags=tags,
            linkups=linkups,
            results=people,
            has_more_people=len(people) >= 30,
            has_more_posts=len(posts) >= 20,
            has_more_linkups=len(linkups) >= 30,
        )


    @app.route('/api/search')
    @login_required
    def api_search():
        """Infinite scroll / AJAX search: people, posts, hashtags."""
        if 'user_id' not in session:
            return jsonify({'success': False}), 401

        query = (request.args.get('q') or '').strip()
        q_tag = query.lstrip('#').strip()
        kind = (request.args.get('kind') or 'people').strip().lower()
        cursor = request.args.get('cursor', 0, type=int)
        limit = min(request.args.get('limit', 20, type=int), 40)
        me = session['user_id']
        conn = get_db_connection()
        items = []
        next_cursor = None

        try:
            if kind == 'people':
                like = '%' + query + '%' if query else '%'
                sql = (
                    "SELECT u.id, u.username, u.full_name, u.profile_pic, u.is_verified, "
                    "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following "
                    "FROM users u "
                    "WHERE u.id != ? "
                    "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
                    "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
                )
                params = [me, me]
                if query:
                    sql += "AND (u.username LIKE ? COLLATE NOCASE OR u.full_name LIKE ? COLLATE NOCASE) "
                    params.extend([like, like])
                if cursor > 0:
                    sql += "AND u.id > ? "
                    params.append(cursor)
                sql += "ORDER BY u.id ASC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    items.append({
                        'id': r['id'],
                        'username': r['username'],
                        'full_name': r['full_name'] if 'full_name' in r.keys() else None,
                        'profile_pic': r['profile_pic'],
                        'is_verified': r['is_verified'] or 0,
                        'is_following': bool(r['is_following']),
                    })
                if items:
                    next_cursor = items[-1]['id']

            elif kind == 'posts' and query:
                like = '%' + query + '%'
                sql = (
                    "SELECT p.id, p.content, p.file_path, p.media_type, p.created_at, p.user_id, "
                    "u.username, u.full_name, u.profile_pic, u.is_verified, "
                    "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
                    "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count "
                    "FROM posts p JOIN users u ON u.id = p.user_id "
                    "WHERE COALESCE(p.is_draft, 0) = 0 "
                    "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
                    "AND p.content LIKE ? "
                    "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
                )
                params = [like]
                if cursor > 0:
                    sql += "AND p.id < ? "
                    params.append(cursor)
                sql += "ORDER BY p.id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    items.append({
                        'id': r['id'],
                        'content': r['content'],
                        'file_path': r['file_path'],
                        'media_type': r['media_type'],
                        'created_at': str(r['created_at'] or ''),
                        'username': r['username'],
                        'full_name': r['full_name'] if 'full_name' in r.keys() else None,
                        'profile_pic': r['profile_pic'],
                        'is_verified': r['is_verified'] or 0,
                        'likes_count': r['likes_count'] or 0,
                        'comments_count': r['comments_count'] or 0,
                    })
                if items:
                    next_cursor = items[-1]['id']

            elif kind == 'tags' and query:
                tag_like = '%' + q_tag + '%'
                rows = conn.execute(
                    "SELECT tag, use_count FROM hashtags "
                    "WHERE tag LIKE ? COLLATE NOCASE "
                    "ORDER BY use_count DESC LIMIT ?",
                    (tag_like, limit)
                ).fetchall()
                for r in rows:
                    items.append({'tag': r['tag'], 'use_count': r['use_count'] or 0})
            elif kind == 'linkup' and query:
                lu_like = '%' + query + '%'
                sql = (
                    "SELECT l.id, l.username, l.display_name, l.category, l.profile_pic, l.is_verified, "
                    "(SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count "
                    "FROM linkups l "
                    "WHERE (l.username LIKE ? COLLATE NOCASE OR l.display_name LIKE ? COLLATE NOCASE) "
                    "AND COALESCE(l.is_active, 1) = 1 "
                )
                params = [lu_like, lu_like]
                if cursor > 0:
                    sql += "AND l.id > ? "
                    params.append(cursor)
                sql += "ORDER BY l.id ASC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    items.append({
                        'id': r['id'],
                        'username': r['username'],
                        'display_name': r['display_name'],
                        'category': r['category'],
                        'profile_pic': r['profile_pic'],
                        'is_verified': r['is_verified'] or 0,
                        'followers_count': r['followers_count'] or 0,
                    })
                if items:
                    next_cursor = items[-1]['id']
        except Exception as e:
            print('[api_search]', e)
        finally:
            conn.close()

        return jsonify({
            'success': True,
            'kind': kind,
            'items': items,
            'next_cursor': next_cursor,
            'has_more': len(items) >= limit if kind != 'tags' else False,
        })

    @app.route('/toggle_follow/<int:user_id>', methods=['POST'])
    def toggle_follow(user_id):
        if 'user_id' not in session:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
                return jsonify({'success': False, 'message': 'Tafadhali ingia kwanza.'}), 401
            return redirect(url_for('login'))

        current_user_id = session['user_id']
        if current_user_id == user_id:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Huwezi kujifuata mwenyewe.'})
            return redirect(url_for('home'))

        conn = get_db_connection()
        target_user = conn.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
        if not target_user:
            conn.close()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Mtumiaji hajapatikana.'}), 404
            return redirect(url_for('home'))
        target_username = target_user['username']

        existing = conn.execute(
            'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
            (current_user_id, user_id)
        ).fetchone()

        wants_json = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in (request.headers.get('Accept') or '')
        )

        if existing:
            conn.execute(
                'DELETE FROM follows WHERE follower_id = ? AND following_id = ?',
                (current_user_id, user_id)
            )
            conn.commit()
            follows_you = conn.execute(
                'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
                (user_id, current_user_id)
            ).fetchone() is not None
            conn.close()
            payload = {
                'success': True,
                'following': False,
                'friends': False,
                'follows_you': follows_you,
                'status': 'follow_back' if follows_you else 'follow',
                'message': 'Umeacha kumfuata.',
            }
            if wants_json:
                return jsonify(payload)
            return redirect(url_for('profile', username=target_username))

        conn.execute(
            'INSERT INTO follows (follower_id, following_id) VALUES (?, ?)',
            (current_user_id, user_id)
        )
        conn.commit()

        # Je, wao wamekufollow? → friends (mutual)
        they_follow_me = conn.execute(
            'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
            (user_id, current_user_id)
        ).fetchone() is not None
        conn.close()

        my_username = session.get('username') or ''
        try:
            if they_follow_me:
                # Notification kwa yule uliyemfollow back
                create_notification(
                    user_id, current_user_id, 'follow_back',
                    'amekufollow back — sasa mmekuwa friends',
                    url=f'/profile/{my_username}'
                )
            else:
                create_notification(
                    user_id, current_user_id, 'follow',
                    'amekufollow',
                    url=f'/profile/{my_username}'
                )
        except Exception as e:
            print('[follow] notif error:', e)

        payload = {
            'success': True,
            'following': True,
            'friends': they_follow_me,
            'follows_you': they_follow_me,
            'status': 'friends' if they_follow_me else 'following',
            'message': 'Mmekuwa friends!' if they_follow_me else 'Umefanikiwa kumfuata.',
        }
        if wants_json:
            return jsonify(payload)
        return redirect(url_for('profile', username=target_username))


    @app.route('/follow/<target_username>', methods=['POST'])
    def follow_user(target_username):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Tafadhali ingia kwanza.'}), 401

        current_user_id = session['user_id']

        # source_post_id: kama Follow ilibonyezwa ndani ya post fulani, tunaihifadhi
        # ili Kijiji Mode Insights ionyeshe "Followers wapya kupitia post hii".
        source_post_id = None
        try:
            raw_source = request.form.get('source_post_id')
            if raw_source is None:
                json_body = request.get_json(silent=True) or {}
                raw_source = json_body.get('source_post_id')
            if raw_source not in (None, '', 'null'):
                source_post_id = int(raw_source)
        except (TypeError, ValueError):
            source_post_id = None

        conn = get_db_connection()

        target_user = conn.execute(
            "SELECT id, username FROM users WHERE username = ?",
            (target_username,)
        ).fetchone()

        if not target_user:
            conn.close()
            return jsonify({'success': False, 'message': 'Mtumiaji hajapatikana.'}), 404

        target_user_id = target_user['id']

        if current_user_id == target_user_id:
            conn.close()
            return jsonify({'success': False, 'message': 'Huwezi kujifuata mwenyewe.'})

        existing = conn.execute(
            'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
            (current_user_id, target_user_id)
        ).fetchone()

        if existing:
            conn.execute(
                'DELETE FROM follows WHERE follower_id = ? AND following_id = ?',
                (current_user_id, target_user_id)
            )
            conn.commit()
            follows_you = conn.execute(
                'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
                (target_user_id, current_user_id)
            ).fetchone() is not None
            conn.close()
            return jsonify({
                'success': True,
                'following': False,
                'friends': False,
                'follows_you': follows_you,
                'status': 'follow_back' if follows_you else 'follow',
                'message': 'Umeacha kumfuata.',
            })

        try:
            conn.execute(
                'INSERT INTO follows (follower_id, following_id, source_post_id) VALUES (?, ?, ?)',
                (current_user_id, target_user_id, source_post_id)
            )
            conn.commit()
        except Exception:
            conn.close()
            return jsonify({'success': True, 'following': True, 'message': 'Tayari unamfuata.'})

        they_follow_me = conn.execute(
            'SELECT id FROM follows WHERE follower_id = ? AND following_id = ?',
            (target_user_id, current_user_id)
        ).fetchone() is not None
        conn.close()

        my_username = session.get('username') or ''
        try:
            if they_follow_me:
                create_notification(
                    target_user_id, current_user_id, 'follow_back',
                    'amekufollow back — sasa mmekuwa friends',
                    url=f'/profile/{my_username}'
                )
            else:
                create_notification(
                    target_user_id, current_user_id, 'follow',
                    'amekufollow',
                    url=f'/profile/{my_username}'
                )
        except Exception as e:
            print('[follow] notif error:', e)

        return jsonify({
            'success': True,
            'following': True,
            'friends': they_follow_me,
            'follows_you': they_follow_me,
            'status': 'friends' if they_follow_me else 'following',
            'message': 'Mmekuwa friends!' if they_follow_me else 'Umefanikiwa kumfuata.',
        })


    @app.route('/user/<username>/followers')
    def followers_list(username):
        conn = get_db_connection()
        cursor = conn.cursor()
        user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            conn.close()
            flash("Mtumiaji hapatikani.")
            return redirect(url_for('home'))

        current_user_id = session.get('user_id', 0) or 0
        rows = cursor.execute(
            "SELECT u.id, u.username, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = u.id AND following_id = ?) AS follows_you "
            "FROM users u JOIN follows f ON f.follower_id = u.id "
            "WHERE f.following_id = ? ORDER BY u.username COLLATE NOCASE",
            (current_user_id, current_user_id, user['id'])
        ).fetchall()
        users = []
        for r in rows:
            is_following = bool(r['is_following'])
            follows_you = bool(r['follows_you'])
            users.append({
                'id': r['id'],
                'username': r['username'],
                'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'],
                'is_following': is_following,
                'follows_you': follows_you,
                'is_friend': is_following and follows_you,
            })
        conn.close()
        return render_template(
            'profile/follows_list.html',
            profile_user=user,
            users=users,
            title="Followers"
        )


    @app.route('/user/<username>/following')
    def following_list(username):
        conn = get_db_connection()
        cursor = conn.cursor()
        user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            conn.close()
            flash("Mtumiaji hapatikani.")
            return redirect(url_for('home'))

        current_user_id = session.get('user_id', 0) or 0
        rows = cursor.execute(
            "SELECT u.id, u.username, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = u.id AND following_id = ?) AS follows_you "
            "FROM users u JOIN follows f ON f.following_id = u.id "
            "WHERE f.follower_id = ? ORDER BY u.username COLLATE NOCASE",
            (current_user_id, current_user_id, user['id'])
        ).fetchall()
        users = []
        for r in rows:
            is_following = bool(r['is_following'])
            follows_you = bool(r['follows_you'])
            users.append({
                'id': r['id'],
                'username': r['username'],
                'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'],
                'is_following': is_following,
                'follows_you': follows_you,
                'is_friend': is_following and follows_you,
            })
        conn.close()
        return render_template(
            'profile/follows_list.html',
            profile_user=user,
            users=users,
            title="Following"
        )


    @app.route('/user/<username>/friends')
    def friends_list(username):
        conn = get_db_connection()
        cursor = conn.cursor()
        user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            conn.close()
            flash("Mtumiaji hapatikani.")
            return redirect(url_for('home'))

        current_user_id = session.get('user_id', 0) or 0
        rows = cursor.execute(
            "SELECT u.id, u.username, u.profile_pic, u.is_verified, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = ? AND following_id = u.id) AS is_following, "
            "(SELECT COUNT(*) FROM follows WHERE follower_id = u.id AND following_id = ?) AS follows_you "
            "FROM users u "
            "JOIN follows f1 ON f1.following_id = u.id AND f1.follower_id = ? "
            "JOIN follows f2 ON f2.follower_id = u.id AND f2.following_id = ? "
            "ORDER BY u.username COLLATE NOCASE",
            (current_user_id, current_user_id, user['id'], user['id'])
        ).fetchall()
        users = []
        for r in rows:
            is_following = bool(r['is_following'])
            follows_you = bool(r['follows_you'])
            users.append({
                'id': r['id'],
                'username': r['username'],
                'profile_pic': r['profile_pic'],
                'is_verified': r['is_verified'],
                'is_following': is_following,
                'follows_you': follows_you,
                'is_friend': is_following and follows_you,
            })
        conn.close()
        return render_template(
            'profile/follows_list.html',
            profile_user=user,
            users=users,
            title="Friends"
        )


    @app.route('/block_user/<int:user_id>', methods=['POST'])
    def block_user(user_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        blocker_id = session['user_id']

        if blocker_id == user_id:
            return jsonify({'success': False, 'message': 'Huwezi kujiblock mwenyewe'})

        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if already blocked
        existing = cursor.execute(
            'SELECT id FROM blocks WHERE blocker_id = ? AND blocked_id = ?',
            (blocker_id, user_id)
        ).fetchone()

        if existing:
            conn.close()
            return jsonify({'success': False, 'message': 'Umeshablok tayari'})

        cursor.execute(
            'INSERT INTO blocks (blocker_id, blocked_id) VALUES (?, ?)',
            (blocker_id, user_id)
        )
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'Mtumiaji ameblockiwa'})

    @app.route('/unblock_user/<int:user_id>', methods=['POST', 'GET'])
    @login_required
    def unblock_user(user_id):
        """Ondoa block kwa user."""
        me = session['user_id']
        conn = get_db_connection()
        conn.execute(
            'DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?',
            (me, user_id)
        )
        conn.commit()
        conn.close()
        if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': 'Block imeondolewa'})
        flash('Block imeondolewa', 'success')
        return redirect(request.referrer or url_for('settings'))


    # ====================== EDIT COMMENT ======================


    # ====================== NOTIFICATIONS / SETTINGS / HISTORY ======================

    @app.route('/notifications')
    def notifications():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        notifs = conn.execute('''
            SELECT notifications.*, users.username AS sender_username, users.profile_pic AS sender_pic, users.is_verified AS sender_verified
            FROM notifications
            JOIN users ON notifications.sender_id = users.id
            WHERE notifications.user_id = ?
            ORDER BY notifications.id DESC
        ''', (session['user_id'],)).fetchall()

        conn.execute('UPDATE notifications SET is_read = 1 WHERE user_id = ?', (session['user_id'],))
        conn.commit()
        conn.close()

        return render_template('profile/notifications.html', notifs=notifs)

    @app.route('/delete_notification/<int:notif_id>', methods=['POST'])
    def delete_notification(notif_id):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401

        conn = get_db_connection()
        notif = conn.execute(
            'SELECT id FROM notifications WHERE id = ? AND user_id = ?',
            (notif_id, session['user_id'])
        ).fetchone()

        if not notif:
            conn.close()
            return jsonify({'success': False, 'message': 'Notification haipatikani'}), 404

        conn.execute(
            'DELETE FROM notifications WHERE id = ? AND user_id = ?',
            (notif_id, session['user_id'])
        )
        conn.commit()
        conn.close()

        return jsonify({'success': True})


    # ====================== COMMUNITY ======================

    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        cursor = conn.cursor()

        user_check = cursor.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

        if user_check and user_check['is_blocked'] == 1:
            conn.close()
            session.clear()
            flash('Akaunti yako imezuiwa na Admin!')
            return redirect(url_for('login'))

        if request.method == 'POST':
            action = request.form.get('action')

            # ===== 1. Badilisha Taarifa (Username + Email) =====
            if action == 'change_info':
                new_username = request.form.get('new_username', '').strip()
                new_email = request.form.get('new_email', '').strip()

                if not new_username or not new_email:
                    flash('Tafadhali jaza sehemu zote!', 'danger')
                else:
                    # Angalia kama username au email tayari zinatumika
                    existing = cursor.execute(
                        'SELECT id FROM users WHERE (username = ? OR email = ?) AND id != ?',
                        (new_username, new_email, session['user_id'])
                    ).fetchone()

                    if existing:
                        flash('Username au Email hii tayari inatumika!', 'danger')
                    else:
                        cursor.execute(
                            'UPDATE users SET username = ?, email = ? WHERE id = ?',
                            (new_username, new_email, session['user_id'])
                        )
                        cursor.execute(
                            'INSERT INTO history (user_id, action_description) VALUES (?, ?)',
                            (session['user_id'], 'Umebadilisha taarifa zako (username na email)')
                        )
                        conn.commit()

                        # Sasisha session
                        session['username'] = new_username
                        flash('Taarifa zako zimesasishwa kwa mafanikio!', 'success')

            # ===== 2. Badilisha Password =====
            elif action == 'change_password':
                new_pass = request.form.get('new_password')
                if new_pass:
                    hashed_pw = generate_password_hash(new_pass)
                    cursor.execute(
                        'UPDATE users SET password_hash = ? WHERE id = ?',
                        (hashed_pw, session['user_id'])
                    )
                    cursor.execute(
                        'INSERT INTO history (user_id, action_description) VALUES (?, ?)',
                        (session['user_id'], 'Umebadilisha nenosiri (password) yako')
                    )
                    conn.commit()
                    flash('Nenosiri limebadilishwa kwa mafanikio!', 'success')

            # ===== 3. Mapendeleo ya Machapisho =====
            elif action == 'update_preferences':
                visibility = request.form.get('post_visibility')
                cursor.execute(
                    'UPDATE users SET post_visibility = ? WHERE id = ?',
                    (visibility, session['user_id'])
                )
                cursor.execute(
                    'INSERT INTO history (user_id, action_description) VALUES (?, ?)',
                    (session['user_id'], 'Umebadilisha mapendeleo ya machapisho')
                )
                conn.commit()
                flash('Mapendeleo yamewekwa sawa!', 'success')

            conn.close()
            return redirect(url_for('settings'))

        # ========== GET Request ==========
        # Pata blocked users
        blocked_users = cursor.execute('''
            SELECT u.id, u.username, u.email
            FROM blocks b
            JOIN users u ON b.blocked_id = u.id
            WHERE b.blocker_id = ?
        ''', (session['user_id'],)).fetchall()

        # Tambua Google account — TU auth_provider au bio (SI profile_pic)
        is_google = False
        if user_check:
            try:
                prov = (user_check['auth_provider'] if 'auth_provider' in user_check.keys() else '') or ''
                bio = (user_check['bio'] if 'bio' in user_check.keys() else '') or ''
                if prov == 'google' or bio == 'Joined with Google':
                    is_google = True
            except Exception:
                is_google = False

        conn.close()
        return render_template(
            'profile/settings.html',
            user=user_check,
            blocked_users=blocked_users,
            is_google=is_google
        )

    @app.route('/history', methods=['GET'])
    def activity_history():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM history WHERE user_id = ? ORDER BY id DESC', (session['user_id'],))
        history_data = [dict(row) for row in cursor.fetchall()]

        conn.close()
        return render_template('profile/history.html', history=history_data)

    @app.route('/history/clear', methods=['POST'])
    def clear_history():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('DELETE FROM history WHERE user_id = ?', (session['user_id'],))
        conn.commit()
        conn.close()

        flash('Historia yako yote imefutwa.', 'success')
        return redirect(url_for('activity_history'))

    @app.route('/delete_history/<int:history_id>')
    def delete_history(history_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        conn.execute('DELETE FROM history WHERE id = ? AND user_id = ?', (history_id, session['user_id']))
        conn.commit()
        conn.close()

        flash('Historia imefutwa!', 'success')
        return redirect(url_for('activity_history'))

    # Route ya Kufuta Post (Imekamilishwa)


    # ====================== CHAT / INBOX / CALLS ======================


    # Simple in-memory typing store (au tumia Redis/cache)
    typing_users = {}  # {user_id: timestamp}

    # Simple in-memory presence store - "amewahi kuonekana lini mara ya mwisho"
    # Inasasishwa kila mara mtumiaji anapo-poll typing-status kwenye ukurasa wa chat.
    online_users = {}  # {user_id: timestamp}
    ONLINE_TIMEOUT_SECONDS = 15  # kama hajapoll ndani ya sekunde hizi, si online tena


    @app.route('/chat/typing/<int:user_id>', methods=['POST'])
    @login_required
    def chat_typing(user_id):
        data = request.get_json() or {}
        online_users[session['user_id']] = time.time()
        if data.get('typing'):
            typing_users[session['user_id']] = time.time()
        else:
            typing_users.pop(session['user_id'], None)
        return jsonify(success=True)

    @app.route('/chat/typing-status/<int:user_id>')
    @login_required
    def get_typing_status(user_id):
        # Mtumiaji anayepoll yupo online sasa hivi
        online_users[session['user_id']] = time.time()

        ts = typing_users.get(user_id, 0)
        is_typing = (time.time() - ts) < 3  # expire baada ya sekunde 3

        last_seen_ts = online_users.get(user_id, 0)
        is_online = (time.time() - last_seen_ts) < ONLINE_TIMEOUT_SECONDS

        last_seen_str = None
        if last_seen_ts:
            last_seen_str = datetime.fromtimestamp(
                last_seen_ts, ZoneInfo("Africa/Dar_es_Salaam")
            ).strftime('%Y-%m-%d %H:%M:%S')

        return jsonify(
            typing=is_typing,
            online=is_online,
            last_seen=last_seen_str
        )

    @app.route('/chat/<username>')
    def chat(username):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = get_db_connection()
        receiver = conn.execute(
            'SELECT id, username, full_name, profile_pic, is_verified FROM users WHERE username = ?',
            (username,)
        ).fetchone()

        if not receiver:
            conn.close()
            flash('Mtumiaji hajapatikana')
            return redirect(url_for('home'))

        if receiver['id'] == session['user_id']:
            conn.close()
            flash('Huwezi kujichatia mwenyewe')
            return redirect(url_for('profile', username=username))

        me = session['user_id']
        other = receiver['id']

        # Delivered: ujumbe waliotumiwa mimi (receiver) — umefika
        conn.execute('''
            UPDATE private_messages SET is_delivered = 1
            WHERE sender_id = ? AND receiver_id = ? AND is_delivered = 0
        ''', (other, me))

        # Read: nimesoma
        conn.execute('''
            UPDATE private_messages SET is_read = 1, is_delivered = 1
            WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
        ''', (other, me))

        # Messages + reply quote (JOIN)
        rows = conn.execute('''
            SELECT
                pm.*,
                u.username AS sender_username,
                rpm.message AS reply_to_text_raw,
                rpm.media_type AS reply_to_media,
                ru.username AS reply_to_name
            FROM private_messages pm
            JOIN users u ON u.id = pm.sender_id
            LEFT JOIN private_messages rpm ON rpm.id = pm.reply_to_id
            LEFT JOIN users ru ON ru.id = rpm.sender_id
            WHERE
                (
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                    OR
                    (pm.sender_id = ? AND pm.receiver_id = ?)
                )
                AND NOT (
                    (pm.sender_id = ? AND COALESCE(pm.deleted_for_sender, 0) = 1)
                    OR
                    (pm.receiver_id = ? AND COALESCE(pm.deleted_for_receiver, 0) = 1)
                )
            ORDER BY pm.id ASC
        ''', (me, other, other, me, me, me)).fetchall()

        conn.commit()
        conn.close()

        # Badilisha ziwe dict + reply fields kwa template
        messages = []
        for r in rows:
            m = dict(r)

            if m.get('created_at') is not None:
                m['created_at'] = str(m['created_at'])

            if m.get('reply_to_id'):
                if m.get('reply_to_text_raw'):
                    m['reply_to_text'] = (m['reply_to_text_raw'] or '')[:120]
                else:
                    m['reply_to_text'] = (m.get('reply_to_media') or 'media').upper()
                m['reply_to_name'] = m.get('reply_to_name') or 'Ujumbe'
            else:
                m['reply_to_text'] = None
                m['reply_to_name'] = None

            messages.append(m)

        return render_template('profile/chat.html', receiver=receiver, messages=messages)


    # ============================================================


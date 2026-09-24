"""
linkup.py — Linkup create/switch/follow/public/edit + badge + admin linkup
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



EMPLOYMENT_TYPES = [
    ('employed', 'Employed'),
    ('self_employed', 'Self-employed'),
    ('business_owner', 'Business owner'),
    ('freelancer', 'Freelancer'),
    ('student', 'Student'),
    ('unemployed', 'Unemployed'),
    ('retired', 'Retired'),
    ('homemaker', 'Homemaker'),
    ('other', 'Other'),
]


# Kikomo cha maandishi ya About/bio (herufi). Kibadilishe hapa tu, sehemu moja.
LINKUP_ABOUT_MAX = 120


def _linkup_profile_fields_from_form(form):
    """Collect About / Work / Education / Location from form (create + edit)."""
    def g(key, maxlen=200):
        # Browser hutuma mstari mpya kama \r\n (herufi 2); tunabadilisha kuwa \n (herufi 1)
        # ili hesabu ya seva ilingane na kihesabu cha fomu.
        v = (form.get(key) or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        return v[:maxlen] if v else None

    about = g('about', LINKUP_ABOUT_MAX) or g('bio', LINKUP_ABOUT_MAX)
    return {
        'about': about,
        'bio': about,  # keep bio in sync for older UI
        'website': g('website', 300),
        'phone': g('phone', 40),
        'email_public': g('email_public', 120),
        'hometown': g('hometown', 120),
        'current_city': g('current_city', 120),
        'country': g('country', 80),
        'workplace_name': g('workplace_name', 150),
        'workplace_role': g('workplace_role', 120),
        'workplace_city': g('workplace_city', 120),
        'employment_type': g('employment_type', 40),
        'primary_school': g('primary_school', 200),
        'primary_year': g('primary_year', 10),
        'secondary_school': g('secondary_school', 200),
        'secondary_year': g('secondary_year', 10),
        'college_name': g('college_name', 200),
        'college_year': g('college_year', 10),
    }


def _format_linkup_created(created_at):
    """Human date: 18 September 2026"""
    if not created_at:
        return None
    s = str(created_at).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d'):
        try:
            from datetime import datetime
            dt = datetime.strptime(s[:26].split('.')[0] if '.' in s and fmt.endswith('%f') else s[:19], fmt if not fmt.endswith('%f') else '%Y-%m-%d %H:%M:%S')
            months = ['January','February','March','April','May','June','July','August','September','October','November','December']
            return f"{dt.day} {months[dt.month-1]} {dt.year}"
        except Exception:
            continue
    try:
        # fallback ISO-ish
        part = s[:10]
        y, m, d = part.split('-')
        months = ['January','February','March','April','May','June','July','August','September','October','November','December']
        return f"{int(d)} {months[int(m)-1]} {y}"
    except Exception:
        return s[:10]


def register_linkup_routes(app):
    # ====================== LINKUP ROUTES (Phase 1) ======================

    @app.route('/linkup/create', methods=['GET', 'POST'])
    @login_required
    def linkup_create():
        me = session['user_id']
        conn = get_db_connection()
        if not user_has_kijiji_mode(conn, me):
            conn.close()
            flash('Washa Kijiji Mode kwanza ili kutengeneza Linkup.', 'info')
            return redirect(url_for('settings_kijiji_mode'))

        existing = get_user_linkups(conn, me)
        if len(existing) >= MAX_LINKUPS_PER_USER:
            conn.close()
            flash(f'Umefikia kiwango cha juu cha Linkup ({MAX_LINKUPS_PER_USER}).', 'warning')
            return redirect(url_for('settings_kijiji_mode'))

        if request.method == 'POST':
            display_name = (request.form.get('display_name') or '').strip()
            username = sanitize_username(request.form.get('username') or '')
            category = (request.form.get('category') or 'general').strip().lower()
            valid_cats = {c[0] for c in LINKUP_CATEGORIES}
            if category not in valid_cats:
                category = 'general'
            fields = _linkup_profile_fields_from_form(request.form)
            bio = fields.get('bio') or fields.get('about')

            errors = []
            if len(display_name) < 2:
                errors.append('Jina la Linkup linahitajika (angalau herufi 2).')
            if len(username) < 3:
                errors.append('Username angalau herufi 3.')
            if username and linkup_username_taken(conn, username):
                errors.append('Username tayari inatumika. Chagua nyingine.')

            profile_pic = None
            file = request.files.get('profile_pic')
            upload_dir = app.config.get('UPLOAD_FOLDER') or UPLOAD_FOLDER
            if file and file.filename and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[-1].lower()
                if ext in IMAGE_EXTS:
                    fname = secure_filename(file.filename)
                    unique = f"linkup_{me}_{int(time.time())}_{fname}"
                    file.save(os.path.join(upload_dir, unique))
                    profile_pic = unique

            form_data = {
                'display_name': display_name,
                'username': username,
                'category': category,
                **{k: (fields.get(k) or '') for k in [
                    'about', 'website', 'phone', 'email_public',
                    'hometown', 'current_city', 'country',
                    'workplace_name', 'workplace_role', 'workplace_city', 'employment_type',
                    'primary_school', 'primary_year', 'secondary_school', 'secondary_year',
                    'college_name', 'college_year',
                ]},
                'bio': bio or '',
            }

            if errors:
                conn.close()
                return render_template(
                    'linkup/linkup_create.html',
                    categories=LINKUP_CATEGORIES,
                    employment_types=EMPLOYMENT_TYPES,
                    error='; '.join(errors),
                    form=form_data,
                )

            try:
                cur = conn.execute(
                    """INSERT INTO linkups (
                        owner_user_id, username, display_name, category, bio, profile_pic,
                        is_active, created_at,
                        about, website, phone, email_public,
                        hometown, current_city, country,
                        workplace_name, workplace_role, workplace_city, employment_type,
                        primary_school, primary_year, secondary_school, secondary_year,
                        college_name, college_year
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 1, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?
                    )""",
                    (
                        me, username, display_name, category, bio, profile_pic, now_tz(),
                        fields.get('about'), fields.get('website'), fields.get('phone'), fields.get('email_public'),
                        fields.get('hometown'), fields.get('current_city'), fields.get('country'),
                        fields.get('workplace_name'), fields.get('workplace_role'), fields.get('workplace_city'), fields.get('employment_type'),
                        fields.get('primary_school'), fields.get('primary_year'), fields.get('secondary_school'), fields.get('secondary_year'),
                        fields.get('college_name'), fields.get('college_year'),
                    )
                )
                new_id = cur.lastrowid
                conn.commit()
                set_active_linkup_id(new_id)
                conn.close()
                flash(f'Linkup "{display_name}" imeundwa. Sasa unachapisha kama Linkup.', 'success')
                return redirect(url_for('linkup_public', username=username))
            except Exception as e:
                conn.close()
                print('[linkup_create]', e)
                flash('Imeshindikana kutengeneza Linkup. Jaribu tena.', 'error')
                return redirect(url_for('linkup_create'))

        conn.close()
        return render_template(
            'linkup/linkup_create.html',
            categories=LINKUP_CATEGORIES,
            employment_types=EMPLOYMENT_TYPES,
            error=None,
            form={},
        )



    @app.route('/linkup/switch', methods=['POST'])
    @login_required
    def linkup_switch():
        """
        Switch kama Facebook Page:
        - Profile mode → identity = user
        - Linkup mode  → identity = Linkup; baada ya switch unaelekezwa kwenye PROFILE KAMILI ya Linkup
        """
        me = session['user_id']
        data = request.get_json(silent=True) or {}
        raw = data.get('linkup_id', request.form.get('linkup_id'))
        conn = get_db_connection()
        if not user_has_kijiji_mode(conn, me):
            conn.close()
            return jsonify({'success': False, 'message': 'Kijiji Mode imezimwa'}), 400

        if raw in (None, '', '0', 'profile', 'none'):
            set_active_linkup_id(None)
            me_user = conn.execute(
                'SELECT username FROM users WHERE id = ?', (me,)
            ).fetchone()
            conn.close()
            uname = me_user['username'] if me_user else session.get('username')
            return jsonify({
                'success': True,
                'mode': 'profile',
                'message': 'Sasa unatumia Profile yako',
                'active_linkup': None,
                'redirect_url': url_for('profile', username=uname) if uname else url_for('home'),
            })

        try:
            lid = int(raw)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'success': False, 'message': 'linkup_id si sahihi'}), 400

        lu = get_linkup_by_id(conn, lid)
        conn.close()
        if not lu or int(lu['owner_user_id']) != int(me) or not lu.get('is_active', 1):
            return jsonify({'success': False, 'message': 'Linkup haipatikani'}), 404

        set_active_linkup_id(lid)
        # Facebook-style: baada ya switch → fungua PROFILE ya Linkup (si home tu)
        return jsonify({
            'success': True,
            'mode': 'linkup',
            'message': f'Sasa unatumia Linkup: {lu["display_name"]}',
            'active_linkup': {
                'id': lu['id'],
                'username': lu['username'],
                'display_name': lu['display_name'],
                'category': lu.get('category'),
                'profile_pic': lu.get('profile_pic'),
                'is_verified': bool(lu.get('is_verified')),
            },
            'redirect_url': url_for('linkup_public', username=lu['username']),
        })


    @app.route('/linkup/me')
    @login_required
    def linkup_me():
        """Ikiwa umeswitch Linkup → fungua profile ya Linkup hiyo; else Kijiji Mode."""
        conn = get_db_connection()
        active = resolve_active_linkup(conn, session['user_id'])
        conn.close()
        if active:
            return redirect(url_for('linkup_public', username=active['username']))
        return redirect(url_for('settings_kijiji_mode'))



    @app.route('/linkup/<int:linkup_id>/follow', methods=['POST'])
    @login_required
    def linkup_follow(linkup_id):
        """Follow / Unfollow Linkup."""
        me = session['user_id']
        conn = get_db_connection()
        lu = get_linkup_by_id(conn, linkup_id)
        if not lu or not lu.get('is_active', 1):
            conn.close()
            return jsonify({'success': False, 'message': 'Linkup haipatikani'}), 404

        # Owner hawezi kujifollow (optional allow - we skip)
        if int(lu['owner_user_id']) == int(me):
            conn.close()
            return jsonify({'success': False, 'message': 'Hii ni Linkup yako'}), 400

        existing = conn.execute(
            'SELECT id FROM linkup_follows WHERE follower_id = ? AND linkup_id = ?',
            (me, linkup_id)
        ).fetchone()
        if existing:
            conn.execute(
                'DELETE FROM linkup_follows WHERE follower_id = ? AND linkup_id = ?',
                (me, linkup_id)
            )
            following = False
            msg = 'Umeacha kufollow Linkup'
        else:
            conn.execute(
                'INSERT OR IGNORE INTO linkup_follows (follower_id, linkup_id, created_at) VALUES (?, ?, ?)',
                (me, linkup_id, now_tz())
            )
            following = True
            msg = f'Unafuata {lu["display_name"]}'
            # Notification kwa mmiliki
            try:
                if int(lu['owner_user_id']) != int(me):
                    notify_user(
                        lu['owner_user_id'], me, 'follow',
                        message=f'amefuata Linkup yako {lu["display_name"]}',
                        url=f'/linkup/{lu["username"]}'
                    )
            except Exception as e:
                print('[linkup_follow] notify:', e)

        followers = count_linkup_followers(conn, linkup_id)
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'following': following,
            'followers_count': followers,
            'message': msg,
        })


    @app.route('/linkup/<username>')
    def linkup_public(username):
        """Ukurasa wa public wa Linkup — mtu yeyote anaweza kuona."""
        conn = get_db_connection()
        lu = get_linkup_by_username(conn, username)
        if not lu:
            conn.close()
            flash('Linkup haipatikani.', 'warning')
            return redirect(url_for('home') if 'user_id' in session else url_for('login'))

        owner = conn.execute(
            'SELECT id, username, full_name, profile_pic, is_verified FROM users WHERE id = ?',
            (lu['owner_user_id'],)
        ).fetchone()
        owner_dict = dict(owner) if owner else {}

        viewer_id = session.get('user_id') or 0
        posts = conn.execute(
            """SELECT p.*, u.username AS owner_username, u.full_name AS owner_full_name,
                      (SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count,
                      (SELECT COUNT(*) FROM likes WHERE post_id = p.id AND user_id = ?) AS user_liked,
                      (SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count
               FROM posts p
               JOIN users u ON u.id = p.user_id
               WHERE p.linkup_id = ?
                 AND COALESCE(p.is_draft, 0) = 0
                 AND COALESCE(p.moderation_status, 'approved') = 'approved'
               ORDER BY p.id DESC LIMIT 50""",
            (viewer_id, lu['id'])
        ).fetchall()
        posts_data = [dict(r) for r in posts]
        posts_count = conn.execute(
            """SELECT COUNT(*) FROM posts WHERE linkup_id = ?
               AND COALESCE(is_draft,0)=0 AND COALESCE(moderation_status,'approved')='approved'""",
            (lu['id'],)
        ).fetchone()[0]
        is_owner = bool(viewer_id and int(viewer_id) == int(lu['owner_user_id']))
        is_following = bool(viewer_id and is_following_linkup(conn, viewer_id, lu['id']))
        followers_count = count_linkup_followers(conn, lu['id'])
        cat_label = dict(LINKUP_CATEGORIES).get(lu.get('category') or 'general', lu.get('category') or 'General')
        created_label = _format_linkup_created(lu.get('created_at'))
        emp_label = dict(EMPLOYMENT_TYPES).get(lu.get('employment_type') or '', lu.get('employment_type') or '')
        linkup_verified = bool(lu.get('is_verified'))
        # Je, viewer ameswitch KAMA Linkup hii sasa hivi?
        active_id = get_active_linkup_id()
        is_active_identity = bool(
            is_owner and active_id and int(active_id) == int(lu['id'])
        )
        pending_linkup_badge = False
        if is_owner and not linkup_verified:
            try:
                pr = conn.execute(
                    """SELECT id FROM badge_requests
                       WHERE user_id = ? AND linkup_id = ? AND COALESCE(request_type,'user') = 'linkup'
                         AND status = 'pending' LIMIT 1""",
                    (viewer_id, lu['id'])
                ).fetchone()
                pending_linkup_badge = bool(pr)
            except Exception:
                pending_linkup_badge = False
        conn.close()

        try:
            return render_template(
                'linkup/linkup_public.html',
                linkup=lu,
                owner=owner_dict,
                posts_data=posts_data,
                posts_count=posts_count,
                is_owner=is_owner,
                is_following=is_following,
                followers_count=followers_count,
                category_label=cat_label,
                logged_in=bool(viewer_id),
                linkup_verified=linkup_verified,
                pending_linkup_badge=pending_linkup_badge,
                is_active_identity=is_active_identity,
                created_label=created_label,
                emp_label=emp_label,
            )

        except Exception:
            name = lu['display_name']
            uname = lu['username']
            return f"""<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
            <title>{name} - Linkup</title>
            <style>body{{font-family:system-ui;background:#f0f2f5;padding:20px;text-align:center}}
            .card{{max-width:480px;margin:0 auto;background:#fff;border-radius:16px;padding:24px}}
            </style></head><body><div class=card>
            <h1>{name}</h1><p>@{uname} · Linkup · {cat_label}</p>
            <p>{posts_count} posts · {followers_count} followers</p>
            <a href="/">Nyumbani</a>
            </div></body></html>"""




    @app.route('/linkup/<int:linkup_id>/edit', methods=['GET', 'POST'])
    @login_required
    def linkup_edit(linkup_id):
        me = session['user_id']
        conn = get_db_connection()
        lu = get_linkup_by_id(conn, linkup_id)
        if not lu or int(lu['owner_user_id']) != int(me):
            conn.close()
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('settings_kijiji_mode'))

        if request.method == 'POST':
            display_name = (request.form.get('display_name') or '').strip()
            category = (request.form.get('category') or lu.get('category') or 'general').strip().lower()
            valid_cats = {c[0] for c in LINKUP_CATEGORIES}
            if category not in valid_cats:
                category = 'general'
            if len(display_name) < 2:
                conn.close()
                flash('Jina linahitajika.', 'error')
                return redirect(url_for('linkup_edit', linkup_id=linkup_id))

            fields = _linkup_profile_fields_from_form(request.form)
            bio = fields.get('bio') or fields.get('about')

            profile_pic = lu.get('profile_pic')
            file = request.files.get('profile_pic')
            upload_dir = app.config.get('UPLOAD_FOLDER') or UPLOAD_FOLDER
            if file and file.filename and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[-1].lower()
                if ext in IMAGE_EXTS:
                    fname = secure_filename(file.filename)
                    unique = f"linkup_{me}_{int(time.time())}_{fname}"
                    file.save(os.path.join(upload_dir, unique))
                    profile_pic = unique

            conn.execute(
                """UPDATE linkups SET
                    display_name=?, category=?, bio=?, profile_pic=?,
                    about=?, website=?, phone=?, email_public=?,
                    hometown=?, current_city=?, country=?,
                    workplace_name=?, workplace_role=?, workplace_city=?, employment_type=?,
                    primary_school=?, primary_year=?, secondary_school=?, secondary_year=?,
                    college_name=?, college_year=?
                WHERE id=? AND owner_user_id=?""",
                (
                    display_name, category, bio, profile_pic,
                    fields.get('about'), fields.get('website'), fields.get('phone'), fields.get('email_public'),
                    fields.get('hometown'), fields.get('current_city'), fields.get('country'),
                    fields.get('workplace_name'), fields.get('workplace_role'), fields.get('workplace_city'), fields.get('employment_type'),
                    fields.get('primary_school'), fields.get('primary_year'), fields.get('secondary_school'), fields.get('secondary_year'),
                    fields.get('college_name'), fields.get('college_year'),
                    linkup_id, me,
                )
            )
            conn.commit()
            conn.close()
            flash('Linkup imesasishwa.', 'success')
            return redirect(url_for('linkup_public', username=lu['username']))

        form = {
            'display_name': lu.get('display_name'),
            'username': lu.get('username'),
            'category': lu.get('category'),
            'bio': lu.get('about') or lu.get('bio') or '',
            'about': lu.get('about') or lu.get('bio') or '',
            'website': lu.get('website') or '',
            'phone': lu.get('phone') or '',
            'email_public': lu.get('email_public') or '',
            'hometown': lu.get('hometown') or '',
            'current_city': lu.get('current_city') or '',
            'country': lu.get('country') or '',
            'workplace_name': lu.get('workplace_name') or '',
            'workplace_role': lu.get('workplace_role') or '',
            'workplace_city': lu.get('workplace_city') or '',
            'employment_type': lu.get('employment_type') or '',
            'primary_school': lu.get('primary_school') or '',
            'primary_year': lu.get('primary_year') or '',
            'secondary_school': lu.get('secondary_school') or '',
            'secondary_year': lu.get('secondary_year') or '',
            'college_name': lu.get('college_name') or '',
            'college_year': lu.get('college_year') or '',
        }
        conn.close()
        return render_template(
            'linkup/linkup_create.html',
            categories=LINKUP_CATEGORIES,
            employment_types=EMPLOYMENT_TYPES,
            error=None,
            form=form,
            editing=True,
            linkup_id=linkup_id,
        )



    @app.route('/api/places/search')
    @login_required
    def api_places_search():
        """Mahali search (OpenStreetMap Nominatim) — hometown / city / country."""
        q = (request.args.get('q') or '').strip()
        if len(q) < 2:
            return jsonify({'success': True, 'places': []})
        try:
            r = requests.get(
                'https://nominatim.openstreetmap.org/search',
                params={
                    'q': q,
                    'format': 'json',
                    'addressdetails': 1,
                    'limit': 8,
                },
                headers={'User-Agent': 'KijijiTanzania/1.0 (linkup-places)'},
                timeout=8,
            )
            data = r.json() if r.status_code == 200 else []
        except Exception as e:
            print('[places]', e)
            data = []
        places_out = []
        for item in data:
            addr = item.get('address') or {}
            city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('state') or ''
            country = addr.get('country') or ''
            label = item.get('display_name') or q
            places_out.append({
                'label': label,
                'city': city,
                'country': country,
                'lat': item.get('lat'),
                'lon': item.get('lon'),
            })
        return jsonify({'success': True, 'places': places_out})


    @app.route('/linkup/<int:linkup_id>/request_badge', methods=['POST'])
    @login_required
    def request_linkup_badge(linkup_id):
        """
        Omba Verified Badge ya Linkup.
        Ombi linatumwa kwa jina la PROFILE ya mmiliki, akiueleza Linkup gani anayotaka kuverify.
        Admin anathibitisha → linkups.is_verified = 1 (badge ya dhahabu).
        """
        me = session['user_id']
        conn = get_db_connection()
        lu = get_linkup_by_id(conn, linkup_id)
        if not lu or int(lu['owner_user_id']) != int(me):
            conn.close()
            return jsonify({'success': False, 'message': 'Huna ruhusa kwa Linkup hii.'}), 403
        if not user_has_kijiji_mode(conn, me):
            conn.close()
            return jsonify({
                'success': False,
                'message': 'Washa Kijiji Mode kwanza kabla ya kuomba badge ya Linkup.'
            }), 403
        if lu.get('is_verified'):
            conn.close()
            return jsonify({'success': False, 'message': 'Linkup hii tayari ina Verified Badge.'}), 400

        existing = conn.execute(
            """SELECT id FROM badge_requests
               WHERE user_id = ? AND linkup_id = ? AND COALESCE(request_type,'user') = 'linkup'
                 AND status = 'pending' LIMIT 1""",
            (me, linkup_id)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({
                'success': False,
                'message': 'Ombi lako la badge ya Linkup hii bado linashughulikiwa.'
            }), 400

        user = conn.execute(
            'SELECT id, username, email, full_name FROM users WHERE id = ?', (me,)
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({'success': False, 'message': 'User hajapatikana.'}), 404

        account_type = (request.form.get('account_type') or 'Linkup').strip()
        full_name = (request.form.get('full_name') or user['full_name'] or user['username'] or '').strip()
        email = (request.form.get('email') or user['email'] or '').strip()
        phone = (request.form.get('phone') or '').strip()
        category = (request.form.get('category') or lu.get('category') or 'general').strip()
        id_type = (request.form.get('id_type') or '').strip()
        id_number = (request.form.get('id_number') or '').strip()
        website_link = (request.form.get('website_link') or '').strip()
        media_links = (request.form.get('media_links') or '').strip()
        other_socials = (request.form.get('other_socials') or '').strip()
        reason = (request.form.get('reason') or '').strip()

        if not reason:
            conn.close()
            return jsonify({
                'success': False,
                'message': 'Eleza kwa nini unataka Verified Badge kwenye Linkup yako.'
            }), 400

        # Prepend clear note for admin: this is for Linkup verification
        reason_full = (
            f"[LINKUP BADGE] Ninataka kuverify Linkup yangu "
            f"“{lu['display_name']}” (@{lu['username']}, category: {lu.get('category') or 'general'}). "
            f"{reason}"
        )

        id_document_path = None
        if 'id_document' in request.files:
            file = request.files['id_document']
            if file and file.filename:
                allowed_extensions = {'jpg', 'jpeg', 'png', 'pdf'}
                original_filename = secure_filename(file.filename)
                if original_filename and '.' in original_filename:
                    extension = original_filename.rsplit('.', 1)[1].lower()
                    if extension in allowed_extensions:
                        upload_folder = app.config.get('UPLOAD_BADGES_FOLDER') or os.path.join(
                            BASE_DIR, 'uploads', 'badges'
                        )
                        os.makedirs(upload_folder, exist_ok=True)
                        unique_filename = f"linkup_{linkup_id}_user_{me}_{uuid.uuid4().hex}.{extension}"
                        id_document_path = os.path.join(upload_folder, unique_filename)
                        file.save(id_document_path)

        try:
            conn.execute(
                '''
                INSERT INTO badge_requests (
                    user_id, username, account_type, full_name, alias_name,
                    email, phone, category, id_type, id_number,
                    id_document_path, website_link, media_links, other_socials,
                    reason, status, request_type, linkup_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'linkup', ?
                )
                ''',
                (
                    me, user['username'], account_type, full_name, lu['display_name'],
                    email, phone, category, id_type or None, id_number or None,
                    id_document_path, website_link or None, media_links or None,
                    other_socials or None, reason_full, linkup_id
                )
            )
            conn.commit()
            conn.close()
            return jsonify({
                'success': True,
                'message': 'Ombi la Verified Badge ya Linkup limetumwa! Admin atalikagua.'
            })
        except Exception as e:
            conn.rollback()
            conn.close()
            print('[request_linkup_badge]', e)
            return jsonify({'success': False, 'message': 'Imeshindikana. Jaribu tena.'}), 500


    @app.route('/admin/approve_linkup_badge/<int:req_id>', methods=['POST'])
    @login_required
    def admin_approve_linkup_badge(req_id):
        """Admin: kubali badge ya Linkup → is_verified=1 (dhahabu)."""
        if not session.get('is_admin') and session.get('role') != 'admin':
            # also check users.role
            conn = get_db_connection()
            u = conn.execute('SELECT role FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            conn.close()
            if not u or (u['role'] or '') != 'admin':
                flash('Huna ruhusa.', 'error')
                return redirect(url_for('home'))

        conn = get_db_connection()
        req = conn.execute('SELECT * FROM badge_requests WHERE id = ?', (req_id,)).fetchone()
        if not req:
            conn.close()
            flash('Ombi halipatikani.', 'error')
            return redirect(request.referrer or url_for('home'))

        linkup_id = req['linkup_id'] if 'linkup_id' in req.keys() else None
        rtype = (req['request_type'] if 'request_type' in req.keys() else 'user') or 'user'
        if rtype != 'linkup' or not linkup_id:
            conn.close()
            flash('Hili si ombi la Linkup badge.', 'error')
            return redirect(request.referrer or url_for('home'))

        conn.execute(
            'UPDATE linkups SET is_verified = 1 WHERE id = ?', (linkup_id,)
        )
        conn.execute(
            "UPDATE badge_requests SET status = 'approved' WHERE id = ?", (req_id,)
        )
        try:
            lu = get_linkup_by_id(conn, linkup_id)
            owner_id = req['user_id']
            notify_user(
                owner_id, session['user_id'], 'badge',
                message=f'Linkup yako “{(lu or {}).get("display_name", "")}” imethibitishwa (Verified Badge).',
                url=f'/linkup/{(lu or {}).get("username", "")}'
            )
        except Exception as e:
            print('[approve_linkup_badge] notify:', e)
        conn.commit()
        conn.close()
        flash('Linkup badge imeidhinishwa (dhahabu).', 'success')
        return redirect(request.referrer or url_for('home'))


    @app.route('/admin/reject_linkup_badge/<int:req_id>', methods=['POST'])
    @login_required
    def admin_reject_linkup_badge(req_id):
        if not session.get('is_admin') and session.get('role') != 'admin':
            conn = get_db_connection()
            u = conn.execute('SELECT role FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            conn.close()
            if not u or (u['role'] or '') != 'admin':
                flash('Huna ruhusa.', 'error')
                return redirect(url_for('home'))

        conn = get_db_connection()
        conn.execute(
            "UPDATE badge_requests SET status = 'rejected' WHERE id = ?", (req_id,)
        )
        conn.commit()
        conn.close()
        flash('Ombi la Linkup badge limekataliwa.', 'info')
        return redirect(request.referrer or url_for('home'))


    # ====================== ADMIN: LINKUP MANAGEMENT ======================

    def _require_admin():
        if 'user_id' not in session:
            return False
        if session.get('is_admin') or session.get('role') == 'admin':
            return True
        try:
            conn = get_db_connection()
            u = conn.execute(
                'SELECT role, email FROM users WHERE id = ?',
                (session['user_id'],)
            ).fetchone()
            conn.close()
            if not u:
                return False
            if (u['role'] or '') == 'admin':
                return True
            if (u['email'] or '').strip().lower() == 'matondomaduhu135@gmail.com':
                return True
            return False
        except Exception:
            return False


    # ====================== PUBLIC: GUNDUA LINKUPS ======================
    @app.route('/linkups')
    @app.route('/explore/linkups')
    def explore_linkups():
        """Orodha ya umma ya Linkups — mtu yeyote anaweza kuona na kufollow."""
        q = (request.args.get('q') or '').strip()
        category = (request.args.get('category') or '').strip().lower()
        me = session.get('user_id')

        conn = get_db_connection()
        items = []
        try:
            sql = (
                "SELECT l.*, "
                "u.username AS owner_username, "
                "(SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count, "
                "(SELECT COUNT(*) FROM posts WHERE linkup_id = l.id "
                "   AND COALESCE(is_draft,0)=0) AS posts_count "
                "FROM linkups l "
                "LEFT JOIN users u ON u.id = l.owner_user_id "
                "WHERE COALESCE(l.is_active, 1) = 1 "
            )
            params = []
            if q:
                sql += " AND (l.display_name LIKE ? OR l.username LIKE ? OR COALESCE(l.bio,'') LIKE ?) "
                like = '%' + q + '%'
                params.extend([like, like, like])
            if category and category != 'all':
                sql += " AND LOWER(COALESCE(l.category,'general')) = ? "
                params.append(category)
            sql += " ORDER BY followers_count DESC, l.id DESC LIMIT 100 "

            rows = conn.execute(sql, params).fetchall()
            items = [dict(r) for r in rows]
            if me:
                for it in items:
                    try:
                        it['is_following'] = is_following_linkup(conn, me, it['id'])
                    except Exception:
                        it['is_following'] = False
            else:
                for it in items:
                    it['is_following'] = False
        except Exception as e:
            print('[explore_linkups]', e)
            items = []
        conn.close()

        return render_template(
            'linkup/explore_linkups.html',
            linkups=items,
            q=q,
            category=category or 'all',
            categories=LINKUP_CATEGORIES,
        )


    @app.route('/admin/linkups')
    @login_required
    def admin_linkups_page():
        """Admin page: Linkups list + pending Linkup badge requests."""
        conn = get_db_connection()
        u = conn.execute('SELECT role FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        is_adm = bool(session.get('is_admin') or session.get('role') == 'admin' or (u and (u['role'] or '') == 'admin'))
        if not is_adm:
            conn.close()
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('home'))

        linkups = []
        try:
            rows = conn.execute(
                "SELECT l.*, u.username AS owner_username, "
                "(SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count, "
                "(SELECT COUNT(*) FROM posts WHERE linkup_id = l.id AND COALESCE(is_draft,0)=0) AS posts_count "
                "FROM linkups l LEFT JOIN users u ON u.id = l.owner_user_id ORDER BY l.id DESC"
            ).fetchall()
            linkups = [dict(r) for r in rows]
        except Exception as e:
            print('[admin_linkups_page] linkups:', e)

        pending_badges = []
        try:
            brows = conn.execute(
                "SELECT br.*, l.display_name AS linkup_name, l.username AS linkup_username "
                "FROM badge_requests br LEFT JOIN linkups l ON l.id = br.linkup_id "
                "WHERE COALESCE(br.request_type, 'user') = 'linkup' "
                "AND COALESCE(br.status, 'pending') = 'pending' ORDER BY br.id DESC"
            ).fetchall()
            pending_badges = [dict(r) for r in brows]
        except Exception as e:
            print('[admin_linkups_page] badges:', e)

        conn.close()
        return render_template('admin/admin_linkups.html', linkups=linkups, pending_badges=pending_badges)


    @app.route('/admin/api/linkups')
    @login_required
    def admin_api_linkups():
        if not _require_admin():
            return jsonify({'success': False, 'message': 'Forbidden — huna ruhusa ya admin'}), 403
        conn = get_db_connection()
        try:
            try:
                rows = conn.execute(
                    "SELECT l.*, "
                    "u.username AS owner_username, "
                    "u.full_name AS owner_full_name, "
                    "u.email AS owner_email, "
                    "(SELECT COUNT(*) FROM linkup_follows WHERE linkup_id = l.id) AS followers_count, "
                    "(SELECT COUNT(*) FROM posts WHERE linkup_id = l.id "
                    "   AND COALESCE(is_draft,0)=0) AS posts_count "
                    "FROM linkups l "
                    "LEFT JOIN users u ON u.id = l.owner_user_id "
                    "ORDER BY l.id DESC"
                ).fetchall()
            except Exception as e1:
                print('[admin_api_linkups] full query failed:', e1)
                rows = conn.execute(
                    "SELECT l.*, u.username AS owner_username "
                    "FROM linkups l LEFT JOIN users u ON u.id = l.owner_user_id "
                    "ORDER BY l.id DESC"
                ).fetchall()

            items = []
            for r in rows:
                d = dict(r)
                items.append({
                    'id': d.get('id'),
                    'username': d.get('username'),
                    'display_name': d.get('display_name'),
                    'category': d.get('category') or 'general',
                    'bio': d.get('bio') or d.get('about') or '',
                    'profile_pic': d.get('profile_pic'),
                    'is_active': int(d.get('is_active') if d.get('is_active') is not None else 1),
                    'is_verified': int(d.get('is_verified') or 0),
                    'owner_username': d.get('owner_username'),
                    'owner_full_name': d.get('owner_full_name'),
                    'owner_email': d.get('owner_email'),
                    'followers_count': int(d.get('followers_count') or 0),
                    'posts_count': int(d.get('posts_count') or 0),
                    'created_at': str(d.get('created_at') or ''),
                })
            conn.close()
            return jsonify({'success': True, 'linkups': items, 'count': len(items)})
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            print('[admin_api_linkups] ERROR:', e)
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'message': str(e)}), 500



    @app.route('/admin/linkup/<int:linkup_id>/verify', methods=['POST'])
    @login_required
    def admin_linkup_verify(linkup_id):
        if not _require_admin():
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Forbidden'}), 403
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('home'))
        conn = get_db_connection()
        lu = conn.execute('SELECT * FROM linkups WHERE id = ?', (linkup_id,)).fetchone()
        if not lu:
            conn.close()
            if request.is_json:
                return jsonify({'success': False, 'message': 'Linkup haipatikani'}), 404
            flash('Linkup haipatikani.', 'error')
            return redirect(url_for('admin_linkups_page'))
        lu = dict(lu)
        new_val = 0 if int(lu.get('is_verified') or 0) == 1 else 1
        conn.execute('UPDATE linkups SET is_verified = ? WHERE id = ?', (new_val, linkup_id))
        conn.commit()
        try:
            if new_val == 1:
                notify_user(
                    lu['owner_user_id'], session['user_id'], 'badge',
                    message=f'Linkup yako “{lu.get("display_name","")}” imethibitishwa (Verified Badge).',
                    url=f'/linkup/{lu.get("username","")}'
                )
        except Exception:
            pass
        conn.close()
        msg = 'Badge imeongezwa' if new_val else 'Badge imeondolewa'
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'is_verified': new_val, 'message': msg})
        flash(msg, 'success')
        return redirect(url_for('admin_linkups_page'))


    @app.route('/admin/linkup/<int:linkup_id>/block', methods=['POST'])
    @login_required
    def admin_linkup_block(linkup_id):
        if not _require_admin():
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Forbidden'}), 403
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('home'))
        conn = get_db_connection()
        lu = conn.execute('SELECT * FROM linkups WHERE id = ?', (linkup_id,)).fetchone()
        if not lu:
            conn.close()
            if request.is_json:
                return jsonify({'success': False, 'message': 'Linkup haipatikani'}), 404
            flash('Linkup haipatikani.', 'error')
            return redirect(url_for('admin_linkups_page'))
        lu = dict(lu)
        cur = int(lu.get('is_active') if lu.get('is_active') is not None else 1)
        new_val = 0 if cur == 1 else 1
        conn.execute('UPDATE linkups SET is_active = ? WHERE id = ?', (new_val, linkup_id))
        conn.commit()
        conn.close()
        msg = 'Linkup imefunguliwa' if new_val else 'Linkup imezuiwa (blocked)'
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'is_active': new_val, 'message': msg})
        flash(msg, 'success')
        return redirect(url_for('admin_linkups_page'))


    @app.route('/admin/linkup/<int:linkup_id>/delete', methods=['POST'])
    @login_required
    def admin_linkup_delete(linkup_id):
        if not _require_admin():
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Forbidden'}), 403
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('home'))
        conn = get_db_connection()
        lu = conn.execute('SELECT * FROM linkups WHERE id = ?', (linkup_id,)).fetchone()
        if not lu:
            conn.close()
            if request.is_json:
                return jsonify({'success': False, 'message': 'Linkup haipatikani'}), 404
            flash('Linkup haipatikani.', 'error')
            return redirect(url_for('admin_linkups_page'))
        try:
            conn.execute('UPDATE posts SET linkup_id = NULL WHERE linkup_id = ?', (linkup_id,))
            conn.execute('DELETE FROM linkup_follows WHERE linkup_id = ?', (linkup_id,))
            try:
                conn.execute(
                    "DELETE FROM badge_requests WHERE linkup_id = ? AND COALESCE(request_type,'user')='linkup'",
                    (linkup_id,)
                )
            except Exception:
                pass
            conn.execute('DELETE FROM linkups WHERE id = ?', (linkup_id,))
            conn.commit()
            conn.close()
            msg = 'Linkup imefutwa. Posts zimebakia kwenye profile ya mmiliki.'
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'message': msg})
            flash(msg, 'success')
            return redirect(url_for('admin_linkups_page'))
        except Exception as e:
            conn.rollback()
            conn.close()
            print('[admin_linkup_delete]', e)
            if request.is_json:
                return jsonify({'success': False, 'message': 'Imeshindikana kufuta'}), 500
            flash('Imeshindikana kufuta.', 'error')
            return redirect(url_for('admin_linkups_page'))



    @app.route('/admin/linkup/<int:linkup_id>/edit', methods=['GET', 'POST'])
    @login_required
    def admin_linkup_edit(linkup_id):
        """IMEZIMWA: Admin HAWEZI kuhariri Linkup ya mtu.
        Admin anaruhusiwa tu: kuona posts, kufuta posts, block, verify badge, kufuta akaunti.
        """
        flash('Admin hairuhusiwi kuhariri Linkup. Tumia Block / Verify / Futa / Posts tu.', 'error')
        return redirect(url_for('admin_linkups_page'))


    @app.route('/admin/linkup/<int:linkup_id>/posts')
    @login_required
    def admin_linkup_posts(linkup_id):
        """Admin: orodha ya posts za Linkup + uwezo wa kufuta."""
        if not _require_admin():
            flash('Huna ruhusa.', 'error')
            return redirect(url_for('home'))

        conn = get_db_connection()
        lu_row = get_linkup_by_id(conn, linkup_id)
        if not lu_row:
            conn.close()
            flash('Linkup haipatikani.', 'error')
            return redirect(url_for('admin_linkups_page'))

        posts = conn.execute(
            "SELECT p.*, u.username FROM posts p "
            "JOIN users u ON u.id = p.user_id "
            "WHERE p.linkup_id = ? ORDER BY p.id DESC LIMIT 100",
            (linkup_id,)
        ).fetchall()
        posts_data = [dict(r) for r in posts]
        conn.close()

        try:
            return render_template(
                'admin/admin_linkup_posts.html',
                linkup=lu_row,
                posts=posts_data,
            )
        except Exception:
            rows_html = ''
            for p in posts_data:
                snippet = (p.get('content') or '')[:80]
                rows_html += (
                    '<div style="padding:10px;border-bottom:1px solid #eee;">'
                    '<strong>#' + str(p['id']) + '</strong> @' + str(p.get('username') or '') +
                    ' · ' + snippet +
                    ' · <form method="post" action="/admin/delete_post/' + str(p['id']) +
                    '" style="display:inline"><button type="submit">Futa</button></form></div>'
                )
            name = lu_row.get('display_name') or 'Linkup'
            return (
                '<!DOCTYPE html><html><body style="font-family:system-ui;padding:16px">'
                '<a href="/admin/linkups">← Linkups</a>'
                '<h2>Posts za ' + str(name) + '</h2>' +
                (rows_html or '<p>Hakuna posts</p>') +
                '</body></html>'
            )


    # (Admin routes zimehamishwa → admin.py)


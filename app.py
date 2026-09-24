"""
app.py — Kijiji Tanzania main application
Wires modules: db, helpers, auth, posts, chat, profile, kijiji, linkup, account
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import sqlite3
import time
import secrets
from urllib.parse import quote
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import json
from pywebpush import webpush, WebPushException
from flask_babel import Babel, gettext as _
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import re
import requests
from io import BytesIO
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    qrcode = None
    ERROR_CORRECT_H = None
    Image = None
    ImageDraw = None
    ImageFont = None

from moderation import moderate_media
from community import register_community_routes
from admin import register_admin_routes

from db import get_db_connection, init_db, BASE_DIR, DB_PATH, UPLOAD_FOLDER, UPLOAD_BADGES_FOLDER, allowed_badge_file, ALLOWED_BADGE_EXTENSIONS
from helpers import (
    init_helpers, login_required, avatar_url, load_translations,
    SUPPORTED_LANGUAGES, LINKUP_CATEGORIES, MAX_LINKUPS_PER_USER,
    apply_user_language, save_user_language, get_user_linkups,
    resolve_active_linkup, set_active_linkup_id, get_active_linkup_id,
    enrich_posts_with_linkup, publish_due_scheduled_posts, muted_ids_for,
    notify_user, create_notification, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIMS,
    # Re-export for admin.py / community.py (they do "from app import ...")
    now_tz, allowed_file, allowed_video, sanitize_username, generate_otp,
    send_otp_email, record_user_session, send_web_push, flag_post_for_admin,
    register_nsfw_violation, get_user_restriction_status, extract_hashtags,
    save_post_hashtags, user_is_muted, can_view_status, IMAGE_EXTS, VIDEO_EXTS,
    ALLOWED_EXTENSIONS, RESTRICTION_HOURS, WARNINGS_BEFORE_RESTRICTION,
    user_has_kijiji_mode, get_linkup_by_id, get_linkup_by_username,
    is_following_linkup, count_linkup_followers, linkup_username_taken,
    generate_profile_qr_card, get_profile_public_url, record_status_view,
    count_status_views, base_username_from_email, username_is_taken,
    suggest_usernames, GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
)


# =============================================================================
# COMPAT for admin.py + community.py  (they do: from app import ...)
# Majina haya YANAPASWA kuwa kwenye module app — usifute block hii.
# =============================================================================
# From db:
#   get_db_connection, BASE_DIR, UPLOAD_FOLDER, UPLOAD_BADGES_FOLDER, allowed_badge_file
# From helpers:
#   now_tz, login_required, register_nsfw_violation, create_notification
# (tayari zimeimport hapo juu — hakikisha zipo)
assert get_db_connection is not None
assert now_tz is not None
assert login_required is not None
assert register_nsfw_violation is not None
assert UPLOAD_BADGES_FOLDER is not None
assert UPLOAD_FOLDER is not None
assert BASE_DIR is not None
assert allowed_badge_file is not None
assert create_notification is not None

# Auth / route modules
from auth import register_auth_routes
from posts import register_posts_routes
from profile import register_profile_routes
from chat import register_chat_routes
from kijiji import register_kijiji_routes
from linkup import register_linkup_routes
from account import register_account_routes

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "HWkzToktm9g98f8srg5Lo6MPDggTrxwGFfwzLnq7XYQ")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "BDLkkmrKM007eSEby3amhKG3or37FOULg6bpmgnKL_M2HsNLxZXhIVA9fmqva0YPYVd3wo3U4omh_CwZUMqHZAo")
VAPID_CLAIMS = {"sub": "mailto:keyaramadhan0@gmail.com"}

app = Flask(__name__)
app.secret_key = 'siri_yangu_ya_mradi_huu_123'

# ========== PERMANENT SESSION (kama TikTok) ==========
app.permanent_session_lifetime = timedelta(days=90)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ====================== BABEL CONFIG ======================
app.config['BABEL_DEFAULT_LOCALE'] = 'sw'
app.config['BABEL_TRANSLATION_DIRECTORIES'] = 'translations'

def get_locale():
    return session.get('language', 'sw')

babel = Babel(app, locale_selector=get_locale)

# ========== GOOGLE / FACEBOOK ==========
GOOGLE_CLIENT_ID = "1083614983079-k0oeie8lkao98r62m91dc0aqcgomhk15.apps.googleusercontent.com"
FACEBOOK_APP_ID = "28132131163112538"
FACEBOOK_APP_SECRET = "613688758b4443fc4adb0b3429be4196"

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['UPLOAD_BADGES_FOLDER'] = UPLOAD_BADGES_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 24 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_BADGES_FOLDER, exist_ok=True)

# Init helpers with app + keys
init_helpers(
    app,
    vapid_private=VAPID_PRIVATE_KEY,
    vapid_public=VAPID_PUBLIC_KEY,
    vapid_claims=VAPID_CLAIMS,
    gmail_address="matondomaduhu135@gmail.com",
    gmail_app_password="neftrxmcxidjourc",
)

# Context processors & filters (must stay on real app)
@app.context_processor
def inject_language():
    language = session.get('language', 'sw')
    if language not in SUPPORTED_LANGUAGES:
        language = 'sw'
    translations = load_translations(language)
    def t(key, default=None):
        return translations.get(key, default if default is not None else key)
    return {
        'current_language': language,
        'languages': SUPPORTED_LANGUAGES,
        't': t
    }

@app.template_filter('avatar')
def avatar_filter(pic, name=None):
    return avatar_url(pic, name)

@app.context_processor
def inject_avatar():
    return {'avatar_url': avatar_url}

@app.context_processor
def inject_counts():
    data = {
        'unread_notifs': 0,
        'unread_messages': 0,
        'my_village_mode': False,
        'my_recommendable': True,
        'my_profile_pic': None,
        'my_full_name': None,
        'my_username': None,
        'my_linkups': [],
        'active_linkup': None,
        'posting_as_linkup': False,
        'identity_name': None,
        'identity_username': None,
        'identity_pic': None,
        'identity_url': None,
        'identity_is_linkup': False,
        'identity_verified': False,
    }
    if 'user_id' not in session:
        return data
    try:
        conn = get_db_connection()
        data['unread_notifs'] = conn.execute(
            'SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0',
            (session['user_id'],)
        ).fetchone()[0]
        data['unread_messages'] = conn.execute(
            'SELECT COUNT(*) FROM private_messages WHERE receiver_id = ? AND is_read = 0',
            (session['user_id'],)
        ).fetchone()[0]
        me_row = conn.execute(
            'SELECT village_mode, warning_count, profile_pic, full_name, username FROM users WHERE id = ?',
            (session['user_id'],)
        ).fetchone()
        if me_row:
            try:
                data['my_village_mode'] = bool(me_row['village_mode'])
                data['my_recommendable'] = (me_row['warning_count'] or 0) == 0
            except Exception:
                pass
            try:
                data['my_profile_pic'] = me_row['profile_pic'] if 'profile_pic' in me_row.keys() else None
                data['my_full_name'] = me_row['full_name'] if 'full_name' in me_row.keys() else None
                data['my_username'] = me_row['username'] if 'username' in me_row.keys() else session.get('username')
                if data['my_profile_pic']:
                    session['profile_pic'] = data['my_profile_pic']
                if data['my_full_name']:
                    session['full_name'] = data['my_full_name']
            except Exception:
                pass
        try:
            if data.get('my_village_mode'):
                data['my_linkups'] = get_user_linkups(conn, session['user_id'])
                active = resolve_active_linkup(conn, session['user_id'])
                data['active_linkup'] = active
                data['posting_as_linkup'] = bool(active)
            else:
                if session.get('active_linkup_id'):
                    set_active_linkup_id(None)
        except Exception as e:
            print('[inject] linkup:', e)
        try:
            if data.get('active_linkup'):
                lu = data['active_linkup']
                data['identity_is_linkup'] = True
                data['identity_name'] = lu.get('display_name') or lu.get('username')
                data['identity_username'] = lu.get('username')
                data['identity_pic'] = lu.get('profile_pic')
                data['identity_url'] = url_for('linkup_public', username=lu['username'])
                data['identity_verified'] = bool(lu.get('is_verified'))
            else:
                data['identity_is_linkup'] = False
                data['identity_name'] = data.get('my_full_name') or data.get('my_username') or session.get('username')
                data['identity_username'] = data.get('my_username') or session.get('username')
                data['identity_pic'] = data.get('my_profile_pic')
                uname = data.get('my_username') or session.get('username')
                data['identity_url'] = url_for('profile', username=uname) if uname else url_for('home')
                data['identity_verified'] = False
        except Exception as e:
            print('[inject] identity:', e)
        conn.close()
    except Exception:
        pass
    return data

@app.errorhandler(413)
def request_entity_too_large(error):
    flash('File ni kubwa sana. Picha na Video max 24MB.')
    return redirect(request.referrer or url_for('home'))

# Register all modular routes
register_auth_routes(app)
register_posts_routes(app)
register_profile_routes(app)
register_chat_routes(app)
register_kijiji_routes(app)
register_linkup_routes(app)
register_account_routes(app)

# Remaining API routes that were at the end of original
@app.route('/api/feed')
@login_required
def api_feed():

    publish_due_scheduled_posts()
    user_id = session['user_id']
    cursor = request.args.get('cursor', 0, type=int)
    limit = min(request.args.get('limit', 15, type=int), 40)
    conn = get_db_connection()
    muted = muted_ids_for(conn, user_id)

    sql = (
        "SELECT p.*, u.username, u.full_name, u.profile_pic, u.is_verified, "
        "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
        "(SELECT COUNT(*) FROM likes WHERE post_id = p.id AND user_id = ?) AS user_liked, "
        "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count, "
        "(SELECT COUNT(*) FROM saved_posts WHERE post_id = p.id AND user_id = ?) AS user_saved "
        "FROM posts p JOIN users u ON u.id = p.user_id "
        "WHERE COALESCE(p.is_draft, 0) = 0 "
        "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
        "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
        "AND (u.is_deactivated = 0 OR u.is_deactivated IS NULL) "
        "AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = ?) "
        "AND p.user_id NOT IN (SELECT blocker_id FROM blocks WHERE blocked_id = ?) "
        "AND (p.linkup_id IS NULL OR p.user_id = ? OR EXISTS ("
        "  SELECT 1 FROM linkup_follows WHERE follower_id = ? AND linkup_id = p.linkup_id"
        "))"
    )
    params = [user_id, user_id, user_id, user_id, user_id, user_id]
    if cursor > 0:
        sql += " AND p.id < ?"
        params.append(cursor)
    sql += " ORDER BY p.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    items = []
    for r in rows:
        if r['user_id'] in muted:
            continue
        item = {
            'id': r['id'], 'user_id': r['user_id'], 'content': r['content'],
            'file_path': r['file_path'], 'media_type': r['media_type'],
            'created_at': str(r['created_at'] or ''), 'username': r['username'],
            'full_name': r['full_name'] if 'full_name' in r.keys() else None,
            'profile_pic': r['profile_pic'], 'is_verified': r['is_verified'],
            'likes_count': r['likes_count'], 'user_liked': bool(r['user_liked']),
            'comments_count': r['comments_count'], 'user_saved': bool(r['user_saved']),
            'category': r['category'] if 'category' in r.keys() else 'general',
            'linkup_id': r['linkup_id'] if 'linkup_id' in r.keys() else None,
            'is_linkup_post': False,
        }
        items.append(item)
    try:
        enrich_posts_with_linkup(conn, items)
    except Exception:
        pass
    conn.close()
    return jsonify({
        'success': True, 'posts': items,
        'next_cursor': items[-1]['id'] if items else None,
        'has_more': len(items) >= limit,
    })



@app.route('/api/kijiji')
@login_required
def api_kijiji():
    user_id = session['user_id']
    cursor = request.args.get('cursor', 0, type=int)
    limit = min(request.args.get('limit', 15, type=int), 40)
    conn = get_db_connection()
    muted = muted_ids_for(conn, user_id)
    sql = (
        "SELECT p.id, p.content AS caption, p.file_path AS media_path, p.media_type, "
        "p.shares, p.created_at, p.user_id, u.username, u.full_name, u.profile_pic, u.is_verified, "
        "(SELECT COUNT(*) FROM likes WHERE post_id = p.id) AS likes_count, "
        "(SELECT COUNT(*) FROM comments WHERE post_id = p.id AND COALESCE(is_hidden,0)=0) AS comments_count, "
        "(SELECT COUNT(*) FROM likes WHERE post_id = p.id AND user_id = ?) AS user_liked "
        "FROM posts p JOIN users u ON u.id = p.user_id "
        "WHERE p.file_path IS NOT NULL AND p.media_type IN ('video', 'image') "
        "AND COALESCE(p.is_draft, 0) = 0 "
        "AND COALESCE(p.moderation_status, 'approved') = 'approved' "
        "AND (u.is_blocked = 0 OR u.is_blocked IS NULL) "
        "AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = ?)"
    )
    params = [user_id, user_id]
    if cursor > 0:
        sql += " AND p.id < ?"
        params.append(cursor)
    sql += " ORDER BY p.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    items = []
    for r in rows:
        if r['user_id'] in muted:
            continue
        items.append({
            'id': r['id'], 'caption': r['caption'], 'media_path': r['media_path'],
            'media_type': r['media_type'], 'shares': r['shares'] or 0,
            'created_at': str(r['created_at'] or ''), 'user_id': r['user_id'],
            'username': r['username'],
            'full_name': r['full_name'] if 'full_name' in r.keys() else None,
            'profile_pic': r['profile_pic'], 'is_verified': r['is_verified'],
            'likes_count': r['likes_count'], 'comments_count': r['comments_count'],
            'user_liked': bool(r['user_liked']),
        })
    conn.close()
    return jsonify({
        'success': True, 'posts': items,
        'next_cursor': items[-1]['id'] if items else None,
        'has_more': len(items) >= limit,
    })



# ===== COMMUNITY + ADMIN =====
register_community_routes(app)
register_admin_routes(app)

if __name__ == '__main__':
    app.run(debug=True)

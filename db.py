"""
db.py — Database layer for Kijiji Tanzania
Contains: get_db_connection, allowed_badge_file, init_db (full schema)
"""
import os
import sqlite3

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
UPLOAD_BADGES_FOLDER = os.path.join(BASE_DIR, 'uploads', 'badges')
ALLOWED_BADGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_BADGES_FOLDER, exist_ok=True)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000;')
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
    except Exception:
        pass
    return conn


def allowed_badge_file(filename):
    """Ruhusu faili za kitambulisho pekee (badge)."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_BADGE_EXTENSIONS


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()

    # ====================== DATABASE SETTINGS ======================

    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA busy_timeout=30000;')
    cursor.execute('PRAGMA foreign_keys = ON;')

    # ====================== USERS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            bio TEXT,
            profile_pic TEXT,
            is_verified INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            warning_message TEXT,
            post_visibility TEXT DEFAULT 'public',
            is_email_verified INTEGER DEFAULT 1
        )
    ''')

    # ====================== USER EXTRA COLUMNS ======================

    for col, typ in [
        ('is_verified', 'INTEGER DEFAULT 0'),
        ('is_blocked', 'INTEGER DEFAULT 0'),
        ('warning_message', 'TEXT'),
        ('post_visibility', "TEXT DEFAULT 'public'"),
        ('is_email_verified', 'INTEGER DEFAULT 1'),
        ('full_name', 'TEXT'),
        ('language', "TEXT DEFAULT 'sw'"),
        ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
        ('auth_provider', "TEXT DEFAULT 'local'"),
        ('is_deactivated', 'INTEGER DEFAULT 0'),
        ('deactivated_at', 'TEXT'),
        ('warning_count', 'INTEGER DEFAULT 0'),
        ('restricted_until', 'TEXT'),
        ('village_mode', 'INTEGER DEFAULT 0'),
        ('profile_category', 'TEXT'),
    ]:
        try:
            cursor.execute(
                f'ALTER TABLE users ADD COLUMN {col} {typ}'
            )
        except sqlite3.OperationalError:
            pass

    try:
        cursor.execute(
            "UPDATE users SET auth_provider = 'google' "
            "WHERE (bio = 'Joined with Google' OR profile_pic LIKE 'http%') "
            "AND (auth_provider IS NULL OR auth_provider = 'local')"
        )
    except Exception:
        pass

    # ====================== PENDING REGISTRATIONS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            otp TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            resend_available_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            full_name TEXT
        )
    ''')

    try:
        cursor.execute(
            'ALTER TABLE pending_registrations ADD COLUMN full_name TEXT'
        )
    except sqlite3.OperationalError:
        pass

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_pending_email '
        'ON pending_registrations(email)'
    )

    # ====================== POSTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            shares INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    try:
        cursor.execute(
            "ALTER TABLE posts ADD COLUMN category TEXT DEFAULT 'general'"
        )
    except sqlite3.OperationalError:
        pass

    # ====================== POST MODERATION (NSFW) ======================
    for col, typ in [
        ('moderation_status', "TEXT DEFAULT 'approved'"),
        ('nsfw_score', 'REAL DEFAULT 0'),
    ]:
        try:
            cursor.execute(
                f'ALTER TABLE posts ADD COLUMN {col} {typ}'
            )
        except sqlite3.OperationalError:
            pass

    # ====================== REPOST (caption + frame ya post asili) ======================
    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN repost_of INTEGER')
    except sqlite3.OperationalError:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS moderation_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            flag_type TEXT NOT NULL,      -- 'manual_review' au 'escalation'
            nsfw_score REAL,
            labels TEXT,
            status TEXT DEFAULT 'pending', -- pending / resolved
            admin_note TEXT,
            created_at TEXT,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            FOREIGN KEY (post_id) REFERENCES posts (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # ====================== COMMENTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            parent_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES posts (id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (parent_id) REFERENCES comments (id) ON DELETE CASCADE
        )
    ''')

    try:
        cursor.execute(
            "ALTER TABLE comments ADD COLUMN is_hidden INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute(
            "ALTER TABLE comments ADD COLUMN updated_at TIMESTAMP"
        )
    except sqlite3.OperationalError:
        pass

    # ====================== COMMENT LIKES ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comment_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            comment_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (comment_id) REFERENCES comments (id) ON DELETE CASCADE
        )
    ''')

    # ====================== LIKES ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (post_id) REFERENCES posts (id)
        )
    ''')

    # ====================== SAVED POSTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS saved_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (post_id) REFERENCES posts (id)
        )
    ''')

    # ====================== NOTIFICATIONS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            post_id INTEGER,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (sender_id) REFERENCES users (id)
        )
    ''')

    try:
        cursor.execute(
            "ALTER TABLE notifications ADD COLUMN message TEXT"
        )
    except sqlite3.OperationalError:
        pass

    # ====================== HISTORY ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action_description TEXT NOT NULL,
            post_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    try:
        cursor.execute(
            "ALTER TABLE history ADD COLUMN post_id INTEGER"
        )
    except sqlite3.OperationalError:
        pass

    # ====================== BADGE REQUESTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS badge_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            account_type TEXT,
            full_name TEXT,
            alias_name TEXT,
            email TEXT,
            phone TEXT,
            category TEXT,
            id_type TEXT,
            id_number TEXT,
            id_document_path TEXT,
            website_link TEXT,
            media_links TEXT,
            other_socials TEXT,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Ongeza columns mpya kama database ya zamani haina
    for col, typ in [
        ('account_type', 'TEXT'),
        ('full_name', 'TEXT'),
        ('alias_name', 'TEXT'),
        ('email', 'TEXT'),
        ('phone', 'TEXT'),
        ('category', 'TEXT'),
        ('id_type', 'TEXT'),
        ('id_number', 'TEXT'),
        ('id_document_path', 'TEXT'),
        ('website_link', 'TEXT'),
        ('media_links', 'TEXT'),
        ('other_socials', 'TEXT'),
        # Phase 3: Linkup verification badge
        ('request_type', "TEXT DEFAULT 'user'"),  # 'user' | 'linkup'
        ('linkup_id', 'INTEGER'),
    ]:
        try:
            cursor.execute(
                f'ALTER TABLE badge_requests ADD COLUMN {col} {typ}'
            )
        except sqlite3.OperationalError:
            pass


    # ====================== ADMIN MESSAGES ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            message TEXT,
            admin_reply TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ====================== FOLLOWS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            follower_id INTEGER,
            following_id INTEGER,
            FOREIGN KEY (follower_id) REFERENCES users(id),
            FOREIGN KEY (following_id) REFERENCES users(id)
        )
    ''')

    # source_post_id: ikiwa follow ilitokea kwa kubonyeza Follow ndani ya
    # post fulani, tunahifadhi post id hiyo ili Insights ionyeshe
    # "Followers wapya kupitia post hii" (Kijiji Mode - dashboard).
    for col, typ in [
        ('source_post_id', 'INTEGER'),
    ]:
        try:
            cursor.execute(f'ALTER TABLE follows ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError:
            pass

    # ====================== REPOSTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reposts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            original_post_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (original_post_id) REFERENCES posts (id),
            UNIQUE(user_id, original_post_id)
        )
    ''')

    # ====================== REPORTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES posts (id),
            FOREIGN KEY (reporter_id) REFERENCES users (id)
        )
    ''')

    # ====================== BLOCKS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(blocker_id, blocked_id),
            FOREIGN KEY (blocker_id) REFERENCES users (id),
            FOREIGN KEY (blocked_id) REFERENCES users (id)
        )
    ''')

    # ====================== POST VIEWS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            watch_seconds REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
    ''')

    # ====================== COMMUNITY & GROUPS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS community_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT DEFAULT 'general',
            creator_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES community_groups (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES community_groups (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS community_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            subcategory TEXT DEFAULT 'all',
            user_id INTEGER NOT NULL,
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # ====================== PRIVATE MESSAGES ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS private_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sender_id) REFERENCES users (id),
            FOREIGN KEY (receiver_id) REFERENCES users (id)
        )
    ''')

    for col, typ in [
        ('is_delivered', 'INTEGER DEFAULT 0'),
        ('file_path', 'TEXT'),
        ('media_type', 'TEXT'),
        ('reaction', 'TEXT'),
        ('is_hidden', 'INTEGER DEFAULT 0'),
        ('reply_to_id', 'INTEGER'),
        ('status_id', 'INTEGER'),
        ('deleted_for_sender', 'INTEGER DEFAULT 0'),
        ('deleted_for_receiver', 'INTEGER DEFAULT 0'),
    ]:
        try:
            cursor.execute(
                f'ALTER TABLE private_messages ADD COLUMN {col} {typ}'
            )
        except sqlite3.OperationalError:
            pass

    # ====================== CALL SIGNALS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS call_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER DEFAULT 0
        )
    ''')

    # ====================== PUSH SUBSCRIPTIONS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_push_user '
        'ON push_subscriptions(user_id)'
    )

    # ====================== PROFILE EXTRA FIELDS ======================

    for col, typ in [
        ('birthday', 'TEXT'),
        ('join_year', 'TEXT'),
        ('sex', 'TEXT'),
        ('marital_status', 'TEXT'),
        ('cover_photo', 'TEXT'),
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
            cursor.execute(
                f'ALTER TABLE users ADD COLUMN {col} {typ}'
            )
        except sqlite3.OperationalError:
            pass

    # ====================== PINNED CHATS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pinned_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            other_user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, other_user_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (other_user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_pinned_user '
        'ON pinned_chats(user_id)'
    )

    # ====================== STATUSES / STORIES ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    try:
        cursor.execute(
            'ALTER TABLE statuses ADD COLUMN music_path TEXT'
        )
    except sqlite3.OperationalError:
        pass

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_status_user '
        'ON statuses(user_id)'
    )

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_status_created '
        'ON statuses(created_at)'
    )

    # ====================== STATUS VIEWS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS status_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status_id INTEGER NOT NULL,
            viewer_id INTEGER NOT NULL,
            viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(status_id, viewer_id),
            FOREIGN KEY (status_id) REFERENCES statuses (id),
            FOREIGN KEY (viewer_id) REFERENCES users (id)
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_status_views_status '
        'ON status_views(status_id)'
    )

    try:
        cols = [
            r[1]
            for r in cursor.execute(
                'PRAGMA table_info(status_views)'
            ).fetchall()
        ]

        if 'viewer_id' not in cols and 'user_id' in cols:
            cursor.execute(
                'ALTER TABLE status_views ADD COLUMN viewer_id INTEGER'
            )

            cursor.execute(
                'UPDATE status_views '
                'SET viewer_id = user_id '
                'WHERE viewer_id IS NULL'
            )

        elif 'viewer_id' not in cols:
            cursor.execute(
                'ALTER TABLE status_views ADD COLUMN viewer_id INTEGER'
            )

    except Exception:
        pass

    # ====================== STATUS REACTIONS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS status_reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reaction TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(status_id, user_id),
            FOREIGN KEY (status_id) REFERENCES statuses (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_status_react_status '
        'ON status_reactions(status_id)'
    )

    # ====================== OTP TABLE ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            otp TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

# ====================== DATA DELETION REQUESTS ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS data_deletion_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_deletion_email '
        'ON data_deletion_requests(email)'
    )


# ====================== GROUP A: MUTE ======================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            muter_id INTEGER NOT NULL,
            muted_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(muter_id, muted_id),
            FOREIGN KEY (muter_id) REFERENCES users (id),
            FOREIGN KEY (muted_id) REFERENCES users (id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mutes_muter ON mutes(muter_id)")

# ====================== GROUP A: CLOSE FRIENDS ======================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS close_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, friend_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (friend_id) REFERENCES users (id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_close_friends_user ON close_friends(user_id)")

# ====================== GROUP A: HASHTAGS ======================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hashtags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT UNIQUE NOT NULL COLLATE NOCASE,
            use_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hashtags_count ON hashtags(use_count DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS post_hashtags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            hashtag_id INTEGER NOT NULL,
            UNIQUE(post_id, hashtag_id),
            FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE,
            FOREIGN KEY (hashtag_id) REFERENCES hashtags (id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_hashtags_tag ON post_hashtags(hashtag_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_hashtags_post ON post_hashtags(post_id)")

# ====================== GROUP A: DRAFTS / SCHEDULE (post columns) ======================

    for col, typ in [
        ("is_draft", "INTEGER DEFAULT 0"),
        ("scheduled_at", "TEXT"),
        ("published_at", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

# ====================== GROUP A: STATUS PRIVACY ======================

    try:
        cursor.execute("ALTER TABLE statuses ADD COLUMN privacy TEXT DEFAULT 'public'")
    except sqlite3.OperationalError:
        pass

# ====================== GROUP A: CHAT STAR / ARCHIVE ======================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS starred_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, message_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (message_id) REFERENCES private_messages (id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS archived_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            other_user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, other_user_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (other_user_id) REFERENCES users (id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_archived_user ON archived_chats(user_id)")

    try:
        cursor.execute("ALTER TABLE private_messages ADD COLUMN edited_at TEXT")
    except sqlite3.OperationalError:
        pass

# ====================== GROUP A: USER SESSIONS ======================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_token TEXT UNIQUE NOT NULL,
            device_info TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id)")

# ====================== GROUP A: REPORTS STATUS ======================

    for col, typ in [
        ("status", "TEXT DEFAULT 'pending'"),
        ("admin_note", "TEXT"),
        ("reviewed_at", "TEXT"),
        ("reviewed_by", "INTEGER"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE reports ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass


# ====================== LINKUPS (Kijiji Mode pages) ======================

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS linkups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            display_name TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            bio TEXT,
            profile_pic TEXT,
            cover_photo TEXT,
            is_active INTEGER DEFAULT 1,
            is_verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_user_id) REFERENCES users (id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_linkups_owner ON linkups(owner_user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_linkups_username ON linkups(username)')
    try:
        cursor.execute('ALTER TABLE linkups ADD COLUMN is_verified INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # ===== Linkup full profile (Facebook-style About) =====
    for col, typ in [
        ('about', 'TEXT'),
        ('website', 'TEXT'),
        ('phone', 'TEXT'),
        ('email_public', 'TEXT'),
        ('hometown', 'TEXT'),
        ('current_city', 'TEXT'),
        ('country', 'TEXT'),
        ('workplace_name', 'TEXT'),
        ('workplace_role', 'TEXT'),
        ('workplace_city', 'TEXT'),
        ('employment_type', 'TEXT'),
        ('primary_school', 'TEXT'),
        ('primary_year', 'TEXT'),
        ('secondary_school', 'TEXT'),
        ('secondary_year', 'TEXT'),
        ('college_name', 'TEXT'),
        ('college_year', 'TEXT'),
    ]:
        try:
            cursor.execute(f'ALTER TABLE linkups ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError:
            pass

    try:
        cursor.execute('ALTER TABLE posts ADD COLUMN linkup_id INTEGER')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_linkup ON posts(linkup_id)')
    except Exception:
        pass


    cursor.execute('''
        CREATE TABLE IF NOT EXISTS linkup_follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            follower_id INTEGER NOT NULL,
            linkup_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(follower_id, linkup_id),
            FOREIGN KEY (follower_id) REFERENCES users (id),
            FOREIGN KEY (linkup_id) REFERENCES linkups (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_linkup_follows_linkup ON linkup_follows(linkup_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_linkup_follows_follower ON linkup_follows(follower_id)'
    )

# ====================== FINAL COMMIT ======================


    conn.commit()
    conn.close()


# Initialize schema on import (safe — uses IF NOT EXISTS / try-except ALTER)
init_db()

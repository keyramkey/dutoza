"""
db.py — Database layer for Kijiji Tanzania
Neon Postgres (production) + SQLite fallback (local only)
"""
import os
import re
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

# ====================== PATHS ======================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
UPLOAD_BADGES_FOLDER = os.path.join(BASE_DIR, "uploads", "badges")
ALLOWED_BADGE_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_BADGES_FOLDER, exist_ok=True)

# ====================== DETECT DATABASE ======================
DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_URL_UNPOOLED")
    or ""
).strip()

USE_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))


def _normalize_database_url(url: str) -> str:
    """Render/Heroku sometimes use postgres:// — psycopg2 wants postgresql://"""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # Neon pooler works fine with sslmode=require
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "sslmode=require"
    return url


# ====================== SQLITE (local fallback) ======================
def _sqlite_connect():
    import sqlite3

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
    except Exception:
        pass
    return conn


# ====================== POSTGRES ======================
class _PgRow(dict):
    """Dict that also supports index access like sqlite3.Row (row[0], row['col'])."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


class _PgCursor:
    """Cursor wrapper: converts ? → %s and returns row-like objects."""

    def __init__(self, real_cursor):
        self._c = real_cursor
        self.lastrowid = None
        self.rowcount = -1

    def _adapt_sql(self, sql: str) -> str:
        if not sql:
            return sql
        s = sql
        # Placeholders
        s = s.replace("?", "%s")
        # Common SQLite → Postgres
        s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.IGNORECASE)
        s = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO",
            "INSERT INTO",
            s,
            flags=re.IGNORECASE,
        )
        # COLLATE NOCASE → just drop (use ILIKE in app when needed)
        s = re.sub(r"\s+COLLATE\s+NOCASE", "", s, flags=re.IGNORECASE)
        # datetime('now', '-24 hours') style — basic mapping
        s = re.sub(
            r"datetime\s*\(\s*'now'\s*,\s*'-(\d+)\s+hours?'\s*\)",
            r"(NOW() - INTERVAL '\1 hours')",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r"datetime\s*\(\s*'now'\s*,\s*'-(\d+)\s+seconds?'\s*\)",
            r"(NOW() - INTERVAL '\1 seconds')",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r"datetime\s*\(\s*'now'\s*\)",
            "NOW()",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"\bCURRENT_TIMESTAMP\b", "NOW()", s, flags=re.IGNORECASE)
        # strftime for Postgres (limited)
        s = re.sub(
            r"strftime\s*\(\s*'%H'\s*,\s*([^)]+)\)",
            r"EXTRACT(HOUR FROM \1)::INTEGER",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r"strftime\s*\(\s*'%w'\s*,\s*([^)]+)\)",
            r"EXTRACT(DOW FROM \1)::INTEGER",
            s,
            flags=re.IGNORECASE,
        )
        return s

    def execute(self, sql, params=None):
        adapted = self._adapt_sql(sql)
        params = params if params is not None else ()
        if isinstance(params, list):
            params = tuple(params)
        try:
            self._c.execute(adapted, params)
        except Exception as e:
            # If INSERT OR IGNORE was stripped and hits unique conflict, ignore
            err = str(e).lower()
            if "duplicate key" in err or "unique constraint" in err:
                self.rowcount = 0
                self.lastrowid = None
                return self
            raise
        self.rowcount = self._c.rowcount
        # lastrowid: try RETURNING id pattern — not available here; use currval when needed
        try:
            if self._c.description is None and "INSERT" in adapted.upper():
                # best-effort lastrowid
                self._c.execute("SELECT lastval()")
                row = self._c.fetchone()
                self.lastrowid = row[0] if row else None
        except Exception:
            self.lastrowid = None
        return self

    def executemany(self, sql, seq_of_params):
        adapted = self._adapt_sql(sql)
        self._c.executemany(adapted, seq_of_params)
        self.rowcount = self._c.rowcount
        return self

    def fetchone(self):
        row = self._c.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return _PgRow(row)
        cols = [d[0] for d in self._c.description] if self._c.description else []
        return _PgRow(dict(zip(cols, row)))

    def fetchall(self):
        rows = self._c.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return [_PgRow(r) for r in rows]
        cols = [d[0] for d in self._c.description] if self._c.description else []
        return [_PgRow(dict(zip(cols, r))) for r in rows]

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


class _PgConnection:
    def __init__(self, real_conn):
        self._conn = real_conn

    def cursor(self):
        from psycopg2.extras import RealDictCursor

        return _PgCursor(self._conn.cursor(cursor_factory=RealDictCursor))

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _postgres_connect():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    url = _normalize_database_url(DATABASE_URL)
    raw = psycopg2.connect(url, connect_timeout=15)
    raw.autocommit = False
    return _PgConnection(raw)


# ====================== PUBLIC API ======================
def get_db_connection():
    """Return connection (Postgres on Render/Neon, SQLite locally)."""
    if USE_POSTGRES:
        return _postgres_connect()
    return _sqlite_connect()


def allowed_badge_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_BADGE_EXTENSIONS
    )


# ====================== SCHEMA (Postgres) ======================
def _init_postgres():
    """Create tables on Neon if they do not exist."""
    conn = _postgres_connect()
    cur = conn.cursor()

    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
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
            is_email_verified INTEGER DEFAULT 1,
            full_name TEXT,
            language TEXT DEFAULT 'sw',
            created_at TIMESTAMP DEFAULT NOW(),
            auth_provider TEXT DEFAULT 'local',
            is_deactivated INTEGER DEFAULT 0,
            deactivated_at TEXT,
            warning_count INTEGER DEFAULT 0,
            restricted_until TEXT,
            village_mode INTEGER DEFAULT 0,
            profile_category TEXT,
            birthday TEXT,
            join_year TEXT,
            sex TEXT,
            marital_status TEXT,
            cover_photo TEXT,
            website TEXT,
            facebook TEXT,
            instagram TEXT,
            tiktok TEXT,
            youtube TEXT,
            whatsapp TEXT,
            telegram TEXT,
            x TEXT,
            linkedin TEXT,
            snapchat TEXT,
            pinterest TEXT,
            reddit TEXT,
            discord TEXT,
            github TEXT,
            twitch TEXT,
            spotify TEXT,
            threads TEXT,
            tumblr TEXT,
            vimeo TEXT,
            wordpress TEXT,
            medium TEXT,
            blogger TEXT,
            facebook_id TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pending_registrations (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            otp TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            resend_available_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            full_name TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            shares INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            category TEXT DEFAULT 'general',
            moderation_status TEXT DEFAULT 'approved',
            nsfw_score REAL DEFAULT 0,
            repost_of INTEGER,
            is_draft INTEGER DEFAULT 0,
            scheduled_at TEXT,
            published_at TEXT,
            linkup_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS moderation_flags (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            flag_type TEXT NOT NULL,
            nsfw_score REAL,
            labels TEXT,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TEXT,
            reviewed_at TEXT,
            reviewed_by INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            content TEXT NOT NULL,
            parent_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            is_hidden INTEGER DEFAULT 0,
            updated_at TIMESTAMP,
            linkup_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS comment_likes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS likes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            post_id INTEGER NOT NULL REFERENCES posts(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS saved_posts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            post_id INTEGER NOT NULL REFERENCES posts(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            sender_id INTEGER NOT NULL REFERENCES users(id),
            type TEXT NOT NULL,
            post_id INTEGER,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            action_description TEXT NOT NULL,
            post_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS badge_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
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
            created_at TIMESTAMP DEFAULT NOW(),
            request_type TEXT DEFAULT 'user',
            linkup_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_messages (
            id SERIAL PRIMARY KEY,
            name TEXT,
            email TEXT,
            message TEXT,
            admin_reply TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS follows (
            id SERIAL PRIMARY KEY,
            follower_id INTEGER REFERENCES users(id),
            following_id INTEGER REFERENCES users(id),
            source_post_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reposts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            original_post_id INTEGER NOT NULL REFERENCES posts(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, original_post_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            reporter_id INTEGER NOT NULL REFERENCES users(id),
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            reviewed_at TEXT,
            reviewed_by INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS blocks (
            id SERIAL PRIMARY KEY,
            blocker_id INTEGER NOT NULL REFERENCES users(id),
            blocked_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(blocker_id, blocked_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS post_views (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            post_id INTEGER NOT NULL REFERENCES posts(id),
            watch_seconds REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS community_groups (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT DEFAULT 'general',
            creator_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_members (
            id SERIAL PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES community_groups(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            joined_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(group_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            id SERIAL PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES community_groups(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS community_posts (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL,
            subcategory TEXT DEFAULT 'all',
            user_id INTEGER NOT NULL REFERENCES users(id),
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS private_messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL REFERENCES users(id),
            receiver_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            is_delivered INTEGER DEFAULT 0,
            file_path TEXT,
            media_type TEXT,
            reaction TEXT,
            is_hidden INTEGER DEFAULT 0,
            reply_to_id INTEGER,
            status_id INTEGER,
            deleted_for_sender INTEGER DEFAULT 0,
            deleted_for_receiver INTEGER DEFAULT 0,
            edited_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS call_signals (
            id SERIAL PRIMARY KEY,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            is_read INTEGER DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pinned_chats (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            other_user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, other_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS statuses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            content TEXT,
            file_path TEXT,
            media_type TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            music_path TEXT,
            privacy TEXT DEFAULT 'public'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS status_views (
            id SERIAL PRIMARY KEY,
            status_id INTEGER NOT NULL REFERENCES statuses(id),
            viewer_id INTEGER NOT NULL REFERENCES users(id),
            viewed_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(status_id, viewer_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS status_reactions (
            id SERIAL PRIMARY KEY,
            status_id INTEGER NOT NULL REFERENCES statuses(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            reaction TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(status_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS otps (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            otp TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS data_deletion_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            email TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW(),
            processed_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mutes (
            id SERIAL PRIMARY KEY,
            muter_id INTEGER NOT NULL REFERENCES users(id),
            muted_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(muter_id, muted_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS close_friends (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            friend_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, friend_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hashtags (
            id SERIAL PRIMARY KEY,
            tag TEXT UNIQUE NOT NULL,
            use_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS post_hashtags (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            hashtag_id INTEGER NOT NULL REFERENCES hashtags(id) ON DELETE CASCADE,
            UNIQUE(post_id, hashtag_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS starred_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            message_id INTEGER NOT NULL REFERENCES private_messages(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS archived_chats (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            other_user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, other_user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            session_token TEXT UNIQUE NOT NULL,
            device_info TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            last_active TIMESTAMP DEFAULT NOW(),
            is_current INTEGER DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS linkups (
            id SERIAL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            bio TEXT,
            profile_pic TEXT,
            cover_photo TEXT,
            is_active INTEGER DEFAULT 1,
            is_verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            about TEXT,
            website TEXT,
            phone TEXT,
            email_public TEXT,
            hometown TEXT,
            current_city TEXT,
            country TEXT,
            workplace_name TEXT,
            workplace_role TEXT,
            workplace_city TEXT,
            employment_type TEXT,
            primary_school TEXT,
            primary_year TEXT,
            secondary_school TEXT,
            secondary_year TEXT,
            college_name TEXT,
            college_year TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS linkup_follows (
            id SERIAL PRIMARY KEY,
            follower_id INTEGER NOT NULL REFERENCES users(id),
            linkup_id INTEGER NOT NULL REFERENCES linkups(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(follower_id, linkup_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS otp_attempts (
            key TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """,
        # Indexes
        "CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_posts_linkup ON posts(linkup_id)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pm_pair ON private_messages(sender_id, receiver_id)",
        "CREATE INDEX IF NOT EXISTS idx_linkups_owner ON linkups(owner_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_linkups_username ON linkups(username)",
        "CREATE INDEX IF NOT EXISTS idx_status_user ON statuses(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)",
    ]

    for stmt in statements:
        try:
            cur.execute(stmt)
        except Exception as e:
            print("[init_postgres] warn:", e)
            try:
                conn.rollback()
            except Exception:
                pass

    conn.commit()
    conn.close()
    print("[db] Postgres schema ready (Neon)")


def _init_sqlite():
    """Original SQLite init for local development."""
    import sqlite3

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.execute("PRAGMA foreign_keys = ON;")

    cursor.execute(
        """
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
        """
    )
    # Minimal bootstrap — full ALTER logic of original init_db can stay
    # for local; production uses Postgres path above.
    conn.commit()
    conn.close()


def init_db():
    if USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()


# Run on import
try:
    init_db()
except Exception as e:
    print("[db] init_db error (will retry on first request):", e)

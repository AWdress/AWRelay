import sqlite3
import os
import time
import threading

DB_DIR = "data"
DB_FILE = os.path.join(DB_DIR, "bot_data.db")

# 全局单连接 + 线程锁。
# 配合 WAL 模式与 check_same_thread=False，可安全地通过 asyncio.to_thread 在事件循环外调用，
# 避免每次操作都重新建立连接。
_conn = None
_lock = threading.Lock()


def _get_conn():
    global _conn
    if _conn is None:
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR)
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init_db():
    """初始化数据库并创建表"""
    with _lock:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                admin_msg_id INTEGER PRIMARY KEY,
                user_chat_id INTEGER,
                user_msg_id INTEGER,
                created_at INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_chat_id INTEGER PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verified_users (
                user_chat_id INTEGER PRIMARY KEY,
                verified_at INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # 话题群组模式：存储用户 chat_id → 话题 message_thread_id 的映射
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS topics (
                user_chat_id INTEGER PRIMARY KEY,
                topic_id INTEGER UNIQUE
            )
        ''')
        # 兼容旧库：若 messages 表缺少 created_at 列则补上
        cols = [row[1] for row in cursor.execute("PRAGMA table_info(messages)").fetchall()]
        if "created_at" not in cols:
            cursor.execute("ALTER TABLE messages ADD COLUMN created_at INTEGER")
        # 初始化默认配置
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('spam_enabled', '1'))
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('spam_keywords', 'USDT,博彩,兼职,t.me/,http://,https://'))
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('captcha_enabled', '1'))
        conn.commit()


def save_mapping(admin_msg_id, user_chat_id, user_msg_id):
    """保存管理员收到的消息ID与普通用户信息的映射"""
    with _lock:
        conn = _get_conn()
        conn.execute('''
            INSERT OR REPLACE INTO messages (admin_msg_id, user_chat_id, user_msg_id, created_at)
            VALUES (?, ?, ?, ?)
        ''', (admin_msg_id, user_chat_id, user_msg_id, int(time.time())))
        conn.commit()


def get_mapping(admin_msg_id):
    """通过管理员消息ID获取原始用户信息"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('''
            SELECT user_chat_id, user_msg_id FROM messages WHERE admin_msg_id = ?
        ''', (admin_msg_id,)).fetchone()
        return row


def cleanup_old_mappings(days=7):
    """清理过期映射，防止数据库无限增长，返回删除条数"""
    cutoff = int(time.time()) - days * 86400
    with _lock:
        conn = _get_conn()
        cursor = conn.execute('DELETE FROM messages WHERE created_at IS NOT NULL AND created_at < ?', (cutoff,))
        conn.commit()
        return cursor.rowcount


def get_topic(user_chat_id):
    """获取用户对应的话题 ID，不存在返回 None"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT topic_id FROM topics WHERE user_chat_id = ?', (user_chat_id,)).fetchone()
        return row[0] if row else None


def save_topic(user_chat_id, topic_id):
    """保存用户与话题的映射"""
    with _lock:
        conn = _get_conn()
        conn.execute('INSERT OR REPLACE INTO topics (user_chat_id, topic_id) VALUES (?, ?)', (user_chat_id, topic_id))
        conn.commit()


def get_user_by_topic(topic_id):
    """通过话题 ID 反查用户 chat_id，不存在返回 None"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT user_chat_id FROM topics WHERE topic_id = ?', (topic_id,)).fetchone()
        return row[0] if row else None


def delete_topic(user_chat_id):
    """删除用户的话题缓存（话题被删除后需清除，以便重建）"""
    with _lock:
        conn = _get_conn()
        conn.execute('DELETE FROM topics WHERE user_chat_id = ?', (user_chat_id,))
        conn.commit()


def ban_user(user_chat_id):
    """将用户加入黑名单"""
    with _lock:
        conn = _get_conn()
        conn.execute('INSERT OR IGNORE INTO banned_users (user_chat_id) VALUES (?)', (user_chat_id,))
        conn.commit()


def unban_user(user_chat_id):
    """将用户移出黑名单"""
    with _lock:
        conn = _get_conn()
        conn.execute('DELETE FROM banned_users WHERE user_chat_id = ?', (user_chat_id,))
        conn.commit()


def is_banned(user_chat_id):
    """检查用户是否在黑名单中"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT 1 FROM banned_users WHERE user_chat_id = ?', (user_chat_id,)).fetchone()
        return row is not None


def set_verified(user_chat_id):
    """标记用户已通过人机验证"""
    with _lock:
        conn = _get_conn()
        conn.execute('INSERT OR REPLACE INTO verified_users (user_chat_id, verified_at) VALUES (?, ?)',
                     (user_chat_id, int(time.time())))
        conn.commit()


def is_verified(user_chat_id):
    """检查用户是否已通过人机验证"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT 1 FROM verified_users WHERE user_chat_id = ?', (user_chat_id,)).fetchone()
        return row is not None


def get_setting(key, default=None):
    """获取动态配置"""
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    """保存动态配置"""
    with _lock:
        conn = _get_conn()
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()

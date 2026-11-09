import time
import aiosqlite
from server.config import DB_PATH, MAX_MESSAGES_PER_CONVERSATION

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-2000; -- 限制 SQLite Page Cache 约 2MB，确保总内存极低

CREATE TABLE IF NOT EXISTS conversations (
    conv_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0,
    last_sender TEXT NOT NULL DEFAULT '',
    last_content TEXT NOT NULL DEFAULT '',
    last_timestamp INTEGER NOT NULL DEFAULT 0,
    unread_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    source_node TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0,
    room_name TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'in', -- 'in': 收到消息, 'out': 我方回复
    status TEXT NOT NULL DEFAULT 'delivered'
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_ts ON messages(conv_id, timestamp DESC, id DESC);
"""


def make_conv_id(sender: str, is_group: bool, room_name: str = "") -> tuple[str, str]:
    """
    根据是否为群聊生成统一会话 ID (conv_id) 与展示标题 (title)。
    群聊以 room_name 作为会话标识；私聊以 sender 作为会话标识。
    """
    clean_room = (room_name or "").strip()
    clean_sender = (sender or "").strip()
    if is_group and clean_room:
        return f"group:{clean_room}", clean_room
    return f"user:{clean_sender}", clean_sender


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


async def save_message(
    *,
    sender: str,
    content: str,
    timestamp: int | None = None,
    source_node: str = "pc",
    is_group: bool = False,
    room_name: str = "",
    direction: str = "in",
    status: str = "delivered",
) -> dict:
    """保存上行/下行消息并更新对应会话摘要，同时自动修剪超长历史记录"""
    ts = int(timestamp) if timestamp else int(time.time())
    conv_id, title = make_conv_id(sender, is_group, room_name)
    unread_delta = 1 if direction == "in" else 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            INSERT INTO messages (conv_id, sender, content, timestamp, source_node, is_group, room_name, direction, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_id,
                sender.strip(),
                content,
                ts,
                source_node,
                1 if is_group else 0,
                (room_name or "").strip(),
                direction,
                status,
            ),
        )
        msg_id = cursor.lastrowid

        # 更新或插入会话表
        await db.execute(
            """
            INSERT INTO conversations (conv_id, title, is_group, last_sender, last_content, last_timestamp, unread_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                title = excluded.title,
                is_group = excluded.is_group,
                last_sender = excluded.last_sender,
                last_content = excluded.last_content,
                last_timestamp = MAX(conversations.last_timestamp, excluded.last_timestamp),
                unread_count = CASE
                    WHEN ? = 'in' THEN conversations.unread_count + 1
                    ELSE 0
                END
            """,
            (
                conv_id,
                title,
                1 if is_group else 0,
                sender.strip(),
                content,
                ts,
                unread_delta,
                direction,
            ),
        )

        # 控制每个会话的最大历史消息数，防止老旧笔记本存储膨胀
        await db.execute(
            """
            DELETE FROM messages
            WHERE conv_id = ? AND id NOT IN (
                SELECT id FROM messages
                WHERE conv_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
            )
            """,
            (conv_id, conv_id, MAX_MESSAGES_PER_CONVERSATION),
        )

        await db.commit()

    return {
        "id": msg_id,
        "conv_id": conv_id,
        "title": title,
        "sender": sender.strip(),
        "content": content,
        "timestamp": ts,
        "source_node": source_node,
        "is_group": bool(is_group),
        "room_name": (room_name or "").strip(),
        "direction": direction,
        "status": status,
    }


async def list_conversations() -> list[dict]:
    """按最新消息时间倒序获取所有会话列表"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT conv_id, title, is_group, last_sender, last_content, last_timestamp, unread_count
            FROM conversations
            ORDER BY last_timestamp DESC
            LIMIT 100
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "conv_id": r["conv_id"],
                    "title": r["title"],
                    "is_group": bool(r["is_group"]),
                    "last_sender": r["last_sender"],
                    "last_content": r["last_content"],
                    "last_timestamp": r["last_timestamp"],
                    "unread_count": r["unread_count"],
                }
                for r in rows
            ]


async def get_conversation_messages(conv_id: str, limit: int = 80) -> list[dict]:
    """获取指定会话的消息历史（按时间正序返回供前端渲染）并清零未读计数"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "UPDATE conversations SET unread_count = 0 WHERE conv_id = ?",
            (conv_id,),
        )
        await db.commit()

        async with db.execute(
            """
            SELECT id, conv_id, sender, content, timestamp, source_node, is_group, room_name, direction, status
            FROM (
                SELECT * FROM messages
                WHERE conv_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, id ASC
            """,
            (conv_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "conv_id": r["conv_id"],
                    "sender": r["sender"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "source_node": r["source_node"],
                    "is_group": bool(r["is_group"]),
                    "room_name": r["room_name"],
                    "direction": r["direction"],
                    "status": r["status"],
                }
                for r in rows
            ]


async def clear_unread(conv_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE conversations SET unread_count = 0 WHERE conv_id = ?",
            (conv_id,),
        )
        await db.commit()

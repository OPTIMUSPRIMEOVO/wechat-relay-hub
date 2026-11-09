import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Web 客户端访问密码 (支持 Header: Authorization: Bearer <pwd> 或 X-Client-Token 或 WS query token)
CLIENT_PASSWORD: str = os.getenv("CLIENT_PASSWORD", "xz1c-secret-2026")

# PC / Android 节点通信静态 API_KEY (支持 Header: X-API-Key 或 WS query api_key)
NODE_API_KEY: str = os.getenv("NODE_API_KEY", "node-static-key-9527")

# PC 节点心跳超时时间 (秒)：默认 10s 一次心跳，超过 15s 判定 PC_OFFLINE
PC_HEARTBEAT_TIMEOUT: float = float(os.getenv("PC_HEARTBEAT_TIMEOUT", "15"))

# 内存消息去重窗口 (秒)：默认 5 秒内相同 (会话 + 发送者 + 内容) 视为重复上报
DEDUP_TTL_SECONDS: float = float(os.getenv("DEDUP_TTL_SECONDS", "5"))

# SQLite 数据库存储路径
DB_PATH: str = os.getenv("DB_PATH", str(DATA_DIR / "relay.db"))

# 每个会话最多保留的历史消息条数（防止老旧笔记本磁盘/内存膨胀）
MAX_MESSAGES_PER_CONVERSATION: int = int(os.getenv("MAX_MESSAGES_PER_CONVERSATION", "200"))


def verify_client_token(token: str | None) -> bool:
    """恒定时间比较校验 Web 客户端访问密码/Token"""
    if not token:
        return False
    clean = token.removeprefix("Bearer ").strip()
    return secrets.compare_digest(clean, CLIENT_PASSWORD)


def verify_node_api_key(api_key: str | None) -> bool:
    """恒定时间比较校验 PC/Android 节点的静态 API_KEY"""
    if not api_key:
        return False
    clean = api_key.removeprefix("Bearer ").strip()
    return secrets.compare_digest(clean, NODE_API_KEY)

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import time
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from server.config import (
    DEDUP_TTL_SECONDS,
    PC_HEARTBEAT_TIMEOUT,
    verify_client_token,
    verify_node_api_key,
)
from server.db import (
    clear_unread,
    get_conversation_messages,
    init_db,
    list_conversations,
    save_message,
)
from server.dedup import MessageDeduplicator
from server.state import RelayStateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("relay.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 全局单例：去重引擎 + 状态与连接池管理器
deduplicator = MessageDeduplicator(ttl_seconds=DEDUP_TTL_SECONDS, max_size=512)
state_manager = RelayStateManager(pc_timeout=PC_HEARTBEAT_TIMEOUT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    watchdog_task = asyncio.create_task(state_manager.watchdog_loop())
    logger.info(
        "WeChat Relay Hub started (PC timeout=%ss, Dedup TTL=%ss)",
        PC_HEARTBEAT_TIMEOUT,
        DEDUP_TTL_SECONDS,
    )
    try:
        yield
    finally:
        watchdog_task.cancel()


app = FastAPI(
    title="WeChat Relay Hub",
    description="Lightweight High-Availability WeChat Web Relay System for Sony XZ1 Compact",
    version="1.0.0",
    lifespan=lifespan,
)


# ==================== 鉴权依赖项 ====================


async def require_client_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_client_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> str:
    candidate = x_client_token or authorization or token
    if not verify_client_token(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing client password/token",
        )
    return "authenticated"


async def require_node_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> str:
    candidate = x_api_key or authorization or api_key
    if not verify_node_api_key(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing node API_KEY",
        )
    return "node_authenticated"


# ==================== Pydantic 数据模型 ====================


class AuthVerifyRequest(BaseModel):
    password: str


class HeartbeatRequest(BaseModel):
    node_type: str = Field(default="pc", description="节点类型: pc 或 mobile")
    timestamp: int | None = None


class WebhookMessageRequest(BaseModel):
    sender: str = Field(..., min_length=1, description="消息发送者昵称/备注")
    content: str = Field(..., min_length=1, description="消息文本内容")
    timestamp: int | None = Field(default=None, description="Unix 秒级时间戳")
    source_node: str = Field(default="pc", description="上报来源节点: pc 或 mobile")
    is_group: bool = Field(default=False, description="是否为群聊消息")
    room_name: str = Field(default="", description="如果是群聊，群名称")


class ClientReplyRequest(BaseModel):
    target: str = Field(..., min_length=1, description="回复目标联系人或群聊名称")
    content: str = Field(..., min_length=1, description="回复文本内容")
    is_group: bool = Field(default=False, description="是否为群聊")
    room_name: str = Field(default="", description="群聊名称（若为群聊）")
    conv_id: str = Field(default="", description="前端当前会话 ID")
    preferred_node: str | None = Field(
        default=None, description="可选强制指定节点 (pc / mobile)，默认由主备状态机自动决定"
    )


# ==================== 核心内部处理函数 ====================


async def process_incoming_wechat_message(msg: WebhookMessageRequest) -> dict[str, Any]:
    """
    统一处理来自 Webhook 或 Node WebSocket 的上行微信新消息：
    1. 顺带刷新对应来源节点的心跳
    2. 执行 5 秒滑动窗口内存去重 (Deduplication)
    3. 写入 SQLite WAL 持久化
    4. 通过 /ws/client 实时推送到所有打开的浏览器前端
    """
    source = "pc" if msg.source_node.lower().strip() == "pc" else "mobile"
    state_manager.record_heartbeat(source)
    await state_manager.broadcast_status_if_changed(force=False)

    is_dup, fingerprint = deduplicator.is_duplicate(
        sender=msg.sender,
        content=msg.content,
        is_group=msg.is_group,
        room_name=msg.room_name,
    )

    if is_dup:
        logger.info(
            "Ignored duplicate message [%s] from %s (sender=%s)",
            fingerprint[:8],
            source,
            msg.sender,
        )
        return {
            "ok": True,
            "deduplicated": True,
            "fingerprint": fingerprint,
            "source_node": source,
        }

    saved = await save_message(
        sender=msg.sender,
        content=msg.content,
        timestamp=msg.timestamp or int(time.time()),
        source_node=source,
        is_group=msg.is_group,
        room_name=msg.room_name,
        direction="in",
        status="delivered",
    )

    await state_manager.broadcast_to_clients(
        {
            "type": "new_message",
            "data": saved,
            "node_status": state_manager.get_status_snapshot(),
        }
    )

    logger.info(
        "Relayed new message [%s] via %s -> conv=%s sender=%s",
        fingerprint[:8],
        source,
        saved["conv_id"],
        saved["sender"],
    )
    return {
        "ok": True,
        "deduplicated": False,
        "fingerprint": fingerprint,
        "message": saved,
    }


async def process_client_reply(req: ClientReplyRequest) -> dict[str, Any]:
    """
    统一处理用户在 Web 前端发起的回复：
    1. 根据主备路由状态机（PC_ONLINE 优先 PC，PC_OFFLINE 自动降级 Android）下发回复指令
    2. 将我方发出的消息持久化到 SQLite 并标记实际路由节点
    3. 广播给所有在线 Web 客户端同步聊天流
    """
    target_name = req.room_name.strip() if (req.is_group and req.room_name.strip()) else req.target.strip()
    dispatch_result = await state_manager.dispatch_reply_command(
        target=target_name,
        content=req.content,
        is_group=req.is_group,
        room_name=req.room_name.strip() if req.is_group else "",
        conv_id=req.conv_id,
        preferred_node=req.preferred_node,
    )

    routed_node = dispatch_result["routed_node"]
    saved_out = await save_message(
        sender="我" if req.is_group else target_name,
        content=req.content,
        timestamp=int(time.time()),
        source_node=routed_node,
        is_group=req.is_group,
        room_name=target_name if req.is_group else "",
        direction="out",
        status=dispatch_result["status"],
    )

    await state_manager.broadcast_to_clients(
        {
            "type": "outbound_message",
            "data": saved_out,
            "dispatch": dispatch_result,
            "node_status": state_manager.get_status_snapshot(),
        }
    )

    return {
        "ok": True,
        "message": saved_out,
        "dispatch": dispatch_result,
        "node_status": state_manager.get_status_snapshot(),
    }


# ==================== 页面与 API 路由 ====================


@app.get("/", include_in_schema=False)
async def serve_index():
    """挂载单文件极简移动端 Web 前端 (针对 4.6 英寸 Sony XZ1 Compact 适配)"""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": int(time.time())}


@app.post("/api/auth/verify")
async def api_verify_auth(body: AuthVerifyRequest):
    """前端首次输入密码登录时的校验接口"""
    if not verify_client_token(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="密码错误，请检查 CLIENT_PASSWORD 配置",
        )
    return {
        "ok": True,
        "node_status": state_manager.get_status_snapshot(),
    }


@app.get("/api/status", dependencies=[Depends(require_client_auth)])
async def api_get_status():
    """查询当前中枢主备状态与内存去重缓存计数"""
    return {
        "ok": True,
        "node_status": state_manager.get_status_snapshot(),
        "dedup_cache_size": deduplicator.size(),
    }


@app.post("/api/heartbeat", dependencies=[Depends(require_node_auth)])
async def api_node_heartbeat(body: HeartbeatRequest = HeartbeatRequest()):
    """
    供 PC 节点 (每 10 秒) 或 Android 备用节点发送心跳包。
    同时返回是否有积压在该节点的待发送回复指令，方便轻量脚本顺带消费。
    """
    snap = state_manager.record_heartbeat(body.node_type)
    await state_manager.broadcast_status_if_changed(force=False)
    pending = await state_manager.poll_pending_commands(body.node_type, wait_seconds=0.0)
    return {
        "ok": True,
        "node_status": snap,
        "commands": pending,
    }


@app.post("/api/webhook/message", dependencies=[Depends(require_node_auth)])
async def api_webhook_message(body: WebhookMessageRequest):
    """
    接收来自 PC 端或 Android 端的微信新消息推送 (JSON)，
    内置 5 秒滑动窗口内存去重，确保前端只收到一次推送。
    """
    return await process_incoming_wechat_message(body)


@app.get("/api/node/commands", dependencies=[Depends(require_node_auth)])
async def api_poll_node_commands(
    node_type: str = Query(default="mobile", description="节点类型: mobile 或 pc"),
    wait: float = Query(default=0.0, ge=0.0, le=25.0, description="长轮询等待秒数 (0 为立即返回)"),
):
    """
    面向 Android Tasker / MacroDroid / 轻量脚本的指令拉取接口（支持长轮询 wait=20）。
    无需手机端拥有公网 IP 或开放任何入站端口。
    """
    cmds = await state_manager.poll_pending_commands(node_type=node_type, wait_seconds=wait)
    await state_manager.broadcast_status_if_changed(force=False)
    return {
        "ok": True,
        "node_type": node_type,
        "count": len(cmds),
        "commands": cmds,
    }


@app.get("/api/conversations", dependencies=[Depends(require_client_auth)])
async def api_list_conversations():
    """获取最近联系人会话列表（自动按最新消息时间倒序排列）"""
    convs = await list_conversations()
    return {
        "ok": True,
        "conversations": convs,
        "node_status": state_manager.get_status_snapshot(),
    }


@app.get("/api/messages", dependencies=[Depends(require_client_auth)])
async def api_get_messages(
    conv_id: str = Query(..., description="会话 ID，例如 user:张三 或 group:家庭群"),
    limit: int = Query(default=80, ge=1, le=200),
):
    """获取指定会话的历史消息流并自动清除未读红点"""
    msgs = await get_conversation_messages(conv_id=conv_id, limit=limit)
    return {
        "ok": True,
        "conv_id": conv_id,
        "messages": msgs,
    }


@app.post("/api/reply", dependencies=[Depends(require_client_auth)])
async def api_send_reply(body: ClientReplyRequest):
    """Web 前端发送回复消息接口（优先路由 PC，离线自动降级 Android）"""
    return await process_client_reply(body)


# ==================== WebSocket 双向实时通道 ====================


@app.websocket("/ws/client")
async def ws_client_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    """
    Web 浏览器前端实时通道 (/ws/client?token=...)
    - 握手校验 Password/Token
    - 实时下发: 新消息 (new_message)、发信回执 (outbound_message)、主备节点切换状态 (node_status)
    - 支持上行: 直接通过 WS 发送 {"action": "send_reply", ...} 或 {"action": "mark_read", "conv_id": ...}
    """
    header_token = websocket.headers.get("x-client-token") or websocket.headers.get("authorization")
    if not verify_client_token(token or header_token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized client token")
        return

    await websocket.accept()
    await state_manager.register_client(websocket)

    try:
        while True:
            payload = await websocket.receive_json()
            action = payload.get("action") or payload.get("type")

            if action == "ping":
                await state_manager.send_to_client(
                    websocket,
                    {
                        "type": "pong",
                        "data": state_manager.get_status_snapshot(),
                    },
                )
            elif action == "mark_read":
                conv_id = payload.get("conv_id", "")
                if conv_id:
                    await clear_unread(conv_id)
            elif action == "send_reply":
                req = ClientReplyRequest(
                    target=payload.get("target", ""),
                    content=payload.get("content", ""),
                    is_group=bool(payload.get("is_group", False)),
                    room_name=payload.get("room_name", ""),
                    conv_id=payload.get("conv_id", ""),
                    preferred_node=payload.get("preferred_node"),
                )
                await process_client_reply(req)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Client WS closed: %s", exc)
    finally:
        state_manager.unregister_client(websocket)


@app.websocket("/ws/node")
async def ws_node_endpoint(
    websocket: WebSocket,
    node_type: str = Query(default="pc"),
    api_key: str | None = Query(default=None),
):
    """
    PC 主节点 / Android 备用节点反向连接通道 (/ws/node?node_type=pc&api_key=...)
    - 节点主动连接中枢，彻底免去节点侧公网 IP 或端口映射需求
    - 支持在连接内发送 {"type": "heartbeat"} 维持 PC_ONLINE 状态
    - 支持在连接内直接上报微信新消息 {"type": "uplink_message", "data": {...}}
    - 实时接收中枢下发的回复指令 {"type": "reply_command", "data": {...}}
    """
    header_key = websocket.headers.get("x-api-key") or websocket.headers.get("authorization")
    if not verify_node_api_key(api_key or header_key):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized node API_KEY")
        return

    norm_node = "pc" if node_type.lower().strip() == "pc" else "mobile"
    await websocket.accept()
    await state_manager.register_node(norm_node, websocket)
    logger.info("Node [%s] connected via WebSocket", norm_node)

    try:
        while True:
            payload = await websocket.receive_json()
            msg_type = payload.get("type") or payload.get("action")

            if msg_type in ("heartbeat", "ping"):
                snap = state_manager.record_heartbeat(norm_node)
                await state_manager.broadcast_status_if_changed(force=False)
                await websocket.send_json({"type": "heartbeat_ack", "data": snap})

            elif msg_type == "uplink_message":
                raw = payload.get("data") or payload
                msg_obj = WebhookMessageRequest(
                    sender=raw.get("sender", "未知联系人"),
                    content=raw.get("content", ""),
                    timestamp=raw.get("timestamp"),
                    source_node=norm_node,
                    is_group=bool(raw.get("is_group", False)),
                    room_name=raw.get("room_name", ""),
                )
                res = await process_incoming_wechat_message(msg_obj)
                await websocket.send_json({"type": "uplink_ack", "data": res})

            elif msg_type == "command_ack":
                await state_manager.broadcast_to_clients(
                    {
                        "type": "command_ack",
                        "data": {
                            "node": norm_node,
                            "command_id": payload.get("command_id"),
                            "ok": payload.get("ok", True),
                            "detail": payload.get("detail", ""),
                        },
                    }
                )
    except WebSocketDisconnect:
        logger.info("Node [%s] WebSocket disconnected", norm_node)
    except Exception as exc:
        logger.warning("Node [%s] WebSocket error: %s", norm_node, exc)
    finally:
        await state_manager.unregister_node(norm_node, websocket)

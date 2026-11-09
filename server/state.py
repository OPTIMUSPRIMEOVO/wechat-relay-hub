import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any
from fastapi import WebSocket
from server.config import PC_HEARTBEAT_TIMEOUT

logger = logging.getLogger("relay.state")


class RelayStateManager:
    """
    中枢核心状态机与双向连接池管理：
    1. 维护 PC 节点心跳时间戳与主备路由状态机 (PC_ONLINE / PC_OFFLINE)
    2. 维护 Web 前端 WebSocket 广播池 (/ws/client)
    3. 维护 PC / Mobile 节点 WebSocket 指令通道 (/ws/node)
    4. 提供面向 Android Tasker 的内存指令队列 + 异步长轮询唤醒机制 (无需公网 IP 与复杂后台保活)
    """

    def __init__(self, pc_timeout: float = PC_HEARTBEAT_TIMEOUT):
        self.pc_timeout = pc_timeout
        self.last_pc_heartbeat: float = 0.0
        self.last_mobile_heartbeat: float = 0.0
        self._last_broadcasted_pc_online: bool | None = None

        # 浏览器前端 WebSocket 集合
        self.client_sockets: set[WebSocket] = set()

        # 节点 WebSocket 字典: {"pc": set[WebSocket], "mobile": set[WebSocket]}
        self.node_sockets: dict[str, set[WebSocket]] = {
            "pc": set(),
            "mobile": set(),
        }

        # 针对不便維持长连接 WebSocket 的 Android 端 (如 Tasker 定时/长轮询拉取) 准备的待执行指令队列
        self.pending_commands: dict[str, deque[dict[str, Any]]] = {
            "pc": deque(maxlen=100),
            "mobile": deque(maxlen=100),
        }
        self.command_events: dict[str, asyncio.Event] = {
            "pc": asyncio.Event(),
            "mobile": asyncio.Event(),
        }

    # -------------------- 心跳与状态机 --------------------

    def record_heartbeat(self, node_type: str = "pc") -> dict[str, Any]:
        now = time.time()
        norm = node_type.lower().strip()
        if norm == "pc":
            self.last_pc_heartbeat = now
        elif norm in ("mobile", "android", "phone"):
            self.last_mobile_heartbeat = now
        return self.get_status_snapshot()

    @property
    def is_pc_online(self) -> bool:
        """
        核心主备状态判定：
        若距离 PC 节点上一次心跳小于 15 秒 (或当前有活跃的 PC WebSocket 连接且心跳未超时)，判定 PC_ONLINE；
        否则判定 PC_OFFLINE。
        """
        if self.last_pc_heartbeat <= 0:
            return False
        return (time.time() - self.last_pc_heartbeat) < self.pc_timeout

    @property
    def active_route_node(self) -> str:
        """下行回复优先路由给 PC 主节点；若 PC_OFFLINE，自动降级路由给 Android 备用节点"""
        return "pc" if self.is_pc_online else "mobile"

    def get_status_snapshot(self) -> dict[str, Any]:
        now = time.time()
        pc_online = self.is_pc_online
        pc_age = round(now - self.last_pc_heartbeat, 1) if self.last_pc_heartbeat > 0 else None
        mobile_age = (
            round(now - self.last_mobile_heartbeat, 1)
            if self.last_mobile_heartbeat > 0
            else None
        )
        return {
            "pc_state": "PC_ONLINE" if pc_online else "PC_OFFLINE",
            "pc_online": pc_online,
            "active_node": "pc" if pc_online else "mobile",
            "active_node_label": "PC 主节点" if pc_online else "手机备用节点",
            "pc_ws_connected": len(self.node_sockets["pc"]) > 0,
            "mobile_ws_connected": len(self.node_sockets["mobile"]) > 0,
            "pc_last_seen_seconds_ago": pc_age,
            "mobile_last_seen_seconds_ago": mobile_age,
            "pending_mobile_commands": len(self.pending_commands["mobile"]),
            "timestamp": int(now),
        }

    # -------------------- Web 前端连接池 (/ws/client) --------------------

    async def register_client(self, ws: WebSocket) -> None:
        self.client_sockets.add(ws)
        await self.send_to_client(
            ws,
            {
                "type": "node_status",
                "data": self.get_status_snapshot(),
            },
        )

    def unregister_client(self, ws: WebSocket) -> None:
        self.client_sockets.discard(ws)

    async def send_to_client(self, ws: WebSocket, payload: dict[str, Any]) -> bool:
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.client_sockets.discard(ws)
            return False

    async def broadcast_to_clients(self, payload: dict[str, Any]) -> None:
        if not self.client_sockets:
            return
        dead: list[WebSocket] = []
        for ws in list(self.client_sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.client_sockets.discard(ws)

    # -------------------- 节点连接池 (/ws/node) --------------------

    async def register_node(self, node_type: str, ws: WebSocket) -> None:
        norm = "pc" if node_type.lower().strip() == "pc" else "mobile"
        self.node_sockets[norm].add(ws)
        self.record_heartbeat(norm)
        await self.broadcast_status_if_changed(force=True)

        # 若队列中有积压的待发指令，节点一连上立即补发
        while self.pending_commands[norm]:
            cmd = self.pending_commands[norm].popleft()
            try:
                await ws.send_json({"type": "reply_command", "data": cmd})
            except Exception:
                # 发送失败则放回队首
                self.pending_commands[norm].appendleft(cmd)
                break

    async def unregister_node(self, node_type: str, ws: WebSocket) -> None:
        norm = "pc" if node_type.lower().strip() == "pc" else "mobile"
        self.node_sockets[norm].discard(ws)
        # 如果 PC 节点主动断开所有 WS 连接，立即将其置为离线以实现秒级故障切换
        if norm == "pc" and len(self.node_sockets["pc"]) == 0:
            self.last_pc_heartbeat = 0.0
        await self.broadcast_status_if_changed(force=True)

    # -------------------- 下行回复路由与指令下发 --------------------

    async def dispatch_reply_command(
        self,
        *,
        target: str,
        content: str,
        is_group: bool = False,
        room_name: str = "",
        conv_id: str = "",
        preferred_node: str | None = None,
    ) -> dict[str, Any]:
        """
        核心主备下行路由：
        1. 若 preferred_node 未指定，则自动根据状态机判断：PC_ONLINE -> 'pc'，PC_OFFLINE -> 'mobile'
        2. 优先通过对应节点的活跃 WebSocket (/ws/node) 实时下发
        3. 若目标为 'pc' 但发送瞬间 WS 异常断开，立即自动降级 (Failover) 到 'mobile'
        4. 若目标节点当前无活跃 WS（例如 Android Tasker 使用 HTTP 轮询模式），则推入内存队列并唤醒长轮询协程
        """
        route_node = preferred_node if preferred_node in ("pc", "mobile") else self.active_route_node
        cmd_payload = {
            "command_id": uuid.uuid4().hex[:12],
            "action": "send_wechat_msg",
            "target": target,
            "content": content,
            "is_group": bool(is_group),
            "room_name": room_name or (target if is_group else ""),
            "conv_id": conv_id,
            "routed_node": route_node,
            "timestamp": int(time.time()),
        }

        delivered_ws = await self._send_to_node_ws(route_node, cmd_payload)
        if delivered_ws:
            return {
                "routed_node": route_node,
                "delivery_mode": "websocket",
                "status": "sent_to_node",
                "command": cmd_payload,
            }

        # 如果原本路由给 PC，但 PC WS 投递失败，则立刻降级至手机备用节点
        if route_node == "pc":
            logger.warning("PC node WS delivery failed, failing over to mobile node immediately.")
            self.last_pc_heartbeat = 0.0
            route_node = "mobile"
            cmd_payload["routed_node"] = "mobile"
            await self.broadcast_status_if_changed(force=True)

            delivered_mobile_ws = await self._send_to_node_ws("mobile", cmd_payload)
            if delivered_mobile_ws:
                return {
                    "routed_node": "mobile",
                    "delivery_mode": "websocket_failover",
                    "status": "sent_to_node",
                    "command": cmd_payload,
                }

        # 存入目标节点的拉取队列 (支持 Android Tasker 长轮询 / HTTP 拉取，或节点重连补发)
        self.pending_commands[route_node].append(cmd_payload)
        self.command_events[route_node].set()
        return {
            "routed_node": route_node,
            "delivery_mode": "queued_for_poll",
            "status": "queued",
            "command": cmd_payload,
        }

    async def _send_to_node_ws(self, node_type: str, cmd_payload: dict[str, Any]) -> bool:
        sockets = self.node_sockets.get(node_type, set())
        if not sockets:
            return False
        dead: list[WebSocket] = []
        sent = False
        for ws in list(sockets):
            try:
                await ws.send_json({"type": "reply_command", "data": cmd_payload})
                sent = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            sockets.discard(ws)
        return sent

    async def poll_pending_commands(
        self, node_type: str = "mobile", wait_seconds: float = 0.0
    ) -> list[dict[str, Any]]:
        """供 Android Tasker 或轻量脚本通过 HTTP (长)轮询拉取待回复指令"""
        norm = "pc" if node_type.lower().strip() == "pc" else "mobile"
        self.record_heartbeat(norm)

        if not self.pending_commands[norm] and wait_seconds > 0:
            evt = self.command_events[norm]
            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=min(wait_seconds, 25.0))
            except asyncio.TimeoutError:
                pass

        items: list[dict[str, Any]] = []
        while self.pending_commands[norm]:
            items.append(self.pending_commands[norm].popleft())
        return items

    # -------------------- 后台状态巡检协程 --------------------

    async def broadcast_status_if_changed(self, force: bool = False) -> None:
        current_online = self.is_pc_online
        if force or (self._last_broadcasted_pc_online != current_online):
            self._last_broadcasted_pc_online = current_online
            await self.broadcast_to_clients(
                {
                    "type": "node_status",
                    "data": self.get_status_snapshot(),
                }
            )

    async def watchdog_loop(self) -> None:
        """每秒巡检一次 PC 心跳是否超过 15 秒，一旦超时立刻通知所有前端切换为手机备用节点状态"""
        while True:
            try:
                await self.broadcast_status_if_changed(force=False)
            except Exception as exc:
                logger.error("Watchdog loop error: %s", exc)
            await asyncio.sleep(1.0)

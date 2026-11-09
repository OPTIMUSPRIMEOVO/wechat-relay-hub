import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] PC-Agent: %(message)s",
)
logger = logging.getLogger("pc_agent")

# ==================== 配置项（支持环境变量或命令行参数覆盖） ====================
RELAY_BASE_URL = os.getenv("RELAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
NODE_API_KEY = os.getenv("NODE_API_KEY", "node-static-key-9527")
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10"))

# 本地微信框架类型: "mock" (调试模式) | "wxauto" | "wcferry" | "gewechat" | "comwechat"
WECHAT_DRIVER = os.getenv("WECHAT_DRIVER", "mock").lower()
GEWECHAT_API_URL = os.getenv("GEWECHAT_API_URL", "http://127.0.0.1:2531/v2/api/message/postText")
COMWECHAT_API_URL = os.getenv("COMWECHAT_API_URL", "http://127.0.0.1:18888/api/sendTextMsg")


# ==================== 本地微信发送钩子函数 (Downlink Execution Hook) ====================

class LocalWeChatBridge:
    """
    封装本地 PC 微信的发送与监听钩子。
    支持主流方案：
    1. wxauto (基于 Windows UIAutomation，免注入，适合个人自用)
    2. WeChatFerry (wcferry，高性能 Hook)
    3. GeweChat / ComWeChat (HTTP RPC 接口调用)
    """

    def __init__(self, driver: str = "mock"):
        self.driver = driver
        self._wcf = None
        self._wxauto = None

        if self.driver == "wcferry":
            try:
                from wcferry import Wcf  # type: ignore
                self._wcf = Wcf(debug=False)
                logger.info("WeChatFerry (Wcf) initialized successfully.")
            except Exception as exc:
                logger.error("Failed to init WeChatFerry: %s", exc)
        elif self.driver == "wxauto":
            try:
                from wxauto import WeChat  # type: ignore
                self._wxauto = WeChat()
                logger.info("wxauto initialized successfully.")
            except Exception as exc:
                logger.error("Failed to init wxauto: %s", exc)

    async def send_wechat_message(
        self,
        target: str,
        content: str,
        is_group: bool = False,
        room_name: str = "",
    ) -> tuple[bool, str]:
        """
        将中枢下发的回复指令真正通过本地 PC 微信发送出去。
        参数:
            target: 好友备注/昵称/wxid 或 群聊名称
            content: 回复的文本内容
            is_group: 是否为群聊
            room_name: 群聊名称
        """
        receiver = room_name if (is_group and room_name) else target
        logger.info("Executing local WeChat send -> receiver=[%s], content=[%s]", receiver, content)

        # --- 方案 A: 调试模式 (Mock) ---
        if self.driver == "mock":
            logger.info("[MOCK SEND SUCCESS] To=%s (is_group=%s): %s", receiver, is_group, content)
            return True, "mock_sent"

        # --- 方案 B: wxauto (Windows 原生控件自动化，无需注入 DLL) ---
        if self.driver == "wxauto" and self._wxauto is not None:
            try:
                await asyncio.to_thread(self._wxauto.SendMsg, msg=content, who=receiver)
                return True, "wxauto_sent"
            except Exception as exc:
                logger.error("wxauto send error: %s", exc)
                return False, str(exc)

        # --- 方案 C: WeChatFerry (wcferry) ---
        if self.driver == "wcferry" and self._wcf is not None:
            try:
                # 若传入的是昵称/备注，可在此通过 self._wcf.get_contacts() 映射为 wxid / xxx@chatroom
                wxid = self._resolve_wcf_wxid(receiver)
                ret = await asyncio.to_thread(self._wcf.send_text, msg=content, receiver=wxid)
                ok = ret == 0
                return ok, f"wcf_ret_{ret}"
            except Exception as exc:
                logger.error("WeChatFerry send error: %s", exc)
                return False, str(exc)

        # --- 方案 D: GeweChat / ComWeChat 等本地 HTTP 接口 ---
        if self.driver in ("gewechat", "comwechat"):
            url = GEWECHAT_API_URL if self.driver == "gewechat" else COMWECHAT_API_URL
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(
                        url,
                        json={
                            "toWxid": receiver,
                            "wxid": receiver,
                            "content": content,
                            "msg": content,
                        },
                    )
                    resp.raise_for_status()
                    return True, resp.text[:120]
            except Exception as exc:
                logger.error("HTTP driver (%s) send error: %s", self.driver, exc)
                return False, str(exc)

        return False, f"Unknown or uninitialized driver: {self.driver}"

    def _resolve_wcf_wxid(self, name_or_wxid: str) -> str:
        """辅助函数：在 WeChatFerry 中将备注名/群名解析为底层 wxid"""
        if not self._wcf:
            return name_or_wxid
        if name_or_wxid.startswith("wxid_") or name_or_wxid.endswith("@chatroom"):
            return name_or_wxid
        try:
            for c in self._wcf.get_contacts():
                if name_or_wxid in (c.get("remark"), c.get("name"), c.get("wxid")):
                    return c.get("wxid", name_or_wxid)
        except Exception:
            pass
        return name_or_wxid


# ==================== PC 节点核心中枢通信客户端 ====================

class PCRelayAgent:
    """
    负责与远端中枢服务器 (FastAPI) 保持通信：
    1. 每 10 秒发送一次心跳 (/api/heartbeat 与 WebSocket 双保险)
    2. 维持 /ws/node 长连接，秒级接收并执行中枢下发的回复指令
    3. 提供 push_incoming_message() 方法将 PC 微信捕获的新消息 POST 到 /api/webhook/message
    """

    def __init__(self, base_url: str, api_key: str, bridge: LocalWeChatBridge):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bridge = bridge
        self.ws_url = self._build_ws_url()
        self._stop_event = asyncio.Event()

    def _build_ws_url(self) -> str:
        ws_scheme_base = (
            self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        )
        qs = urlencode({"node_type": "pc", "api_key": self.api_key})
        return f"{ws_scheme_base}/ws/node?{qs}"

    async def push_incoming_message(
        self,
        *,
        sender: str,
        content: str,
        is_group: bool = False,
        room_name: str = "",
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """将 PC 端捕获到的微信新消息推送至中枢 /api/webhook/message"""
        payload = {
            "sender": sender,
            "content": content,
            "timestamp": timestamp or int(time.time()),
            "source_node": "pc",
            "is_group": is_group,
            "room_name": room_name if is_group else "",
        }
        headers = {"X-API-Key": self.api_key}
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/webhook/message",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Uplinked message to hub (sender=%s, dedup=%s)",
                sender,
                data.get("deduplicated"),
            )
            return data

    async def http_heartbeat_loop(self) -> None:
        """
        独立每 10 秒向 /api/heartbeat 发送一次心跳包。
        即使 WebSocket 处于重连间隙，也能确保 PC 在线状态稳定，且顺带拉取任何积压指令。
        """
        headers = {"X-API-Key": self.api_key}
        async with httpx.AsyncClient(timeout=6.0) as client:
            while not self._stop_event.is_set():
                try:
                    resp = await client.post(
                        f"{self.base_url}/api/heartbeat",
                        json={"node_type": "pc", "timestamp": int(time.time())},
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        body = resp.json()
                        for cmd in body.get("commands", []):
                            await self._handle_reply_command(cmd, ws=None)
                except Exception as exc:
                    logger.warning("HTTP heartbeat failed: %s", exc)

                await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def ws_node_loop(self) -> None:
        """维持到中枢 /ws/node 的出站 WebSocket 连接，无需本地公网 IP 或端口映射"""
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                logger.info("Connecting to Relay Hub WS: %s", self.base_url)
                # 兼容不同版本 websockets (Python 3.10-3.13)
                try:
                    connect_ctx = websockets.connect(
                        self.ws_url,
                        extra_headers={"X-API-Key": self.api_key},
                        ping_interval=15,
                        ping_timeout=15,
                    )
                except TypeError:
                    connect_ctx = websockets.connect(
                        self.ws_url,
                        ping_interval=15,
                        ping_timeout=15,
                    )

                async with connect_ctx as ws:
                    logger.info("Connected to Relay Hub as PC Primary Node (🟢 PC_ONLINE)")
                    backoff = 2.0

                    while not self._stop_event.is_set():
                        raw = await ws.recv()
                        packet = json.loads(raw)
                        pkt_type = packet.get("type")

                        if pkt_type == "reply_command":
                            cmd = packet.get("data") or {}
                            await self._handle_reply_command(cmd, ws=ws)

            except Exception as exc:
                logger.warning("Node WS disconnected (%s). Reconnecting in %.1fs...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 20.0)

    async def _handle_reply_command(self, cmd: dict[str, Any], ws: Any = None) -> None:
        cmd_id = cmd.get("command_id", "")
        target = cmd.get("target", "")
        content = cmd.get("content", "")
        is_group = bool(cmd.get("is_group", False))
        room_name = cmd.get("room_name", "")

        ok, detail = await self.bridge.send_wechat_message(
            target=target,
            content=content,
            is_group=is_group,
            room_name=room_name,
        )
        if ws is not None:
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "command_ack",
                            "command_id": cmd_id,
                            "ok": ok,
                            "detail": detail,
                        }
                    )
                )
            except Exception:
                pass

    async def wcferry_message_listener_loop(self) -> None:
        """若启用 WeChatFerry，自动从队列读取新消息并上报至中枢"""
        if self.bridge.driver != "wcferry" or not self.bridge._wcf:
            return
        wcf = self.bridge._wcf
        wcf.enable_receiving_msg()
        logger.info("WeChatFerry message receiving loop started.")
        while not self._stop_event.is_set():
            try:
                msg = await asyncio.to_thread(wcf.get_msg, block=True)
                if msg and msg.type == 1 and not msg.from_self():
                    is_group = bool(msg.from_group())
                    sender_name = msg.sender
                    room_name = msg.roomid if is_group else ""
                    await self.push_incoming_message(
                        sender=sender_name,
                        content=msg.content,
                        is_group=is_group,
                        room_name=room_name,
                    )
            except Exception:
                await asyncio.sleep(0.5)

    async def wxauto_message_listener_loop(self) -> None:
        """若启用 wxauto，定时从 PC 微信监听新收到的消息并上报至中枢"""
        if self.bridge.driver != "wxauto" or not self.bridge._wxauto:
            return
        wx = self.bridge._wxauto
        logger.info("wxauto message listener loop started (listening for PC WeChat messages)...")
        while not self._stop_event.is_set():
            try:
                msgs = await asyncio.to_thread(wx.GetNewMessage)
                if msgs and isinstance(msgs, dict):
                    for chat_name, msg_list in msgs.items():
                        for item in msg_list:
                            content = ""
                            actual_sender = chat_name
                            if isinstance(item, (list, tuple)) and len(item) >= 2:
                                actual_sender = str(item[0]) if item[0] else chat_name
                                content = str(item[1])
                            elif hasattr(item, "sender") and hasattr(item, "content"):
                                actual_sender = item.sender
                                content = item.content
                            elif isinstance(item, str):
                                content = item
                            if content.strip():
                                await self.push_incoming_message(
                                    sender=actual_sender,
                                    content=content.strip(),
                                    is_group=False,
                                    room_name="",
                                )
            except Exception as exc:
                logger.debug("wxauto poll error: %s", exc)
            await asyncio.sleep(1.0)

    async def run_forever(self) -> None:
        await asyncio.gather(
            self.http_heartbeat_loop(),
            self.ws_node_loop(),
            self.wcferry_message_listener_loop(),
            self.wxauto_message_listener_loop(),
        )


async def run_simulation_demo(agent: PCRelayAgent) -> None:
    """联调测试模式：启动后模拟一条来自 PC 的微信消息上报，并保持在线等待 Web 前端回复"""
    await asyncio.sleep(2.0)
    logger.info("Sending simulated incoming WeChat message from PC node...")
    try:
        await agent.push_incoming_message(
            sender="文件传输助手",
            content="你好！这是一条来自 PC 主节点的测试消息（Sony XZ1C 联调测试）。",
            is_group=False,
        )
    except Exception as exc:
        logger.error("Simulation push failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Relay Hub - PC Primary Node Agent")
    parser.add_argument("--url", default=RELAY_BASE_URL, help="Relay Hub base URL (e.g. https://wx.example.com)")
    parser.add_argument("--key", default=NODE_API_KEY, help="Static NODE_API_KEY")
    parser.add_argument("--driver", default=WECHAT_DRIVER, choices=["mock", "wxauto", "wcferry", "gewechat", "comwechat"])
    parser.add_argument("--simulate", action="store_true", help="Send a test message on startup for quick verification")
    args = parser.parse_args()

    bridge = LocalWeChatBridge(driver=args.driver)
    agent = PCRelayAgent(base_url=args.url, api_key=args.key, bridge=bridge)

    async def runner():
        tasks = [asyncio.create_task(agent.run_forever())]
        if args.simulate:
            tasks.append(asyncio.create_task(run_simulation_demo(agent)))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("PC Agent stopped by user (中枢将在 15 秒内自动降级至手机备用节点).")


if __name__ == "__main__":
    main()

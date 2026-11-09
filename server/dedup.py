import hashlib
import time
from collections import OrderedDict
from threading import Lock


class MessageDeduplicator:
    """
    基于 TTL + LRU 的极轻量级内存消息去重器。
    当 PC 节点和 Android 备用节点同时在线时，同一条微信消息可能被两端同时捕获并推送至中枢。
    通过计算 (conversation_id + sender + content) 的指纹，在时间窗口 (默认 5s) 内精确拦截重复推送。
    """

    def __init__(self, ttl_seconds: float = 5.0, max_size: int = 512):
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def compute_fingerprint(
        sender: str,
        content: str,
        is_group: bool = False,
        room_name: str = "",
    ) -> str:
        """
        构建标准化消息指纹。
        去除首尾空白符，防止不同端（如 PC 文本 vs 手机通知栏截断空白）产生微小尾随空格差异。
        """
        norm_sender = (sender or "").strip()
        norm_content = (content or "").strip()
        norm_room = (room_name or "").strip() if is_group else ""
        raw = f"{int(bool(is_group))}|{norm_room}|{norm_sender}|{norm_content}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def is_duplicate(
        self,
        sender: str,
        content: str,
        is_group: bool = False,
        room_name: str = "",
    ) -> tuple[bool, str]:
        """
        判断消息是否在 TTL 窗口内已处理过。
        返回: (is_dup: bool, fingerprint: str)
        """
        now = time.monotonic()
        fp = self.compute_fingerprint(sender, content, is_group, room_name)

        with self._lock:
            # 1. 惰性清理已过期的头部旧记录
            expired_keys = []
            for k, ts in self._cache.items():
                if now - ts > self.ttl_seconds:
                    expired_keys.append(k)
                else:
                    # OrderedDict 按插入/更新时间升序排列，遇到未过期的即可停止扫描
                    break
            for k in expired_keys:
                self._cache.pop(k, None)

            # 2. 命中未过期指纹 -> 判定为重复消息
            if fp in self._cache:
                return True, fp

            # 3. 写入新指纹，并維持 LRU 容量上限
            self._cache[fp] = now
            self._cache.move_to_end(fp)
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)

            return False, fp

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

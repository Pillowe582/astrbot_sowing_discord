# -*- coding: utf-8 -*-
"""按目标群判断「最近是否已经搬过同一条史」(纯逻辑, 不依赖 AstrBot)。

去重范围严格按目标群隔离:
- 判断只发生在一个目标群内部: 该群最近有没有收到过同一条史(消息指纹/内容哈希);
- 不看其它目标群: 一条史已经搬给群 A, 不会阻止它再搬给群 B;
- 因此多目标群场景下只会跳过「已经有的群」, 其它群照常接收。

与 core/tag_trigger.py 的分工: 状态机负责「这条 tag 该转发哪条消息」,
本模块负责「这条消息还该发给哪些目标群」。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

# 单个目标群最多保留多少条去重记录(超出按时间淘汰最旧的)
MAX_TARGET_ENTRIES = 5000


class TargetDedupe:
    """按目标群隔离的重复史记录。"""

    def __init__(
        self,
        fingerprint_seconds: float = 3600.0,
        content_seconds: float = 3600.0,
    ):
        # 0 表示该层去重关闭
        self.fingerprint_seconds = max(0.0, float(fingerprint_seconds))
        self.content_seconds = max(0.0, float(content_seconds))
        self._fingerprints: Dict[str, Dict[str, float]] = {}
        self._contents: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def has_duplicate(
        self,
        target,
        fingerprint: str = "",
        content_id: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """该目标群最近是否已经收到过同指纹/同内容的史。"""
        target = str(target)
        moment = _now() if now is None else float(now)
        self._prune_target(target, moment)

        if fingerprint and self.fingerprint_seconds > 0:
            if fingerprint in self._fingerprints.get(target, {}):
                return True
        if content_id and self.content_seconds > 0:
            if content_id in self._contents.get(target, {}):
                return True
        return False

    def mark_sent(
        self,
        target,
        fingerprint: str = "",
        content_id: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记录「这条史已经成功转发到该目标群」。"""
        target = str(target)
        moment = _now() if now is None else float(now)

        if fingerprint and self.fingerprint_seconds > 0:
            self._fingerprints.setdefault(target, {})[fingerprint] = moment
            self._cap_target(self._fingerprints, target)
        if content_id and self.content_seconds > 0:
            self._contents.setdefault(target, {})[content_id] = moment
            self._cap_target(self._contents, target)
        self.prune(moment)

    def filter_targets(
        self,
        targets: Iterable,
        fingerprint: str = "",
        content_id: str = "",
        now: Optional[float] = None,
    ) -> Tuple[List, List]:
        """把目标群分成 (还需要发送的, 最近已有相同史而跳过的)。"""
        send: List = []
        skipped: List = []
        for target in targets:
            if self.has_duplicate(target, fingerprint, content_id, now=now):
                skipped.append(target)
            else:
                send.append(target)
        return send, skipped

    def prune(self, now: Optional[float] = None) -> None:
        """清理超出各自时间窗口的记录。"""
        moment = _now() if now is None else float(now)
        for target in list(self._fingerprints.keys()):
            self._prune_target(target, moment)
        for target in list(self._contents.keys()):
            self._prune_target(target, moment)

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "fingerprints": {target: dict(items) for target, items in self._fingerprints.items()},
            "contents": {target: dict(items) for target, items in self._contents.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, **kwargs) -> "TargetDedupe":
        dedupe = cls(**kwargs)
        try:
            for target, items in (data.get("fingerprints") or {}).items():
                bucket = dedupe._fingerprints.setdefault(str(target), {})
                for fingerprint, ts in (items or {}).items():
                    try:
                        bucket[str(fingerprint)] = float(ts)
                    except (TypeError, ValueError):
                        continue
            for target, items in (data.get("contents") or {}).items():
                bucket = dedupe._contents.setdefault(str(target), {})
                for content_id, ts in (items or {}).items():
                    try:
                        bucket[str(content_id)] = float(ts)
                    except (TypeError, ValueError):
                        continue
        except (AttributeError, TypeError, ValueError):
            # 损坏的状态文件: 能读多少读多少
            pass
        return dedupe

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _prune_target(self, target: str, now: float) -> None:
        self._prune_bucket(self._fingerprints, target, self.fingerprint_seconds, now)
        self._prune_bucket(self._contents, target, self.content_seconds, now)

    @staticmethod
    def _prune_bucket(
        buckets: Dict[str, Dict[str, float]],
        target: str,
        window_seconds: float,
        now: float,
    ) -> None:
        bucket = buckets.get(target)
        if not bucket:
            return
        if window_seconds <= 0:
            buckets.pop(target, None)
            return
        for key, ts in list(bucket.items()):
            if now - ts > window_seconds:
                bucket.pop(key, None)
        if not bucket:
            buckets.pop(target, None)

    @staticmethod
    def _cap_target(buckets: Dict[str, Dict[str, float]], target: str) -> None:
        bucket = buckets.get(target)
        if not bucket or len(bucket) <= MAX_TARGET_ENTRIES:
            return
        overflow = len(bucket) - MAX_TARGET_ENTRIES
        for key, _ in sorted(bucket.items(), key=lambda kv: kv[1])[:overflow]:
            bucket.pop(key, None)


def _now() -> float:
    import time

    return time.time()

# -*- coding: utf-8 -*-
"""按目标群判断「最近是否已经搬过同一条史」(纯逻辑, 不依赖 AstrBot)。

去重范围:
- 只在一个目标群内部判断: 该群最近收到过的史是否与当前这条特征相同;
- 不看其它目标群: 一条史已经搬给群 A, 不会阻止它再搬给群 B;
- 每个目标群按「条数」保留记录(不按时间过期): 只要还在这 N 条里,
  不管隔了多久都算重复; N 由 tag_dedupe_max_records 配置, 默认 500,
  即「向上找 N 条, 能找多远找多远」;
- N=0 表示关闭去重, 所有目标群都照常接收。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

# 单个目标群最多保留的记录条数上限(配置再大也不会超过, 防止状态文件无限膨胀)
MAX_TARGET_ENTRIES = 5000
DEFAULT_MAX_RECORDS = 500


class TargetDedupe:
    """按目标群隔离的重复史记录(条数窗口)。"""

    def __init__(self, max_records: int = DEFAULT_MAX_RECORDS):
        try:
            value = int(max_records)
        except (TypeError, ValueError):
            value = DEFAULT_MAX_RECORDS
        self.max_records = max(0, min(value, MAX_TARGET_ENTRIES))
        # target -> [{"f": 指纹, "c": 内容哈希, "ts": 发送时间}, ...](旧 -> 新)
        self._records: Dict[str, List[dict]] = {}

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
        """该目标群最近 N 条已搬记录里, 是否有同指纹/同内容的史。"""
        if self.max_records <= 0:
            return False
        for record in self._records.get(str(target), ()):
            if fingerprint and record.get("f") == fingerprint:
                return True
            if content_id and record.get("c") == content_id:
                return True
        return False

    def mark_sent(
        self,
        target,
        fingerprint: str = "",
        content_id: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记录「这条史已经成功转发到该目标群」, 超出条数窗口的旧记录被淘汰。"""
        if self.max_records <= 0:
            return
        fingerprint = str(fingerprint or "")
        content_id = str(content_id or "")
        if not fingerprint and not content_id:
            return
        target = str(target)
        records = self._records.setdefault(target, [])
        records.append(
            {
                "f": fingerprint,
                "c": content_id,
                "ts": float(now) if now is not None else _now(),
            }
        )
        if len(records) > self.max_records:
            del records[: len(records) - self.max_records]

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
            if self.has_duplicate(target, fingerprint, content_id):
                skipped.append(target)
            else:
                send.append(target)
        return send, skipped

    def prune(self, now: Optional[float] = None) -> None:
        """清理空桶(记录本身按条数淘汰, 不按时间过期)。"""
        for target in list(self._records.keys()):
            if not self._records.get(target):
                self._records.pop(target, None)

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "version": 2,
            "targets": {
                target: [dict(record) for record in records]
                for target, records in self._records.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict, **kwargs) -> "TargetDedupe":
        dedupe = cls(**kwargs)
        try:
            targets = data.get("targets")
            if isinstance(targets, dict):
                for target, records in targets.items():
                    bucket = dedupe._records.setdefault(str(target), [])
                    for record in records or []:
                        if not isinstance(record, dict):
                            continue
                        fingerprint = str(record.get("f") or "")
                        content_id = str(record.get("c") or "")
                        if not fingerprint and not content_id:
                            continue
                        try:
                            ts = float(record.get("ts") or 0)
                        except (TypeError, ValueError):
                            ts = 0.0
                        bucket.append({"f": fingerprint, "c": content_id, "ts": ts})
                    dedupe._trim(str(target))
            elif "fingerprints" in data or "contents" in data:
                dedupe._load_legacy(data)
        except (AttributeError, TypeError, ValueError):
            # 损坏的状态文件: 能读多少读多少
            pass
        return dedupe

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _trim(self, target: str) -> None:
        records = self._records.get(target)
        if not records:
            self._records.pop(target, None)
            return
        if len(records) > self.max_records:
            del records[: len(records) - self.max_records]

    def _load_legacy(self, data: dict) -> None:
        """兼容旧版按时间窗口保存的 fingerprints/contents 字典。"""
        entries = []  # (ts, fingerprint, content_id)
        for bucket in data.get("fingerprints") or {}:
            pass
        for key, content_id in (("fingerprints", None), ("contents", True)):
            buckets = data.get(key) or {}
            if not isinstance(buckets, dict):
                continue
            for target, items in buckets.items():
                if not isinstance(items, dict):
                    continue
                for value, ts in items.items():
                    try:
                        moment = float(ts)
                    except (TypeError, ValueError):
                        moment = 0.0
                    entries.append(
                        (
                            str(target),
                            moment,
                            "" if content_id else str(value),
                            str(value) if content_id else "",
                        )
                    )
        for target, moment, fingerprint, content_id in sorted(entries, key=lambda item: item[1]):
            records = self._records.setdefault(target, [])
            records.append({"f": fingerprint, "c": content_id, "ts": moment})
            if len(records) > self.max_records:
                del records[: len(records) - self.max_records]


def _now() -> float:
    import time

    return time.time()

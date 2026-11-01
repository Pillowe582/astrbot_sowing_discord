# -*- coding: utf-8 -*-
"""#tag 触发搬史的状态机（纯逻辑，不依赖 AstrBot，可直接单元测试）。

设计约定:
- 以 (group_id, sender_id) 为维度维护状态:
    last      该发送者最近一条消息(任意类型)
    pending   尚未匹配到史的 tag(默认 60 秒内有效)
    forwarded 已转发消息 ID 的去重表(默认保留 1 小时)
- 匹配规则:
    * tag 的上一条消息恰好是同人的媒体消息, 且时间差 <= match_window_seconds
      -> 转发该媒体消息
    * 否则 tag 进入 pending, 等待该发送者 pending 期间的下一条消息:
      - 是媒体消息且时间差 <= pending_seconds -> 转发该媒体消息
      - 是其它消息 -> 取消等待(严格相邻)
    * 同一条媒体消息只转发一次(去重窗口内)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

MEDIA_KINDS = ("image", "video", "forward")
TEXT_KIND = "text"
OTHER_KIND = "other"

_CQ_PATTERN = re.compile(r"\[CQ:.*?\]", re.S)
_MAX_FORWARDED_ENTRIES = 5000


def is_tag_text(text: str, prefix: str = "#") -> bool:
    """判断文本是否为 tag: 去掉首尾空白后以前缀开头, 且前缀后必须紧跟非空白内容。"""
    if not isinstance(text, str) or not prefix:
        return False
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return False
    if len(stripped) <= len(prefix):
        return False
    return not stripped[len(prefix)].isspace()


def classify_message(message, text: str = "") -> str:
    """返回 image / video / forward / text / other。

    message 支持三种形态:
    - AstrBot 的 Component 列表(如 Plain/Image/Video/Node 对象)
    - OneBot 历史消息里的 dict 段列表(如 {"type": "image", "data": {...}})
    - CQ 码字符串(兜底)
    """
    if isinstance(message, str):
        if "[CQ:image" in message:
            return "image"
        if "[CQ:video" in message:
            return "video"
        if "[CQ:forward" in message or "[CQ:node" in message:
            return "forward"
        return TEXT_KIND if _CQ_PATTERN.sub("", message).strip() else OTHER_KIND

    if isinstance(message, (list, tuple)):
        media: Optional[str] = None
        has_text = bool(text and text.strip())
        for seg in message:
            if isinstance(seg, dict):
                seg_type = str(seg.get("type", "")).lower()
                if seg_type == "image":
                    media = media or "image"
                elif seg_type == "video":
                    media = media or "video"
                elif seg_type in ("forward", "node", "nodes"):
                    media = media or "forward"
                elif seg_type in ("text", "plain"):
                    if str(seg.get("data", {}).get("text", "")).strip():
                        has_text = True
            else:
                cname = seg.__class__.__name__.lower()
                if cname == "image":
                    media = media or "image"
                elif cname == "video":
                    media = media or "video"
                elif cname in ("forward", "node", "nodes"):
                    media = media or "forward"
                elif cname in ("plain", "text"):
                    if str(getattr(seg, "text", "")).strip():
                        has_text = True
        if media:
            return media
        return TEXT_KIND if has_text else OTHER_KIND

    return TEXT_KIND if (text and text.strip()) else OTHER_KIND


@dataclass
class MsgMeta:
    """一条消息的轻量元信息。"""

    group_id: str
    sender_id: str
    message_id: int
    ts: float
    kind: str = OTHER_KIND
    text: str = ""

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_KINDS

    def to_dict(self) -> dict:
        return {
            "g": self.group_id,
            "s": self.sender_id,
            "id": self.message_id,
            "ts": self.ts,
            "k": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MsgMeta":
        return cls(
            group_id=str(data.get("g", "")),
            sender_id=str(data.get("s", "")),
            message_id=int(data.get("id", 0)),
            ts=float(data.get("ts", 0)),
            kind=str(data.get("k", OTHER_KIND)),
            text="",
        )


@dataclass
class Action:
    """状态机的输出: 需要转发的一条媒体消息。"""

    kind: str
    message_id: int
    media_kind: str = OTHER_KIND
    trigger_tag_id: Optional[int] = None


@dataclass
class _SenderState:
    last: Optional[MsgMeta] = None
    pending_tag: Optional[MsgMeta] = None


class TagTrigger:
    """按发送者维护 last/pending 状态的纯逻辑状态机。"""

    def __init__(
        self,
        prefix: str = "#",
        match_window_seconds: float = 30.0,
        pending_seconds: float = 60.0,
        dedupe_seconds: float = 3600.0,
        forward_on_pending: bool = True,
    ):
        self.prefix = prefix or "#"
        self.match_window_seconds = max(0.0, float(match_window_seconds))
        self.pending_seconds = max(0.0, float(pending_seconds))
        self.dedupe_seconds = max(0.0, float(dedupe_seconds))
        self.forward_on_pending = bool(forward_on_pending)
        self._states: Dict[Tuple[str, str], _SenderState] = {}
        self._forwarded: Dict[int, float] = {}

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def is_tag(self, meta: MsgMeta) -> bool:
        return meta.kind == TEXT_KIND and is_tag_text(meta.text, self.prefix)

    def needs_history(self, meta: MsgMeta) -> bool:
        """该发送者在内存/磁盘中没有任何状态时, 需要向上回填历史消息。

        只对 tag 消息返回 True; bot 运行期间收到的普通消息会先建立状态。
        """
        return self.is_tag(meta) and (meta.group_id, meta.sender_id) not in self._states

    def apply_history(self, tag: MsgMeta, history: Iterable[MsgMeta]) -> None:
        """用历史消息回填该发送者在 tag 之前的最后一条消息。"""
        self._prune(tag.ts)
        key = (tag.group_id, tag.sender_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _SenderState()

        best: Optional[MsgMeta] = None
        for item in history:
            if item.group_id != tag.group_id or item.sender_id != tag.sender_id:
                continue
            if item.message_id == tag.message_id:
                continue
            if item.ts > tag.ts:
                continue
            if best is None or (item.ts, item.message_id) > (best.ts, best.message_id):
                best = item
        if best is not None:
            state.last = best

    def observe(self, meta: MsgMeta) -> Optional[Action]:
        """处理一条消息, 返回需要转发的 Action(没有则返回 None)。"""
        self._prune(meta.ts)
        key = (meta.group_id, meta.sender_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _SenderState()

        action: Optional[Action] = None

        if self.is_tag(meta):
            if self._can_match_tag(state.last, meta):
                matched = state.last
                action = Action("forward", matched.message_id, matched.kind, meta.message_id)
                self._mark_forwarded(matched, meta.ts)
                state.pending_tag = None
            elif not self._is_replayed_tag(state.last, meta):
                # 未命中且不是重复 tag -> 进入 pending, 等待同人的下一条消息
                state.pending_tag = meta
            state.last = meta
        else:
            pending = state.pending_tag
            if pending is not None:
                # 严格相邻: 只匹配 pending tag 之后该发送者的下一条消息
                if (
                    state.last is not None
                    and state.last.message_id == pending.message_id
                    and meta.is_media
                    and self.forward_on_pending
                    and 0 <= meta.ts - pending.ts <= self.pending_seconds
                    and self._forwardable(meta)
                ):
                    action = Action("forward", meta.message_id, meta.kind, pending.message_id)
                    self._mark_forwarded(meta, meta.ts)
                state.pending_tag = None
            state.last = meta

        return action

    def expire(self, now: Optional[float] = None) -> None:
        """清理过期的 pending 与去重记录(由 observe 自动调用, 也可手动调用)。"""
        self._prune(_now() if now is None else float(now))

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        states: Dict[str, dict] = {}
        for (group_id, sender_id), state in self._states.items():
            item: Dict[str, dict] = {}
            if state.last is not None:
                item["last"] = state.last.to_dict()
            if state.pending_tag is not None:
                item["pending"] = state.pending_tag.to_dict()
            if item:
                states[f"{group_id}|{sender_id}"] = item
        return {
            "version": 1,
            "states": states,
            "forwarded": {str(message_id): ts for message_id, ts in self._forwarded.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, **kwargs) -> "TagTrigger":
        trigger = cls(**kwargs)
        try:
            for key, item in (data.get("states") or {}).items():
                group_id, _, sender_id = str(key).partition("|")
                if not sender_id:
                    continue
                state = _SenderState()
                last = item.get("last")
                if last:
                    state.last = MsgMeta.from_dict(last)
                pending = item.get("pending")
                if pending:
                    state.pending_tag = MsgMeta.from_dict(pending)
                trigger._states[(group_id, sender_id)] = state
            for message_id, ts in (data.get("forwarded") or {}).items():
                try:
                    trigger._forwarded[int(message_id)] = float(ts)
                except (TypeError, ValueError):
                    continue
        except (AttributeError, TypeError, ValueError):
            # 损坏的状态文件: 能读多少读多少, 读不了的直接丢弃
            pass
        return trigger

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _can_match_tag(self, last: Optional[MsgMeta], tag: MsgMeta) -> bool:
        if last is None or not last.is_media:
            return False
        if last.message_id == tag.message_id:
            return False
        delta = tag.ts - last.ts
        if delta < 0 or delta > self.match_window_seconds:
            return False
        return self._forwardable(last)

    def _is_replayed_tag(self, last: Optional[MsgMeta], tag: MsgMeta) -> bool:
        """上一条就是窗口内已转发过的史: 视为重复 tag, 不再进入 pending。"""
        if last is None or not last.is_media:
            return False
        delta = tag.ts - last.ts
        if delta < 0 or delta > self.match_window_seconds:
            return False
        return not self._forwardable(last)

    def _forwardable(self, meta: MsgMeta) -> bool:
        return meta.message_id not in self._forwarded

    def _mark_forwarded(self, meta: MsgMeta, now: float) -> None:
        self._forwarded[meta.message_id] = now
        if len(self._forwarded) > _MAX_FORWARDED_ENTRIES:
            overflow = len(self._forwarded) - _MAX_FORWARDED_ENTRIES
            for message_id, _ in sorted(self._forwarded.items(), key=lambda kv: kv[1])[:overflow]:
                self._forwarded.pop(message_id, None)

    def _prune(self, now: float) -> None:
        for state in self._states.values():
            if state.pending_tag is not None and now - state.pending_tag.ts > self.pending_seconds:
                state.pending_tag = None
        if self.dedupe_seconds > 0:
            for message_id, ts in list(self._forwarded.items()):
                if now - ts > self.dedupe_seconds:
                    self._forwarded.pop(message_id, None)


def _now() -> float:
    import time

    return time.time()

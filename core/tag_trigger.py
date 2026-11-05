# -*- coding: utf-8 -*-
"""#tag 触发搬史的状态机（纯逻辑，不依赖 AstrBot，可直接单元测试）。

设计约定:
- 以 (group_id, sender_id) 为维度维护状态:
    last      该发送者最近一条消息(任意类型)
    pending   尚未匹配到史的 tag(默认 60 秒内有效)
- 匹配规则:
    * tag 的上一条消息恰好是同人的媒体消息, 且时间差 <= match_window_seconds
      -> 转发该媒体消息
      (平台时间可能因转发消息/时钟偏差比 tag 略晚, 允许 -clock_skew_seconds
       以内的负时间差; 否则会出现「史事件先到, 但平台时间比 tag 晚 1 秒」被漏转)
    * 否则 tag 进入 pending, 等待该发送者 pending 期间的下一条消息:
      - 是媒体消息且时间差 <= pending_seconds -> 转发该媒体消息
      - 是其它消息 -> 取消等待(严格相邻)
- 重复检测不在这里做: 是否「目标群最近已经有了这条史」由发送层按目标群判断,
  状态机只负责回答「这条 tag 应该转发哪条消息」。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

MEDIA_KINDS = ("image", "video", "forward")
TEXT_KIND = "text"
OTHER_KIND = "other"

_CQ_PATTERN = re.compile(r"\[CQ:.*?\]", re.S)
_MAX_RECENT_MESSAGES = 10

# 部分适配器把媒体消息的 message_str 记成占位文本(组件列表为空)时的兜底
_PLACEHOLDER_KINDS = {
    "[图片]": "image",
    "[视频]": "video",
    "[聊天记录]": "forward",
    "[合并转发]": "forward",
    "[转发消息]": "forward",
}


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
        remaining = _CQ_PATTERN.sub("", message).strip()
        if remaining in _PLACEHOLDER_KINDS:
            return _PLACEHOLDER_KINDS[remaining]
        return TEXT_KIND if remaining else OTHER_KIND

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
        if has_text:
            stripped = str(text or "").strip()
            if stripped in _PLACEHOLDER_KINDS:
                return _PLACEHOLDER_KINDS[stripped]
            return TEXT_KIND
        return OTHER_KIND

    stripped = str(text or "").strip()
    if stripped in _PLACEHOLDER_KINDS:
        return _PLACEHOLDER_KINDS[stripped]
    return TEXT_KIND if stripped else OTHER_KIND


# ---------------------------------------------------------------------- #
# 消息指纹: 不依赖网络, 用于识别「同一条媒体内容再次出现」
# ---------------------------------------------------------------------- #
def normalize_url(url) -> str:
    """去掉 URL 里会变化的签名/过期参数, 只保留协议+域名+路径。"""
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        if not parts.scheme or not parts.netloc:
            return url.strip()
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    except ValueError:
        return url.strip()


def _segment_token(seg) -> dict:
    """把一个消息段转成便于比较/哈希的稳定 token。"""
    if isinstance(seg, dict):
        seg_type = str(seg.get("type", "")).lower()
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        token: dict = {"t": seg_type}
        for key, token_key in (
            ("file", "f"),
            ("path", "f"),
            ("file_id", "f"),
            ("id", "i"),
            ("res_id", "i"),
        ):
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                token[token_key] = str(value)
        url = data.get("url")
        if isinstance(url, str) and url:
            token["u"] = normalize_url(url)
        if seg_type in ("text", "plain"):
            text = data.get("text")
            if isinstance(text, str) and text:
                token["tx"] = text
        else:
            content = data.get("content") or data.get("messages")
            if content is not None:
                token["c"] = hashlib.sha256(
                    json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:16]
        return token

    cname = seg.__class__.__name__.lower()
    token = {"t": cname}
    for attr in ("file", "path", "file_id"):
        value = getattr(seg, attr, None)
        if isinstance(value, str) and value.strip():
            token["f"] = value
            break
    url = getattr(seg, "url", None)
    if isinstance(url, str) and url:
        token["u"] = normalize_url(url)
    if cname in ("plain", "text"):
        text = getattr(seg, "text", None)
        if isinstance(text, str) and text:
            token["tx"] = text
    return token


def message_fingerprint(message, text: str = "") -> str:
    """计算消息内容指纹: 文件 id / 转发记录 id / 规范化 URL / 文本。

    返回 32 位十六进制字符串; 消息为空时返回空字符串(表示无法去重)。
    """
    if isinstance(message, (list, tuple)):
        tokens = [_segment_token(seg) for seg in message]
    elif isinstance(message, str) and message.strip():
        tokens = [{"t": "raw", "tx": message.strip()}]
    else:
        tokens = []
    if not tokens:
        return ""
    payload = json.dumps(
        {"segments": tokens, "text": (text or "").strip()},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class MsgMeta:
    """一条消息的轻量元信息。"""

    group_id: str
    sender_id: str
    message_id: int
    ts: float
    kind: str = OTHER_KIND
    text: str = ""
    fingerprint: str = ""

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
            "f": self.fingerprint,
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
            fingerprint=str(data.get("f", "") or ""),
        )


@dataclass
class Action:
    """状态机的输出: 需要转发 message_id 对应的媒体消息。"""

    kind: str
    message_id: int
    media_kind: str = OTHER_KIND
    trigger_tag_id: Optional[int] = None
    fingerprint: str = ""


@dataclass
class _SenderState:
    last: Optional[MsgMeta] = None
    pending_tag: Optional[MsgMeta] = None
    # 该发送者最近处理的若干条消息(按处理顺序), 用于处理「事件到达顺序与平台时间不一致」
    recent: List[MsgMeta] = field(default_factory=list)


class TagTrigger:
    """按发送者维护 last/pending 状态的纯逻辑状态机。"""

    def __init__(
        self,
        prefix: str = "#",
        match_window_seconds: float = 30.0,
        pending_seconds: float = 60.0,
        forward_on_pending: bool = True,
        clock_skew_seconds: float = 10.0,
    ):
        self.prefix = prefix or "#"
        self.match_window_seconds = max(0.0, float(match_window_seconds))
        self.pending_seconds = max(0.0, float(pending_seconds))
        self.forward_on_pending = bool(forward_on_pending)
        # 平台时间可能比实际事件顺序略有偏差(尤其转发消息), 允许这个范围内的负时间差
        self.clock_skew_seconds = max(0.0, float(clock_skew_seconds))
        self._states: Dict[Tuple[str, str], _SenderState] = {}

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

        candidates: List[MsgMeta] = []
        for item in history:
            if item.group_id != tag.group_id or item.sender_id != tag.sender_id:
                continue
            if item.message_id == tag.message_id or item.ts > tag.ts:
                continue
            candidates.append(item)
        if not candidates:
            return

        candidates.sort(key=lambda item: (item.ts, item.message_id))
        for item in candidates[-_MAX_RECENT_MESSAGES:]:
            state.recent.append(item)
        state.last = candidates[-1]

    def observe(self, meta: MsgMeta) -> Optional[Action]:
        """处理一条消息, 返回需要转发的 Action(没有则返回 None)。"""
        self._prune(meta.ts)
        key = (meta.group_id, meta.sender_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _SenderState()

        state.recent.append(meta)
        if len(state.recent) > _MAX_RECENT_MESSAGES:
            del state.recent[: -_MAX_RECENT_MESSAGES]

        action: Optional[Action] = None

        if self.is_tag(meta):
            if self._can_match_tag(state.last, meta):
                matched = state.last
                action = Action(
                    "forward",
                    matched.message_id,
                    matched.kind,
                    meta.message_id,
                    matched.fingerprint,
                )
                state.pending_tag = None
            else:
                # 未命中 -> 进入 pending, 等待同人的下一条消息
                state.pending_tag = meta
        else:
            pending = state.pending_tag
            if pending is not None:
                if self._can_match_pending(state, meta, pending):
                    # 正常顺序: 该发送者 pending 之后的下一条消息就是媒体
                    action = Action(
                        "forward",
                        meta.message_id,
                        meta.kind,
                        pending.message_id,
                        meta.fingerprint,
                    )
                elif self._can_match_late_media(state, meta, pending):
                    # 乱序: 史实际先发出, 但事件比 tag 晚到, 按平台时间判断仍是紧邻的史
                    action = Action(
                        "forward",
                        meta.message_id,
                        meta.kind,
                        pending.message_id,
                        meta.fingerprint,
                    )
                state.pending_tag = None

        # 只有平台时间更新(或不更旧)的消息才覆盖 last, 避免迟到事件把状态往回拨;
        # 但 tag 已经产生动作时, last 必须是 tag 本身, 否则「平台时间略晚的史」
        # 会一直占着 last, 让后续重复 tag 反复命中同一条史
        if action is not None and self.is_tag(meta):
            state.last = meta
        elif state.last is None or meta.ts >= state.last.ts:
            state.last = meta

        return action

    def expire(self, now: Optional[float] = None) -> None:
        """清理过期的 pending(由 observe 自动调用, 也可手动调用)。"""
        self._prune(_now() if now is None else float(now))

    def last_for(self, meta: MsgMeta) -> Optional[MsgMeta]:
        """该发送者最近一条消息(用于日志/诊断, 需在 observe 之前调用)。"""
        state = self._states.get((meta.group_id, meta.sender_id))
        return state.last if state else None

    def has_pending(self, meta: MsgMeta) -> bool:
        """该发送者当前是否有等待中的 tag。"""
        state = self._states.get((meta.group_id, meta.sender_id))
        return bool(state is not None and state.pending_tag is not None)

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
            "version": 3,
            "states": states,
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
        except (AttributeError, TypeError, ValueError):
            # 损坏的状态文件: 能读多少读多少, 读不了的直接丢弃
            pass
        return trigger

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _can_match_pending(
        self, state: _SenderState, meta: MsgMeta, pending: MsgMeta
    ) -> bool:
        """正常顺序: tag 之后该发送者的*下一条*消息就是媒体。"""
        return self._adjacent_pending(state, meta, pending)

    def _adjacent_pending(self, state: _SenderState, meta: MsgMeta, pending: MsgMeta) -> bool:
        if not meta.is_media or not self.forward_on_pending:
            return False
        if state.last is None or state.last.message_id != pending.message_id:
            return False
        return 0 <= meta.ts - pending.ts <= self.pending_seconds

    def _can_match_late_media(
        self, state: _SenderState, meta: MsgMeta, pending: MsgMeta
    ) -> bool:
        """乱序: 史的平台时间在 tag 之前, 但事件比 tag 晚到。

        仅当两者按平台时间紧邻(中间没有该发送者的其它消息)时才匹配。
        """
        return self._adjacent_late_media(state, meta, pending)

    def _adjacent_late_media(self, state: _SenderState, meta: MsgMeta, pending: MsgMeta) -> bool:
        if not meta.is_media or not self.forward_on_pending:
            return False
        if not (meta.ts <= pending.ts and pending.ts - meta.ts <= self.match_window_seconds):
            return False
        for other in state.recent:
            if other.message_id in (meta.message_id, pending.message_id):
                continue
            if meta.ts < other.ts < pending.ts:
                return False
        return True

    def _can_match_tag(self, last: Optional[MsgMeta], tag: MsgMeta) -> bool:
        return self._same_sender_media_within_window(last, tag)

    def _same_sender_media_within_window(self, last: Optional[MsgMeta], tag: MsgMeta) -> bool:
        if last is None or not last.is_media:
            return False
        if last.message_id == tag.message_id:
            return False
        delta = tag.ts - last.ts
        # 允许小幅负时间差: 转发消息/适配器的平台时间可能比 tag 晚一点点,
        # 但事件的到达顺序已经证明它就在 tag 前面, 不能因此漏转
        return -self.clock_skew_seconds <= delta <= self.match_window_seconds

    def _prune(self, now: float) -> None:
        for state in self._states.values():
            if state.pending_tag is not None and now - state.pending_tag.ts > self.pending_seconds:
                state.pending_tag = None


def _now() -> float:
    import time

    return time.time()

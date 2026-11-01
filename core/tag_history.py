# -*- coding: utf-8 -*-
"""bot 重启后向上回填历史消息, 用于判断 tag 之前的那条消息。

优先使用 OneBot v11 的 ``get_group_msg_history``:
- NapCat / go-cqhttp: 参数 ``group_id`` + ``count``, 返回 ``{"messages": [...]}``
- Lagrange 等扩展实现: 参数名/类型可能不同, 这里做多组参数降级尝试

任何一步失败都返回 None, 由调用方记录日志并跳过本次 tag(宁可漏搬, 不可误搬)。
"""

from __future__ import annotations

from typing import List, Optional

from astrbot.api import logger

from .tag_trigger import MsgMeta, classify_message


async def fetch_recent_messages(bot, group_id, count: int = 50) -> Optional[List[MsgMeta]]:
    """拉取群最近 ``count`` 条消息, 转为 MsgMeta 列表。

    失败(接口不存在 / 参数不兼容 / 无权限)时返回 None。
    """
    if not count or count <= 0:
        count = 50

    attempts = _build_attempts(group_id, count)
    if not attempts:
        logger.warning("[SowingDiscord] 历史回填失败: 群号无效 %r", group_id)
        return None

    last_error: Optional[BaseException] = None
    for params in attempts:
        try:
            response = await bot.api.call_action("get_group_msg_history", **params)
        except Exception as e:  # noqa: BLE001 - 不同 OneBot 实现的异常类型不统一
            last_error = e
            continue

        messages = _extract_messages(response)
        if messages is None:
            last_error = ValueError(f"返回结构不符合预期: {type(response).__name__}")
            continue
        return _to_metas(group_id, messages)

    logger.warning(
        "[SowingDiscord] 历史回填失败 (群: %s): %s", group_id, last_error
    )
    return None


def _build_attempts(group_id, count: int) -> List[dict]:
    attempts: List[dict] = []
    group_int = _as_int(group_id)
    if group_int is not None:
        attempts.append({"group_id": group_int, "count": count})
    group_str = str(group_id).strip() if group_id is not None else ""
    if group_str:
        attempts.append({"group_id": group_str, "count": count})
    if group_int is not None:
        # 少数实现默认只返回正序的一小段, 显式要求反向取最新
        attempts.append({"group_id": group_int, "count": count, "reverse_order": True})
    return attempts


def _extract_messages(response) -> Optional[list]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        messages = response.get("messages")
        if isinstance(messages, list):
            return messages
    return None


def _to_metas(group_id, messages: list) -> List[MsgMeta]:
    result: List[MsgMeta] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        message_id = _as_int(item.get("message_id"))
        timestamp = item.get("time")
        if message_id is None or timestamp in (None, ""):
            continue
        sender = item.get("sender")
        sender_id = ""
        if isinstance(sender, dict):
            sender_id = str(sender.get("user_id", "") or "")
        if not sender_id:
            sender_id = str(item.get("user_id", "") or "")
        if not sender_id:
            continue

        message = item.get("message")
        if message is None:
            message = item.get("raw_message", "")
        if isinstance(message, dict):
            message = [message]
        text = item.get("message_str") or ""
        if not text and isinstance(message, str):
            text = message
        kind = classify_message(message, text)

        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            continue
        result.append(MsgMeta(str(group_id), sender_id, message_id, ts, kind))

    result.sort(key=lambda meta: (meta.ts, meta.message_id))
    return result


def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

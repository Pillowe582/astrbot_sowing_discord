# -*- coding: utf-8 -*-
"""core/tag_trigger.py 的纯逻辑单元测试(不依赖 AstrBot, 无需安装框架)。

运行:
    python -m pytest tests -q
"""

import pytest

from core.tag_trigger import (
    MsgMeta,
    TagTrigger,
    classify_message,
    is_tag_text,
    message_fingerprint,
    normalize_url,
)

GROUP = "1032915502"


def media(mid, ts, sender="u1", kind="image", group=GROUP):
    return MsgMeta(group, sender, mid, ts, kind)


def text(mid, ts, sender="u1", body="哈哈", group=GROUP):
    return MsgMeta(group, sender, mid, ts, "text", body)


def tag(mid, ts, sender="u1", body="#史", group=GROUP):
    return MsgMeta(group, sender, mid, ts, "text", body)


# --------------------------------------------------------------------------- #
# 文本 / 消息类型识别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#史", True),
        ("  #史  ", True),
        ("#", False),
        ("# 史", False),
        ("##", True),
        ("普通文字", False),
        ("", False),
    ],
)
def test_is_tag_text(raw, expected):
    assert is_tag_text(raw) is expected


def test_is_tag_text_custom_prefix():
    assert is_tag_text("搬运一下", prefix="搬运") is True
    assert is_tag_text("搬运", prefix="搬运") is False
    assert is_tag_text("搬运 一下", prefix="搬运") is False
    assert is_tag_text("＃史", prefix="＃") is True


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    pass


class Video:
    pass


class Node:
    pass


def test_classify_message_component_objects():
    assert classify_message([Plain("你好")], "你好") == "text"
    assert classify_message([Image()], "") == "image"
    assert classify_message([Video()], "") == "video"
    assert classify_message([Node()], "") == "forward"


def test_classify_message_dict_segments():
    assert classify_message([{"type": "image", "data": {"file": "a"}}], "") == "image"
    assert classify_message([{"type": "video", "data": {}}], "") == "video"
    assert classify_message([{"type": "forward", "data": {"id": "1"}}], "") == "forward"
    assert classify_message([{"type": "text", "data": {"text": "hi"}}], "hi") == "text"


def test_classify_message_cq_string():
    assert classify_message("[CQ:image,file=x]") == "image"
    assert classify_message("[CQ:video,file=x]") == "video"
    assert classify_message("[CQ:forward,id=1]") == "forward"
    assert classify_message("纯文本") == "text"


# --------------------------------------------------------------------------- #
# 核心匹配规则
# --------------------------------------------------------------------------- #
def test_media_then_tag_forwards():
    trigger = TagTrigger()
    assert trigger.observe(media(1, 100.0)) is None

    action = trigger.observe(tag(2, 110.0))

    assert action is not None
    assert action.kind == "forward"
    assert action.message_id == 1
    assert action.media_kind == "image"
    assert action.trigger_tag_id == 2


def test_tag_window_boundary():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    assert trigger.observe(tag(2, 130.0)) is not None  # 恰好 30 秒, 允许

    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    assert trigger.observe(tag(2, 130.001)) is None  # 超过 30 秒


def test_text_comment_breaks_adjacency_but_pending_still_waits():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    trigger.observe(text(2, 105.0))

    # 上一条是文字评论 -> 不转发之前的史
    assert trigger.observe(tag(3, 110.0)) is None

    # 但该 tag 进入 pending, 同一人 60 秒内发来的下一条媒体仍会转发
    action = trigger.observe(media(4, 120.0))
    assert action is not None
    assert action.message_id == 4


def test_tag_first_then_media_forwards():
    trigger = TagTrigger()
    assert trigger.observe(tag(1, 100.0)) is None

    action = trigger.observe(media(2, 110.0))

    assert action is not None
    assert action.message_id == 2


def test_pending_boundary_at_60_seconds():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    assert trigger.observe(media(2, 160.0)) is not None


def test_pending_expires_after_60_seconds():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    assert trigger.observe(media(2, 160.001)) is None


def test_pending_cancelled_by_text():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    trigger.observe(text(2, 105.0))
    assert trigger.observe(media(3, 110.0)) is None


def test_pending_expire_api():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    trigger.expire(161.0)
    assert trigger.observe(media(2, 170.0)) is None


def test_consecutive_tags_wait_for_next_media():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    assert trigger.observe(tag(2, 105.0)).message_id == 1  # 第一个 tag 转发史

    assert trigger.observe(tag(3, 110.0)) is None  # 第二个 tag 静默等待

    action = trigger.observe(media(4, 120.0))  # 同一人 60 秒内补发史
    assert action is not None
    assert action.message_id == 4


def test_second_tag_without_media_is_dropped():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    trigger.observe(tag(2, 105.0))
    trigger.observe(tag(3, 110.0))
    trigger.expire(171.0)
    assert trigger.observe(media(4, 180.0)) is None


def test_replayed_tag_after_pending_forward_does_not_wait():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    action = trigger.observe(media(2, 110.0))
    assert action is not None

    # 这条 tag 的上一句就是刚转发过的史 -> 视为重复 tag, 不再进入 pending
    assert trigger.observe(tag(3, 115.0)) is None
    assert trigger.observe(media(4, 120.0)) is None


def test_dedupe_prevents_double_forward():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    trigger.observe(tag(2, 105.0))

    # 重复 tag 让状态机进入 pending, 但下一条仍是这条已转发的史时不会被再次转发
    trigger.observe(tag(3, 108.0))
    assert trigger.observe(media(1, 109.0)) is None


def test_other_sender_does_not_break_adjacency():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0, sender="u1"))
    trigger.observe(text(2, 105.0, sender="u2"))
    trigger.observe(media(3, 106.0, sender="u2"))

    action = trigger.observe(tag(4, 110.0, sender="u1"))

    assert action is not None
    assert action.message_id == 1


def test_tag_must_match_same_sender():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0, sender="u1"))

    assert trigger.observe(tag(2, 105.0, sender="u2")) is None


# --------------------------------------------------------------------------- #
# 重启历史回填
# --------------------------------------------------------------------------- #
def test_needs_history_only_for_tag_without_state():
    trigger = TagTrigger()
    assert trigger.needs_history(tag(1, 100.0)) is True
    assert trigger.needs_history(media(2, 100.0)) is False


def test_apply_history_matches_media_before_tag():
    trigger = TagTrigger()
    current_tag = tag(50, 1000.0)
    history = [
        media(40, 990.0),
        text(41, 995.0, body="之前的评论"),
        text(42, 999.0, body="再评论"),
    ]

    assert trigger.needs_history(current_tag) is True
    trigger.apply_history(current_tag, history)
    assert trigger.needs_history(current_tag) is False

    # 历史里 tag 之前最后一条是文字评论 -> 不转发
    assert trigger.observe(current_tag) is None


def test_apply_history_media_right_before_tag_forwards():
    trigger = TagTrigger()
    current_tag = tag(50, 1000.0)
    trigger.apply_history(current_tag, [media(40, 995.0), text(45, 997.0, body="评论")])

    # 最后一条(按时间)是文字评论
    assert trigger.observe(current_tag) is None

    trigger = TagTrigger()
    trigger.apply_history(
        current_tag,
        [media(40, 985.0), text(45, 990.0, body="评论"), media(49, 995.0)],
    )
    action = trigger.observe(current_tag)
    assert action is not None
    assert action.message_id == 49


def test_apply_history_ignores_other_senders_and_future_messages():
    trigger = TagTrigger()
    current_tag = tag(50, 1000.0)
    trigger.apply_history(
        current_tag,
        [
            media(40, 995.0, sender="u2"),
            media(60, 1005.0),  # tag 之后的消息不能算
        ],
    )
    assert trigger.observe(current_tag) is None


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def test_classify_message_placeholders():
    # 适配器没给组件、只给了占位文本时的兜底
    assert classify_message([], "[图片]") == "image"
    assert classify_message([], "[视频]") == "video"
    assert classify_message([], "[聊天记录]") == "forward"
    assert classify_message([], "[转发消息]") == "forward"
    assert classify_message([], "普通文字") == "text"
    assert classify_message("[图片]") == "image"
    assert classify_message("") == "other"


# --------------------------------------------------------------------------- #
# 事件乱序(史先发出, 但事件比 tag 晚到)
# --------------------------------------------------------------------------- #
def test_media_processed_after_tag_still_forwards():
    trigger = TagTrigger()
    trigger.observe(text(1, 100.0, body="上一条是文字评论"))

    assert trigger.observe(tag(2, 110.0)) is None
    assert trigger.has_pending(tag(2, 110.0)) is True

    # 史在 105s 发出, 但事件比 tag 晚到 -> 仍应命中
    action = trigger.observe(media(3, 105.0))
    assert action is not None
    assert action.message_id == 3
    assert action.trigger_tag_id == 2


def test_late_media_with_other_message_between_is_ignored():
    trigger = TagTrigger()
    trigger.observe(text(1, 100.0, body="评论A"))
    trigger.observe(text(2, 108.0, body="评论B"))
    trigger.observe(tag(3, 110.0))

    # 104s 的史与 tag 之间还夹着 108s 的评论 -> 不算相邻
    assert trigger.observe(media(4, 104.0)) is None


def test_late_media_outside_window_is_ignored():
    trigger = TagTrigger()
    trigger.observe(text(1, 100.0))
    trigger.observe(tag(2, 200.0))
    assert trigger.observe(media(3, 169.0)) is None  # 间隔 31s > 30s


def test_late_media_does_not_rollback_last():
    trigger = TagTrigger()
    trigger.observe(text(1, 100.0))
    trigger.observe(tag(2, 120.0))
    assert trigger.observe(media(3, 110.0)) is not None

    # 迟到事件的平台时间更旧, 不能把 last 回拨
    last = trigger.last_for(text(4, 130.0))
    assert last is not None and last.message_id == 2


def test_diagnostic_helpers():
    trigger = TagTrigger()
    assert trigger.last_for(tag(1, 100.0)) is None

    trigger.observe(media(1, 100.0))
    last = trigger.last_for(tag(2, 105.0))
    assert last is not None and last.message_id == 1
    assert trigger.is_forwarded(media(1, 100.0)) is False
    assert trigger.has_pending(tag(2, 105.0)) is False

    assert trigger.observe(tag(2, 105.0)) is not None
    assert trigger.is_forwarded(media(1, 100.0)) is True


def test_state_roundtrip_keeps_dedupe_and_pending():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    trigger.observe(tag(2, 105.0))  # 转发 media(1)
    trigger.observe(tag(3, 106.0))  # 进入 pending

    restored = TagTrigger.from_dict(trigger.to_dict())

    # pending 仍在 -> 同一人补发媒体可以命中
    action = restored.observe(media(4, 110.0))
    assert action is not None
    assert action.message_id == 4

    # 已转发记录仍在 -> 重复 tag 不会让后续媒体被转发
    assert restored.observe(tag(5, 111.0)) is None
    assert restored.observe(media(6, 120.0)) is None


def test_from_dict_tolerates_broken_data():
    trigger = TagTrigger.from_dict({"states": "not-a-dict", "forwarded": {"bad": "x"}})
    assert trigger.observe(tag(1, 100.0)) is None


# --------------------------------------------------------------------------- #
# 消息指纹(免网络去重)
# --------------------------------------------------------------------------- #
def fp_media(mid, ts, fingerprint, sender="u1", kind="image"):
    return MsgMeta(GROUP, sender, mid, ts, kind, "", fingerprint)


def test_normalize_url_strips_query_and_host_case():
    assert (
        normalize_url("https://MULTIMEDIA.nt.qq.com.cn/a/b?rkey=123&x=1")
        == "https://multimedia.nt.qq.com.cn/a/b"
    )
    assert normalize_url("") == ""
    assert normalize_url("not a url") == "not a url"


def test_message_fingerprint_stable_for_same_segments():
    message = [
        {"type": "image", "data": {"file": "abc.image", "url": "https://x/img?rkey=1"}},
        {"type": "text", "data": {"text": "配文"}},
    ]
    other = [
        {"type": "image", "data": {"file": "abc.image", "url": "https://x/img?rkey=2"}},
        {"type": "text", "data": {"text": "配文"}},
    ]
    assert message_fingerprint(message) == message_fingerprint(other)
    assert message_fingerprint(message) != message_fingerprint(
        [{"type": "image", "data": {"file": "zzz.image"}}]
    )
    assert message_fingerprint([]) == ""
    assert message_fingerprint("") == ""


def test_message_fingerprint_components():
    class Image:
        def __init__(self, url):
            self.file = "abc.image"
            self.url = url

    # 同一个组件类, 只有 URL 的签名参数不同 -> 指纹相同
    assert message_fingerprint([Image("https://x/img?rkey=1")]) == message_fingerprint(
        [Image("https://x/img?rkey=2")]
    )
    assert message_fingerprint([Image("https://x/img?rkey=1")]) != ""

    class ImageOther:
        def __init__(self):
            self.file = "zzz.image"

    assert message_fingerprint([Image("https://x/img?rkey=1")]) != message_fingerprint(
        [ImageOther()]
    )


def test_fingerprint_dedupe_skips_same_content_with_new_message_id():
    trigger = TagTrigger()
    assert trigger.observe(fp_media(1, 100.0, "ffff")) is None
    assert trigger.observe(tag(2, 105.0)).kind == "forward"

    # 同内容的新消息(新消息 ID, 同指纹), 再发 tag -> 判定为重复史
    assert trigger.observe(fp_media(3, 200.0, "ffff")) is None
    action = trigger.observe(tag(4, 205.0))
    assert action is not None
    assert action.kind == "duplicate"
    assert action.message_id == 3
    assert action.reason == "fingerprint"


def test_fingerprint_dedupe_expires_after_window():
    trigger = TagTrigger(dedupe_seconds=100)
    trigger.observe(fp_media(1, 100.0, "ffff"))
    trigger.observe(tag(2, 105.0))

    # 超过去重窗口后, 同指纹的史可以再次搬运
    assert trigger.observe(fp_media(3, 300.0, "ffff")) is None
    action = trigger.observe(tag(4, 305.0))
    assert action is not None
    assert action.kind == "forward"
    assert action.message_id == 3


def test_fingerprint_dedupe_in_pending_branch():
    trigger = TagTrigger()
    trigger.observe(fp_media(1, 100.0, "ffff"))
    trigger.observe(tag(2, 105.0))

    trigger.observe(tag(3, 110.0))  # pending
    action = trigger.observe(fp_media(4, 115.0, "ffff"))
    assert action is not None
    assert action.kind == "duplicate"
    assert action.message_id == 4


def test_fingerprint_does_not_change_message_id_dedupe():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))  # 无指纹
    trigger.observe(tag(2, 105.0))
    # 同一条消息再次出现(同消息 ID) -> 静默, 不产生 duplicate Action
    trigger.observe(tag(3, 108.0))
    assert trigger.observe(media(1, 109.0)) is None


# --------------------------------------------------------------------------- #
# 内容哈希(字节级去重, 哈希由上层计算后传入)
# --------------------------------------------------------------------------- #
def test_content_dedupe_window():
    trigger = TagTrigger(content_dedupe_seconds=100)
    assert trigger.content_duplicate("cid") is False

    trigger.mark_content_forwarded("cid", now=100.0)
    assert trigger.content_duplicate("cid", now=150.0) is True
    assert trigger.content_duplicate("cid", now=250.0) is False  # 过期


def test_content_dedupe_ignores_empty():
    trigger = TagTrigger()
    assert trigger.content_duplicate("") is False
    trigger.mark_content_forwarded("")
    assert trigger.content_duplicate("") is False


def test_state_roundtrip_keeps_fingerprints_and_content():
    trigger = TagTrigger(content_dedupe_seconds=3600)
    trigger.observe(fp_media(1, 100.0, "ffff"))
    trigger.observe(tag(2, 105.0))
    trigger.mark_content_forwarded("cid", now=105.0)

    restored = TagTrigger.from_dict(trigger.to_dict())
    assert restored.fingerprint_forwarded("ffff") is True
    assert restored.content_duplicate("cid", now=110.0) is True

    # 重启后同指纹的史仍然会被判重
    restored.observe(fp_media(3, 200.0, "ffff"))
    action = restored.observe(tag(4, 205.0))
    assert action is not None and action.kind == "duplicate"


def test_from_dict_old_version_without_new_fields():
    trigger = TagTrigger.from_dict({"version": 1, "states": {}, "forwarded": {"1": 100.0}})
    assert trigger.is_forwarded(media(1, 100.0)) is True
    assert trigger.fingerprint_forwarded("ffff") is False
    assert trigger.content_duplicate("cid") is False

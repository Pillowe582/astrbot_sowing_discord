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


def test_tag_matches_media_with_slightly_late_platform_time():
    # 线上问题: 史事件先到, 但转发消息的平台时间比 tag 晚 1 秒;
    # 事件顺序已经证明它在 tag 前面, 不能因为负时间差漏转
    trigger = TagTrigger()
    trigger.observe(media(1, 101.0))
    action = trigger.observe(tag(2, 100.0))
    assert action is not None
    assert action.message_id == 1


def test_clock_skew_boundary():
    trigger = TagTrigger()
    trigger.observe(media(1, 110.0))
    assert trigger.observe(tag(2, 100.0)) is not None  # 恰好 -10 秒, 仍在偏差内

    trigger = TagTrigger()
    trigger.observe(media(1, 110.001))
    assert trigger.observe(tag(2, 100.0)) is None  # 超出时钟偏差


def test_clock_skew_is_configurable():
    trigger = TagTrigger(clock_skew_seconds=0)
    trigger.observe(media(1, 101.0))
    assert trigger.observe(tag(2, 100.0)) is None


def test_skew_matched_tag_does_not_leave_media_as_last():
    trigger = TagTrigger()
    trigger.observe(media(1, 101.0))
    assert trigger.observe(tag(2, 100.0)).message_id == 1

    # last 应更新为 tag 本身, 第二个 tag 进入等待而不是反复命中媒体 1
    assert trigger.observe(tag(3, 100.5)) is None
    action = trigger.observe(media(4, 120.0))
    assert action is not None
    assert action.message_id == 4


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


def test_second_tag_matches_same_media_in_matching_layer():
    trigger = TagTrigger()
    trigger.observe(tag(1, 100.0))
    action = trigger.observe(media(2, 110.0))
    assert action is not None

    # 匹配层仍会再次命中这条史(是否重复由发送层按目标群判断)
    action = trigger.observe(tag(3, 115.0))
    assert action is not None
    assert action.message_id == 2

    # tag 成为 last 后, 后续普通消息不会命中
    assert trigger.observe(media(4, 120.0)) is None


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
    assert trigger.has_pending(tag(2, 105.0)) is False

    assert trigger.observe(tag(2, 105.0)) is not None


def test_action_carries_matched_fingerprint():
    trigger = TagTrigger()
    trigger.observe(fp_media(1, 100.0, "ffff"))
    action = trigger.observe(tag(2, 105.0))
    assert action is not None
    assert action.fingerprint == "ffff"


def test_state_roundtrip_keeps_pending_and_last():
    trigger = TagTrigger()
    trigger.observe(media(1, 100.0))
    trigger.observe(tag(2, 105.0))  # 转发 media(1)
    trigger.observe(tag(3, 106.0))  # 进入 pending

    restored = TagTrigger.from_dict(trigger.to_dict())

    # pending 仍在 -> 同一人补发媒体可以命中
    action = restored.observe(media(4, 110.0))
    assert action is not None
    assert action.message_id == 4


def test_from_dict_tolerates_broken_data():
    trigger = TagTrigger.from_dict({"states": "not-a-dict"})
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


def test_from_dict_old_version_data_is_ignored():
    # 旧版状态文件里的 forwarded/fingerprints/content 去重字段已迁移到发送层
    trigger = TagTrigger.from_dict(
        {"version": 2, "states": {}, "forwarded": {"1": 100.0}, "content": {"cid": 100.0}}
    )
    action = trigger.observe(media(1, 100.0))
    assert action is None
    assert trigger.observe(tag(2, 105.0)).message_id == 1


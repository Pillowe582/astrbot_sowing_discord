# -*- coding: utf-8 -*-
"""core/target_dedupe.py 的纯逻辑单元测试(不依赖 AstrBot, 无需安装框架)。

运行:
    python -m pytest tests -q
"""

from core.target_dedupe import TargetDedupe


def test_duplicate_is_scoped_per_target():
    dedupe = TargetDedupe()
    dedupe.mark_sent("T1", fingerprint="fp", content_id="cid", now=100.0)

    # 只影响已经收到过这条史的目标群, 其它群照常接收
    assert dedupe.has_duplicate("T1", "fp", "cid", now=100.0) is True
    assert dedupe.has_duplicate("T2", "fp", "cid", now=100.0) is False
    assert dedupe.has_duplicate("T3", "fp", "cid", now=100.0) is False


def test_same_content_for_other_target_is_not_duplicate():
    dedupe = TargetDedupe()
    dedupe.mark_sent("A", content_id="cid", now=100.0)

    # 同内容搬到 B 不算重复
    assert dedupe.has_duplicate("B", content_id="cid", now=100.0) is False
    dedupe.mark_sent("B", content_id="cid", now=101.0)
    assert dedupe.has_duplicate("B", content_id="cid", now=101.0) is True


def test_fingerprint_window_expiry():
    dedupe = TargetDedupe(fingerprint_seconds=100)
    dedupe.mark_sent("T1", fingerprint="fp", now=100.0)

    assert dedupe.has_duplicate("T1", "fp", now=150.0) is True
    assert dedupe.has_duplicate("T1", "fp", now=250.0) is False  # 过期


def test_content_window_expiry():
    dedupe = TargetDedupe(content_seconds=50)
    dedupe.mark_sent("T1", content_id="cid", now=100.0)

    assert dedupe.has_duplicate("T1", content_id="cid", now=140.0) is True
    assert dedupe.has_duplicate("T1", content_id="cid", now=160.0) is False  # 过期


def test_windows_can_disable_dedupe():
    dedupe = TargetDedupe(fingerprint_seconds=0, content_seconds=0)
    dedupe.mark_sent("T1", fingerprint="fp", content_id="cid", now=100.0)
    assert dedupe.has_duplicate("T1", "fp", "cid", now=100.0) is False


def test_empty_keys_never_duplicate():
    dedupe = TargetDedupe()
    assert dedupe.has_duplicate("T1", "", "") is False
    dedupe.mark_sent("T1", "", "")
    assert dedupe.has_duplicate("T1", "", "") is False


def test_filter_targets_splits_send_and_skipped():
    dedupe = TargetDedupe()
    dedupe.mark_sent("B", fingerprint="fp", now=100.0)

    send, skipped = dedupe.filter_targets(["A", "B"], "fp", "cid", now=100.0)

    assert send == ["A"]
    assert skipped == ["B"]


def test_filter_targets_without_duplicates_sends_all():
    dedupe = TargetDedupe()
    send, skipped = dedupe.filter_targets(["A", "B"], "fp", now=100.0)
    assert send == ["A", "B"]
    assert skipped == []


def test_roundtrip_keeps_per_target_records():
    dedupe = TargetDedupe()
    dedupe.mark_sent("A", fingerprint="fp", content_id="cid", now=100.0)
    dedupe.mark_sent("B", content_id="cid2", now=100.0)

    restored = TargetDedupe.from_dict(dedupe.to_dict())

    assert restored.has_duplicate("A", "fp", now=110.0) is True
    assert restored.has_duplicate("A", content_id="cid", now=110.0) is True
    assert restored.has_duplicate("B", "fp", now=110.0) is False
    assert restored.has_duplicate("B", content_id="cid2", now=110.0) is True


def test_from_dict_tolerates_broken_data():
    dedupe = TargetDedupe.from_dict({"fingerprints": "not-a-dict"})
    assert dedupe.has_duplicate("T1", "fp", now=100.0) is False

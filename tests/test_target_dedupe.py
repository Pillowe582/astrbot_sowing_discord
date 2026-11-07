# -*- coding: utf-8 -*-
"""core/target_dedupe.py 的纯逻辑单元测试(不依赖 AstrBot, 无需安装框架)。

运行:
    python -m pytest tests -q
"""

from core.target_dedupe import MAX_TARGET_ENTRIES, TargetDedupe


def test_duplicate_is_scoped_per_target():
    dedupe = TargetDedupe()
    dedupe.mark_sent("T1", fingerprint="fp", content_id="cid", now=100.0)

    # 只影响已经收到过这条史的目标群, 其它群照常接收
    assert dedupe.has_duplicate("T1", "fp", "cid") is True
    assert dedupe.has_duplicate("T2", "fp", "cid") is False
    assert dedupe.has_duplicate("T3", "fp", "cid") is False


def test_same_content_for_other_target_is_not_duplicate():
    dedupe = TargetDedupe()
    dedupe.mark_sent("A", content_id="cid")

    # 同内容搬到 B 不算重复
    assert dedupe.has_duplicate("B", content_id="cid") is False
    dedupe.mark_sent("B", content_id="cid")
    assert dedupe.has_duplicate("B", content_id="cid") is True


def test_count_window_keeps_only_recent_records():
    dedupe = TargetDedupe(max_records=3)
    for index in range(3):
        dedupe.mark_sent("T1", fingerprint=f"fp{index}", now=100.0 + index)

    assert dedupe.has_duplicate("T1", "fp0") is True
    assert dedupe.has_duplicate("T1", "fp2") is True

    # 第 4 条进入窗口后, 最旧的记录被挤出, 不再参与判重
    dedupe.mark_sent("T1", fingerprint="fp3", now=200.0)
    assert dedupe.has_duplicate("T1", "fp0") is False
    assert dedupe.has_duplicate("T1", "fp3") is True


def test_count_window_is_not_time_based():
    dedupe = TargetDedupe(max_records=5)
    dedupe.mark_sent("T1", fingerprint="fp", now=0.0)

    # 隔很久(一年)只要还在最近 N 条内, 仍然算重复
    assert dedupe.has_duplicate("T1", "fp", now=365 * 24 * 3600.0) is True


def test_zero_records_disables_dedupe():
    dedupe = TargetDedupe(max_records=0)
    dedupe.mark_sent("T1", fingerprint="fp", content_id="cid")
    assert dedupe.has_duplicate("T1", "fp", "cid") is False


def test_records_are_capped():
    dedupe = TargetDedupe(max_records=10**9)
    assert dedupe.max_records == MAX_TARGET_ENTRIES


def test_empty_keys_never_duplicate():
    dedupe = TargetDedupe()
    assert dedupe.has_duplicate("T1", "", "") is False
    dedupe.mark_sent("T1", "", "")
    assert dedupe.has_duplicate("T1", "", "") is False


def test_filter_targets_splits_send_and_skipped():
    dedupe = TargetDedupe()
    dedupe.mark_sent("B", fingerprint="fp")

    send, skipped = dedupe.filter_targets(["A", "B"], "fp", "cid")

    assert send == ["A"]
    assert skipped == ["B"]


def test_filter_targets_without_duplicates_sends_all():
    dedupe = TargetDedupe()
    send, skipped = dedupe.filter_targets(["A", "B"], "fp")
    assert send == ["A", "B"]
    assert skipped == []


def test_roundtrip_keeps_per_target_records():
    dedupe = TargetDedupe()
    dedupe.mark_sent("A", fingerprint="fp", content_id="cid")
    dedupe.mark_sent("B", content_id="cid2")

    restored = TargetDedupe.from_dict(dedupe.to_dict())

    assert restored.has_duplicate("A", "fp") is True
    assert restored.has_duplicate("A", content_id="cid") is True
    assert restored.has_duplicate("B", "fp") is False
    assert restored.has_duplicate("B", content_id="cid2") is True


def test_roundtrip_respects_count_window():
    dedupe = TargetDedupe(max_records=2)
    for index in range(5):
        dedupe.mark_sent("A", fingerprint=f"fp{index}")

    restored = TargetDedupe.from_dict(dedupe.to_dict(), max_records=2)

    assert restored.has_duplicate("A", "fp4") is True
    assert restored.has_duplicate("A", "fp1") is False


def test_from_dict_reads_legacy_time_buckets():
    # 旧版按时间窗口保存的 fingerprints/contents 字典, 升级后仍能读
    restored = TargetDedupe.from_dict(
        {
            "version": 1,
            "fingerprints": {"A": {"fp": 100.0}},
            "contents": {"A": {"cid": 100.0}},
        }
    )
    assert restored.has_duplicate("A", "fp") is True
    assert restored.has_duplicate("A", content_id="cid") is True
    assert restored.has_duplicate("B", "fp") is False


def test_from_dict_tolerates_broken_data():
    dedupe = TargetDedupe.from_dict({"targets": "not-a-dict"})
    assert dedupe.has_duplicate("T1", "fp") is False

# -*- coding: utf-8 -*-
"""core/media_hash.py 的单元测试(不依赖 AstrBot, 不访问真实网络)。

运行:
    python -m pytest tests -q
"""

import asyncio
import hashlib

from core import media_hash


class FakeApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        response = self.responses.get(action)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        return response


class FakeBot:
    def __init__(self, responses):
        self.api = FakeApi(responses)


def test_combine_hashes():
    assert media_hash.combine_hashes([]) is None
    assert media_hash.combine_hashes(["", None]) is None
    assert media_hash.combine_hashes(["abc"]) == hashlib.sha256(b"abc").hexdigest()
    assert media_hash.combine_hashes(["abc", "def"]) == hashlib.sha256(
        b"abc\ndef"
    ).hexdigest()
    # 顺序不同 -> 内容 ID 不同
    assert media_hash.combine_hashes(["abc", "def"]) != media_hash.combine_hashes(
        ["def", "abc"]
    )


def test_segments_of_various_shapes():
    seg = {"type": "image", "data": {"url": "http://x/1"}}
    assert media_hash._segments_of([seg]) == [seg]
    assert media_hash._segments_of({"message": [seg]}) == [seg]
    assert media_hash._segments_of({"messages": [seg]}) == [seg]
    assert media_hash._segments_of({"content": seg}) == [seg]
    assert media_hash._segments_of({"message": {"type": "image"}}) == [
        {"type": "image"}
    ]
    assert media_hash._segments_of(None) == []
    assert media_hash._segments_of("nonsense") == []


def test_extract_media_urls():
    message = [
        {"type": "image", "data": {"url": "http://x/1"}},
        {"type": "image", "data": {"file": "/local/path.png"}},
        {"type": "video", "data": {"url": "http://x/v"}},
        {"type": "text", "data": {"text": "hi"}},
        {"type": "forward", "data": {"id": "1"}},
    ]
    assert media_hash.extract_media_urls(message) == ["http://x/1"]
    assert media_hash.extract_media_urls(message, allow_video=True) == [
        "http://x/1",
        "http://x/v",
    ]


def test_walk_forward_images():
    payload = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "content": [
                        {"type": "text", "data": {"text": "评论"}},
                        {"type": "image", "data": {"url": "http://x/1"}},
                    ]
                },
            },
            {
                "type": "node",
                "data": {
                    "content": [
                        {"type": "image", "data": {"url": "http://x/2"}},
                    ]
                },
            },
        ]
    }
    assert media_hash._walk_forward_images(payload) == [
        "http://x/1",
        "http://x/2",
    ]


def test_compute_content_id_from_image(monkeypatch):
    monkeypatch.setattr(
        media_hash, "_download_sha256", lambda url, max_bytes, timeout: "abc"
    )
    bot = FakeBot(
        {
            "get_msg": {
                "message": [
                    {"type": "image", "data": {"url": "http://127.0.0.1:1/x"}},
                ]
            }
        }
    )
    content_id = asyncio.run(
        media_hash.compute_media_content_id(bot, 123, timeout=1.0)
    )
    assert content_id == hashlib.sha256(b"abc").hexdigest()
    assert bot.api.calls[0] == ("get_msg", {"message_id": 123})


def test_compute_content_id_from_forward_card(monkeypatch):
    """转发卡片里的图片应与直接发同一张图得到相同内容 ID。"""
    monkeypatch.setattr(
        media_hash, "_download_sha256", lambda url, max_bytes, timeout: "abc"
    )
    bot = FakeBot(
        {
            "get_msg": {
                "message": [
                    {"type": "forward", "data": {"id": "77"}},
                ]
            },
            "get_forward_msg": {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "content": [
                                {"type": "image", "data": {"url": "http://127.0.0.1:1/x"}},
                            ]
                        },
                    }
                ]
            },
        }
    )
    content_id = asyncio.run(
        media_hash.compute_media_content_id(bot, 456, timeout=1.0)
    )
    assert content_id == hashlib.sha256(b"abc").hexdigest()


def test_compute_content_id_includes_text_content():
    """纯文字聊天记录也要能算出内容 ID(否则这类史完全无法去重)。"""
    bot = FakeBot({"get_msg": {"message": [{"type": "text", "data": {"text": "hi"}}]}})
    content_id = asyncio.run(media_hash.compute_media_content_id(bot, 1))
    expected = hashlib.sha256(
        media_hash._text_part("hi").encode("utf-8")
    ).hexdigest()
    assert content_id == expected

    other = FakeBot(
        {"get_msg": {"message": [{"type": "text", "data": {"text": "hello"}}]}}
    )
    assert asyncio.run(media_hash.compute_media_content_id(other, 1)) != content_id


def test_compute_content_id_placeholder_text_is_ignored():
    bot = FakeBot(
        {"get_msg": {"message": [{"type": "text", "data": {"text": "[图片]"}}]}}
    )
    assert asyncio.run(media_hash.compute_media_content_id(bot, 1)) is None


def test_compute_content_id_returns_none_without_content():
    bot = FakeBot({"get_msg": {"message": []}})
    assert asyncio.run(media_hash.compute_media_content_id(bot, 1)) is None


def test_compute_content_id_text_affects_image_content_id(monkeypatch):
    monkeypatch.setattr(
        media_hash, "_download_sha256", lambda url, max_bytes, timeout: "abc"
    )
    message = [
        {"type": "image", "data": {"url": "http://x/1"}},
        {"type": "text", "data": {"text": "配文 A"}},
    ]
    bot = FakeBot({"get_msg": {"message": message}})
    first = asyncio.run(media_hash.compute_media_content_id(bot, 1))

    other = FakeBot(
        {
            "get_msg": {
                "message": [
                    {"type": "image", "data": {"url": "http://x/1"}},
                    {"type": "text", "data": {"text": "配文 B"}},
                ]
            }
        }
    )
    second = asyncio.run(media_hash.compute_media_content_id(other, 1))

    assert first is not None
    assert first != second


def test_forward_card_text_and_media_both_count(monkeypatch):
    """聊天记录里的文字/图片都要参与内容 ID。"""
    monkeypatch.setattr(
        media_hash, "_download_sha256", lambda url, max_bytes, timeout: "abc"
    )
    payload = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "content": [
                        {"type": "text", "data": {"text": "第一层"}},
                        {"type": "image", "data": {"url": "http://x/1"}},
                    ]
                },
            }
        ]
    }
    bot = FakeBot(
        {
            "get_msg": {"message": [{"type": "forward", "data": {"id": "77"}}]},
            "get_forward_msg": payload,
        }
    )
    content_id = asyncio.run(media_hash.compute_media_content_id(bot, 456))
    assert content_id is not None

    other = FakeBot(
        {
            "get_msg": {"message": [{"type": "forward", "data": {"id": "78"}}]},
            "get_forward_msg": {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "content": [
                                {"type": "text", "data": {"text": "另一段聊天"}},
                                {
                                    "type": "image",
                                    "data": {"url": "http://x/1"},
                                },
                            ]
                        },
                    }
                ]
            },
        }
    )
    assert asyncio.run(media_hash.compute_media_content_id(other, 456)) != content_id


def test_compute_content_id_tolerates_api_errors():
    bot = FakeBot({"get_msg": RuntimeError("boom")})
    assert asyncio.run(media_hash.compute_media_content_id(bot, 1)) is None


# --------------------------------------------------------------------------- #
# 视频: 按第一帧去重(ffmpeg 首帧优先, 文件头兜底)
# --------------------------------------------------------------------------- #
def test_walk_forward_media_can_include_video():
    payload = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "content": [
                        {"type": "image", "data": {"url": "http://x/1"}},
                        {"type": "video", "data": {"url": "http://x/v"}},
                    ]
                },
            }
        ]
    }
    assert media_hash._walk_forward_media(payload) == [("image", "http://x/1")]
    assert media_hash._walk_forward_media(payload, allow_video=True) == [
        ("image", "http://x/1"),
        ("video", "http://x/v"),
    ]


def test_find_ffmpeg_hint_and_auto(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    assert media_hash._find_ffmpeg(str(fake)) == str(fake)
    assert media_hash._find_ffmpeg("definitely-not-a-real-ffmpeg-xyz") is None

    monkeypatch.setattr(media_hash.shutil, "which", lambda name: "/fake/ffmpeg")
    monkeypatch.setattr(media_hash, "_FFMPEG_PROBE", None)
    assert media_hash._find_ffmpeg() == "/fake/ffmpeg"


def test_video_hash_prefers_first_frame(monkeypatch):
    async def fake_frame(url, timeout, ffmpeg):
        assert url == "http://x/v"
        assert ffmpeg == "/fake/ffmpeg"
        return "framehash"

    monkeypatch.setattr(media_hash, "_find_ffmpeg", lambda hint="": "/fake/ffmpeg")
    monkeypatch.setattr(media_hash, "_extract_video_first_frame_sha256", fake_frame)
    assert (
        asyncio.run(media_hash._video_hash("http://x/v", timeout=1.0))
        == "vf:framehash"
    )


def test_video_hash_falls_back_to_prefix_when_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(media_hash, "_find_ffmpeg", lambda hint="": None)
    monkeypatch.setattr(
        media_hash, "_download_prefix_sha256", lambda url, n, timeout: "abc"
    )
    assert asyncio.run(media_hash._video_hash("http://x/v", timeout=1.0)) == "vp:abc"


def test_video_hash_falls_back_when_frame_decode_fails(monkeypatch):
    async def broken_frame(url, timeout, ffmpeg):
        return None

    monkeypatch.setattr(media_hash, "_find_ffmpeg", lambda hint="": "/fake/ffmpeg")
    monkeypatch.setattr(media_hash, "_extract_video_first_frame_sha256", broken_frame)
    monkeypatch.setattr(
        media_hash, "_download_prefix_sha256", lambda url, n, timeout: "abc"
    )
    assert asyncio.run(media_hash._video_hash("http://x/v", timeout=1.0)) == "vp:abc"


def test_video_hash_returns_none_when_everything_fails(monkeypatch):
    async def broken_frame(url, timeout, ffmpeg):
        return None

    def broken_prefix(url, n, timeout):
        raise OSError("boom")

    monkeypatch.setattr(media_hash, "_find_ffmpeg", lambda hint="": "/fake/ffmpeg")
    monkeypatch.setattr(media_hash, "_extract_video_first_frame_sha256", broken_frame)
    monkeypatch.setattr(media_hash, "_download_prefix_sha256", broken_prefix)
    assert asyncio.run(media_hash._video_hash("http://x/v", timeout=1.0)) is None


def test_compute_content_id_video_with_allow_video(monkeypatch):
    async def fake_video_hash(url, *, timeout, ffmpeg_path=""):
        return "vf:framehash"

    monkeypatch.setattr(media_hash, "_video_hash", fake_video_hash)
    bot = FakeBot(
        {
            "get_msg": {
                "message": [
                    {"type": "video", "data": {"url": "http://127.0.0.1:1/v"}},
                ]
            }
        }
    )
    content_id = asyncio.run(
        media_hash.compute_media_content_id(bot, 9, allow_video=True)
    )
    assert content_id == media_hash.combine_hashes(["vf:framehash"])


def test_compute_content_id_skips_video_by_default(monkeypatch):
    async def fake_video_hash(url, *, timeout, ffmpeg_path=""):
        raise AssertionError("video should not be hashed when allow_video=False")

    monkeypatch.setattr(media_hash, "_video_hash", fake_video_hash)
    bot = FakeBot(
        {
            "get_msg": {
                "message": [
                    {"type": "video", "data": {"url": "http://127.0.0.1:1/v"}},
                ]
            }
        }
    )
    assert asyncio.run(media_hash.compute_media_content_id(bot, 9)) is None

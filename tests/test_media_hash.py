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


def test_compute_content_id_returns_none_without_media():
    bot = FakeBot({"get_msg": {"message": [{"type": "text", "data": {"text": "hi"}}]}})
    assert asyncio.run(media_hash.compute_media_content_id(bot, 1)) is None


def test_compute_content_id_tolerates_api_errors():
    bot = FakeBot({"get_msg": RuntimeError("boom")})
    assert asyncio.run(media_hash.compute_media_content_id(bot, 1)) is None

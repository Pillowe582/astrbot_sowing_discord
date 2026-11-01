# -*- coding: utf-8 -*-
"""媒体内容哈希: 把一条消息里的图片/视频字节算成 SHA-256, 用于重复搬史检测。

设计要点:
- 只依赖 stdlib + 调用方传入的 OneBot bot 对象, 不 import astrbot;
- 计算「容器无关的内容 ID」: 取消息里所有媒体文件的字节哈希, 用 \n 拼接后再
  SHA-256。因此「一张图片」和「转发卡片里包着同一张图片」会得到相同的 ID;
- 任何网络/解析失败都返回 None, 调用方退回消息指纹去重, 不阻塞搬运。
"""

from __future__ import annotations

import asyncio
import hashlib
import urllib.request
from typing import Dict, Iterable, List, Optional

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_MEDIA_PARTS = 9
_MAX_FORWARD_WALK_NODES = 200
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) astrbot-sowing-discord"


def combine_hashes(file_hashes: Iterable[str]) -> Optional[str]:
    """把每个媒体文件的字节哈希组合成内容 ID; 没有媒体时返回 None。"""
    parts = [str(item) for item in file_hashes if item]
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _segments_of(payload) -> list:
    """从 get_msg / get_forward_msg 的返回里取出消息段列表。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("message", "content", "messages"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
    return []


def _segment_url(segment, allow_video: bool) -> Optional[str]:
    """取出图片/视频段里可下载的 URL。"""
    if not isinstance(segment, dict):
        return None
    seg_type = str(segment.get("type", "")).lower()
    if seg_type == "image":
        pass
    elif seg_type == "video":
        if not allow_video:
            return None
    else:
        return None
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    for key in ("url", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def extract_media_urls(message, allow_video: bool = False) -> List[str]:
    """从消息段列表里提取所有可下载的图片/视频 URL(仅供测试与复用)。"""
    urls: List[str] = []
    for segment in _segments_of(message):
        url = _segment_url(segment, allow_video)
        if url:
            urls.append(url)
    return urls


def _walk_forward_images(payload) -> List[str]:
    """递归遍历转发卡片内容, 收集其中的图片 URL(防止转发套转发)。"""
    urls: List[str] = []
    visited = 0

    def walk(node) -> None:
        nonlocal visited
        if visited >= _MAX_FORWARD_WALK_NODES:
            return
        visited += 1
        if isinstance(node, dict):
            if str(node.get("type", "")).lower() == "image":
                url = _segment_url(node, allow_video=False)
                if url:
                    urls.append(url)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return urls


def _download_sha256(url: str, max_bytes: int, timeout: float) -> str:
    """同步下载并流式哈希, 超过 max_bytes 直接放弃(抛异常由上层兜底)。"""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("media exceeds max bytes")
            digest.update(chunk)
    return digest.hexdigest()


async def _hash_url(url: str, max_bytes: int, timeout: float) -> Optional[str]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_download_sha256, url, max_bytes, timeout),
            timeout=timeout + 1.0,
        )
    except (asyncio.TimeoutError, ValueError, OSError, Exception):
        return None


async def _fetch_forward_payload(bot, segment) -> Optional[dict]:
    """按 OneBot 常见参数形态尝试拉取转发卡片内容。"""
    if not isinstance(segment, dict):
        return None
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    candidates = []
    for key in ("id", "res_id", "message_id"):
        value = data.get(key)
        if value not in (None, ""):
            candidates.append(value)
    for value in candidates:
        for params in (
            {"message_id": value},
            {"id": value},
            {"message_id": _as_int(value)},
        ):
            if "message_id" in params and params["message_id"] is None:
                continue
            try:
                return await bot.api.call_action("get_forward_msg", **params)
            except Exception:
                continue
    return None


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _media_hashes_in_segments(
    bot,
    segments: list,
    *,
    max_bytes: int,
    timeout: float,
    allow_video: bool,
    max_parts: int,
) -> List[str]:
    hashes: List[str] = []
    for segment in segments:
        if len(hashes) >= max_parts:
            break
        url = _segment_url(segment, allow_video)
        if url:
            digest = await _hash_url(url, max_bytes, timeout)
            if digest:
                hashes.append(digest)
            continue
        if isinstance(segment, dict) and str(segment.get("type", "")).lower() in (
            "forward",
            "node",
            "nodes",
        ):
            payload = await _fetch_forward_payload(bot, segment)
            if payload is None:
                continue
            for inner_url in _walk_forward_images(payload):
                if len(hashes) >= max_parts:
                    break
                digest = await _hash_url(inner_url, max_bytes, timeout)
                if digest:
                    hashes.append(digest)
    return hashes


async def compute_media_content_id(
    bot,
    message_id: int,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    allow_video: bool = False,
    max_parts: int = _MAX_MEDIA_PARTS,
) -> Optional[str]:
    """计算某条消息的媒体内容 ID; 无媒体/任何失败返回 None。"""
    try:
        detail = await bot.api.call_action("get_msg", message_id=int(message_id))
    except Exception:
        return None
    segments = _segments_of(detail)
    if not segments:
        return None
    try:
        hashes = await _media_hashes_in_segments(
            bot,
            segments,
            max_bytes=max_bytes,
            timeout=timeout,
            allow_video=allow_video,
            max_parts=max_parts,
        )
    except Exception:
        return None
    return combine_hashes(hashes)

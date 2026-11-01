# -*- coding: utf-8 -*-
"""媒体内容哈希: 把一条消息里的图片/视频做成内容 ID, 用于重复搬史检测。

设计要点:
- 只依赖 stdlib + 调用方传入的 OneBot bot 对象, 不 import astrbot;
- 计算「容器无关的内容 ID」: 取消息里所有媒体文件的哈希, 用 \n 拼接后再
  SHA-256。因此「一张图片」和「转发卡片里包着同一张图片」会得到相同的 ID;
- 图片: 下载文件字节算 SHA-256(单文件默认上限 8MB);
- 视频: 按「首帧」去重。优先用 ffmpeg 解码第 1 帧(缩放到 16x16 灰度)再哈希,
  这样不用下载整个视频; 找不到 ffmpeg 或解码失败时, 退回「视频开头 N 字节」
  的 SHA-256, 同一视频文件依然能认出来;
- 任何网络/解析失败都返回 None, 调用方退回消息指纹去重, 不阻塞搬运。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import urllib.request
from typing import Dict, Iterable, List, Optional

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
# ffmpeg 不可用时, 取视频开头这么多字节做 SHA-256(近似首帧/文件头)
VIDEO_PREFIX_BYTES = 256 * 1024
# 首帧统一缩放到 16x16 灰度后再哈希, 与分辨率/编码参数解耦
VIDEO_FRAME_SIZE = 16
_MAX_MEDIA_PARTS = 9
_MAX_FORWARD_WALK_NODES = 200
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) astrbot-sowing-discord"

# ffmpeg 探测结果缓存: None=还没探测, ""=没找到, 其它=可执行文件路径
_FFMPEG_PROBE: Optional[str] = None


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


def _walk_forward_media(payload, allow_video: bool = False) -> List[tuple]:
    """递归遍历转发卡片内容, 收集其中的 (类型, URL)(防止转发套转发)。"""
    media: List[tuple] = []
    visited = 0

    def walk(node) -> None:
        nonlocal visited
        if visited >= _MAX_FORWARD_WALK_NODES:
            return
        visited += 1
        if isinstance(node, dict):
            seg_type = str(node.get("type", "")).lower()
            if seg_type in ("image", "video"):
                if seg_type == "video" and not allow_video:
                    return
                url = _segment_url(node, allow_video=allow_video)
                if url:
                    media.append((seg_type, url))
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return media


def _walk_forward_images(payload) -> List[str]:
    """兼容旧调用: 只收集转发卡片里的图片 URL。"""
    return [url for kind, url in _walk_forward_media(payload) if kind == "image"]


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


def _find_ffmpeg(hint: str = "") -> Optional[str]:
    """定位 ffmpeg: 优先用配置里的路径, 否则在 PATH 里找; 结果缓存。"""
    global _FFMPEG_PROBE
    hint = str(hint or "").strip()
    if hint:
        if os.path.isfile(hint):
            return hint
        return shutil.which(hint)
    if _FFMPEG_PROBE is None:
        _FFMPEG_PROBE = shutil.which("ffmpeg") or ""
    return _FFMPEG_PROBE or None


def _ffmpeg_frame_args(ffmpeg: str, url: str) -> List[str]:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    # -user_agent 只对 http(s) 输入有效, 本地文件会报 "Option user_agent not found"
    if url.startswith(("http://", "https://")):
        args += ["-user_agent", _USER_AGENT]
    args += [
        "-i",
        url,
        "-frames:v",
        "1",
        "-vf",
        f"scale={VIDEO_FRAME_SIZE}:{VIDEO_FRAME_SIZE}:flags=bilinear,format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    return args


async def _extract_video_first_frame_sha256(url: str, timeout: float, ffmpeg: str) -> Optional[str]:
    """用 ffmpeg 解码视频第一帧(16x16 灰度), 返回首帧像素的 SHA-256。

    只让 ffmpeg 读取首帧所需的数据, 不需要先把整个视频下载下来。
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_frame_args(ffmpeg, url),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return None
    except (OSError, ValueError):
        return None
    if proc.returncode != 0 or not stdout:
        return None
    return hashlib.sha256(stdout).hexdigest()


def _download_prefix_sha256(url: str, max_bytes: int, timeout: float) -> str:
    """下载视频开头 max_bytes 字节并哈希(ffmpeg 不可用时的兜底)。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Range": f"bytes=0-{max_bytes - 1}",
        },
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while total < max_bytes:
            chunk = response.read(min(65536, max_bytes - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    if total == 0:
        raise ValueError("empty video body")
    return digest.hexdigest()


async def _video_hash(
    url: str, *, timeout: float, ffmpeg_path: str = ""
) -> Optional[str]:
    """视频内容哈希: 优先首帧, 失败退回文件头; 返回带前缀的哈希。"""
    ffmpeg = _find_ffmpeg(ffmpeg_path)
    if ffmpeg:
        frame = await _extract_video_first_frame_sha256(url, timeout, ffmpeg)
        if frame:
            return "vf:" + frame
    try:
        prefix = await asyncio.wait_for(
            asyncio.to_thread(_download_prefix_sha256, url, VIDEO_PREFIX_BYTES, timeout),
            timeout=timeout + 1.0,
        )
    except (asyncio.TimeoutError, ValueError, OSError, Exception):
        return None
    return ("vp:" + prefix) if prefix else None


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
    ffmpeg_path: str = "",
) -> List[str]:
    hashes: List[str] = []
    for segment in segments:
        if len(hashes) >= max_parts:
            break
        seg_type = (
            str(segment.get("type", "")).lower() if isinstance(segment, dict) else ""
        )
        if seg_type == "image":
            url = _segment_url(segment, allow_video=False)
            if url:
                digest = await _hash_url(url, max_bytes, timeout)
                if digest:
                    hashes.append(digest)
                continue
        elif seg_type == "video" and allow_video:
            url = _segment_url(segment, allow_video=True)
            if url:
                digest = await _video_hash(url, timeout=timeout, ffmpeg_path=ffmpeg_path)
                if digest:
                    hashes.append(digest)
                continue
        if isinstance(segment, dict) and seg_type in ("forward", "node", "nodes"):
            payload = await _fetch_forward_payload(bot, segment)
            if payload is None:
                continue
            for media_type, inner_url in _walk_forward_media(
                payload, allow_video=allow_video
            ):
                if len(hashes) >= max_parts:
                    break
                if media_type == "video":
                    digest = await _video_hash(
                        inner_url, timeout=timeout, ffmpeg_path=ffmpeg_path
                    )
                else:
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
    ffmpeg_path: str = "",
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
            ffmpeg_path=ffmpeg_path,
        )
    except Exception:
        return None
    return combine_hashes(hashes)

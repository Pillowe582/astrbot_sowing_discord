from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.all import *
from astrbot.api import logger
from aiocqhttp.exceptions import ActionFailed
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType
from .core.forward_manager import ForwardManager, forward_single_msg
from .core.evaluation.evaluator import Evaluator
from .core.evaluation.rules import GoodEmojiRule
from .core.tag_trigger import (
    MsgMeta,
    TagTrigger,
    classify_message,
    is_tag_text,
    message_fingerprint,
)
from .core.tag_history import fetch_recent_messages
from .core.media_hash import compute_media_content_id
from .storage.local_cache import LocalCache
from .config import TEMP_DIR
import asyncio
import json
import os
import time
import uuid
import re
from datetime import datetime, time as dtime


def _cfg_int(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _cfg_float(config: dict, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _cfg_bool(config: dict, key: str, default: bool) -> bool:
    return bool(config.get(key, default))


@register("astrbot_sowing_discord", "anka", "anka - 搬史插件", "0.92.4")
class Sowing_Discord(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.instance_id = str(uuid.uuid4())[:8]

        self.banshi_interval = config.get("banshi_interval", 3600)
        self.banshi_cache_seconds = config.get("banshi_cache_seconds", 3600)
        self.cooldown_day_seconds = config.get("banshi_cooldown_day_seconds", 600)
        self.cooldown_night_seconds = config.get("banshi_cooldown_night_seconds", 3600)
        self.cooldown_day_start_str = config.get("banshi_cooldown_day_start", "09:00")
        self.cooldown_night_start_str = config.get("banshi_cooldown_night_start", "01:00")

        self._day_start = self._parse_time_str(self.cooldown_day_start_str, dtime(9, 0))
        self._night_start = self._parse_time_str(self.cooldown_night_start_str, dtime(1, 0))

        self.banshi_group_list = config.get("banshi_group_list", [])
        self.banshi_target_list = config.get("banshi_target_list", [])
        self.block_source_messages = config.get("block_source_messages", False)

        self.allowed_msg_types = config.get("allowed_message_types", ["text", "image", "video", "forward"])

        # ---------------- tag 触发模式配置 ----------------
        self.trigger_mode = (
            "emoji"
            if str(config.get("trigger_mode", "tag") or "tag").strip().lower() == "emoji"
            else "tag"
        )
        self.tag_prefix = str(config.get("tag_prefix", "#") or "#")
        self.tag_match_window = _cfg_float(config, "tag_match_window_seconds", 30)
        self.tag_clock_skew = _cfg_float(config, "tag_clock_skew_seconds", 10)
        self.tag_pending_seconds = _cfg_float(config, "tag_pending_seconds", 60)
        self.tag_forward_on_pending = _cfg_bool(config, "tag_forward_on_pending", True)
        self.tag_history_fallback = _cfg_bool(config, "tag_history_fallback", True)
        self.tag_history_count = _cfg_int(config, "tag_history_count", 50)
        self.tag_dedupe_seconds = _cfg_float(config, "tag_dedupe_seconds", 3600)
        self.tag_cooldown_seconds = _cfg_float(config, "tag_cooldown_seconds", 0)

        # 重复史检测: 消息指纹(免网络) + 媒体内容哈希(下载字节, 默认只算图片)
        self.tag_content_dedupe = _cfg_bool(config, "tag_content_dedupe", True)
        self.tag_content_dedupe_seconds = _cfg_float(
            config, "tag_content_dedupe_seconds", 3600
        )
        self.tag_content_dedupe_max_bytes = _cfg_int(
            config, "tag_content_dedupe_max_bytes", 8 * 1024 * 1024
        )
        self.tag_content_dedupe_timeout = _cfg_float(
            config, "tag_content_dedupe_timeout_seconds", 10
        )
        self.tag_content_dedupe_video = _cfg_bool(config, "tag_content_dedupe_video", True)
        self.tag_content_dedupe_ffmpeg = str(
            config.get("tag_content_dedupe_ffmpeg", "") or ""
        ).strip()

        self.tag_state_file = os.path.join(TEMP_DIR, "tag_state.json")
        self.tagger = TagTrigger(
            prefix=self.tag_prefix,
            match_window_seconds=self.tag_match_window,
            pending_seconds=self.tag_pending_seconds,
            dedupe_seconds=self.tag_dedupe_seconds,
            forward_on_pending=self.tag_forward_on_pending,
            content_dedupe_seconds=self.tag_content_dedupe_seconds,
            clock_skew_seconds=self.tag_clock_skew,
        )
        self._tag_lock = asyncio.Lock()
        self._last_tag_send_ts = 0.0
        self._load_tag_state()

        self.local_cache = LocalCache(max_age_seconds=self.banshi_cache_seconds)
        self.forward_lock = asyncio.Lock()
        self._forward_task = None

    def _parse_time_str(self, time_str: str, fallback: dtime) -> dtime:
        try:
            if isinstance(time_str, str):
                parts = time_str.split(":")
                h = int(parts[0])
                m = int(parts[1]) if len(parts) > 1 else 0
                if 0 <= h < 24 and 0 <= m < 60:
                    return dtime(h, m)
        except Exception as e:
            logger.warning(
                f"[SowingDiscord][ID:{self.instance_id}] 冷却时间段解析失败: {time_str}, 使用默认值。错误: {e}"
            )
        return fallback

    def _get_banshi_interval_dynamic(self) -> int:
        now = datetime.now().time()
        if now >= self._day_start or now < self._night_start:
            return self.cooldown_day_seconds
        return self.cooldown_night_seconds

    # 【修复与优化】完美兼容 AstrBot 的对象格式与 OneBot API 的字典格式
    def _is_allowed_msg_type(self, message) -> bool:
        if not self.allowed_msg_types:
            return False

        allowed = set(self.allowed_msg_types)
        found_types = set()

        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    # 情况 A: 处理底层 OneBot API 返回的 dict 格式（转发复核时使用）
                    mtype = seg.get("type", "")
                    if mtype == "image":
                        found_types.add("image")
                    elif mtype == "video":
                        found_types.add("video")
                    elif mtype in ["forward", "node"]:
                        found_types.add("forward")
                    elif mtype in ["text", "face", "at", "reply"]:
                        if mtype == "text" and not seg.get("data", {}).get("text", "").strip():
                            continue
                        found_types.add("text")
                else:
                    # 情况 B: 处理 AstrBot 框架传递过来的 Component 对象（源群入库前验证时使用）
                    cname = seg.__class__.__name__.lower()
                    if cname == "image":
                        found_types.add("image")
                    elif cname == "video":
                        found_types.add("video")
                    elif cname in ["forward", "node"]:
                        found_types.add("forward")
                    elif cname in ["plain", "text", "face", "at", "reply"]:
                        # 对象的文本属性一般是 text
                        if cname in ["plain", "text"] and not getattr(seg, "text", "").strip():
                            continue
                        found_types.add("text")

        elif isinstance(message, str):
            # 情况 C: 处理纯 CQ码 字符串备用兜底
            if "[CQ:image" in message:
                found_types.add("image")
            if "[CQ:video" in message:
                found_types.add("video")
            if "[CQ:forward" in message or "[CQ:node" in message:
                found_types.add("forward")

            text_only = re.sub(r'\[CQ:.*?\]', '', message).strip()
            if text_only:
                found_types.add("text")

        # 规则1: 如果消息中包含了用户【未授权】的核心媒体类型，拦截整条消息。
        for t in ["image", "video", "forward"]:
            if t in found_types and t not in allowed:
                return False

        # 规则2: 如果消息没有任何符合用户设定项的元素，拦截。
        if not found_types.intersection(allowed):
            return False

        return True

    # ------------------------------------------------------------------ #
    # tag 状态持久化
    # ------------------------------------------------------------------ #
    def _load_tag_state(self):
        if not os.path.exists(self.tag_state_file):
            return
        try:
            with open(self.tag_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tagger = TagTrigger.from_dict(
                data,
                prefix=self.tag_prefix,
                match_window_seconds=self.tag_match_window,
                pending_seconds=self.tag_pending_seconds,
                dedupe_seconds=self.tag_dedupe_seconds,
                forward_on_pending=self.tag_forward_on_pending,
                content_dedupe_seconds=self.tag_content_dedupe_seconds,
            )
            logger.info(
                f"[SowingDiscord][ID:{self.instance_id}] 已加载 tag 状态: {self.tag_state_file}"
            )
        except Exception as e:
            logger.warning(
                f"[SowingDiscord][ID:{self.instance_id}] 加载 tag 状态失败，将从空状态开始。错误: {e}"
            )

    def _save_tag_state(self):
        try:
            data = self.tagger.to_dict()
            tmp_path = self.tag_state_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, self.tag_state_file)
        except Exception as e:
            logger.warning(
                f"[SowingDiscord][ID:{self.instance_id}] 保存 tag 状态失败。错误: {e}"
            )

    # ------------------------------------------------------------------ #
    # tag 触发模式
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_int(value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _extract_platform_ts(self, obj) -> float:
        """优先使用平台消息时间(raw_message["time"]), 兜底接收时间。

        AstrBot 的 message_obj.timestamp 是「接收时间」而非平台时间,
        和历史消息里的 time 混用会产生偏差, 所以这里尽量取原始事件时间。
        """
        raw = getattr(obj, "raw_message", None)
        try:
            if raw is not None and hasattr(raw, "get"):
                raw_time = raw.get("time")
                if raw_time:
                    return float(raw_time)
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            timestamp = getattr(obj, "timestamp", None)
            if timestamp:
                return float(timestamp)
        except (TypeError, ValueError):
            pass
        return time.time()

    def _extract_meta(self, event: AstrMessageEvent):
        obj = event.message_obj
        message_id = self._to_int(getattr(obj, "message_id", None))
        if message_id is None:
            return None
        group_id = str(getattr(obj, "group_id", "") or "")
        if not group_id:
            return None
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            sender_id = str(getattr(getattr(obj, "sender", None), "user_id", "") or "")
        if not sender_id or sender_id == "None":
            return None

        message = getattr(obj, "message", None)
        if not message:
            # 少数适配器不填 message_obj.message, 退回到原始事件里的消息段
            raw = getattr(obj, "raw_message", None)
            if isinstance(raw, dict):
                message = raw.get("message")
        text = str(getattr(obj, "message_str", "") or "")
        kind = classify_message(message, text)

        return MsgMeta(
            group_id=group_id,
            sender_id=sender_id,
            message_id=message_id,
            ts=self._extract_platform_ts(obj),
            kind=kind,
            text=text.strip(),
            fingerprint=message_fingerprint(message, text),
        )

    async def _handle_tag_message(self, event: AstrMessageEvent, meta: MsgMeta):
        """处理一条源群消息: 同步更新状态机, 命中后异步转发。"""
        async with self._tag_lock:
            is_tag = self.tagger.is_tag(meta)
            if is_tag:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 收到 tag 候选：群: {meta.group_id}, 发送者: {meta.sender_id}, 消息 ID: {meta.message_id}, 时间: {meta.ts:.0f}, 内容: {meta.text!r}"
                )
            elif meta.is_media:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 观察到媒体消息：群: {meta.group_id}, 发送者: {meta.sender_id}, 消息 ID: {meta.message_id}, 时间: {meta.ts:.0f}, 类型: {meta.kind}"
                )

            if self.tagger.needs_history(meta) and self.tag_history_fallback:
                history = await fetch_recent_messages(
                    event.bot, meta.group_id, self.tag_history_count
                )
                if history is None:
                    logger.warning(
                        f"[SowingDiscord][ID:{self.instance_id}] 历史回填失败，本次 tag 无法判断，跳过 (消息 ID: {meta.message_id})。"
                    )
                    return
                self.tagger.apply_history(meta, history)
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 已回填 {len(history)} 条历史消息，用于判断 tag (消息 ID: {meta.message_id})。"
                )

            prev_last = self.tagger.last_for(meta)
            action = self.tagger.observe(meta)
            self._save_tag_state()

        if action is None:
            if is_tag:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] tag 未命中：{self._explain_tag_miss(meta, prev_last)}"
                )
            return

        if action.kind == "duplicate":
            logger.info(
                f"[SowingDiscord][ID:{self.instance_id}] 检测到重复史（消息指纹与最近 {self.tag_dedupe_seconds:g} 秒内已搬运过的史相同），跳过转发 (消息 ID: {action.message_id}, 触发 tag: {action.trigger_tag_id})。"
            )
            return

        if action.media_kind not in self.allowed_msg_types:
            logger.info(
                f"[SowingDiscord][ID:{self.instance_id}] tag 命中的消息类型 {action.media_kind} 不在 allowed_message_types 中，跳过转发 (消息 ID: {action.message_id})。"
            )
            return

        content_id = None
        if self.tag_content_dedupe:
            content_id = await compute_media_content_id(
                event.bot,
                action.message_id,
                max_bytes=self.tag_content_dedupe_max_bytes,
                timeout=self.tag_content_dedupe_timeout,
                allow_video=self.tag_content_dedupe_video,
                ffmpeg_path=self.tag_content_dedupe_ffmpeg,
            )
            if content_id:
                if self.tagger.content_duplicate(content_id):
                    logger.info(
                        f"[SowingDiscord][ID:{self.instance_id}] 检测到重复史（内容哈希 {content_id[:12]}… 与最近 {self.tag_content_dedupe_seconds:g} 秒内已搬运的一致），跳过转发 (消息 ID: {action.message_id})。"
                    )
                    self._save_tag_state()
                    return
            else:
                logger.debug(
                    f"[SowingDiscord][ID:{self.instance_id}] 内容哈希不可用（无媒体/下载失败/超过大小限制），本次仅按消息指纹去重 (消息 ID: {action.message_id})。"
                )

        logger.info(
            f"[SowingDiscord][ID:{self.instance_id}] tag 命中：转发消息 (ID: {action.message_id}, 类型: {action.media_kind})，触发 tag: {action.trigger_tag_id}。"
        )
        forwarded = await self._send_tag_forward(event, action)
        if content_id and forwarded:
            self.tagger.mark_content_forwarded(content_id)
            self._save_tag_state()

    def _explain_tag_miss(self, meta: MsgMeta, prev_last) -> str:
        """给「tag 没命中」生成人话原因，方便线上排查。"""
        window = self.tag_match_window
        if prev_last is None:
            return "该发送者没有上一条消息记录（重启后首条 tag 且历史回填失败时会这样，已跳过一次）"
        if not prev_last.is_media:
            return (
                f"上一条不是媒体（ID: {prev_last.message_id}, 类型: {prev_last.kind}），"
                f"已进入等待，同人 {self.tag_pending_seconds:g} 秒内补发媒体仍会转发"
            )
        if self.tagger.is_forwarded(prev_last):
            return f"上一条媒体已转发过，按重复 tag 忽略（ID: {prev_last.message_id}）"
        delta = meta.ts - prev_last.ts
        if delta < -self.tag_clock_skew:
            return (
                f"上一条媒体的平台时间比 tag 晚 {-delta:.1f}s（史 ts={prev_last.ts:.0f}, "
                f"tag ts={meta.ts:.0f}），超出允许的时钟偏差 {self.tag_clock_skew:g} 秒"
            )
        return (
            f"上一条媒体与 tag 间隔 {delta:.1f}s，超出 {window:g} 秒限制"
            f"（史 ts={prev_last.ts:.0f}, tag ts={meta.ts:.0f}），"
            f"已进入等待，同人 {self.tag_pending_seconds:g} 秒内补发媒体仍会转发"
        )

    async def _resolve_targets(self, event: AstrMessageEvent) -> list:
        if self.banshi_target_list:
            return list(self.banshi_target_list)
        groups = await self.get_group_list(event)
        if groups:
            self.banshi_target_list = groups
        return list(groups)

    async def _send_tag_forward(self, event: AstrMessageEvent, action) -> bool:
        """把命中的史转发到所有目标群(串行, 队列化); 至少成功一个目标群返回 True。"""
        targets = await self._resolve_targets(event)
        if not targets:
            logger.warning(
                f"[SowingDiscord][ID:{self.instance_id}] 没有可用的目标群，跳过转发 (消息 ID: {action.message_id})。"
            )
            return False

        sent_any = False
        async with self.forward_lock:
            if self.tag_cooldown_seconds > 0 and self._last_tag_send_ts > 0:
                wait_seconds = self.tag_cooldown_seconds - (
                    time.time() - self._last_tag_send_ts
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

            for target_id in targets:
                try:
                    await forward_single_msg(event.bot, action.message_id, target_id)
                    sent_any = True
                    logger.info(
                        f"[SowingDiscord][ID:{self.instance_id}] 成功转发消息 (ID: {action.message_id}) 到目标群: {target_id}。"
                    )
                except ActionFailed as e:
                    logger.error(
                        f"[SowingDiscord][ID:{self.instance_id}] 转发失败 (API 拒绝)：消息 ID {action.message_id} -> 目标群 {target_id}，原因: {e}"
                    )
                except Exception as e:
                    logger.error(
                        f"[SowingDiscord][ID:{self.instance_id}] 转发异常：消息 ID {action.message_id} -> 目标群 {target_id}，原因: {e}"
                    )
                await asyncio.sleep(1)

            self._last_tag_send_ts = time.time()
            if self.tag_cooldown_seconds > 0:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] tag 冷却开始：时长 {self.tag_cooldown_seconds} 秒 (持有锁)。"
                )
                await asyncio.sleep(self.tag_cooldown_seconds)
        return sent_any

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_message(self, event: AstrMessageEvent):
        source_group_id = event.message_obj.group_id

        # 将群号统一转为字符串进行判定，防止因格式不同（int/str）导致匹配失败
        is_in_source_list = str(source_group_id) in [str(g) for g in self.banshi_group_list]

        # 【核心修复】如果是源群消息，且开启了屏蔽源群选项，立即拦截事件，阻止大模型和下游插件响应！
        if is_in_source_list and self.block_source_messages:
            event.stop_event()

        if self.trigger_mode == "tag":
            meta = self._extract_meta(event)
            if meta is None:
                text = str(getattr(event.message_obj, "message_str", "") or "")
                if is_tag_text(text, self.tag_prefix):
                    logger.info(
                        f"[SowingDiscord][ID:{self.instance_id}] 收到 tag 内容，但该消息没有群号（私聊？）或消息 ID 非数字，无法处理，已忽略。"
                    )
                return None
            if not is_in_source_list:
                if is_tag_text(meta.text, self.tag_prefix):
                    logger.info(
                        f"[SowingDiscord][ID:{self.instance_id}] 收到 tag 候选，但群 {meta.group_id} 不在 banshi_group_list 来源群列表中，已忽略。"
                    )
                return None
            await self._handle_tag_message(event, meta)
            return None

        await self._handle_emoji_message(event)
        return None

    # ------------------------------------------------------------------ #
    # 旧版 emoji 触发模式(保留, 由 trigger_mode 切换)
    # ------------------------------------------------------------------ #
    async def _handle_emoji_message(self, event: AstrMessageEvent):
        forward_manager = ForwardManager(event)
        evaluator = Evaluator(event)
        evaluator.add_rule(GoodEmojiRule())

        source_group_id = event.message_obj.group_id
        msg_id = event.message_obj.message_id

        is_in_source_list = str(source_group_id) in [str(g) for g in self.banshi_group_list]

        sender_id = event.get_sender_id()

        if not self.banshi_target_list:
            self.banshi_target_list = await self.get_group_list(event)

        if is_in_source_list:
            # 兼容获取消息结构
            raw_msg = getattr(event.message_obj, 'message', getattr(event.message_obj, 'raw_message', ""))
            if not self._is_allowed_msg_type(raw_msg):
                logger.debug(f"[SowingDiscord][ID:{self.instance_id}] 预拦截：消息 (ID: {msg_id}) 包含未被允许的类型。")
            else:
                try:
                    int(msg_id)
                    await self.local_cache.add_cache(msg_id)
                    logger.info(
                        f"[SowingDiscord][ID:{self.instance_id}] 任务：缓存。已缓存消息 (ID: {msg_id}, 源头群: {source_group_id}, 发送者: {sender_id})。"
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        f"[SowingDiscord][ID:{self.instance_id}] 拦截异常：消息 ID [{msg_id}] 不是有效的纯数字形式，跳过缓存。"
                    )

        try:
            waiting_messages = await self.local_cache.get_waiting_messages()
        except ValueError as e:
            logger.error(
                f"[SowingDiscord][ID:{self.instance_id}] 缓存严重损坏！遇到了无法转换为数字的旧数据：{e}。请进入插件目录删掉损坏的缓存文件！"
            )
            waiting_messages = []

        if waiting_messages:
            if not self.forward_lock.locked():
                # 【优化】将其作为后台任务运行，避免冷却期的 sleep 卡死事件循环，导致其他消息无响应
                asyncio.create_task(self._execute_forward_and_cool(event, forward_manager, evaluator))

        return None

    async def _execute_forward_and_cool(
        self, event, forward_manager, evaluator
    ):
        client = event.bot

        try:
            current_task = asyncio.current_task()
            self._forward_task = current_task
            cleaned_count = await self.local_cache._cleanup_expired_cache()
            if cleaned_count > 0:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 转发前自动清理了 {cleaned_count} 条超出最大缓存时长的消息。"
                )

            try:
                # 重新获取最新的待转发列表
                waiting_messages = await self.local_cache.get_waiting_messages()
            except ValueError:
                waiting_messages = []

            async with self.forward_lock:
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 执行任务：转发。检测到 {len(waiting_messages)} 条待转发消息，开始处理..."
                )

                for index, msg_id_to_forward in enumerate(waiting_messages):
                    earliest_timestamp_limit = time.time() - self.banshi_cache_seconds
                    target_list_str = ", ".join(map(str, self.banshi_target_list))

                    try:
                        message_detail = await client.api.call_action(
                            "get_msg", message_id=int(msg_id_to_forward)
                        )
                        message_time = message_detail.get("time", 0)
                        msg_content = message_detail.get("message", [])

                        if message_time < earliest_timestamp_limit:
                            await self.local_cache.remove_cache(msg_id_to_forward)
                            continue

                        if not msg_content:
                            await self.local_cache.remove_cache(msg_id_to_forward)
                            continue

                        # API 拿到的是字典形式，进行二次类型校验
                        if not self._is_allowed_msg_type(msg_content):
                            logger.info(
                                f"[SowingDiscord] 预检查失败：消息ID {msg_id_to_forward} 包含未被配置允许的消息类型。"
                            )
                            await self.local_cache.remove_cache(msg_id_to_forward)
                            continue

                    except ActionFailed:
                        await self.local_cache.remove_cache(msg_id_to_forward)
                        continue

                    start_time_for_cooldown = time.time()
                    try:
                        if await evaluator.evaluate(msg_id_to_forward):
                            logger.info(
                                f"[SowingDiscord][ID:{self.instance_id}] 转发详情 (No.{index + 1}, ID: {msg_id_to_forward})：目标群列表: [{target_list_str}]。"
                            )

                            for target_id in self.banshi_target_list:
                                await forward_manager.send_forward_msg_raw(
                                    msg_id_to_forward, target_id
                                )
                                logger.info(
                                    f"[SowingDiscord][ID:{self.instance_id}] 发送日志：成功转发消息 (ID: {msg_id_to_forward}) 到目标群: {target_id}。"
                                )
                                await asyncio.sleep(1)

                            await self.local_cache.remove_cache(msg_id_to_forward)

                            interval = self._get_banshi_interval_dynamic()
                            self._forward_task = current_task
                            logger.info(
                                f"[SowingDiscord][ID:{self.instance_id}] 冷却开始：时长 {interval} 秒 (持有锁)。"
                            )
                            await asyncio.sleep(interval)
                            self._forward_task = None

                        else:
                            await self.local_cache.remove_cache(msg_id_to_forward)

                    except ActionFailed as e:
                        logger.error(
                            f"[SowingDiscord][ID:{self.instance_id}] 转发失败 (API 拒绝)：消息 ID {msg_id_to_forward} ... 原因: {e}"
                        )
                        await self.local_cache.remove_cache(msg_id_to_forward)
                        continue
                    except asyncio.CancelledError:
                        self._forward_task = None
                        raise

            logger.info(
                f"[SowingDiscord][ID:{self.instance_id}] 本次所有待转发消息处理完毕，释放转发锁。"
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._forward_task = None

    # 【修复退出报错】添加了 async，防止重启框架/重载插件时由于阻塞报错
    async def terminate(self):
        try:
            self._save_tag_state()
        except Exception as e:
            logger.error(
                f"[SowingDiscord][ID:{self.instance_id}] 终止时保存 tag 状态失败: {e}"
            )
        try:
            if self._forward_task and not self._forward_task.done():
                logger.info(
                    f"[SowingDiscord][ID:{self.instance_id}] 插件终止：正在取消冷却任务。"
                )
                self._forward_task.cancel()
        except Exception as e:
            logger.error(
                f"[SowingDiscord][ID:{self.instance_id}] 终止时取消任务失败: {e}"
            )

    async def get_group_list(self, event: AstrMessageEvent):
        client = event.bot
        response = await client.api.call_action("get_group_list", no_cache=False)
        group_ids = [item["group_id"] for item in response]
        return group_ids

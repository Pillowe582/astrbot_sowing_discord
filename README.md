# 🤖 Astrbot Sowing_discord 模块 - 搬史插件

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![Python Version](https://img.shields.io/badge/Python-3.10.14%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
[![PRs Welcome](https://img.shields.io/badge/PRs-Welcome-brightgreen)](CONTRIBUTING.md)
[![Contributors](https://img.shields.io/github/contributors/Pillowe582/astrbot_sowing_discord?color=green)](https://github.com/Pillowe582/astrbot_sowing_discord/graphs/contributors)
[![Last Commit](https://img.shields.io/github/last-commit/Pillowe582/astrbot_sowing_discord)](https://github.com/Pillowe582/astrbot_sowing_discord/commits/main)

</div>

<div align="center">

[![Moe Counter](https://count.getloli.com/get/@GalChat?theme=moebooru)](https://github.com/Pillowe582/astrbot_sowing_discord)

</div>

让 AI 学会搬史, 搬史进入新时代! 全自动搬史!

## 📢 公告

# 近期计划更新

本项目为公益项目, 不接受任何赞助, 所有服务均为免费提供, 所有付费服务与广告都是骗人的, 请不要相信并向作者报告

如果您想加入群聊, 为项目做出贡献, 请添加:

请前往发布页加群:

发布页:

https://sowingdiscord.ankaanka.org

收藏发布页不迷路~

所有群，包括聊天群（请在聊天群聊天）请从入口群加入，因为所有群基本都无法搜索了，入口群群简介有其他群链接和群号，入口群加入其他群后可以退出（或者你可以不加这个群，这个群将会永久禁言)。

- 群聊的答案都是**保证可以搜索的**， 你并不需要懂得答案的由来, 你只是需要证明具有基础的搜索与解决问题的能力 

注意, 不要相信任何你无法准确证明真实的消息, 这可能是谣言

## ❓ 常见问题

1. **Q: 为什么我的 bot 没有自动搬史?**

   - A: 当前默认使用 **tag 触发模式**, 插件逻辑如下:
     - 1. 插件监听 `banshi_group_list` 中来源群的所有消息
     - 2. 当有人发送以 `#` 开头的 tag 消息时, 插件会找**同一个发送者**、**严格相邻**的上一条消息
     - 3. 如果上一条是图片 / 视频 / 聊天记录, 且与 tag 间隔在 30 秒以内, 就转发这条史
     - 4. 如果 tag 先发、史后发: 插件会等同一人 60 秒内的下一条消息, 是媒体才转发, 是文字则取消等待
     - 5. bot 重启后如果内存里没有状态, 会调用 `get_group_msg_history` 向上回填历史; 回填失败则跳过该 tag, 不会乱转
   - 如需恢复旧版「贴表情评估」触发方式, 把 `trigger_mode` 改为 `emoji` 即可 (旧版需要等待约 10 分钟)

2. **Q: 有时候发 `#tag` 不触发, 怎么排查?**

   - A: 先看插件日志(关键字 `[SowingDiscord]`), 现在会打印:
     - `收到 tag 候选：...` / `收到 tag 候选，但群 xxx 不在 banshi_group_list ...` —— 确认群号是否配置正确
     - `观察到媒体消息：... 类型: image|video|forward` —— 确认史有没有被识别成媒体
     - `tag 未命中：...` —— 会直接写出原因(上一条不是媒体 / 间隔超出 / 没有上一条记录); 重复史不会再让匹配失败, 而是在发送时按目标群跳过
   - 常见原因: 史和 tag 间隔超过 30 秒; 中间夹了文字评论; 发送者不是同一人; 来源群不在 `banshi_group_list`; bot 重启后历史回填失败(接口不支持时该 tag 会被跳过)。转发消息的「平台时间晚于 tag 1~2 秒」属于正常时钟偏差, 已在 `tag_clock_skew_seconds`(默认 10 秒)内自动容忍

3. **Q: 同一张史被搬来搬去, 怎么避免重复搬运?**

   - A: 插件默认开启两层重复检测, 并且**按目标群判断**:
     - **消息指纹**: 对消息里的文件 id / 转发记录 id / 规范化 URL / 文本做哈希, 同一条消息换了个消息 ID 再出现也能认出来, 不需要网络;
     - **内容哈希**: 图片下载字节算 SHA-256(转发卡片会拆开取里面的图片); 视频则用 ffmpeg 解码第一帧(缩放成 16x16 灰度)再哈希, 不用下载整个视频。因此同一张图被重新上传、或套了一层转发卡片再出现时, 内容 ID 依然相同。
     - 判定范围是「这个目标群最近有没有收到过特征相同的史」: 别的群收到过不影响当前群; 新加了目标群时, 插件会把重复的史补发过去, 只跳过已经收到过的那几个群。
   - 命中重复时日志会打印 `以下目标群最近已有相同史，跳过转发`, 并跳过对应目标群。
   - 局限: 视频按首帧去重, 重新编码过的视频首帧像素可能变化, 不一定能认出; 没装 ffmpeg 时自动退回「视频开头 256KB 字节」哈希(同一视频文件仍可去重); 图片单文件超过 `tag_content_dedupe_max_bytes`(默认 8MB)或下载失败时会退回消息指纹; 去重窗口由 `tag_dedupe_seconds` / `tag_content_dedupe_seconds` 控制(默认都是 3600 秒)。

## ✨ 功能

- 🚫 自动从指定的群聊(推荐加入 1032915502, 共建搬史中心), 转发聊天记录(史)到其他指定的群聊(详见设置)
- 🏷️ 支持 `#tag` 触发: 由群友主动指定要搬的史, 命中即转发, 无需等待
- 🔁 同一发送者的严格相邻匹配: 史和 tag 之间只有文字评论时不转发
- ♻️ 重复史检测: 消息指纹(免网络) + 媒体内容哈希(图片按文件字节, 视频按首帧), **按目标群**判断, 只跳过已经收到过同特征史的目标群
- 🧠 重启兜底: 状态持久化到 `sowing_discord_cache/tag_state.json`, 必要时调用 OneBot 历史接口回填

## 🛠️ 使用方法

1. 加入天天发史的群聊
2. 设置该群聊为搬史源头, 设置目标群聊
3. 在来源群里发一条史(图片/视频/聊天记录), 然后 30 秒内发一条 `#任意内容` 的 tag
4. bot 就会把那条史转到目标群!

## ⚙️ 配置说明

### tag 触发模式 (默认)

- `trigger_mode`: 触发方式, `tag`(默认) 或 `emoji`(旧版)
- `tag_prefix`: tag 触发前缀, 默认 `#`
- `tag_match_window_seconds`: 史与 tag 的最大时间间隔(秒), 默认 30
- `tag_clock_skew_seconds`: 允许的平台时间偏差(秒), 默认 10; 转发消息的平台时间可能比实际发送时间晚 1~2 秒, 该值内的负间隔仍按紧邻处理
- `tag_pending_seconds`: tag 先于史时等待同一人发史的时间(秒), 默认 60
- `tag_forward_on_pending`: 是否允许「tag 先到、史后到」也转发, 默认 true
- `tag_history_fallback`: bot 重启后是否调用 `get_group_msg_history` 回填历史, 默认 true
- `tag_history_count`: 历史回填拉取的最大消息条数, 默认 50
- `tag_dedupe_seconds`: 按目标群去重的消息指纹窗口(秒), 默认 3600
- `tag_content_dedupe`: 是否开启媒体内容哈希去重(按目标群判断), 默认 true
- `tag_content_dedupe_seconds`: 按目标群的内容哈希去重窗口(秒), 默认 3600
- `tag_content_dedupe_max_bytes`: 图片参与内容哈希的最大字节数, 默认 8388608 (8MB)
- `tag_content_dedupe_timeout_seconds`: 单个媒体文件下载/解码超时(秒), 默认 10
- `tag_content_dedupe_video`: 是否对视频按首帧去重, 默认 true (需 ffmpeg)
- `tag_content_dedupe_ffmpeg`: ffmpeg 可执行文件路径, 留空自动在 PATH 中查找
- `tag_cooldown_seconds`: 两次转发之间的最小间隔(秒), 0 表示不限制

### 通用

- `banshi_group_list`: 史的来源群列表
- `banshi_target_list`: 白名单, 史的目标列表, 可以填写多个群或用户, 默认空白即全部群
- `block_source_messages`: 开启后将屏蔽 banshi_group_list 中的群消息
- `allowed_message_types`: 允许转发的消息类型 (`text` / `image` / `video` / `forward`)

### 旧版 emoji 模式

- `banshi_interval`: 搬史间隔, 单位: 秒（兼容项，实际冷却由时间段动态决定）
- `banshi_cache_seconds`: 消息缓存时间限制, 也是转发前检查历史消息的时间窗口 (秒)
- `banshi_cooldown_day_seconds`: 动态冷却-白天冷却秒数（默认 600）
- `banshi_cooldown_night_seconds`: 动态冷却-夜间冷却秒数（默认 3600）
- `banshi_cooldown_day_start`: 动态冷却-白天时段起始时间（HH:MM，默认 09:00）
- `banshi_cooldown_night_start`: 动态冷却-夜间时段起始时间（HH:MM，默认 01:00）

## 👥 贡献指南

欢迎通过以下方式参与项目：

- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📄 许可证

本项目基于 MIT 许可证开源。

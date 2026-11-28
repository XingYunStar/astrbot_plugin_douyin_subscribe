# 更新日志

本项目的所有重要变更都记录在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **关于 1.1.x**：这组版本号只在个别实例上本地试跑过，从未发布。
> 其中的改动最终合并进了 `1.2.0`，因此本文件没有 1.1.x 条目。

---

## [1.2.1] - 2026-11-28

把「动态图文」真正按它的本质处理，并修掉了几个一直藏着的坑。

### 变更

- **动态图文按图片投递**，三种形态各司其职：
  | 作品类型 | 「把动图合并成一个视频」 | 群里收到 |
  | --- | --- | --- |
  | 普通图文 | — | 聊天记录卡片，里面是**图片** |
  | 动态图文 | 关 | 聊天记录卡片，里面是**动图视频**（不再是静态图） |
  | 动态图文 | 开 | **不发卡片**，只发合并后的那一个视频 |
- **合并时把背景音乐与动图原声混音**（`amix`，两者都保留）。
  BGM 比视频短就循环、比视频长就裁到视频长度；动图没有音轨时退化为只用 BGM。
- WebUI 预览对动态图文直接返回**真正合并出来的那个 MP4**，所见即所得。

### 修复

- **图文的 `aweme.video.play_addr` 其实是背景音乐**（实测 `audio/mpeg` 的 mp3）。
  之前被当成视频地址，导致：
  - 「降级发链接」发出去的是一首歌；
  - 预览里的 `<video>` 根本放不出来（源是 mp3）；
  - `no_watermark_url()` 拿它拼出无意义的地址。

  现在图文一律不填 `video_url` / `video_uri`，背景音乐单独存进 `music_url`。
- **`fetch_bytes` 会截断大文件**：aiohttp 的 `StreamReader.read(n)` 只保证「最多 n 字节」，
  不会等凑满——实测 248 KB 的动图只拿到 16 KB。改为循环读到 EOF 或超出上限。
  这个 bug 之前一直隐形，因为头像只有几 KB、一次就读完了。
- **预览视频 403**：抖音视频 CDN 会拒绝 Referer 不是抖音的请求（插件页挂在 AstrBot
  面板域名下），部分情况还按 IP 签权。改为服务端取回字节转 `data:` URI，浏览器直接播，
  彻底不走外部请求。
- 合并动图后**立刻删除源片段**，避免 `media_tmp` 堆积
  （实测一趟测试就攒到 419 个文件 / 123 MB）。
- 没有文件服务（`callback_api_base` 未配置）时，动图**降级为直链发送**，而不是直接失败。

### 文档

- `callback_api_base` 对**本地文件投递是必填**（原文写的是「大多数情况下不用填」）。
- 补充 `ENOENT: no such file or directory ... media_tmp/*.mp4` 与
  `highway upload error_code=921` 的排查提示。
- 记录「图文 video 实为背景音乐」「作品列表必须带 `publish_video_strategy_type`」两个坑。

---

## [1.2.0] - 2026-11-27

### 新增

- **屏蔽关键词**：作品文案命中任意关键词就不推送。支持多个关键词（每行一个或逗号分隔）。
  被屏蔽的作品**不占用**「测试推送 / 推送效果实时预览 / 首次订阅推送最近作品」的名额，
  一直跳到找到未被屏蔽的作品为止；全部命中时给出明确提示，而不是假装推送成功。
- **动态图文动图合并**：开启后用 ffmpeg 把一条作品的全部动图拼成一个视频。
  各段规格一致时走 `concat + -c copy` 无损直拼，不重编码；否则统一 scale+pad 后重编码。
- WebUI 增加对应开关与关键词输入框，预览里会标注被屏蔽的作品及命中的关键词。

### 变更

- **图文改发「群聊聊天记录」卡片**（OneBot `node` 合并转发），文本和图片都在卡片里，
  群里只看到一张卡片，而不是被十几条图片刷屏。
- 链接解析支持更多形态：`/video/`、`/note/`、`/slides/`、`?modal_id=`、纯数字 ID。

### 修复

- **作品列表接口必须带 `publish_video_strategy_type`**。不带它时接口只返回视频
  （`aweme_type=0`），图文/图集（`aweme_type=68`）会被整个过滤掉，且 `has_more=0`——
  表现就是「这号作品怎么这么少」「图文从来没被推送过」。
  实测某账号主页 54 条（8 视频 + 46 图文），不带参数只回 8 条。
  （早期版本曾下过「带它会清空列表」的相反结论，那是错的。）

### 文档

- 踩坑表新增上述参数陷阱，并给出逐参数隔离的实测数据。

---

## [1.0.7] - 2026-10-07

### 修复

- 多个群同时订阅同一账号时，**回执只发给第一个群**，其余群收不到。

---

## [1.0.6] - 2026-10-07

### 变更

- 三个开关默认改为开启：`first_sync_push_latest`、`admin_only`、`debug`。

### 修复

- WebUI 刷新后**状态丢失**：订阅列表、配置项在重载后不显示。

---

## [1.0.5] - 2026-10-07

### 修复

- 修改「轮询与订阅」相关配置后**不会重新计时**，要等上一轮的间隔走完。
  现在配置一变就按新间隔重新排程。

---

## [1.0.4] - 2026-10-07

### 变更

- 「首次订阅时推送最近作品」对**已存在的订阅**也生效，不再只对新建订阅有效。

---

## [1.0.3] - 2026-10-07

### 新增

- **WebUI 支持设置抖音 Cookie**，不必再去改配置文件。保存后会立即用新 Cookie 验证。

---

## [1.0.2] - 2026-10-07

### 新增

- **首次订阅时推送最近作品**，并带时间范围，避免刚订阅就收到一堆陈年旧作。

---

## [1.0.1] - 2026-10-07

### 修复

- 推送里的**作者头像显示异常**。

---

## [1.0.0] - 2026-10-07

首个正式版本。

### 新增

- 订阅抖音账号，TA 发布新作品时**自动推送到群**。
- **纯 Python 实现的签名**，无需 Node.js，容器里开箱即用。
- **WebUI 管理页**：搜索订阅、群列表选择、推送效果实时预览、测试推送。
- 图文发图片、视频发视频；支持**视频大小限制降级**、**视频 / 群文件**两种发送方式。
- 定时轮询，可配置间隔；失败自动退避。

---

[1.2.1]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.2.1
[1.2.0]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.2.0
[1.0.7]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.7
[1.0.6]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.6
[1.0.5]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.5
[1.0.4]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.4
[1.0.3]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.3
[1.0.2]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.2
[1.0.1]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.1
[1.0.0]: https://github.com/XingYunStar/astrbot_plugin_douyin_subscribe/releases/tag/v1.0.0

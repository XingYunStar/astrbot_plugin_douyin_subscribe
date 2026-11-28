"""把一条作品按配置组装成待发送的消息链。

推送构成（全部实测于 AstrBot 4.25.5 + aiocqhttp）：

* **图文 / 动态图文** → 组装成一张**群聊聊天记录卡片**（合并转发）发送，
  文本与全部图片都放在卡片里；卡片发送失败时逐张发送图片兜底
* **动态图文** 额外可把每张图附带的动图（MP4）合并成一个视频（配置项，默认关）
* **视频** → 作为视频消息发送，或作为群文件发送（可在配置里切换）
* **附加信息** → 作者 / 简介 / 发布时间 / 作品链接，**四项默认全部关闭**，
  即默认状态下"只发作品本体"

消息结构
--------
所有文本会被合并进**同一个 Plain 段**（用换行分隔），媒体段接在其后。
早期版本对每段文本各调一次 ``MessageChain.message()``，那会产生多个 Plain 段，
而 OneBot 把它们当作独立段拼接、不补换行——结果就是"一堆文字挤成一坨"。
现在统一走 ``lines`` 列表，最后一次性成段。

关于投递方式（踩过的坑）
------------------------
* **视频直链必须带 Cookie**：抖音视频 CDN 无 Cookie 一律 403。平台进程没有这些
  Cookie，所以插件必须自己下载，再交给 AstrBot 的文件服务托管。
* **图床在 TLS 指纹层面拒绝 aiohttp**：同一 URL 同请求头下 aiohttp 403、requests 200。
* **图文是 WebP**：QQ 侧支持不完整，上传会报 ``highway upload error 921``，需转 JPEG。
* **video / file 段必须单独成条**：OneBot 返回 retcode 1400，由 ``split_chain()`` 处理。
* **合并转发里的图片会被 base64 内联**（``Node.to_dict`` 的实现如此），所以卡片内的
  图片必须**先降分辨率再压 JPEG**，否则 15 张 1920×1080 会撑成好几 MB。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video
from astrbot.core.message.message_event_result import MessageChain

from .douyin_api import DouyinClient, DouyinError, DouyinPost

MB = 1024 * 1024

#: 合并转发卡片里的图片：长边压到这个尺寸、JPEG 质量，控制 base64 体积
CARD_IMAGE_MAX_SIDE = 1080
CARD_IMAGE_QUALITY = 75
#: 合并动图时统一到的画布尺寸与帧率
LIVE_CANVAS = (1280, 720)
LIVE_FPS = 30

_FALLBACK_TEMPLATE = "🎬 视频体积约 {size:.1f}MB，超过上限 {limit}MB，改为发送链接："
_FALLBACK_UNKNOWN = "🎬 视频体积无法确认，超过上限 {limit}MB，改为发送链接："


def parse_keywords(raw: Any) -> list[str]:
    """把配置里的屏蔽关键词切成列表。

    支持换行、英文逗号、中文逗号、分号分隔；也接受已经切好的列表。
    会去空白、去重，并保持用户写的顺序。
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[\n,，;；]+", str(raw or ""))
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


@dataclass
class PushOptions:
    """从插件配置解析出来的推送选项。"""

    show_author: bool = False
    show_desc: bool = False
    show_time: bool = False
    show_link: bool = False
    desc_max_length: int = 100
    at_all: bool = False
    video_mode: str = "video"  # "video" | "file"
    size_limit_mb: int = 100
    cover_on_fallback: bool = True
    prefer_no_watermark: bool = False
    #: 链接形态："resource" = 资源直链，"page" = 作品网页
    link_style: str = "resource"
    #: 视频投递方式："auto" | "direct" | "download"
    video_delivery: str = "auto"
    #: 动态图文：把每张图附带的动图（MP4）合并成一个视频发送（默认关 = 逐段发送）
    merge_live: bool = False
    #: 屏蔽关键词：作品文案命中任一个就不推送（空列表表示不屏蔽）
    block_keywords: list[str] = field(default_factory=list)

    def matched_keyword(self, text: str) -> str:
        """返回文案里命中的屏蔽关键词；没命中返回空串。

        英文关键词按**大小写不敏感**匹配（中文本来没有大小写，不受影响）。
        """
        low = (text or "").lower()
        if not low:
            return ""
        for kw in self.block_keywords:
            if kw and kw.lower() in low:
                return kw
        return ""

    @classmethod
    def from_config(cls, config: Any) -> "PushOptions":
        content = (config or {}).get("content") or {}
        video = (config or {}).get("video") or {}
        try:
            limit = int(video.get("size_limit_mb", 100))
        except (TypeError, ValueError):
            limit = 100
        try:
            desc_len = int(content.get("desc_max_length", 100))
        except (TypeError, ValueError):
            desc_len = 100
        mode = str(video.get("mode") or "video").lower()
        if mode not in ("video", "file"):
            mode = "video"
        return cls(
            show_author=bool(content.get("show_author", False)),
            show_desc=bool(content.get("show_desc", False)),
            show_time=bool(content.get("show_time", False)),
            show_link=bool(content.get("show_link", False)),
            desc_max_length=max(desc_len, 1),
            at_all=bool(content.get("at_all", False)),
            video_mode=mode,
            size_limit_mb=max(limit, 0),
            cover_on_fallback=bool(video.get("cover_on_fallback", True)),
            prefer_no_watermark=bool(video.get("prefer_no_watermark", False)),
            link_style=(
                "page"
                if str(content.get("link_style") or "resource") == "page"
                else "resource"
            ),
            video_delivery=(
                str(video.get("delivery") or "auto")
                if str(video.get("delivery") or "auto") in ("auto", "direct", "download")
                else "auto"
            ),
            merge_live=bool(content.get("merge_live", False)),
            block_keywords=parse_keywords(content.get("block_keywords")),
        )


@dataclass
class PushPlan:
    """一次推送的组装结果，附带可供 WebUI 预览的诊断信息。"""

    chain: MessageChain
    text: str = ""
    media_kind: str = ""  # image / card / video / file / link / none
    media_count: int = 0
    #: 文本是否已装进合并转发卡片里；为 True 时不再单独发一条纯文本
    text_in_card: bool = False
    #: 动态图文里动图（MP4）的段数，供预览展示
    live_count: int = 0
    video_size_bytes: int = 0
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    #: 结构化文本行 [{"key": 字段, "text": 文案}]，供 WebUI 分色渲染
    lines: list[dict[str, str]] = field(default_factory=list)
    #: 媒体投递失败时的兜底消息（文本 + 链接 + 封面）。
    fallback_chain: MessageChain | None = None
    #: 被本地化（下载到本地并由文件服务托管）的媒体文件路径。
    local_paths: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "media_kind": self.media_kind,
            "media_count": self.media_count,
            "text_in_card": self.text_in_card,
            "live_count": self.live_count,
            "video_size_mb": round(self.video_size_bytes / MB, 2)
            if self.video_size_bytes
            else 0,
            "degraded": self.degraded,
            "notes": list(self.notes),
            "lines": list(self.lines),
        }


def pick_link(post: DouyinPost, opt: PushOptions) -> str:
    """按配置挑一条链接：资源直链（默认）或作品网页。"""
    if opt.link_style == "page":
        return post.share_url
    return post.resource_url(prefer_no_watermark=opt.prefer_no_watermark) or post.share_url


def format_lines(post: DouyinPost, opt: PushOptions) -> list[dict[str, str]]:
    """按开关拼装附加信息，返回 ``[{"key": 字段, "text": 文案}, ...]``。

    ``key`` 用于让 WebUI 预览给每个字段上不同的底色，取值：
    ``author`` / ``desc`` / ``time`` / ``link`` / ``atall`` / ``warn``。
    """
    lines: list[dict[str, str]] = []

    if opt.show_author:
        name = post.author.nickname or "未知作者"
        lines.append({"key": "author", "text": f"📱【{name}】发布了新{post.kind}"})

    if opt.show_desc and post.desc.strip():
        desc = post.desc.strip()
        if len(desc) > opt.desc_max_length:
            desc = desc[: opt.desc_max_length].rstrip() + "…"
        # 文案内部的换行保留，避免长文案变成一大坨
        for one in desc.splitlines() or [desc]:
            lines.append({"key": "desc", "text": one})

    if opt.show_time and post.create_time:
        lines.append(
            {
                "key": "time",
                "text": "🕐 "
                + time.strftime("%Y-%m-%d %H:%M", time.localtime(post.create_time)),
            }
        )

    if opt.show_link:
        lines.append({"key": "link", "text": f"🔗 {pick_link(post, opt)}"})

    return lines


def split_chain(chain: MessageChain) -> list[MessageChain]:
    """把一条逻辑消息链拆成若干条「可以安全发送」的消息。

    OneBot / aiocqhttp 有硬性约束：``video`` 与 ``file`` 段必须是所在消息里的
    **唯一**一个段，否则会返回
    ``message element "video" must be the only segment in a message``（retcode 1400）。
    所以带视频/群文件的推送必须拆成「文本」+「视频」两条消息发送。

    文本、图片、@ 等段之间可以自由组合，因此连续的非视频/文件段会被合并成一条。
    """
    groups: list[list] = []
    buf: list = []
    for seg in chain.chain:
        if isinstance(seg, (Video, File)):
            if buf:
                groups.append(buf)
                buf = []
            groups.append([seg])
        else:
            buf.append(seg)
    if buf:
        groups.append(buf)
    return [chain.derive(g) for g in groups]


def _safe_filename(post: DouyinPost) -> str:
    """给群文件取一个安全的文件名。"""
    name = post.author.nickname or "douyin"
    keep = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    keep = keep[:24] or "douyin"
    return f"{keep}_{post.aweme_id}.mp4"


class PushBuilder:
    """组装推送消息链。"""

    def __init__(
        self, client: DouyinClient, logger=None, tmp_dir: Path | None = None
    ) -> None:
        self.client = client
        self.log = logger
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def build(
        self,
        post: DouyinPost,
        opt: PushOptions,
        *,
        is_group: bool = True,
        force_link: bool = False,
        force_download: bool = False,
        force_plain_images: bool = False,
    ) -> PushPlan:
        """把一条作品组装成消息链。

        ``force_link=True`` 时跳过媒体投递，只发链接（用于调试或超大文件）。
        ``force_download=True`` 时强制先下载再投递（平台直链投递失败后的重试路径）。
        ``force_plain_images=True`` 时图文**不**走合并转发卡片、改为逐张发送——
        这是卡片发送失败（协议端不支持 ``node`` 段）时的降级路径。
        """
        notes: list[str] = []
        lines: list[dict[str, str]] = list(format_lines(post, opt))
        media: list[Any] = []
        plan = PushPlan(chain=MessageChain())
        plan.text = "\n".join(x["text"] for x in lines)

        if post.is_image:
            await self._build_image_post(
                post, opt, plan, lines, media, notes,
                force_link=force_link, plain=force_plain_images,
            )
        else:
            await self._build_video_post(
                post, opt, plan, lines, media, notes, is_group, force_link,
                force_download,
            )

        # ---- 统一成链：所有文本合成一个 Plain，媒体依次追加 ----
        chain = MessageChain()
        at_all = bool(is_group and opt.at_all)
        if at_all:
            chain.at_all()
        final_text = "\n".join(x["text"] for x in lines if x.get("text"))
        if at_all and is_group:
            # AtAll 是独立消息段，但预览里也要能标出来
            lines.insert(0, {"key": "atall", "text": "@全体成员"})
        if final_text and not plan.text_in_card:
            # 文本已经装进合并转发卡片里了，就不再单独发一条（否则群里会看到两份）
            chain.message(final_text)
        chain.chain.extend(media)
        plan.chain = chain
        plan.text = final_text
        plan.lines = lines
        plan.notes = notes          # 诊断信息要回写，否则 WebUI 预览看不到
        return plan

    # ------------------------------------------------------------------
    # 图文
    # ------------------------------------------------------------------

    async def _build_image_post(
        self,
        post: DouyinPost,
        opt: PushOptions,
        plan: PushPlan,
        lines: list[str],
        media: list[Any],
        notes: list[str],
        *,
        force_link: bool,
        plain: bool = False,
    ) -> None:
        """图文 / 动态图文的消息组装。

        三种形态，取决于作品类型与「把动图合并成一个视频」开关：

        * **普通图文** → 一张「群聊聊天记录」卡片，里面是**图片**
        * **动态图文 + 合并关** → 一张卡片，里面是**动图（视频）**，不是静态图
        * **动态图文 + 合并开** → **不发卡片**，直接把合并后的那一个视频发出去

        ``plain=True`` 时不组卡片、改为逐张追加——协议端不支持 ``node`` 段
        （卡片发不出去）时由 ``_push_post`` 走这条降级路径。
        """
        if force_link:
            self._append_link_line(lines, post, opt)
            plan.media_kind = "link"
            return

        # ---- 动态图文：卡片里要放动图（视频），而不是静态图 ----
        if post.is_live:
            if opt.merge_live:
                merged = await self._build_live_videos(post, opt, plan, notes)
                if merged:
                    # 合并模式：不用卡片，就发这一个视频（文本走外层 Plain）
                    plan.media_kind = opt.video_mode if opt.video_mode == "file" else "video"
                    plan.media_count = len(merged)
                    plan.live_count = len(merged)
                    media.extend(merged)
                    return

            clips: list[Any] = []
            for url in post.live_videos:
                comp = await self._make_live_item(url, plan, notes)
                if comp is not None:
                    clips.append(comp)

            if clips:
                if plain:
                    plan.media_kind = "video"
                    plan.media_count = len(clips)
                    plan.live_count = len(clips)
                    media.extend(clips)
                    notes.append("已退回逐段发送（合并转发卡片不可用）")
                    return
                media.append(self._make_card(post, lines, clips))
                plan.text_in_card = True
                plan.media_kind = "card"
                plan.media_count = len(clips)
                plan.live_count = len(clips)
                return

            notes.append("未取到动图，卡片里改放静态图")

        # 1) 图片：卡片里的图片会被 base64 内联，所以先降分辨率再压 JPEG
        images: list[Any] = []
        for url in post.images:
            if not url:
                continue
            comp = await self._make_image(
                url, plan, notes,
                max_side=CARD_IMAGE_MAX_SIDE,
                quality=CARD_IMAGE_QUALITY,
                # 卡片里图片是 base64 内联的，必须本地 JPEG；
                # 走直链会被平台当成 WebP 上传并报 921
                force_local=not plain,
            )
            if comp is not None:
                images.append(comp)

        if not images:
            notes.append("该图文作品没有可用的图片链接，已改为发送链接")
            self._append_link_line(lines, post, opt)
            plan.media_kind = "link"
            return

        if plain:
            # 降级：卡片发不出去，退回逐张发送（文本仍走外层 Plain）
            plan.media_kind = "image"
            plan.media_count = len(images)
            media.extend(images)
            notes.append("已退回逐张发送（合并转发卡片不可用）")
            return

        # 2) 组装卡片：先一条文本节点，再每张图一个节点
        media.append(self._make_card(post, lines, images))
        plan.text_in_card = True
        plan.media_kind = "card"
        plan.media_count = len(images)

    def _make_card(
        self, post: DouyinPost, lines: list[str], images: list[Any]
    ) -> Any:
        """把文本与图片装进一张「群聊聊天记录」卡片（OneBot ``node`` 合并转发）。"""
        nick = (post.author.nickname or "抖音")[:24]
        nodes: list[Node] = []
        text = "\n".join(x["text"] for x in lines if x.get("text"))
        if text:
            nodes.append(Node(content=[Plain(text)], name=nick, uin="0"))
        nodes.extend(Node(content=[img], name=nick, uin="0") for img in images)
        return Nodes(nodes=nodes)

    async def _build_live_videos(
        self, post: DouyinPost, opt: PushOptions, plan: PushPlan, notes: list[str]
    ) -> list[Any]:
        """动态图文的动图（MP4）。

        * ``merge_live`` 关闭（默认）→ 每段动图各自作为一条视频发送
        * ``merge_live`` 开启 → 用 ffmpeg 把全部动图拼成**一个**视频

        合并失败会自动退回逐段发送，不会因此丢掉动图。

        **没有文件服务时（``callback_api_base`` 未配置）只能用直链**：本地文件
        是插件自己下载的，平台（NapCat 等独立容器）读不到那个路径，会报
        ``ENOENT: no such file or directory``。这种情况下合并也没意义（产物是
        本地文件），所以直接跳过合并、按原始 CDN 直链逐段发。
        """
        clips = post.live_videos
        if not clips:
            return []

        cap = opt.size_limit_mb * MB if opt.size_limit_mb else 0
        can_local = self.file_service_ready()

        if opt.merge_live:
            if not can_local:
                notes.append(
                    "未配置 callback_api_base，合并后的视频无法交给平台，"
                    "改为逐段直链发送（配置它之后即可合并）"
                )
            else:
                try:
                    merged, bgm_note = await self._merge_live_clips(
                        clips, bgm_url=post.music_url
                    )
                    if merged:
                        plan.local_paths.append(merged)
                        notes.append(f"已将 {len(clips)} 段动图合并为一个视频")
                        if bgm_note:
                            notes.append(bgm_note)
                        return [Video.fromFileSystem(merged)]
                except Exception as exc:
                    notes.append(f"动图合并失败（{exc}），改为逐段发送")

        out: list[Any] = []
        for url in clips:
            if can_local:
                try:
                    path = await self._localize(url, max_bytes=cap)
                    plan.local_paths.append(path)
                    out.append(Video.fromFileSystem(path))
                    continue
                except DouyinError as exc:
                    notes.append(f"动图下载失败（{exc}），改发直链")
            # 没有文件服务 → 直接把 CDN 直链交给平台去拉
            out.append(Video.fromURL(url))
        if out and not opt.merge_live:
            notes.append(f"本条为动态图文，附带 {len(out)} 段动图（逐段发送）")
        return out

    async def _merge_live_clips(
        self, urls: list[str], bgm_url: str = ""
    ) -> tuple[str | None, str]:
        """下载全部动图并用 ffmpeg 拼成一个 MP4。

        返回 ``(本地路径, 附加说明)``；失败抛异常由调用方退回逐段发送。

        策略从最省到最保险：

        1. **无损直拼**：各段规格（编码/分辨率/帧率）完全一致时，用 concat
           demuxer + ``-c copy`` 直接拼接——不重编码，秒级，体积不变。
           **有背景音乐时跳过这一步**：要换音轨就必须重编码。
        2. **重编码拼接**：统一 scale+pad 到固定画布后拼；有背景音乐时把 BGM
           作为整段音轨（比视频短就循环、比视频长就裁到视频长度），
           原各段的音轨丢弃——这也是抖音里实际听到的效果。
        3. 全都失败 → 抛异常。
        """
        if not urls:
            return None, ""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise DouyinError("未安装 ffmpeg，无法合并动图")
        if self.tmp_dir is None:
            raise DouyinError("未配置临时目录")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        stem = f"dylive_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        inputs: list[str] = []
        for i, url in enumerate(urls):
            dest = self.tmp_dir / f"{stem}_{i}.mp4"
            await self.client.download_to(url, dest, max_bytes=0)
            inputs.append(str(dest))

        out = self.tmp_dir / f"{stem}.mp4"

        # 合并成功后源片段就没用了，立刻删掉——否则一条 15 张图的动态图文会
        # 在 media_tmp 里留下 15 个中间文件，攒几百个就是上百 MB
        # （这些片段不用等 cleanup_tmp 的一小时，删除它们也不影响已经合并好的产物）。
        def _drop_sources() -> None:
            for path in inputs:
                Path(path).unlink(missing_ok=True)

        # 背景音乐：先下到本地，后面作为整段音轨
        bgm_path = ""
        bgm_note = ""
        if bgm_url:
            try:
                bgm_dest = self.tmp_dir / f"{stem}_bgm.mp3"
                await self.client.download_to(bgm_url, bgm_dest, max_bytes=50 * MB)
                bgm_path = str(bgm_dest)
            except DouyinError as exc:
                bgm_note = f"背景音乐下载失败（{exc}），改用动图自带的音轨"

        # 有 BGM 时必须重编码（换音轨没法 -c copy），所以跳过无损直拼
        if not bgm_path:
            sigs = [await self._probe_video_sig(p) for p in inputs]
            if all(sigs) and len(set(sigs)) == 1:
                if await self._run_ffmpeg_copy(ffmpeg, inputs, str(out)):
                    self._dbg(f"动图已无损拼接：{out.name}（{len(inputs)} 段）")
                    _drop_sources()
                    return str(out), bgm_note
                self._dbg("无损拼接失败，改用重编码")

        # 2) 重编码拼接
        for with_audio in (True, False):
            if await self._run_ffmpeg_concat(
                ffmpeg, inputs, str(out), with_audio, bgm=bgm_path
            ):
                self._dbg(
                    f"动图已合并（重编码）：{out.name}（{len(inputs)} 段，"
                    f"{out.stat().st_size / 1024:.0f}KB"
                    f"{'，已带背景音乐' if bgm_path else ''}）"
                )
                if bgm_path:
                    Path(bgm_path).unlink(missing_ok=True)
                    bgm_note = "已把作品背景音乐与动图原声混音（两者都保留）"
                _drop_sources()
                return str(out), bgm_note
        if bgm_path:
            Path(bgm_path).unlink(missing_ok=True)
        raise DouyinError("ffmpeg 合并动图失败")

    async def _probe_video_sig(self, path: str) -> str:
        """取视频的「编码+分辨率+帧率」指纹，用于判断能否无损直拼。"""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return ""
        code, out = await self._run_cmd([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-of", "default=nw=1", path,
        ])
        if code != 0:
            return ""
        kv = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        return f"{kv.get('codec_name','')}|{kv.get('width','')}x{kv.get('height','')}|{kv.get('r_frame_rate','')}"

    async def _run_ffmpeg_copy(
        self, ffmpeg: str, inputs: list[str], out: str
    ) -> bool:
        """concat demuxer + ``-c copy``：规格一致时的无损直拼。"""
        if self.tmp_dir is None:
            return False
        lst = self.tmp_dir / f"concat_{uuid.uuid4().hex[:8]}.txt"
        try:
            lst.write_text(
                "".join(f"file '{p}'\n" for p in inputs), encoding="utf-8"
            )
            code, _ = await self._run_cmd([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c", "copy", "-movflags", "+faststart", out,
            ])
        finally:
            lst.unlink(missing_ok=True)
        return code == 0 and Path(out).exists() and Path(out).stat().st_size > 0

    async def _run_ffmpeg_concat(
        self,
        ffmpeg: str,
        inputs: list[str],
        out: str,
        with_audio: bool,
        bgm: str = "",
    ) -> bool:
        """调用 ffmpeg 把多段视频统一规格后拼接。返回是否成功。

        ``bgm`` 非空时：``with_audio=True`` 把**动图自身的音轨**与背景音乐
        **混音**（``amix``，两者都保留）；``with_audio=False`` 则只用背景音乐。
        BGM 用 ``-stream_loop -1`` 循环，``amix`` 的 ``duration=first`` 让它跟着
        视频长度走，不会比画面长。
        """
        w, h = LIVE_CANVAS
        n = len(inputs)
        # 动图自身是否提供音轨
        use_clip_audio = with_audio
        parts: list[str] = []
        labels: list[str] = []
        for i in range(n):
            parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={LIVE_FPS}[v{i}]"
            )
            if use_clip_audio:
                parts.append(
                    f"[{i}:a]aresample=44100,"
                    f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
                )
                labels.append(f"[v{i}][a{i}]")
            else:
                labels.append(f"[v{i}]")
        if use_clip_audio:
            parts.append(
                "".join(labels) + f"concat=n={n}:v=1:a=1[vo][ao]"
            )
        else:
            parts.append("".join(labels) + f"concat=n={n}:v=1:a=0[vo]")

        if bgm and use_clip_audio:
            # 动图音轨 + 背景音乐混在一起：两个都保留，长度跟视频走
            parts.append(
                f"[{n}:a]aresample=44100,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[bgm]"
            )
            parts.append(
                "[ao][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )

        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for path in inputs:
            cmd += ["-i", path]
        bgm_index = -1
        if bgm:
            # 背景音乐作为最后一个输入；比视频短就循环、比视频长就被 -shortest 裁掉
            bgm_index = len(inputs)
            cmd += ["-stream_loop", "-1", "-i", bgm]
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vo]"]
        if bgm_index >= 0 and with_audio:
            # 混音后的音轨
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        elif bgm_index >= 0:
            # 动图没有音轨 → 只用背景音乐
            cmd += ["-map", f"{bgm_index}:a", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        elif with_audio:
            cmd += ["-map", "[ao]", "-c:a", "aac", "-b:a", "96k"]
        else:
            cmd += ["-an"]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out,
        ]
        code, stderr = await self._run_cmd(cmd, capture_stderr=True)
        if code != 0:
            msg = (stderr or "").strip().splitlines()
            self._dbg(f"ffmpeg 失败（音轨={with_audio}）：{msg[-1] if msg else '未知'}")
            return False
        return Path(out).exists() and Path(out).stat().st_size > 0

    async def _run_cmd(
        self, cmd: list[str], *, capture_stderr: bool = False
    ) -> tuple[int, str]:
        """跑一个外部命令，返回 ``(返回码, 输出)``；超时或异常按失败处理。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except (asyncio.TimeoutError, OSError) as exc:
            self._dbg(f"命令失败：{Path(cmd[0]).name} {type(exc).__name__}: {exc}")
            return -1, ""
        text = (out or b"").decode("utf-8", "replace")
        if capture_stderr:
            text += (err or b"").decode("utf-8", "replace")
        return proc.returncode or 0, text

    # ------------------------------------------------------------------
    # 视频
    # ------------------------------------------------------------------

    async def _build_video_post(
        self,
        post: DouyinPost,
        opt: PushOptions,
        plan: PushPlan,
        lines: list[str],
        media: list[Any],
        notes: list[str],
        is_group: bool,
        force_link: bool,
        force_download: bool = False,
    ) -> None:
        # 优先 play_addr 直链：它是 CDN 上的真实文件地址，不需要跳转。
        # aweme.snssdk.com 的播放接口会 302，部分 OneBot 实现处理不好，
        # 因此仅在用户显式开启 prefer_no_watermark 时才优先使用。
        direct_url = post.video_url
        nwm_url = post.no_watermark_url()

        if force_link:
            video_url = ""
        elif opt.prefer_no_watermark and nwm_url:
            video_url = nwm_url
        else:
            video_url = direct_url or nwm_url

        if not video_url:
            notes.append("未取到视频直链，已改为发送链接")
            self._append_link_line(lines, post, opt)
            plan.media_kind = "link"
            return

        # ---- 体积判定 ----
        limit_bytes = opt.size_limit_mb * MB
        size = 0
        size_known = True
        if limit_bytes > 0 and not force_link:
            size = await self.client.probe_size(video_url)
            if size == 0:
                size = post.video_size_hint_bytes
                if size:
                    notes.append("体积探测失败，改用码率估算值判断")
                else:
                    # 宁可发链接，也不要盲目尝试下载几百 MB 的文件
                    size_known = False
                    size = limit_bytes + 1
                    notes.append("体积探测失败且无法估算，按超限处理")
        plan.video_size_bytes = size if size_known else 0

        over_limit = bool(limit_bytes > 0 and size > limit_bytes)
        if over_limit or force_link:
            if over_limit:
                lines.append(
                    {
                        "key": "warn",
                        "text": _FALLBACK_TEMPLATE.format(
                            size=size / MB, limit=opt.size_limit_mb
                        )
                        if size_known
                        else _FALLBACK_UNKNOWN.format(limit=opt.size_limit_mb),
                    }
                )
                notes.append(
                    f"视频 {size / MB:.1f}MB 超过 {opt.size_limit_mb}MB 上限"
                    if size_known
                    else "体积未知，按超限处理"
                )
                plan.degraded = True
            else:
                notes.append("调试模式：强制发送链接")
            self._append_link_line(lines, post, opt)
            if opt.cover_on_fallback and post.cover_url:
                comp = await self._make_image(post.cover_url, plan, notes)
                if comp is not None:
                    media.append(comp)
            plan.media_kind = "link"
            plan.media_count = len(media)
            return

        # ---- 未超限：决定直链投递还是先下载 ----
        #
        # 关键区别（实测）：
        # * **公开作品**的直链不带 Cookie 也能下载（300+MB 的视频实测 200 完整回来），
        #   所以可以直接把 URL 交给平台，省掉一次下载。
        # * **「仅自己可见」的作品**（``status.private_status == 1``）直链无 Cookie
        #   一定 403，链接也无法分享，必须由插件带 Cookie 下下来再经文件服务托管。
        #
        # 因此默认 "auto"：公开走直链，私密走下载；失败时上层还会用
        # ``force_download=True`` 重试一次，最后才降级成发链接。
        need_download = force_download or opt.video_delivery == "download"
        if opt.video_delivery == "auto" and post.is_private:
            need_download = True
            notes.append("该作品为「仅自己可见」，直链无法分享，已改为本地化投递")
        elif opt.video_delivery == "auto" and not post.is_private:
            notes.append("公开作品：优先直链投递，失败会自动改为下载后重试")

        local_path = ""
        localize_failed = False
        if need_download:
            if self.file_service_ready():
                try:
                    local_path = await self._localize(video_url, limit_bytes)
                    plan.local_paths.append(local_path)
                except DouyinError as exc:
                    localize_failed = True
                    notes.append(f"视频下载失败（{exc}），改为发送链接")
            else:
                localize_failed = True
                notes.append(
                    "该作品需要本地化投递（仅自己可见），但未配置 "
                    "callback_api_base，已改为发送链接。"
                    "配置位置：AstrBot WebUI → 配置 → 系统配置 → 对外可达的回调接口地址"
                )

        if localize_failed:
            self._append_link_line(lines, post, opt)
            if opt.cover_on_fallback and post.cover_url:
                comp = await self._make_image(post.cover_url, plan, notes)
                if comp is not None:
                    media.append(comp)
            plan.media_kind = "link"
            plan.media_count = len(media)
            plan.degraded = True
            return

        if opt.video_mode == "file" and is_group:
            media.append(
                File(name=_safe_filename(post), file=local_path)
                if local_path
                else File(name=_safe_filename(post), url=video_url)
            )
            plan.media_kind = "file"
        else:
            if opt.video_mode == "file" and not is_group:
                notes.append("私聊不支持群文件，已自动改用视频发送")
            media.append(
                Video.fromFileSystem(local_path)
                if local_path
                else Video.fromURL(video_url)
            )
            plan.media_kind = "video"
        plan.media_count = 1

        # 平台仍可能拒绝媒体（下载失败 / retcode 100），预先备好一条兜底消息
        plan.fallback_chain = await self._build_fallback(post, opt, lines)

    @staticmethod
    def _append_link_line(
        lines: list[str], post: DouyinPost, opt: PushOptions
    ) -> None:
        """补一行链接，但已经出现过就不重复补。

        ``show_link`` 开启时链接已经在文本里了，降级再补一次会看到两条一样的链接。
        """
        link = pick_link(post, opt)
        if any(link and link in line.get("text", "") for line in lines):
            return
        lines.append({"key": "link", "text": f"🔗 {link}"})

    # ------------------------------------------------------------------
    # 媒体本地化
    # ------------------------------------------------------------------

    @staticmethod
    def file_service_ready() -> bool:
        """AstrBot 的文件服务是否可用（决定能否把本地文件暴露给消息平台）。"""
        try:
            from astrbot.core import astrbot_config

            return bool(str(astrbot_config.get("callback_api_base") or "").strip())
        except Exception:
            return False

    async def _make_image(
        self,
        url: str,
        plan: PushPlan,
        notes: list[str],
        *,
        max_side: int = 0,
        quality: int = 92,
        force_local: bool = False,
    ) -> Any | None:
        """生成一个图片组件，优先走本地化 + 文件服务。

        直链投递看着能用，但平台自行下载抖音图床时行为不一致（实测同一张封面
        平台能下载成功、却在 QQ 侧报 ``highway upload error 921``）。

        ``max_side`` 非 0 时把长边压到该像素——合并转发卡片里的图片会被
        base64 内联，不压的话 15 张 1920×1080 能撑到好几 MB。

        ``force_local=True`` 时不管文件服务是否可用都本地化。卡片必须要它：
        ``Node.to_dict()`` 是把**本地文件**读出来 base64 内联的，根本不经过文件
        服务；而如果这里退回 ``Image.fromURL``，平台拿到的是 **WebP**，照样报 921。
        """
        if not url:
            return None
        if force_local or self.file_service_ready():
            try:
                path = await self._localize_image(
                    url, max_side=max_side, quality=quality
                )
                plan.local_paths.append(path)
                return Image.fromFileSystem(path)
            except DouyinError as exc:
                notes.append(f"图片本地化失败（{exc}），改发直链")
                if force_local:
                    return None      # 卡片必须用本地 JPEG，退回 WebP 会 921
        return Image.fromURL(url)

    async def _make_live_item(
        self, clip_url: str, plan: PushPlan, notes: list[str]
    ) -> Any | None:
        """把一个动图做成能放进「合并转发卡片」的 Video 段。

        ``Node.to_dict()`` 只把 **Image / Record** 转成 base64；视频段是原样输出
        ``file`` 字段的，本地路径（``file:///...``）平台容器根本读不到。所以这里
        先把动图下载到本地、再注册到 AstrBot 文件服务，换成 HTTP 地址让平台来拉；
        没有文件服务时退回抖音 CDN 直链。
        """
        if not clip_url:
            return None
        if self.file_service_ready():
            try:
                path = await self._localize(clip_url, max_bytes=0)
                plan.local_paths.append(path)
                url = await Video.fromFileSystem(path).register_to_file_service()
                return Video(file=url)
            except Exception as exc:
                notes.append(f"动图注册文件服务失败（{exc}），改发直链")
        return Video.fromURL(clip_url)

    async def _localize(self, url: str, max_bytes: int) -> str:
        """带 Cookie 把视频下载到本地临时文件，返回路径。

        文件保留一段时间供平台拉取，由 ``cleanup_tmp()`` 定期清理——
        ``send_message`` 返回不代表平台已经取完文件，立即删除会导致偶发失败。
        """
        if self.tmp_dir is None:
            raise DouyinError("未配置临时目录")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        name = f"dy_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.mp4"
        dest = self.tmp_dir / name
        size = await self.client.download_to(url, dest, max_bytes=max_bytes)
        self._dbg(f"视频已本地化：{name}（{size / 1024 / 1024:.2f}MB）")
        return str(dest)

    async def _localize_image(
        self, url: str, *, max_side: int = 0, quality: int = 92
    ) -> str:
        """把图片下载到本地，必要时转成 JPEG。

        抖音图文下发的是 WebP。QQ 侧对 WebP 支持不完整，直接发会出现
        ``highway upload error_code=921``，所以这里统一转成 JPEG。

        ``max_side`` 非 0 时把长边缩到该像素（合并转发卡片里图片是 base64 内联的，
        不缩会把消息撑得很大）。
        """
        if self.tmp_dir is None:
            raise DouyinError("未配置临时目录")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        stem = f"dyimg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        raw = self.tmp_dir / f"{stem}.bin"
        await self.client.download_to(url, raw, max_bytes=30 * MB)

        converted = self.tmp_dir / f"{stem}.jpg"
        try:
            from PIL import Image as PILImage

            with PILImage.open(raw) as img:
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                if max_side > 0:
                    img.thumbnail((max_side, max_side), PILImage.LANCZOS)
                img.save(converted, "JPEG", quality=quality, optimize=True)
            raw.unlink(missing_ok=True)
            self._dbg(
                f"图片已转为 JPEG：{converted.name}（{converted.stat().st_size / 1024:.0f}KB）"
            )
            return str(converted)
        except Exception as exc:
            # 转换失败就原样使用（JPEG/PNG 本来就不需要转换）
            self._dbg(f"图片转换跳过（{type(exc).__name__}），按原格式使用")
            fallback = self.tmp_dir / f"{stem}.img"
            if raw.exists():
                raw.rename(fallback)
            return str(fallback)

    def cleanup_tmp(self, max_age_seconds: int = 3600) -> int:
        """清理过期的临时媒体文件，返回删除数量。"""
        if self.tmp_dir is None or not self.tmp_dir.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for pattern in ("dy_*.mp4", "dyimg_*", "dylive_*"):
            for item in self.tmp_dir.glob(pattern):
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    def _dbg(self, message: str) -> None:
        if self.log:
            self.log.debug(f"[douyin] {message}")

    # ------------------------------------------------------------------
    # 兜底
    # ------------------------------------------------------------------

    async def _build_fallback(
        self, post: DouyinPost, opt: PushOptions, lines: list[str]
    ) -> MessageChain:
        """构造媒体投递失败时使用的兜底消息。"""
        plan = PushPlan(chain=MessageChain())
        fb_lines = [x["text"] for x in lines]
        fb_lines.append(f"⚠️ 媒体投递失败，请点链接查看：\n🔗 {pick_link(post, opt)}")
        fb = MessageChain()
        fb.message("\n".join(x for x in fb_lines if x))
        if opt.cover_on_fallback and post.cover_url:
            comp = await self._make_image(post.cover_url, plan, [])
            if comp is not None:
                fb.chain.append(comp)
        return fb

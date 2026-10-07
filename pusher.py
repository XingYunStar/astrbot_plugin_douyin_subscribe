"""把一条作品按配置组装成待发送的消息链。

推送构成（全部实测于 AstrBot 4.25.5 + aiocqhttp）：

* **图文** → 依次发送作品内的全部图片
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
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.message_components import File, Image, Video
from astrbot.core.message.message_event_result import MessageChain

from .douyin_api import DouyinClient, DouyinError, DouyinPost

MB = 1024 * 1024

_FALLBACK_TEMPLATE = "🎬 视频体积约 {size:.1f}MB，超过上限 {limit}MB，改为发送链接："
_FALLBACK_UNKNOWN = "🎬 视频体积无法确认，超过上限 {limit}MB，改为发送链接："


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
        )


@dataclass
class PushPlan:
    """一次推送的组装结果，附带可供 WebUI 预览的诊断信息。"""

    chain: MessageChain
    text: str = ""
    media_kind: str = ""  # image / video / file / link / none
    media_count: int = 0
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
    ) -> PushPlan:
        """把一条作品组装成消息链。

        ``force_link=True`` 时跳过媒体投递，只发链接（用于调试或超大文件）。
        ``force_download=True`` 时强制先下载再投递（平台直链投递失败后的重试路径）。
        """
        notes: list[str] = []
        lines: list[dict[str, str]] = list(format_lines(post, opt))
        media: list[Any] = []
        plan = PushPlan(chain=MessageChain())
        plan.text = "\n".join(x["text"] for x in lines)

        if post.is_image:
            await self._build_image_post(
                post, opt, plan, lines, media, notes, force_link=force_link
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
        if final_text:
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
    ) -> None:
        if force_link:
            self._append_link_line(lines, post, opt)
            plan.media_kind = "link"
            return

        for url in post.images:
            if not url:
                continue
            comp = await self._make_image(url, plan, notes)
            if comp is not None:
                media.append(comp)

        plan.media_kind = "image"
        plan.media_count = len(media)
        if not media:
            notes.append("该图文作品没有可用的图片链接，已改为发送链接")
            self._append_link_line(lines, post, opt)
            plan.media_kind = "link"

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
        self, url: str, plan: PushPlan, notes: list[str]
    ) -> Any | None:
        """生成一个图片组件，优先走本地化 + 文件服务。

        直链投递看着能用，但平台自行下载抖音图床时行为不一致（实测同一张封面
        平台能下载成功、却在 QQ 侧报 ``highway upload error 921``）。
        """
        if not url:
            return None
        if self.file_service_ready():
            try:
                path = await self._localize_image(url)
                plan.local_paths.append(path)
                return Image.fromFileSystem(path)
            except DouyinError as exc:
                notes.append(f"图片本地化失败（{exc}），改发直链")
        return Image.fromURL(url)

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

    async def _localize_image(self, url: str) -> str:
        """把图片下载到本地，必要时转成 JPEG。

        抖音图文下发的是 WebP。QQ 侧对 WebP 支持不完整，直接发会出现
        ``highway upload error_code=921``，所以这里统一转成 JPEG。
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
                img.save(converted, "JPEG", quality=92)
            raw.unlink(missing_ok=True)
            self._dbg(f"图片已转为 JPEG：{converted.name}")
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
        for pattern in ("dy_*.mp4", "dyimg_*"):
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

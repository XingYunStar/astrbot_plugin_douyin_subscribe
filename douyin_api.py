"""抖音 Web 接口客户端：纯 Python 签名 + 作品获取与解析。

签名原理
--------
抖音对 ``/aweme/v1/web/aweme/post/`` 一类接口要求 ``x-secsdk-web-signature``，
它由字节的 securitySDK 生成。该签名**不是**黑盒：它是一个纯函数

    sig = md5(f"{uifid}_{timestamp}_{query}")

其中 ``query`` 是「除签名外全部参数」按请求顺序序列化后的字符串，``SALT`` 是
securitySDK 打包文件里的一个字符串常量（douyin_web 使用 project-id=34）。
它不依赖任何会话密钥或握手，所以可以用标准库直接算出来。

``uifid`` 直接从 Cookie 的 ``UIFID`` 字段读取，无需浏览器导出任何东西。

本模块只依赖标准库 + aiohttp，不需要 Node.js，也不需要携带任何第三方 JS。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: securitySDK 中 douyin_web（project-id=34）使用的盐值。
#: 字节更新前端 bundle 时此值**可能**变化；若签名突然全部失效，优先怀疑这里。
#: 更换方式：从 https://lf-security.bytegoofy.com 对应版本的 runtime_bundler 中
#: 提取该字符串常量即可，无需重新逆向算法。
SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"

WEB_BASE = "https://www.douyin.com"
POST_PATH = "/aweme/v1/web/aweme/post/"
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
PROFILE_PATH = "/aweme/v1/web/user/profile/other/"
SELF_PROFILE_PATH = "/aweme/v1/web/user/profile/self/"
SEARCH_PATH = "/aweme/v1/web/general/search/single/"

#: 按抖音号（unique_id）解析账号的接口。
#: 实测比站内搜索可靠得多——站内搜索经常返回 200 空结果，且对未登录/高频请求
#: 会直接给出 ``status_msg: "blocked"``；而这个接口走的是另一条链路。
IES_USER_INFO = "https://www.iesdouyin.com/web/api/v2/user/info/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

#: 公共查询参数。aid=6383 是必需的，缺失会导致接口返回「用户未登录」。
COMMON_PARAMS: dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": "1707",
    "screen_height": "1067",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "152.0.0.0",
}

#: 从 Cookie 中寻找 uifid 时的候选键名，按优先级排列。
UIFID_COOKIE_KEYS = ("UIFID", "UIFID_TEMP", "uifid", "uifid_temp", "uifidtemp", "UIFIDTEMP")

_SEC_UID_RE = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_\-]+")
_TRAILING_ID_RE = re.compile(r"(\d{15,25})")


class DouyinError(Exception):
    """接口调用或解析失败。"""


class CookieInvalidError(DouyinError):
    """Cookie 缺失或已失效。"""


class RiskControlError(DouyinError):
    """疑似被风控拦截（HTTP 200 但内容为空）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class DouyinAccount:
    """一个抖音账号的引用。"""

    sec_uid: str
    nickname: str = ""
    unique_id: str = ""  # 抖音号
    avatar: str = ""
    aweme_count: int = 0
    follower_count: int = 0

    @property
    def display(self) -> str:
        if self.nickname and self.unique_id:
            return f"{self.nickname}（{self.unique_id}）"
        return self.nickname or self.unique_id or self.sec_uid[:16]

    @property
    def profile_url(self) -> str:
        return f"https://www.douyin.com/user/{self.sec_uid}"


@dataclass
class DouyinPost:
    """一条作品（视频或图文）。"""

    aweme_id: str
    desc: str = ""
    create_time: int = 0
    is_image: bool = False
    images: list[str] = field(default_factory=list)
    video_url: str = ""
    video_uri: str = ""
    cover_url: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    digg_count: int = 0
    comment_count: int = 0
    collect_count: int = 0
    share_count: int = 0
    author: DouyinAccount = field(default_factory=lambda: DouyinAccount(sec_uid=""))
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- 便捷属性 -----------------------------------------------------------

    @property
    def share_url(self) -> str:
        return f"https://www.douyin.com/video/{self.aweme_id}"

    @property
    def kind(self) -> str:
        return "图文" if self.is_image else "视频"

    @property
    def is_private(self) -> bool:
        """作品是否为「仅自己可见」。

        实测：``status.private_status == 1`` 的作品，其视频直链**不带 Cookie
        一定 403**，链接也无法分享；而公开作品（``private_status != 1``）的直链
        不带 Cookie 也能正常下载（实测 200 + 完整 video/mp4）。
        """
        status = self.raw.get("status") or {}
        if isinstance(status, dict):
            if status.get("private_status") == 1:
                return True
            if status.get("is_delete"):
                return True
        return bool(self.raw.get("is_private"))

    @property
    def is_shareable(self) -> bool:
        """作品直链是否可以对外分享（公开作品才可）。"""
        return not self.is_private

    @property
    def timestamp(self) -> float:
        return float(self.create_time or 0)

    @property
    def video_size_hint_bytes(self) -> int:
        """由码率与时长估算的视频体积，仅作兜底（可能偏差较大）。"""
        if self.duration_ms <= 0:
            return 0
        for br in (self.raw.get("video", {}) or {}).get("bit_rate") or []:
            rate = br.get("bit_rate")
            if rate:
                return int(rate / 8 * (self.duration_ms / 1000))
        return 0

    def no_watermark_url(self, ratio: str = "1080p") -> str:
        """由 play_addr.uri 拼接的播放接口，通常返回无水印版本。"""
        if not self.video_uri:
            return ""
        return (
            "https://aweme.snssdk.com/aweme/v1/play/"
            f"?video_id={self.video_uri}&ratio={ratio}&line=0"
        )

    def resource_url(self, prefer_no_watermark: bool = False) -> str:
        """返回**可直接下载的资源直链**（不是作品网页）。

        视频返回 CDN 上的真实文件地址（``play_addr``），图文返回首图地址。
        注意：视频直链在缺少 Cookie 时会返回 403，浏览器里已登录抖音时可直接打开。
        """
        if self.is_image:
            return self.images[0] if self.images else self.share_url
        if prefer_no_watermark:
            nwm = self.no_watermark_url()
            if nwm:
                return nwm
        return self.video_url or self.share_url

    def to_public_dict(self) -> dict[str, Any]:
        """供 WebUI 预览使用的精简结构（不包含 raw）。"""
        return {
            "aweme_id": self.aweme_id,
            "kind": self.kind,
            "is_image": self.is_image,
            "desc": self.desc,
            "create_time": self.create_time,
            "create_time_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.create_time)
            )
            if self.create_time
            else "",
            "images": list(self.images),
            "image_count": len(self.images),
            "video_url": self.video_url,
            "video_uri": self.video_uri,
            "no_watermark_url": self.no_watermark_url(),
            "cover_url": self.cover_url,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "statistics": {
                "digg": self.digg_count,
                "comment": self.comment_count,
                "collect": self.collect_count,
                "share": self.share_count,
            },
            "author": {
                "sec_uid": self.author.sec_uid,
                "nickname": self.author.nickname,
                "unique_id": self.author.unique_id,
                "avatar": self.author.avatar,
            },
            "share_url": self.share_url,
            "is_private": self.is_private,
            "is_shareable": self.is_shareable,
        }


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------


def extract_uifid(cookie: str) -> str:
    """从 Cookie 字符串中提取 uifid，找不到则返回空串。"""
    if not cookie:
        return ""
    jar: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    for key in UIFID_COOKIE_KEYS:
        if jar.get(key):
            return jar[key]
    return ""


def build_signature(params: dict[str, str], uifid: str, timestamp: str | int) -> str:
    """计算 x-secsdk-web-signature。

    ``params`` 必须是**除签名外**的全部参数，且保持将要发送的顺序。
    """
    query = urllib.parse.urlencode(list(params.items()))
    payload = f"{uifid}_{timestamp}_{SIGN_SALT}_{query}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def sign_params(
    params: dict[str, str],
    uifid: str,
    timestamp: str | int | None = None,
) -> dict[str, str]:
    """给参数补上 uifid / timestamp / x-secsdk-web-signature 三件套。"""
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    if not uifid:
        raise CookieInvalidError(
            "Cookie 中找不到 UIFID，无法生成签名。请确认粘贴的是完整的 douyin.com Cookie。"
        )
    signed = dict(params)
    signed["uifid"] = uifid
    signed["timestamp"] = ts
    signed["x-secsdk-web-signature"] = build_signature(signed, uifid, ts)
    return signed


def parse_cookie(cookie: str) -> dict[str, str]:
    """把 Cookie 字符串解析成字典。"""
    jar: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                jar[k] = v
    return jar


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _first_url(node: Any) -> str:
    if isinstance(node, dict):
        urls = node.get("url_list") or []
        if urls:
            return str(urls[0])
    return ""


def parse_author(data: dict[str, Any]) -> DouyinAccount:
    au = data.get("author") or {}
    return DouyinAccount(
        sec_uid=str(au.get("sec_uid") or ""),
        nickname=str(au.get("nickname") or ""),
        unique_id=str(au.get("unique_id") or au.get("short_id") or ""),
        avatar=_first_url(au.get("avatar_thumb") or au.get("avatar_larger") or {}),
        aweme_count=int(au.get("aweme_count") or 0),
        follower_count=int(au.get("follower_count") or 0),
    )


def parse_post(data: dict[str, Any]) -> DouyinPost:
    """把接口返回的单条 aweme 结构转成 DouyinPost。"""
    video = data.get("video") or {}
    images_raw = data.get("images") or []

    images: list[str] = []
    for item in images_raw:
        url = _first_url(item)
        if url:
            images.append(url)

    # 图文判定：有 images 就是图文（aweme_type 68 亦可，但 images 更可靠）
    is_image = bool(images)

    stats = data.get("statistics") or {}
    return DouyinPost(
        aweme_id=str(data.get("aweme_id") or ""),
        desc=str(data.get("desc") or ""),
        create_time=int(data.get("create_time") or 0),
        is_image=is_image,
        images=images,
        video_url=_first_url(video.get("play_addr")),
        video_uri=str((video.get("play_addr") or {}).get("uri") or ""),
        cover_url=_first_url(video.get("cover")) or _first_url(video.get("origin_cover")),
        duration_ms=int(video.get("duration") or data.get("duration") or 0),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        digg_count=int(stats.get("digg_count") or 0),
        comment_count=int(stats.get("comment_count") or 0),
        collect_count=int(stats.get("collect_count") or 0),
        share_count=int(stats.get("share_count") or 0),
        author=parse_author(data),
        raw=data,
    )


def parse_profile(user: dict[str, Any]) -> DouyinAccount:
    return DouyinAccount(
        sec_uid=str(user.get("sec_uid") or ""),
        nickname=str(user.get("nickname") or ""),
        unique_id=str(user.get("unique_id") or user.get("short_id") or ""),
        avatar=_first_url(user.get("avatar_thumb") or user.get("avatar_larger") or {}),
        aweme_count=int(user.get("aweme_count") or 0),
        follower_count=int(user.get("follower_count") or 0),
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class DouyinClient:
    """异步抖音客户端。

    设计要点：
    * 每次请求独立生成签名（签名与 URL 强绑定，不可复用）。
    * **作品列表接口绝不能带 ``publish_video_strategy_type``**——带上它（值为 2 时）
      接口会返回 ``HTTP 200 + status_code:0`` 但 ``aweme_list`` 为空，看起来像风控，
      实则参数语义不匹配。这个坑经逐参数隔离实测确认，务必不要"顺手补全参数"。
    * 成功判定以「是否真的拿到数据」为准，而不是 ``status_code``——风控会返回
      ``200 + status_code:0 + 空内容``。
    """

    def __init__(
        self,
        cookie: str,
        *,
        timeout: int = 20,
        debug: bool = False,
        user_agent: str = DEFAULT_UA,
        logger=None,
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.debug = debug
        self.user_agent = user_agent
        self.log = logger
        self._cookies = parse_cookie(self.cookie)
        self.uifid = extract_uifid(self.cookie)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self.last_error: str = ""

    # -- 会话管理 -----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        "Referer": f"{WEB_BASE}/",
                    },
                    cookies=self._cookies,
                )
            return self._session

    async def close(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    def _dbg(self, msg: str) -> None:
        self.last_error = ""
        if self.debug and self.log:
            self.log.info(f"[douyin] {msg}")

    # -- 底层请求 -----------------------------------------------------------

    async def _request(
        self,
        path: str,
        params: dict[str, str],
        *,
        referer: str | None = None,
        need_sign: bool = True,
    ) -> dict[str, Any]:
        """发起一次请求并返回 JSON。

        ``need_sign=False`` 时直接发送（profile/资料类接口无需签名）。
        """
        full_params = {**COMMON_PARAMS, **params}
        if need_sign:
            full_params = sign_params(full_params, self.uifid)

        session = await self._get_session()
        headers = {"Referer": referer or f"{WEB_BASE}/"}
        url = WEB_BASE + path
        started = time.monotonic()
        try:
            async with session.get(url, params=full_params, headers=headers) as resp:
                text = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as exc:
            raise DouyinError(f"请求超时（{self.timeout}s）") from exc
        except aiohttp.ClientError as exc:
            raise DouyinError(f"网络错误：{exc}") from exc

        elapsed = time.monotonic() - started
        self._dbg(f"{path} -> HTTP {status}, {len(text)}B, {elapsed * 1000:.0f}ms")

        if status == 403:
            body = text[:120].strip()
            if "Uifid" in body:
                raise CookieInvalidError(
                    "接口返回 403（Uifid Not Found）。通常表示 Cookie 中没有 UIFID 字段，"
                    "或 Cookie 已失效，请重新获取完整 Cookie。"
                )
            raise DouyinError(f"接口返回 403，疑似签名或风控问题：{body}")

        if status != 200:
            raise DouyinError(f"接口返回 HTTP {status}：{text[:120]}")

        if not text.strip():
            # 抖音的软拦截常见形态：HTTP 200 + 空 body
            raise RiskControlError(
                "接口返回 HTTP 200 但内容为空，疑似触发风控或 Cookie 失效。"
            )

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DouyinError(f"返回内容不是合法 JSON：{text[:120]}") from exc

    # -- 业务接口 -----------------------------------------------------------

    async def fetch_profile(self, sec_uid: str) -> DouyinAccount:
        """获取账号资料（免签名）。"""
        data = await self._request(
            PROFILE_PATH,
            {
                "sec_user_id": sec_uid,
                "publish_video_strategy_type": "2",
                "personal_center_strategy": "1",
            },
            referer=f"{WEB_BASE}/user/{sec_uid}",
            need_sign=False,
        )
        user = data.get("user")
        if not user:
            raise DouyinError(
                f"未取到账号资料（status_code={data.get('status_code')}, "
                f"status_msg={data.get('status_msg')}），请确认账号链接是否正确。"
            )
        return parse_profile(user)

    async def fetch_self(self) -> DouyinAccount:
        """校验 Cookie 是否有效，并返回当前登录账号。"""
        data = await self._request(
            SELF_PROFILE_PATH,
            {},
            need_sign=False,
        )
        code = data.get("status_code")
        user = data.get("user")
        if code == 8 or not user:
            raise CookieInvalidError(
                f"Cookie 无效或已过期（status_code={code}, status_msg={data.get('status_msg')}）。"
            )
        return parse_profile(user)

    async def fetch_posts_page(
        self,
        sec_uid: str,
        max_cursor: str = "0",
        count: int = 18,
    ) -> tuple[list[DouyinPost], str, bool]:
        """获取一页作品。

        返回 ``(作品列表, 下一页游标, 是否还有更多)``。

        注意：这里**刻意不传** ``publish_video_strategy_type``——实测带了它
        （值 2）会让 ``aweme_list`` 变成空数组，而 ``status_code`` 仍是 0。
        """
        data = await self._request(
            POST_PATH,
            {
                "sec_user_id": sec_uid,
                "count": str(count),
                "max_cursor": str(max_cursor),
                "locate_query": "false",
                "show_live_replay_strategy": "1",
            },
            referer=f"{WEB_BASE}/user/{sec_uid}",
        )

        aweme_list = data.get("aweme_list") or []
        if not aweme_list:
            # 区分「真的没有作品」和「被软拦截」
            code = data.get("status_code")
            if code not in (0, None):
                raise DouyinError(
                    f"获取作品列表失败（status_code={code}, "
                    f"status_msg={data.get('status_msg')}）"
                )
            # status_code=0 但列表为空：交给上层结合 profile.aweme_count 判断
            return [], str(data.get("max_cursor") or "0"), bool(data.get("has_more"))

        posts = [parse_post(a) for a in aweme_list if a.get("aweme_id")]
        return posts, str(data.get("max_cursor") or "0"), bool(data.get("has_more"))

    async def fetch_posts(
        self,
        sec_uid: str,
        limit: int = 20,
        *,
        page_size: int = 18,
    ) -> list[DouyinPost]:
        """获取最近若干条作品（自动翻页直到达标或没有更多）。"""
        collected: list[DouyinPost] = []
        cursor = "0"
        seen: set[str] = set()
        while len(collected) < limit:
            posts, cursor, has_more = await self.fetch_posts_page(
                sec_uid, max_cursor=cursor, count=min(page_size, max(limit - len(collected), 1))
            )
            fresh = [p for p in posts if p.aweme_id not in seen]
            for p in fresh:
                seen.add(p.aweme_id)
            collected.extend(fresh)
            if not has_more or not posts or not cursor or cursor == "0":
                break
            if not fresh:
                break
            await asyncio.sleep(0.6)  # 翻页留出间隔，降低风控概率
        return collected[:limit]

    async def fetch_detail(self, aweme_id: str) -> DouyinPost:
        """获取单条作品详情（需签名）。"""
        data = await self._request(
            DETAIL_PATH,
            {"aweme_id": str(aweme_id)},
            referer=f"{WEB_BASE}/video/{aweme_id}",
        )
        detail = data.get("aweme_detail")
        if not detail:
            raise DouyinError(
                f"未取到作品详情（status_code={data.get('status_code')}, "
                f"status_msg={data.get('status_msg')}）"
            )
        return parse_post(detail)

    # -- 账号标识解析 -------------------------------------------------------

    async def resolve_account(self, text: str) -> DouyinAccount:
        """把用户输入解析成账号。

        支持三种形式（按可靠性排序）：

        1. 主页 / 分享链接或直接的 ``sec_uid``（最稳）
        2. 抖音号（``unique_id``），通过搜索接口精确匹配
        3. 纯数字用户 ID（不推荐，成功率低）
        """
        text = (text or "").strip()
        if not text:
            raise DouyinError("订阅内容为空，请提供抖音主页链接、分享链接或抖音号。")

        # 1) 直接从文本里抠 sec_uid
        m = _SEC_UID_RE.search(text)
        if m:
            sec_uid = m.group(0)
            try:
                return await self.fetch_profile(sec_uid)
            except DouyinError:
                # 资料拿不到也要能用，构造一个最简引用
                return DouyinAccount(sec_uid=sec_uid)

        # 2) 短链 / 任意链接 -> 跟随跳转找 sec_uid
        if text.startswith("http"):
            resolved = await self._resolve_redirect(text)
            if resolved:
                m = _SEC_UID_RE.search(resolved)
                if m:
                    sec_uid = m.group(0)
                    try:
                        return await self.fetch_profile(sec_uid)
                    except DouyinError:
                        return DouyinAccount(sec_uid=sec_uid)
                # 可能是作品短链 -> 取作者
                mm = _TRAILING_ID_RE.search(resolved)
                if mm and "/video/" in resolved:
                    post = await self.fetch_detail(mm.group(1))
                    if post.author.sec_uid:
                        return post.author
            raise DouyinError(
                "无法从该链接解析出账号。请改用账号主页链接"
                "（形如 https://www.douyin.com/user/MS4wLjABAAAA... ）。"
            )

        # 3) 先按抖音号精确解析（可靠），失败再退回站内搜索（可能被限流）
        account = await self.resolve_by_unique_id(text)
        if account is not None:
            return account
        return await self.search_account(text)

    async def resolve_by_unique_id(self, unique_id: str) -> DouyinAccount | None:
        """按抖音号精确解析账号，失败返回 None。

        走 ``iesdouyin`` 的用户信息接口，实测比站内搜索稳定得多：
        站内搜索对同样的关键词经常返回 200 + 空列表，甚至 ``status_msg: "blocked"``。
        """
        unique_id = (unique_id or "").strip()
        if not unique_id or len(unique_id) > 64:
            return None
        session = await self._get_session()
        try:
            async with session.get(
                IES_USER_INFO,
                params={"unique_id": unique_id},
                headers={"Referer": f"{WEB_BASE}/"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

        user = data.get("user_info") if isinstance(data, dict) else None
        if not user or not user.get("sec_uid"):
            return None
        account = parse_profile(user)
        if not account.unique_id:
            account.unique_id = unique_id
        return account

    async def _resolve_redirect(self, url: str) -> str:
        """跟随短链跳转，返回最终 URL。"""
        session = await self._get_session()
        try:
            async with session.get(
                url,
                allow_redirects=True,
                headers={"Referer": f"{WEB_BASE}/"},
            ) as resp:
                return str(resp.url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""

    async def search_accounts(self, keyword: str, limit: int = 10) -> list[DouyinAccount]:
        """按关键词搜索账号，返回候选列表。

        注意：抖音站内搜索对用户卡片召回很少（通常只有 1 个），且对高频请求会
        返回 HTTP 200 + 空列表（``status_msg`` 可能是 ``blocked``）。调用方需要
        能接受"搜不到"并把用户引导到精确输入路径（主页链接 / 抖音号）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        data = await self._request(
            SEARCH_PATH,
            {
                "keyword": keyword,
                "search_channel": "aweme_user_web",
                "count": str(max(limit, 1)),
                "offset": "0",
                "search_source": "normal_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
            },
            referer=f"{WEB_BASE}/",
            need_sign=False,
        )

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in data.get("data") or []:
            if item.get("type") != 4:
                continue
            for entry in item.get("user_list") or []:
                info = entry.get("user_info") or {}
                sec = str(info.get("sec_uid") or "")
                if sec and sec not in seen:
                    seen.add(sec)
                    candidates.append(info)

        # 搜索结果里没有用户卡片时，若关键词本身像抖音号，走精确解析兜底
        if not candidates:
            account = await self.resolve_by_unique_id(keyword)
            if account is not None:
                candidates.append(
                    {
                        "sec_uid": account.sec_uid,
                        "nickname": account.nickname,
                        "unique_id": account.unique_id,
                        "follower_count": account.follower_count,
                    }
                )

        # 抖音号/昵称完全匹配的排前面
        def rank(info: dict[str, Any]) -> tuple[int, str]:
            uid = str(info.get("unique_id") or "")
            nick = str(info.get("nickname") or "")
            if uid == keyword:
                return (0, nick)
            if nick == keyword:
                return (1, nick)
            return (2, nick)

        candidates.sort(key=rank)
        return [parse_profile(info) for info in candidates[:limit]]

    async def search_account(self, keyword: str) -> DouyinAccount:
        """用抖音号/昵称搜索账号并精确匹配（返回唯一结果）。"""
        candidates = await self.search_accounts(keyword, limit=10)
        if not candidates:
            raise DouyinError(
                f"搜索不到账号「{keyword}」。抖音站内搜索对高频请求会返回空结果，"
                "建议改用账号主页链接，或直接输入抖音号（如 xingyun6535）。"
            )
        # search_accounts 已按完全匹配优先排序
        exact = next(
            (c for c in candidates if c.unique_id == keyword or c.nickname == keyword),
            None,
        )
        return exact or candidates[0]

    # -- 体积探测与下载 -----------------------------------------------------

    async def fetch_bytes(
        self, url: str, max_bytes: int = 512 * 1024
    ) -> tuple[int, bytes, str]:
        """取回一个资源的原始字节，返回 ``(状态码, 内容, content-type)``。

        头像之类的图片走这里：抖音图床在 TLS 指纹层面拒绝 aiohttp，
        因此同样采用「先 aiohttp、失败退 requests」的策略。
        """
        try:
            session = await self._get_session()
            async with session.get(
                url, headers={"Referer": f"{WEB_BASE}/"}
            ) as resp:
                if resp.status != 200:
                    raise DouyinError(f"HTTP {resp.status}")
                body = await resp.content.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise DouyinError("内容过大")
                return resp.status, body, resp.headers.get("Content-Type", "")
        except Exception:
            return await asyncio.to_thread(self._fetch_bytes_requests, url, max_bytes)

    def _fetch_bytes_requests(
        self, url: str, max_bytes: int
    ) -> tuple[int, bytes, str]:
        try:
            import requests
        except ImportError:
            return 0, b"", ""
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self.user_agent, "Referer": f"{WEB_BASE}/"},
                cookies=self._cookies,
                timeout=self.timeout,
                allow_redirects=True,
            )
            body = resp.content[: max_bytes + 1]
            if resp.status_code != 200 or len(body) > max_bytes:
                return 0, b"", ""
            return resp.status_code, body, resp.headers.get("Content-Type", "")
        except Exception:
            return 0, b"", ""

    async def probe_size(self, url: str) -> int:
        """探测远端文件体积（字节）。失败返回 0。

        注意：抖音 CDN **不支持 HEAD**，必须用 GET + Range。
        部分 CDN 节点在 TLS 指纹层面拒绝 aiohttp，因此失败后回退 requests。
        """
        if not url:
            return 0
        size = await self._probe_aiohttp(url)
        if size:
            return size
        try:
            return await asyncio.to_thread(self._probe_requests, url)
        except Exception:
            return 0

    async def _probe_aiohttp(self, url: str) -> int:
        session = await self._get_session()
        try:
            async with session.get(
                url,
                headers={"Range": "bytes=0-0", "Referer": f"{WEB_BASE}/"},
            ) as resp:
                if resp.status not in (200, 206):
                    return 0
                content_range = resp.headers.get("Content-Range") or ""
                if "/" in content_range:
                    total = content_range.rsplit("/", 1)[-1].strip()
                    if total.isdigit():
                        return int(total)
                length = resp.headers.get("Content-Length") or ""
                if length.isdigit():
                    return int(length)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 0
        return 0

    def _probe_requests(self, url: str) -> int:
        try:
            import requests
        except ImportError:
            return 0
        try:
            with requests.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Referer": f"{WEB_BASE}/",
                    "Range": "bytes=0-0",
                },
                cookies=self._cookies,
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            ) as resp:
                if resp.status_code not in (200, 206):
                    return 0
                content_range = resp.headers.get("Content-Range") or ""
                if "/" in content_range:
                    total = content_range.rsplit("/", 1)[-1].strip()
                    if total.isdigit():
                        return int(total)
                length = resp.headers.get("Content-Length") or ""
                if length.isdigit():
                    return int(length)
        except Exception:
            return 0
        return 0

    async def download_to(self, url: str, dest: "Path", max_bytes: int = 0) -> int:
        """把视频/图片下载到本地文件，返回实际字节数。

        两件事必须做对，否则平台侧一定失败：

        1. **必须携带 Cookie**。抖音视频 CDN 在没有 Cookie 时一律返回 403
           （实测：无 Cookie → 403，带 Cookie → 200/206）。消息平台进程没有这些
           Cookie，所以不能把抖音直链直接交给它下载。
        2. **图床（``*.douyinpic.com``）在 TLS 指纹层面拒绝 aiohttp**。同一个 URL、
           同样的请求头，aiohttp 得到 403 而 requests 得到 200。因此这里先用
           aiohttp，失败后自动退回 requests（在线程里跑，避免阻塞事件循环）。

        ``max_bytes`` 大于 0 时会在超出该体积时中止并抛错。
        """
        from pathlib import Path as _Path

        dest = _Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            return await self._download_aiohttp(url, dest, max_bytes)
        except DouyinError as exc:
            self._dbg(f"aiohttp 下载失败（{exc}），改用 requests 重试")
            return await asyncio.to_thread(
                self._download_requests, url, dest, max_bytes
            )

    async def _download_aiohttp(self, url: str, dest, max_bytes: int) -> int:
        session = await self._get_session()
        written = 0
        try:
            async with session.get(
                url, headers={"Referer": f"{WEB_BASE}/"}
            ) as resp:
                if resp.status not in (200, 206):
                    raise DouyinError(f"HTTP {resp.status}")
                with open(dest, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise DouyinError(
                                f"文件超过限制（已下载 {written / 1024 / 1024:.1f}MB）"
                            )
                        fh.write(chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            dest.unlink(missing_ok=True)
            raise DouyinError(f"{type(exc).__name__}") from exc
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        return written

    def _download_requests(self, url: str, dest, max_bytes: int) -> int:
        """线程内用 requests 下载（同步）。"""
        try:
            import requests  # AstrBot 环境自带
        except ImportError as exc:  # pragma: no cover
            raise DouyinError("requests 不可用，无法下载图床内容") from exc

        written = 0
        try:
            with requests.get(
                url,
                headers={"User-Agent": self.user_agent, "Referer": f"{WEB_BASE}/"},
                cookies=self._cookies,
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            ) as resp:
                if resp.status_code not in (200, 206):
                    raise DouyinError(f"HTTP {resp.status_code}")
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise DouyinError(
                                f"文件超过限制（已下载 {written / 1024 / 1024:.1f}MB）"
                            )
                        fh.write(chunk)
        except DouyinError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise DouyinError(f"{type(exc).__name__}: {exc}") from exc
        return written

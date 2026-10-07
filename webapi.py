"""WebUI 页面所用的后端接口。

AstrBot 会把 ``context.register_web_api`` 注册的路由挂到
``/api/plug/<插件名>/<路径>`` 之下（见 ``dashboard/server.py`` 的
``srv_plug_route``），因此这里注册的 route 需要自带插件名前缀。

接口一览::

    GET  /status    插件运行状态
    GET  /config    读取当前配置
    POST /config    保存配置（部分字段）
    GET  /subs      当前订阅列表
    GET  /preview   解析一个账号或作品，返回「将要推送成什么样」的预览

``/preview`` 只组装消息链、不发送，所以可以放心在页面上反复点击。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, cast

try:  # AstrBot 自带 quart
    from quart import jsonify as quart_jsonify
except Exception:  # pragma: no cover - 仅在非 AstrBot 环境下触发
    quart_jsonify = None

from astrbot.api import logger

from .douyin_api import DouyinError, DouyinPost
from .pusher import PushOptions, split_chain

if TYPE_CHECKING:
    from .main import DouyinSubscribePlugin

PLUGIN_NAME = "astrbot_plugin_douyin_subscribe"

_AWEME_ID_RE = re.compile(r"(?:/video/|^)(\d{15,25})")

#: 允许代理取回头像的域名后缀。头像一律由服务端取回并转成 data URI，
#: 让浏览器完全不发外部请求——这样能绕开 iframe sandbox / CSP / 广告拦截 /
#: 网络策略等一切客户端限制（实测这些 CDN 在服务端各种请求头下都返回 200）。
_AVATAR_HOST_SUFFIXES = (
    ".douyinpic.com",
    ".douyincdn.com",
    ".byteimg.com",
    ".qlogo.cn",
    ".qq.com",
    ".qpic.cn",
)

_AVATAR_TTL = 6 * 3600
_AVATAR_MAX_BYTES = 512 * 1024
_AVATAR_PX = 64


def _shrink_image(body: bytes, mime: str) -> tuple[bytes, str]:
    """把头像缩到 ``_AVATAR_PX`` 见方并转 JPEG，返回 (字节, mime)。

    页面上头像只显示 26~44px，原图动辄 5~8KB；缩图后单个约 1~2KB，
    230 个群的接口响应从 ~1.4MB 降到 ~0.4MB。

    注意：只要原图大于目标尺寸就**一定**返回缩图，不再拿字节数做取舍。
    抖音 CDN 的图本来就用标准量化表压得很狠（接近 quality 50），把它缩到
    64px 再按 q85 重编码，体积反而可能变大；早先据此回退成原图，导致同一个
    接口里头像尺寸在 64×64 和 100×100 之间跳，前端表现不一致。
    这里改成逐档降质，取第一个比原图小的一档；都不行也仍用缩图——尺寸一致
    比省那点字节更重要。
    """
    try:
        import io

        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(body)) as img:
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            if max(img.size) <= _AVATAR_PX:
                return body, mime  # 本来就不大，没必要动
            img.thumbnail((_AVATAR_PX, _AVATAR_PX), PILImage.LANCZOS)
            best: bytes | None = None
            for quality in (85, 75, 65):
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if best is None or len(out) < len(best):
                    best = out
                if len(out) < len(body):
                    return out, "image/jpeg"
            if best:
                return best, "image/jpeg"
    except Exception:
        pass
    return body, mime


class PageApi:
    """WebUI 后端接口集合。"""

    def __init__(self, plugin: "DouyinSubscribePlugin") -> None:
        self.plugin = plugin
        self.context = plugin.context

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self) -> None:
        routes: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("/status", self.page_status, ["GET"], "插件运行状态"),
            ("/config", self.page_get_config, ["GET"], "读取插件配置"),
            ("/config", self.page_save_config, ["POST"], "保存插件配置"),
            ("/cookie", self.page_save_cookie, ["POST"], "保存并验证抖音 Cookie"),
            ("/subs", self.page_subs, ["GET"], "订阅列表"),
            ("/sessions", self.page_sessions, ["GET"], "可用会话列表"),
            ("/groups", self.page_groups, ["GET"], "拉取机器人群列表"),
            ("/search", self.page_search, ["GET"], "搜索抖音账号"),
            ("/subscribe", self.page_subscribe, ["POST"], "新增订阅"),
            ("/unsubscribe", self.page_unsubscribe, ["POST"], "取消订阅"),
            ("/check", self.page_check, ["POST"], "立即检查一次"),
            ("/test-push", self.page_test_push, ["POST"], "发送一条测试推送"),
            ("/preview", self.page_preview, ["GET"], "预览推送效果"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}", handler, methods, desc
                )
            except Exception as exc:  # pragma: no cover
                logger.error(f"[抖音订阅] 注册 WebUI 接口 {path} 失败：{exc}")

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    @staticmethod
    def _jsonify(payload: dict[str, Any]):
        if quart_jsonify is None:
            raise RuntimeError("Web 框架不可用，无法返回 JSON")
        return cast(Callable[[dict[str, Any]], Any], quart_jsonify)(payload)

    def _ok(self, data: Any = None, message: str = ""):
        # 必须使用 AstrBot 的 Response 结构：前端桥接层会检查 status == "error"，
        # 并自动解包 data（见 dashboard/plugin_page_bridge.js 与 PluginPagePage 构建产物）。
        return self._jsonify(
            {
                "status": "ok",
                "message": message,
                "data": data if data is not None else {},
            }
        )

    def _fail(self, message: str, data: Any = None):
        # 统一返回 200，把错误放进 body，避免前端 axios 抛通用错误后丢失原因
        return self._jsonify({"status": "error", "message": message, "data": data})

    # ------------------------------------------------------------------
    # 头像：服务端取回并转 data URI
    # ------------------------------------------------------------------

    def _avatar_cache(self) -> dict[str, tuple[str, float]]:
        cache = getattr(self.plugin, "_avatar_cache", None)
        if cache is None:
            cache = {}
            self.plugin._avatar_cache = cache
        return cache

    @staticmethod
    def _avatar_host_ok(url: str) -> bool:
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return False
        host = host.lower()
        return bool(host) and any(
            host == s.lstrip(".") or host.endswith(s) for s in _AVATAR_HOST_SUFFIXES
        )

    async def avatar_data_uri(self, url: str) -> str:
        """把头像 URL 取回并编码成 data URI；失败返回空串。

        结果带 TTL 缓存在插件实例上，重复渲染不会反复请求。
        """
        url = (url or "").strip()
        if not url or not url.startswith("http") or not self._avatar_host_ok(url):
            return ""

        cache = self._avatar_cache()
        hit = cache.get(url)
        now = time.time()
        if hit and hit[1] > now:
            return hit[0]

        plugin = self.plugin
        client = plugin._client
        if client is None:
            return ""
        try:
            status, body, ctype = await client.fetch_bytes(url, _AVATAR_MAX_BYTES)
        except Exception:
            return ""
        if status != 200 or not body:
            return ""

        mime = (ctype or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            # 有些 CDN 不返回 content-type，按文件头猜
            if body[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif body[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                return ""

        # 缩到 64px 再内嵌：页面上头像只显示 26~44px，原图 5~8KB 太浪费。
        # 230 个群原图内嵌约 1.4MB，缩图后降到 ~0.4MB。
        body, mime = await asyncio.to_thread(_shrink_image, body, mime)

        data_uri = f"data:{mime};base64,{base64.b64encode(body).decode()}"
        cache[url] = (data_uri, now + _AVATAR_TTL)
        if len(cache) > 2000:  # 简单防膨胀
            for key in list(cache)[:500]:
                cache.pop(key, None)
        return data_uri

    async def enrich_avatars(
        self, items: list[dict[str, Any]], *, key: str = "avatar", limit: int = 300
    ) -> None:
        """并发把列表中各项的 avatar 换成 data URI（就地修改）。

        取不到的保持原样，前端仍可用原 URL 兜底。
        """
        targets = [it for it in items[:limit] if it.get(key)]
        if not targets:
            return
        sem = asyncio.Semaphore(12)

        async def one(item: dict[str, Any]) -> None:
            async with sem:
                uri = await self.avatar_data_uri(str(item.get(key) or ""))
                if uri:
                    item[key + "_data"] = uri

        results = await asyncio.gather(
            *(one(it) for it in targets), return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                logger.debug(f"[抖音订阅] 头像并发取回异常：{r}")

    # ------------------------------------------------------------------
    # 接口实现
    # ------------------------------------------------------------------

    async def page_status(self):
        """插件运行状态。"""
        plugin = self.plugin
        counts = plugin.store.subscription_count()
        accounts = plugin.store.all_accounts()
        cookie_ok: bool | None = None
        cookie_msg = ""
        login_name = ""
        login_avatar = ""
        if plugin._cookie():
            try:
                me = await plugin._client_or_raise().fetch_self()
                cookie_ok = True
                login_name = me.display
                login_avatar = me.avatar
            except DouyinError as exc:
                cookie_ok = False
                cookie_msg = str(exc)
        else:
            cookie_ok = False
            cookie_msg = "尚未配置 Cookie"

        status_payload = {
                "running": plugin._running,
                "enabled": bool(plugin._cfg("enabled", default=True)),
                "cookie_configured": bool(plugin._cookie()),
                "cookie_ok": cookie_ok,
                "cookie_message": cookie_msg,
                "login_account": login_name,
                "login_avatar": login_avatar,
                "subscription_count": counts,
                "session_count": plugin.store.session_count(),
                "account_count": len(accounts),
                "interval": int(plugin._interval_for(len(accounts))),
                "jitter": int(plugin._cfg("polling", "jitter", default=30) or 0),
                "stats": dict(plugin._stats),
                "options": self._options_dict(PushOptions.from_config(plugin.config)),
        }
        if login_avatar:
            await self.enrich_avatars([status_payload], key="login_avatar", limit=1)
        return self._ok(status_payload)

    async def page_get_config(self):
        """读取插件配置（Cookie 只回传是否存在，不回传内容）。"""
        cfg = dict(self.plugin.config or {})
        if "cookie" in cfg:
            raw = str(cfg.get("cookie") or "")
            cfg["cookie"] = "" if not raw else f"****（已配置，{len(raw)} 字符）"
        return self._ok(cfg)

    async def page_save_cookie(self):
        """单独保存抖音 Cookie，并**立即验证一次**。

        抖音 Cookie 长达 4000+ 字符，不适合跟着表单自动保存（每次粘贴都会触发
        写盘 + 重建客户端）。这里给一个专用入口：保存后立刻打一次 ``profile/self``，
        把「存了但其实是失效的」这种最恼人的情况当场暴露出来。
        """
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        cookie = str(body.get("cookie") or "").strip()
        if not cookie:
            return self._fail("Cookie 不能为空")

        # 基本形状检查：缺了这两个关键字段，连签名都生成不出来
        lower = cookie.lower()
        if "sessionid" not in lower:
            return self._fail(
                "这看起来不像完整的抖音 Cookie：缺少 sessionid。"
                "请按 F12 → Network → 任意请求 → 复制完整 Cookie 请求头。"
            )
        if "uifid" not in lower:
            return self._fail(
                "这看起来不像完整的抖音 Cookie：缺少 UIFID。"
                "签名所需的 uifid 要从它里面取，缺了就完全没法工作。"
            )

        plugin = self.plugin
        try:
            plugin.config["cookie"] = cookie
            if hasattr(plugin.config, "save_config"):
                plugin.config.save_config()
        except Exception as exc:
            return self._fail(f"写入配置失败：{exc}")

        plugin._rebuild_client()
        try:
            me = await plugin._client_or_raise().fetch_self()
        except DouyinError as exc:
            return self._fail(f"Cookie 已保存，但验证失败：{exc}")

        return self._ok(
            {"account": me.display, "length": len(cookie), "ok": True},
            f"Cookie 有效，登录账号：{me.display}",
        )

    async def page_save_config(self):
        """保存页面可编辑的配置项。"""
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        plugin = self.plugin
        cfg = plugin.config
        changed: list[str] = []

        if isinstance(body.get("cookie"), str) and body["cookie"].strip():
            # 页面回传的是掩码时忽略，避免把真实 Cookie 覆盖掉
            value = body["cookie"].strip()
            if not value.startswith("****"):
                cfg["cookie"] = value
                changed.append("cookie")

        for section, keys in (
            (
                "content",
                (
                    "show_author",
                    "show_desc",
                    "show_time",
                    "show_link",
                    "at_all",
                    "link_style",
                ),
            ),
            ("video", ("mode", "cover_on_fallback", "prefer_no_watermark", "delivery")),
            ("polling", ("adapt_interval",)),
            ("subscribe", ("first_sync_push_latest", "admin_only")),
            ("advanced", ("debug",)),
        ):
            src = body.get(section)
            if not isinstance(src, dict):
                continue
            dst = cfg.setdefault(section, {})
            for key in keys:
                if key in src:
                    dst[key] = src[key]
                    changed.append(f"{section}.{key}")

        for section, key, caster in (
            ("video", "size_limit_mb", int),
            ("polling", "base_interval", int),
            ("polling", "jitter", int),
            ("polling", "max_concurrent", int),
            ("subscribe", "max_posts_per_check", int),
            ("subscribe", "max_subs_per_session", int),
            ("subscribe", "first_sync_max_age_hours", int),
            ("content", "desc_max_length", int),
        ):
            src = body.get(section)
            if isinstance(src, dict) and key in src:
                try:
                    cfg.setdefault(section, {})[key] = caster(src[key])
                    changed.append(f"{section}.{key}")
                except (TypeError, ValueError):
                    return self._fail(f"{section}.{key} 不是合法数值")

        try:
            if hasattr(cfg, "save_config"):
                cfg.save_config()
            else:  # pragma: no cover - 兼容普通 dict
                import json
                from pathlib import Path

                path = Path(
                    "/AstrBot/data/config/"
                    f"{PLUGIN_NAME}_config.json"
                )
                path.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8"
                )
        except Exception as exc:
            return self._fail(f"保存失败：{exc}")

        # 让改动立即生效（Cookie / 间隔等）
        try:
            plugin._rebuild_client()
        except Exception:
            pass

        return self._ok({"changed": changed}, "已保存")

    async def page_subs(self):
        """订阅列表。"""
        plugin = self.plugin
        out: list[dict[str, Any]] = []
        for umo in plugin.store.sessions:
            subs = []
            for sec in plugin.store.subs_of(umo):
                st = plugin.store.get_account(sec)
                subs.append(
                    {
                        "sec_uid": sec,
                        "nickname": st.get("nickname") or "",
                        "unique_id": st.get("unique_id") or "",
                        "avatar": st.get("avatar") or "",
                        "seen": len(st.get("seen") or []),
                        "fail_count": int(st.get("fail_count") or 0),
                        "next_check": int(st.get("next_check") or 0),
                    }
                )
            out.append(
                {"umo": umo, "avatar": self._umo_avatar(umo), "subs": subs}
            )
        flat = [s for entry in out for s in entry["subs"]]
        await self.enrich_avatars(flat, limit=100)
        # 会话自身的头像（群头像 / 私聊头像）
        await self.enrich_avatars(out, limit=100)
        return self._ok(out)

    async def page_groups(self):
        """拉取某个机器人（平台实例）的群列表，供 WebUI 选择订阅目标。

        走 OneBot 的 ``get_group_list``——这与「QQ群管」插件的做法一致：
        通过 ``context.platform_manager.platform_insts`` 找到 aiocqhttp 适配器，
        再用 ``inst.get_client()`` 拿到 OneBot 客户端调用接口。
        """
        from quart import request as quart_request

        platform_id = (quart_request.args.get("platform") or "").strip()
        if not platform_id:
            return self._fail("请先选择机器人")

        plugin = self.plugin
        inst = None
        try:
            inst = plugin.context.get_platform_inst(platform_id)
        except Exception:
            inst = None
        if inst is None:
            for candidate in plugin.context.platform_manager.platform_insts:
                try:
                    if candidate.meta().id == platform_id:
                        inst = candidate
                        break
                except Exception:
                    continue
        if inst is None:
            return self._fail(f"找不到机器人 {platform_id}，它可能没有在运行")

        try:
            client = inst.get_client()
        except Exception as exc:
            return self._fail(
                f"该机器人不支持拉取群列表（{type(exc).__name__}）。"
                "目前仅 aiocqhttp(OneBot) 适配器支持。"
            )
        if client is None:
            return self._fail("机器人当前未连接，请确认协议端已登录且已连上 AstrBot")

        try:
            result = await client.call_action("get_group_list")
        except Exception as exc:
            return self._fail(f"调用 get_group_list 失败：{exc}")

        raw = result
        if isinstance(result, dict):
            raw = result.get("data") or []
        if not isinstance(raw, list):
            return self._fail("群列表返回格式异常")

        groups: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("group_id") or "").strip()
            if not gid:
                continue
            groups.append(
                {
                    "group_id": gid,
                    "group_name": str(item.get("group_name") or gid),
                    "member_count": int(item.get("member_count") or 0),
                    "max_member_count": int(item.get("max_member_count") or 0),
                    # QQ 群头像有固定的公开地址规则，无需再调接口：
                    #   https://p.qlogo.cn/gh/<群号>/<群号>/<尺寸>
                    # 尺寸 100 适合列表展示，0 为原图。
                    "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/100",
                    # 直接把 UMO 一起算好，前端选中即可用
                    "umo": f"{platform_id}:GroupMessage:{gid}",
                }
            )
        groups.sort(key=lambda g: g["group_name"])
        # 群头像由服务端取回并转 data URI，浏览器不再直连 p.qlogo.cn
        await self.enrich_avatars(groups)
        return self._ok({"groups": groups, "platform": platform_id})

    @staticmethod
    def _umo_avatar(umo: str) -> str:
        """从 unified_msg_origin 推出对应会话的头像地址。

        UMO 形如 ``platform:GroupMessage:群号`` / ``platform:FriendMessage:QQ号``。
        QQ 的头像有固定公开地址规则，不需要额外接口：

        * 群聊 → ``https://p.qlogo.cn/gh/<群号>/<群号>/<尺寸>``
        * 私聊 → ``https://q1.qlogo.cn/g?b=qq&nk=<QQ号>&s=<尺寸>``
        """
        parts = (umo or "").split(":", 2)
        if len(parts) < 3:
            return ""
        kind, sid = parts[1].lower(), parts[2].strip()
        if not sid or not sid.isdigit():
            return ""
        if kind == "groupmessage":
            return f"https://p.qlogo.cn/gh/{sid}/{sid}/100"
        if kind == "friendmessage":
            return f"https://q1.qlogo.cn/g?b=qq&nk={sid}&s=100"
        return ""

    async def page_sessions(self):
        """可用会话列表：已订阅过的会话 + 可用的平台适配器。"""
        plugin = self.plugin
        known = []
        for umo in plugin.store.sessions:
            known.append({"umo": umo, "count": len(plugin.store.subs_of(umo))})

        platforms: list[dict[str, Any]] = []
        try:
            for inst in plugin.context.platform_manager.platform_insts:
                meta = inst.meta()
                platforms.append(
                    {
                        "id": meta.id,
                        "name": meta.name,
                        "description": getattr(meta, "description", "") or "",
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[抖音订阅] 读取平台列表失败：{exc}")

        return self._ok({"known": known, "platforms": platforms})

    async def page_search(self):
        """按关键词 / 抖音号 / 链接搜索账号。"""
        from quart import request as quart_request

        keyword = (quart_request.args.get("keyword") or "").strip()
        if not keyword:
            return self._fail("请输入要搜索的昵称、抖音号，或直接粘贴主页链接")

        try:
            limit = int(quart_request.args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 20))

        plugin = self.plugin
        try:
            client = plugin._client_or_raise()
        except DouyinError as exc:
            return self._fail(str(exc))

        items: list[dict[str, Any]] = []
        degraded = False
        note = ""

        # 链接 / sec_uid 直接解析，绕过搜索（最可靠）
        is_direct = bool(re.search(r"MS4wLjABAAAA", keyword)) or keyword.startswith(
            "http"
        )
        try:
            if is_direct:
                account = await client.resolve_account(keyword)
                items = [self._account_dict(account)]
            else:
                # 抖音号优先精确解析
                exact = await client.resolve_by_unique_id(keyword)
                if exact is not None:
                    items.append(self._account_dict(exact))
                try:
                    found = await client.search_accounts(keyword, limit=limit)
                except DouyinError:
                    found = []
                seen = {i["sec_uid"] for i in items}
                for acc in found:
                    if acc.sec_uid not in seen:
                        seen.add(acc.sec_uid)
                        items.append(self._account_dict(acc))
                if not items:
                    degraded = True
                    note = (
                        "没有找到匹配的账号。抖音站内搜索对高频请求会返回空结果，"
                        "建议直接粘贴账号主页链接，或输入完整抖音号。"
                    )
        except DouyinError as exc:
            return self._fail(f"搜索失败：{exc}")

        # 标注哪些已经订阅过
        subscribed = set()
        for umo in plugin.store.sessions:
            subscribed.update(plugin.store.subs_of(umo))
        for item in items:
            item["subscribed"] = item["sec_uid"] in subscribed
        await self.enrich_avatars(items)

        return self._ok({"items": items, "degraded": degraded, "note": note})

    async def page_subscribe(self):
        """新增订阅：{umo, sec_uid 或 keyword}。"""
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        umo = str(body.get("umo") or "").strip()
        if not umo or umo.count(":") < 2:
            return self._fail("请先选择或填写目标会话（格式 platform:GroupMessage:群号）")

        plugin = self.plugin
        try:
            client = plugin._client_or_raise()
        except DouyinError as exc:
            return self._fail(str(exc))

        sec_uid = str(body.get("sec_uid") or "").strip()
        try:
            if sec_uid:
                account = await client.fetch_profile(sec_uid)
            else:
                keyword = str(body.get("keyword") or "").strip()
                if not keyword:
                    return self._fail("缺少 sec_uid 或 keyword")
                account = await client.resolve_account(keyword)
        except DouyinError as exc:
            return self._fail(f"解析账号失败：{exc}")

        msg, ok = await plugin.subscribe_account(umo, account)
        if not ok:
            return self._fail(msg)
        return self._ok({"umo": umo, "account": self._account_dict(account)}, msg)

    async def page_unsubscribe(self):
        """取消订阅：{umo, sec_uid}。"""
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        umo = str(body.get("umo") or "").strip()
        sec_uid = str(body.get("sec_uid") or "").strip()
        if not umo or not sec_uid:
            return self._fail("缺少 umo 或 sec_uid")

        plugin = self.plugin
        state = plugin.store.get_account(sec_uid)
        if not plugin.store.remove_sub(umo, sec_uid):
            return self._fail("该会话并未订阅此账号")
        await plugin.store.asave()
        name = state.get("nickname") or sec_uid[:20] + "…"
        return self._ok({"umo": umo, "sec_uid": sec_uid}, f"已取消订阅 {name}")

    async def page_check(self):
        """立即检查一次（忽略退避与间隔）。"""
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        sec_uid = str(body.get("sec_uid") or "").strip()
        plugin = self.plugin

        try:
            if sec_uid:
                st = plugin.store.get_account(sec_uid)
                if not st.get("seen") and not st.get("nickname"):
                    return self._fail("该账号不在订阅中")
                st["next_check"] = 0
                await plugin._check_account(sec_uid)
                await plugin.store.asave()
                return self._ok({"checked": [sec_uid]}, "已检查该账号")

            accounts = plugin.store.all_accounts()
            if not accounts:
                return self._fail("当前没有任何订阅")
            for sec in accounts:
                plugin.store.get_account(sec)["next_check"] = 0
            await plugin._poll_once()
            return self._ok({"checked": accounts}, f"已检查 {len(accounts)} 个账号")
        except DouyinError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            logger.error(f"[抖音订阅] 立即检查失败：{exc}")
            return self._fail(f"检查失败：{exc}")

    async def page_test_push(self):
        """把某个账号的最新作品真实推送到指定会话。

        用于在正式依赖自动推送之前，先确认目标群/私聊确实能收到消息。
        """
        from quart import request as quart_request

        body = await quart_request.get_json(silent=True) or {}
        umo = str(body.get("umo") or "").strip()
        if not umo or umo.count(":") < 2:
            return self._fail("请先选择目标会话")

        plugin = self.plugin
        # 允许指定具体作品（aweme_id），便于分别测试视频与图文
        aweme_id = str(body.get("aweme_id") or "").strip()
        try:
            client = plugin._client_or_raise()
            if aweme_id:
                post = await client.fetch_detail(aweme_id)
                posts = [post]
            else:
                sec_uid = str(body.get("sec_uid") or "").strip()
                if not sec_uid:
                    subs = plugin.store.subs_of(umo)
                    if not subs:
                        return self._fail("该会话还没有订阅，无法取用作品")
                    sec_uid = subs[0]
                posts = await client.fetch_posts(sec_uid, limit=1)
        except DouyinError as exc:
            return self._fail(f"获取作品失败：{exc}")
        if not posts:
            return self._fail("没有取到作品")

        post = posts[0]
        builder = plugin._builder
        if builder is None:
            return self._fail("推送模块尚未就绪")

        is_group = ":GroupMessage:" in umo
        degraded_note = ""
        try:
            plan = await builder.build(post, plugin._push_options(), is_group=is_group)
            chunks = split_chain(plan.chain)
            ok = False
            for chunk in chunks:
                ok = await plugin.context.send_message(umo, chunk)
                if not ok:
                    break
                if len(chunks) > 1:
                    await asyncio.sleep(0.6)
        except Exception as exc:
            # 媒体被平台拒绝时降级为链接，与自动推送路径保持一致
            logger.warning(f"[抖音订阅] 媒体投递失败：{exc}，尝试降级为链接")
            if plan.fallback_chain is None:
                return self._fail(f"发送失败：{exc}")
            try:
                ok = await plugin.context.send_message(umo, plan.fallback_chain)
            except Exception as exc2:
                return self._fail(f"发送失败：{exc}；降级发送也失败：{exc2}")
            if not ok:
                return self._fail(f"发送失败：{exc}")
            degraded_note = f"（媒体投递失败已降级为链接：{exc}）"

        if not ok:
            return self._fail(
                f"未找到会话 {umo} 对应的平台适配器，消息未送达。"
                "请确认该平台适配器正在运行。"
            )
        plugin._stats["pushes"] += 1
        return self._ok(
            {"umo": umo, "aweme_id": post.aweme_id, "plan": plan.describe()},
            f"已向 {umo} 发送测试推送{degraded_note}",
        )

    @staticmethod
    def _account_dict(account) -> dict[str, Any]:
        return {
            "sec_uid": account.sec_uid,
            "nickname": account.nickname,
            "unique_id": account.unique_id,
            "avatar": account.avatar,
            "aweme_count": account.aweme_count,
            "follower_count": account.follower_count,
            "profile_url": account.profile_url,
        }

    async def page_preview(self):
        """解析账号或作品，返回推送预览（不发送）。"""
        from quart import request as quart_request

        target = (quart_request.args.get("target") or "").strip()
        if not target:
            return self._fail("请填写要预览的账号主页链接、抖音号或作品链接")

        mode = (quart_request.args.get("mode") or "").strip().lower()
        force_link = (quart_request.args.get("force_link") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            limit = int(quart_request.args.get("limit") or 1)
        except (TypeError, ValueError):
            limit = 1
        limit = max(1, min(limit, 10))

        plugin = self.plugin
        try:
            client = plugin._client_or_raise()
        except DouyinError as exc:
            return self._fail(str(exc))

        # ---- 取出作品 ----
        posts: list[DouyinPost] = []
        account_info: dict[str, Any] = {}
        # 作品链接（/video/<id>）或纯数字作品 ID 都按作品解析，其余按账号解析
        aweme_match = _AWEME_ID_RE.search(target)
        is_aweme = bool(
            aweme_match
            and (
                "/video/" in target
                or re.fullmatch(r"\d{15,25}", target)
            )
        )
        try:
            if is_aweme:
                posts = [await client.fetch_detail(aweme_match.group(1))]
            else:
                account = await client.resolve_account(target)
                account_info = {
                    "sec_uid": account.sec_uid,
                    "nickname": account.nickname,
                    "unique_id": account.unique_id,
                    "avatar": account.avatar,
                    "aweme_count": account.aweme_count,
                    "follower_count": account.follower_count,
                    "profile_url": account.profile_url,
                }
                posts = await client.fetch_posts(account.sec_uid, limit=limit)
        except DouyinError as exc:
            return self._fail(f"解析失败：{exc}")

        if not posts:
            return self._fail("没有取到任何作品（账号可能没有公开发布，或触发了风控）")

        # ---- 组装预览 ----
        opt = PushOptions.from_config(plugin.config)
        if mode in ("video", "file"):
            opt.video_mode = mode

        builder = plugin._builder
        if builder is None:
            return self._fail("推送模块尚未就绪，请稍后重试")

        previews: list[dict[str, Any]] = []
        for post in posts:
            try:
                plan = await builder.build(
                    post, opt, is_group=True, force_link=force_link
                )
            except Exception as exc:
                logger.error(f"[抖音订阅] 预览组装失败：{exc}")
                continue
            previews.append(
                {
                    "post": post.to_public_dict(),
                    "plan": plan.describe(),
                    "chain": self._describe_chain(plan.chain),
                }
            )

        if not previews:
            return self._fail("组装预览失败，请查看 AstrBot 日志")

        return self._ok(
            {
                "account": account_info,
                "options": self._options_dict(opt),
                "previews": previews,
                "size_limit_mb": opt.size_limit_mb,
            }
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _options_dict(opt: PushOptions) -> dict[str, Any]:
        return {
            "show_author": opt.show_author,
            "show_desc": opt.show_desc,
            "show_time": opt.show_time,
            "show_link": opt.show_link,
            "desc_max_length": opt.desc_max_length,
            "at_all": opt.at_all,
            "video_mode": opt.video_mode,
            "size_limit_mb": opt.size_limit_mb,
            "cover_on_fallback": opt.cover_on_fallback,
            "prefer_no_watermark": opt.prefer_no_watermark,
        }

    @staticmethod
    def _describe_chain(chain) -> list[dict[str, Any]]:
        """把消息链转成前端可读的结构。"""
        out: list[dict[str, Any]] = []
        for seg in chain.chain:
            name = type(seg).__name__
            item: dict[str, Any] = {"type": name}
            if name == "Plain":
                item["text"] = getattr(seg, "text", "")
            elif name == "Image":
                item["url"] = getattr(seg, "file", "")
            elif name == "Video":
                item["url"] = getattr(seg, "file", "")
            elif name == "File":
                item["name"] = getattr(seg, "name", "")
                item["url"] = getattr(seg, "url", "")
            out.append(item)
        return out

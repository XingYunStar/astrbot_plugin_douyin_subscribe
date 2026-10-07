"""抖音订阅推送插件 —— 主模块。

功能概览
--------
* ``/抖音订阅`` 订阅一个抖音账号，之后该账号发布新作品会自动推送
* ``/抖音取消`` ``/抖音列表`` ``/抖音查询`` ``/抖音状态`` 管理订阅
* 图文作品发图片，视频作品发视频（或群文件），附加信息四项可选且默认关闭
* 视频超过体积上限时自动降级为发送链接
* 轮询间隔随订阅数量自适应，并叠加随机抖动以降低风控概率
* 首次订阅默认静默同步，不推送历史作品

数据获取全程为纯 Python：``x-secsdk-web-signature`` 由标准库 hashlib 计算，
不依赖 Node.js，也不携带任何第三方 JS。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

from .douyin_api import (
    CookieInvalidError,
    DouyinAccount,
    DouyinClient,
    DouyinError,
    DouyinPost,
    RiskControlError,
)
from .pusher import PushBuilder, PushOptions, split_chain
from .store import SubscriptionStore

PLUGIN_NAME = "astrbot_plugin_douyin_subscribe"

#: 轮询主循环的最小步长（秒）。主循环每隔这么久醒来一次，检查哪些账号到点了。
TICK = 20.0

#: 首次同步时至少拉取的作品数，保证去重基线足够。
MIN_BASELINE = 20


def _is_group_umo(umo: str) -> bool:
    """判断 unified_msg_origin 是否为群聊会话。"""
    parts = (umo or "").split(":", 2)
    return len(parts) >= 2 and parts[1].lower() == "groupmessage"


class DouyinSubscribePlugin(Star):
    """抖音账号作品订阅推送。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        # Star 不会保存 config，必须自己存
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SubscriptionStore(self.data_dir)
        self.store.load()

        self._client: DouyinClient | None = None
        self._builder: PushBuilder | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._sem = asyncio.Semaphore(2)
        self._stats: dict[str, Any] = {
            "started_at": 0,
            "checks": 0,
            "pushes": 0,
            "errors": 0,
            "last_error": "",
            "last_check_at": 0,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件被激活时调用。"""
        self._rebuild_client()
        try:
            from .webapi import PageApi

            PageApi(self).register()
        except Exception as exc:
            logger.error(f"[抖音订阅] 注册 WebUI 接口失败：{exc}")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name=f"{PLUGIN_NAME}_poll")
        self._stats["started_at"] = int(time.time())
        logger.info(
            f"[抖音订阅] 已启动：{self.store.subscription_count()} 条订阅 / "
            f"{self.store.session_count()} 个会话"
        )
        if not self._cookie():
            logger.warning("[抖音订阅] 尚未配置 Cookie，轮询不会产生任何结果")

    async def terminate(self) -> None:
        """插件停用或重载时调用。必须幂等且不抛异常。"""
        self._running = False
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        try:
            await self.store.asave()
        except Exception as exc:
            logger.warning(f"[抖音订阅] 退出时保存订阅失败：{exc}")

    # ------------------------------------------------------------------
    # 配置与客户端
    # ------------------------------------------------------------------

    def _cfg(self, *path: str, default: Any = None) -> Any:
        """按路径读取配置，缺失时返回默认值。"""
        node: Any = self.config
        for key in path:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
            if node is None:
                return default
        return node

    def _cookie(self) -> str:
        return str(self._cfg("cookie", default="") or "").strip()

    def _rebuild_client(self) -> None:
        cookie = self._cookie()
        timeout = int(self._cfg("polling", "request_timeout", default=20) or 20)
        debug = bool(self._cfg("advanced", "debug", default=False))
        if self._client:
            asyncio.create_task(self._client.close())
        self._client = DouyinClient(
            cookie, timeout=timeout, debug=debug, logger=logger
        )
        self._builder = PushBuilder(
            self._client, logger=logger, tmp_dir=self.data_dir / "media_tmp"
        )

    def _client_or_raise(self) -> DouyinClient:
        if not self._cookie():
            raise CookieInvalidError(
                "尚未配置抖音 Cookie。请在插件配置中填写后重试。"
            )
        if self._client is None:
            self._rebuild_client()
        assert self._client is not None
        return self._client

    def _push_options(self) -> PushOptions:
        return PushOptions.from_config(self.config)

    def _ensure_client_cookie(self) -> None:
        """配置里的 Cookie 被改动过时，重建客户端。"""
        if self._client is None:
            self._rebuild_client()
            return
        if self._client.cookie != self._cookie():
            self._rebuild_client()

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------

    def _interval_for(self, account_count: int) -> float:
        """计算当前的自适应检查间隔（秒，不含抖动）。"""
        base = float(self._cfg("polling", "base_interval", default=300) or 300)
        base = max(base, 30.0)
        if not bool(self._cfg("polling", "adapt_interval", default=True)):
            return base
        conc = max(int(self._cfg("polling", "max_concurrent", default=2) or 2), 1)
        cap = float(self._cfg("polling", "single_round_max_wait", default=1800) or 1800)
        # 账号越多，单个账号的检查间隔越长，保证整体请求速率不失控
        interval = base * max(account_count, 1) / conc
        return max(base, min(interval, max(cap, base)))

    def _jittered(self, interval: float) -> float:
        jitter = float(self._cfg("polling", "jitter", default=30) or 0)
        if jitter <= 0:
            return interval
        return interval + random.uniform(0, jitter)

    async def _poll_loop(self) -> None:
        """主循环：不断检查到点的账号。"""
        await asyncio.sleep(15)  # 等 AstrBot 启动完成、平台适配器就绪
        while self._running:
            try:
                if bool(self._cfg("enabled", default=True)):
                    if self._builder is not None:
                        self._builder.cleanup_tmp()
                    await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                self._stats["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.error(f"[抖音订阅] 轮询循环异常：{exc}", exc_info=True)
            await asyncio.sleep(TICK)

    async def _poll_once(self) -> None:
        """检查所有到点的账号。"""
        self._ensure_client_cookie()
        due = self.store.due_accounts()
        if not due:
            return

        # 按订阅数量动态限制并发
        conc = max(int(self._cfg("polling", "max_concurrent", default=2) or 2), 1)
        self._sem = asyncio.Semaphore(conc)

        async def _guarded(sec: str) -> None:
            async with self._sem:
                await self._check_account(sec)

        results = await asyncio.gather(
            *(_guarded(sec) for sec in due), return_exceptions=True
        )
        for sec, res in zip(due, results):
            if isinstance(res, Exception):
                logger.error(f"[抖音订阅] 检查账号 {sec[:16]}… 失败：{res}")

        self._stats["last_check_at"] = int(time.time())
        self.store.save()

    async def _check_account(self, sec_uid: str) -> None:
        """检查单个账号是否有新作品。"""
        client = self._client_or_raise()
        state = self.store.get_account(sec_uid)
        count = int(
            self._cfg("subscribe", "first_sync_max_posts", default=20) or 0
        )
        fetch_limit = max(count, MIN_BASELINE)

        try:
            posts = await client.fetch_posts(sec_uid, limit=fetch_limit)
        except (CookieInvalidError, RiskControlError) as exc:
            self._on_account_failure(sec_uid, str(exc))
            return
        except DouyinError as exc:
            self._on_account_failure(sec_uid, str(exc))
            return

        self._stats["checks"] += 1

        # 空结果判定：已建立基线却突然拿不到内容，视为软拦截而非"没有新作品"
        if not posts:
            if state.get("seen"):
                self._on_account_failure(
                    sec_uid, "接口返回空列表（疑似风控软拦截，已退避）"
                )
            else:
                self.store.record_success(sec_uid, time.time() + self._next_delay(1))
            return

        if not self.store.is_initialized(sec_uid):
            # 首次同步：静默建立去重基线
            self.store.mark_seen(sec_uid, [p.aweme_id for p in posts])
            self.store.set_initialized(sec_uid, True)
            self.store.record_success(sec_uid, time.time() + self._next_delay(1))
            logger.info(
                f"[抖音订阅] 账号 {sec_uid[:16]}… 首次同步完成，记录 {len(posts)} 条基线"
            )
            return

        new_posts = [p for p in posts if not self.store.is_seen(sec_uid, p.aweme_id)]
        if new_posts:
            # 接口按时间倒序返回，推送时改为从旧到新更符合阅读直觉
            new_posts.sort(key=lambda p: p.create_time)
            max_push = max(
                int(self._cfg("subscribe", "max_posts_per_check", default=3) or 1), 1
            )
            to_push = new_posts[-max_push:] if len(new_posts) > max_push else new_posts
            if len(new_posts) > max_push:
                logger.info(
                    f"[抖音订阅] 账号 {sec_uid[:16]}… 发现 {len(new_posts)} 条新作品，"
                    f"按配置只推送最新 {max_push} 条"
                )
            for post in to_push:
                await self._push_post(sec_uid, post)
            # 全部标记为已见，避免下次重复
            self.store.mark_seen(sec_uid, [p.aweme_id for p in new_posts])

        self.store.record_success(
            sec_uid, time.time() + self._next_delay(len(self.store.all_accounts()))
        )

    def _next_delay(self, account_count: int) -> float:
        return self._jittered(self._interval_for(account_count))

    def _on_account_failure(self, sec_uid: str, message: str) -> None:
        base = float(self._cfg("polling", "base_interval", default=300) or 300)
        backoff_max = float(self._cfg("polling", "error_backoff", default=1800) or 1800)
        n = self.store.record_failure(sec_uid, int(backoff_max), int(max(base, 30)))
        self._stats["errors"] += 1
        self._stats["last_error"] = message
        logger.warning(
            f"[抖音订阅] 账号 {sec_uid[:16]}… 第 {n} 次失败：{message}"
            f"（已退避至 {self.store.get_account(sec_uid).get('next_check')}）"
        )

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    async def _push_post(
        self, sec_uid: str, post: DouyinPost, only_umo: str | None = None
    ) -> None:
        """把一条作品推送给该账号的订阅者。

        ``only_umo`` 非空时只推给这一个会话——订阅时的"补推历史作品"用它，
        避免因为一个人新订阅就把历史作品广播给所有老订阅者。
        """
        assert self._builder is not None
        if only_umo:
            subscribers = [only_umo]
        else:
            subscribers = self.store.subscribers_of(sec_uid)
        if not subscribers:
            return
        opt = self._push_options()

        logger.info(
            f"[抖音订阅] {post.author.nickname or sec_uid[:12]} 新{post.kind}"
            f"（{post.aweme_id}），推送给 {len(subscribers)} 个会话"
        )

        for umo in subscribers:
            is_group = _is_group_umo(umo)
            try:
                plan = await self._builder.build(post, opt, is_group=is_group)
            except Exception as exc:
                logger.error(f"[抖音订阅] 组装消息失败：{exc}")
                continue

            delivered, err = await self._send_plan(umo, plan)

            # 直链投递失败时（常见于作品被设为「仅自己可见」，或平台下载受限），
            # 再用「先下载再投递」重试一次，这一步能救回绝大多数失败。
            if not delivered and plan.media_kind in ("video", "file") and not plan.local_paths:
                logger.info(f"[抖音订阅] 直链投递失败（{err}），改为下载后重试：{umo}")
                try:
                    plan2 = await self._builder.build(
                        post, opt, is_group=is_group, force_download=True
                    )
                except Exception as exc:
                    plan2 = None
                    logger.warning(f"[抖音订阅] 重新组装失败：{exc}")
                if plan2 is not None and plan2.media_kind in ("video", "file"):
                    delivered, err = await self._send_plan(umo, plan2)
                    plan = plan2
                else:
                    logger.warning(f"[抖音订阅] 无法改为下载投递：{umo}")

            # 仍然失败 → 退化为「文本 + 链接 + 封面」
            if not delivered:
                logger.warning(f"[抖音订阅] 媒体投递最终失败（{err}），降级为链接：{umo}")
                if plan.fallback_chain is not None:
                    try:
                        delivered = await self.context.send_message(
                            umo, plan.fallback_chain
                        )
                    except Exception as exc2:
                        logger.error(f"[抖音订阅] 降级发送到 {umo} 也失败：{exc2}")

            if delivered:
                self._stats["pushes"] += 1

    async def _send_plan(self, umo: str, plan) -> tuple[bool, str]:
        """发送一个消息链，返回 (是否成功, 错误描述)。

        视频/群文件段必须单独成条，否则 aiocqhttp 会返回 retcode 1400。
        """
        chunks = split_chain(plan.chain)
        try:
            for idx, chunk in enumerate(chunks):
                ok = await self.context.send_message(umo, chunk)
                if not ok:
                    return False, "未找到会话对应的平台适配器"
                if len(chunks) > 1 and idx < len(chunks) - 1:
                    await asyncio.sleep(0.6)  # 分条发送时留出间隔
        except Exception as exc:
            return False, str(exc)
        return True, ""

    # ------------------------------------------------------------------
    # 订阅辅助
    # ------------------------------------------------------------------

    async def _subscribe(
        self, event: AstrMessageEvent, keyword: str
    ) -> tuple[str, bool]:
        """解析并写入订阅。返回 (可读结果, 是否成功)。"""
        client = self._client_or_raise()
        umo = event.unified_msg_origin

        limit = int(self._cfg("subscribe", "max_subs_per_session", default=20) or 0)
        if limit > 0 and len(self.store.subs_of(umo)) >= limit:
            return f"本会话订阅数已达上限（{limit}），请先取消一些订阅。", False

        try:
            account = await client.resolve_account(keyword)
        except CookieInvalidError as exc:
            return str(exc), False
        except DouyinError as exc:
            return str(exc), False

        return await self.subscribe_account(umo, account)

    async def subscribe_account(
        self, umo: str, account: "DouyinAccount"
    ) -> tuple[str, bool]:
        """把一个已解析好的账号订阅到指定会话。

        WebUI 与聊天命令共用这一条路径，保证行为一致（含静默首次同步）。
        """
        client = self._client_or_raise()

        limit = int(self._cfg("subscribe", "max_subs_per_session", default=20) or 0)
        if limit > 0 and len(self.store.subs_of(umo)) >= limit:
            return f"该会话订阅数已达上限（{limit}）。", False

        if not account.sec_uid:
            return "解析账号失败，请改用账号主页链接重试。", False

        added = self.store.add_sub(umo, account.sec_uid)
        self.store.update_profile(
            account.sec_uid,
            nickname=account.nickname,
            unique_id=account.unique_id,
            avatar=account.avatar,
        )

        # 首次同步：建立去重基线
        silent = bool(self._cfg("subscribe", "silent_first_sync", default=True))
        baseline_note = ""
        if not self.store.is_initialized(account.sec_uid):
            try:
                count = int(
                    self._cfg("subscribe", "first_sync_max_posts", default=20) or 0
                )
                posts = await client.fetch_posts(
                    account.sec_uid, limit=max(count, MIN_BASELINE)
                )
                # 无论是否静默，都要记录基线，否则下次轮询会把历史作品当成新作品
                self.store.mark_seen(account.sec_uid, [p.aweme_id for p in posts])
                self.store.set_initialized(account.sec_uid, True)
                baseline_note = f"\n已记录 {len(posts)} 条历史作品作为基线"

                if not silent and posts:
                    # 关闭静默同步时，把最新几条推给**本次订阅的会话**（不打扰其它订阅者）
                    recent = sorted(posts, key=lambda p: p.create_time)[
                        -max(int(self._cfg("subscribe", "max_posts_per_check", default=3) or 1), 1) :
                    ]
                    for post in recent:
                        try:
                            await self._push_post(
                                account.sec_uid, post, only_umo=umo
                            )
                        except Exception as exc:
                            logger.error(f"[抖音订阅] 订阅时推送失败：{exc}")
                    baseline_note += f"，并已推送最新 {len(recent)} 条"
                elif silent:
                    baseline_note += "（静默同步，不会推送）"
            except DouyinError as exc:
                baseline_note = f"\n（建立基线失败：{exc}，将在下次轮询时重试）"

        # 立刻安排一次检查
        self.store.record_success(
            account.sec_uid,
            time.time() + self._next_delay(len(self.store.all_accounts())),
        )
        await self.store.asave()

        head = "✅ 订阅成功" if added else "ℹ️ 该账号已在订阅列表中"
        return (
            f"{head}\n"
            f"账号：{account.display}\n"
            f"主页：{account.profile_url}\n"
            f"作品数：{account.aweme_count}　粉丝：{account.follower_count}"
            f"{baseline_note}",
            True,
        )

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @filter.command("抖音订阅", alias={"dy订阅", "订阅抖音"})
    async def cmd_subscribe(self, event: AstrMessageEvent, target: str = ""):
        """订阅一个抖音账号。用法：/抖音订阅 <主页链接 或 抖音号>"""
        if bool(self._cfg("subscribe", "admin_only", default=False)) and not event.is_admin():
            yield event.plain_result("仅管理员可以增删订阅。")
            return
        if not target.strip():
            yield event.plain_result(
                "请提供要订阅的账号：\n"
                "  /抖音订阅 https://www.douyin.com/user/MS4wLjABAAAA...\n"
                "  /抖音订阅 <抖音号>\n"
                "推荐直接粘贴账号主页链接，成功率最高。"
            )
            return
        msg, _ok = await self._subscribe(event, target.strip())
        yield event.plain_result(msg)

    @filter.command("抖音取消", alias={"dy取消", "取消抖音"})
    async def cmd_unsubscribe(self, event: AstrMessageEvent, target: str = ""):
        """取消订阅。用法：/抖音取消 <关键词> 或 /抖音取消 all"""
        if bool(self._cfg("subscribe", "admin_only", default=False)) and not event.is_admin():
            yield event.plain_result("仅管理员可以增删订阅。")
            return

        umo = event.unified_msg_origin
        target = target.strip()
        if not target:
            yield event.plain_result("请提供要取消的账号关键词，或输入 all 清空全部订阅。")
            return

        if target.lower() == "all":
            n = self.store.clear_subs(umo)
            await self.store.asave()
            yield event.plain_result(f"已清空本会话的 {n} 条订阅。")
            return

        sec = self.store.find_account_by_keyword(umo, target)
        if not sec:
            yield event.plain_result(f"本会话没有匹配「{target}」的订阅。")
            return
        state = self.store.get_account(sec)
        self.store.remove_sub(umo, sec)
        await self.store.asave()
        yield event.plain_result(
            f"已取消订阅：{state.get('nickname') or sec[:20] + '…'}"
        )

    @filter.command("抖音列表", alias={"dy列表", "订阅列表"})
    async def cmd_list(self, event: AstrMessageEvent):
        """查看本会话的订阅列表。"""
        umo = event.unified_msg_origin
        subs = self.store.subs_of(umo)
        if not subs:
            yield event.plain_result("本会话还没有任何订阅。使用 /抖音订阅 添加。")
            return
        lines = [f"📋 本会话共订阅 {len(subs)} 个账号："]
        now = time.time()
        for i, sec in enumerate(subs, 1):
            st = self.store.get_account(sec)
            name = st.get("nickname") or "（未知账号）"
            uid = st.get("unique_id") or ""
            nxt = int(st.get("next_check") or 0)
            wait = max(int(nxt - now), 0)
            fails = int(st.get("fail_count") or 0)
            tail = f"　下次检查 {wait}s 后"
            if fails:
                tail += f"　⚠️连续失败 {fails} 次"
            lines.append(f"{i}. {name}{f'（{uid}）' if uid else ''}{tail}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音查询", alias={"dy查询"})
    async def cmd_query(self, event: AstrMessageEvent, target: str = ""):
        """立即检查某个已订阅账号的最新作品。用法：/抖音查询 <关键词>"""
        umo = event.unified_msg_origin
        target = target.strip()
        sec = self.store.find_account_by_keyword(umo, target) if target else None
        if not sec and len(self.store.subs_of(umo)) == 1:
            sec = self.store.subs_of(umo)[0]
        if not sec:
            yield event.plain_result(
                "请指定要查询的账号关键词；若本会话只订阅了一个账号，可省略参数。"
            )
            return

        try:
            client = self._client_or_raise()
            posts = await client.fetch_posts(sec, limit=3)
        except (CookieInvalidError, DouyinError) as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        if not posts:
            yield event.plain_result("没有取到作品，可能该账号暂无作品或触发了风控。")
            return

        st = self.store.get_account(sec)
        lines = [f"🔍 {st.get('nickname') or sec[:20]} 的最新作品："]
        for p in posts:
            when = time.strftime("%m-%d %H:%M", time.localtime(p.create_time))
            lines.append(f"• [{p.kind}] {when}　{p.desc[:30] or '(无文案)'}")
            lines.append(f"  {p.share_url}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音状态", alias={"dy状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件运行状态与 Cookie 有效性。"""
        lines = ["⚙️ 抖音订阅插件状态"]
        counts = self.store.subscription_count()
        lines.append(f"订阅总数：{counts}　会话数：{self.store.session_count()}")
        interval = self._interval_for(len(self.store.all_accounts()))
        lines.append(
            f"当前检查间隔：约 {int(interval)}s（+ 抖动 "
            f"{int(self._cfg('polling', 'jitter', default=30) or 0)}s）"
        )
        lines.append(
            f"累计检查 {self._stats['checks']} 次　推送 {self._stats['pushes']} 条　"
            f"错误 {self._stats['errors']} 次"
        )
        if self._stats["last_error"]:
            lines.append(f"最近错误：{self._stats['last_error'][:80]}")

        if not self._cookie():
            lines.append("\n❌ 未配置 Cookie，插件无法工作。")
        else:
            try:
                me = await self._client_or_raise().fetch_self()
                lines.append(f"\n✅ Cookie 有效　登录账号：{me.nickname}（{me.unique_id}）")
            except DouyinError as exc:
                lines.append(f"\n❌ Cookie 异常：{exc}")

        yield event.plain_result("\n".join(lines))

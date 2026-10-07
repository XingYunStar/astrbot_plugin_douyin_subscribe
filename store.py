"""订阅关系与去重状态的持久化。

存储结构（``<插件数据目录>/subscriptions.json``）::

    {
      "version": 1,
      "accounts": {
        "<sec_uid>": {
          "nickname": "...", "unique_id": "...", "avatar": "...",
          "seen": ["aweme_id", ...],     # 已推送/已记录的作品，用于去重
          "initialized": true,           # 是否已建立首次同步基线
          "last_check": 1699999999,      # 上次检查时间
          "fail_count": 0,               # 连续失败次数（指数退避用）
          "next_check": 1700000000       # 下次允许检查的时间
        }
      },
      "sessions": {
        "<unified_msg_origin>": {
          "subs": ["<sec_uid>", ...]
        }
      }
    }

账号状态是**全局**的：多个会话订阅同一个账号时只轮询一次，推送时广播给所有订阅者。
这样订阅数增长不会线性放大请求量，是控风控的关键设计。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

#: 每个账号最多保留多少条已见作品 ID，用于去重。
MAX_SEEN = 300

SCHEMA_VERSION = 1


class SubscriptionStore:
    """订阅与去重状态的读写，线程/协程安全（内部使用 asyncio.Lock）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "subscriptions.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "accounts": {},
            "sessions": {},
            "timing_sig": None,
        }
        self._loaded = False

    # -- 载入 / 保存 --------------------------------------------------------

    def load(self) -> None:
        """从磁盘载入。文件损坏时备份并重建，避免插件因脏数据无法启动。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._loaded = True
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("根节点不是对象")
        except Exception:
            backup = self.path.with_suffix(f".bad.{int(time.time())}.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self._data = {
                "version": SCHEMA_VERSION,
                "accounts": {},
                "sessions": {},
                "timing_sig": None,
            }
            self._loaded = True
            return

        self._data = {
            "version": raw.get("version", SCHEMA_VERSION),
            "accounts": raw.get("accounts") or {},
            "sessions": raw.get("sessions") or {},
            "timing_sig": raw.get("timing_sig"),
        }
        # 修正历史数据里可能缺失的字段
        for state in self._data["accounts"].values():
            state.setdefault("seen", [])
            state.setdefault("initialized", False)
            state.setdefault("fail_count", 0)
            state.setdefault("next_check", 0)
            state.setdefault("last_check", 0)

        # 会话记录补字段；并把 1.0.6 及以前记在**账号**上的 latest_synced 继承到
        # 各会话——否则升级后每个群都会被当成「还没收到过最近作品」，一次性收到
        # 一堆补推。顺手清掉已退订账号残留的标记。
        for entry in self._data["sessions"].values():
            subs = entry.setdefault("subs", [])
            done: list[str] = entry.setdefault("latest_synced", [])
            for sec in subs:
                acct = self._data["accounts"].get(sec) or {}
                if acct.get("latest_synced") and sec not in done:
                    done.append(sec)
            entry["latest_synced"] = [s for s in done if s in subs]
        self._loaded = True

    def save(self) -> None:
        """原子写入：先写临时文件再 rename，避免断电/崩溃产生半截 JSON。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    async def asave(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.save)

    # -- 账号状态 -----------------------------------------------------------

    def _account(self, sec_uid: str) -> dict[str, Any]:
        accounts = self._data["accounts"]
        if sec_uid not in accounts:
            accounts[sec_uid] = {
                "nickname": "",
                "unique_id": "",
                "avatar": "",
                "seen": [],
                "initialized": False,
                "last_check": 0,
                "fail_count": 0,
                "next_check": 0,
            }
        return accounts[sec_uid]

    def get_account(self, sec_uid: str) -> dict[str, Any]:
        return self._account(sec_uid)

    def update_profile(
        self,
        sec_uid: str,
        *,
        nickname: str = "",
        unique_id: str = "",
        avatar: str = "",
    ) -> None:
        state = self._account(sec_uid)
        if nickname:
            state["nickname"] = nickname
        if unique_id:
            state["unique_id"] = unique_id
        if avatar:
            state["avatar"] = avatar

    def is_seen(self, sec_uid: str, aweme_id: str) -> bool:
        return aweme_id in self._account(sec_uid).get("seen", [])

    def mark_seen(self, sec_uid: str, aweme_ids: Iterable[str]) -> None:
        """记录已见作品，列表尾部为最新，超出上限时丢弃最旧的。"""
        state = self._account(sec_uid)
        seen: list[str] = state.setdefault("seen", [])
        known = set(seen)
        for aid in aweme_ids:
            if aid and aid not in known:
                seen.append(aid)
                known.add(aid)
        if len(seen) > MAX_SEEN:
            del seen[: len(seen) - MAX_SEEN]

    def set_initialized(self, sec_uid: str, value: bool = True) -> None:
        self._account(sec_uid)["initialized"] = value

    def is_initialized(self, sec_uid: str) -> bool:
        return bool(self._account(sec_uid).get("initialized"))

    # -- 订阅回执：按「会话 × 账号」记 ---------------------------------------

    def set_latest_synced(self, umo: str, sec_uid: str, value: bool = True) -> None:
        """标记某个**会话**是否已经收到过该账号的「最近作品」。

        这里刻意按「会话 × 账号」而不是按账号来记。按账号记会漏掉这种情况：
        同一个账号先后订阅到两个群，账号在第一次订阅时就已 initialized，
        于是第二次订阅被整块跳过，**第二个群什么也收不到**。

        与 ``initialized`` 的分工：``initialized`` 管的是账号级的去重基线
        （拉过历史作品没有），本标记管的是「这个群要不要收一条订阅回执」。
        """
        entry = self._data["sessions"].get(umo)
        if not entry or sec_uid not in (entry.get("subs") or []):
            return  # 不是有效订阅关系，不凭空造会话记录
        lst: list[str] = entry.setdefault("latest_synced", [])
        if value:
            if sec_uid not in lst:
                lst.append(sec_uid)
        elif sec_uid in lst:
            lst.remove(sec_uid)

    def is_latest_synced(self, umo: str, sec_uid: str) -> bool:
        entry = self._data["sessions"].get(umo) or {}
        return sec_uid in (entry.get("latest_synced") or [])

    def pending_latest_for(self, sec_uid: str) -> list[str]:
        """订阅了该账号、但还没收到过「最近作品」的会话列表。"""
        return [
            umo
            for umo, entry in self._data["sessions"].items()
            if sec_uid in (entry.get("subs") or [])
            and sec_uid not in (entry.get("latest_synced") or [])
        ]

    # -- 轮询调度辅助 -------------------------------------------------------

    def touch_check(self, sec_uid: str) -> None:
        self._account(sec_uid)["last_check"] = int(time.time())

    def record_failure(self, sec_uid: str, backoff_max: int, base: int) -> int:
        """累加失败次数并计算下次检查时间，返回失败次数。"""
        state = self._account(sec_uid)
        n = int(state.get("fail_count", 0)) + 1
        state["fail_count"] = n
        delay = min(base * (2 ** (n - 1)), max(backoff_max, base))
        state["next_check"] = int(time.time()) + delay
        return n

    def record_success(self, sec_uid: str, next_check: float) -> None:
        state = self._account(sec_uid)
        state["fail_count"] = 0
        state["last_check"] = int(time.time())
        state["next_check"] = int(next_check)

    def due_accounts(self, now: float | None = None) -> list[str]:
        """返回所有到点该检查的账号 sec_uid。"""
        now = time.time() if now is None else now
        return [
            sec
            for sec, state in self._data["accounts"].items()
            if float(state.get("next_check", 0)) <= now
        ]

    # -- 重新计时 -----------------------------------------------------------

    def timing_signature(self) -> str | None:
        """上一次生效过的「轮询 / 订阅」配置指纹，老存档可能没有。"""
        return self._data.get("timing_sig")

    def set_timing_signature(self, sig: str) -> None:
        self._data["timing_sig"] = sig

    def retime_accounts(
        self, interval: float, *, now: float | None = None, skip_failing: bool = True
    ) -> tuple[int, int]:
        """按新的间隔给所有账号重新排期。

        两个刻意的设计：

        * **错峰**：把 n 个账号均匀铺在一个 interval 内，而不是统统设成
          ``now + interval``——后者会让所有账号在同一时刻到点、集中发请求，
          抖音风控对突发很敏感，容易吃 403。
        * **不提前退避中的账号**：``fail_count > 0`` 的账号保持原有时间，
          否则刚改完配置就把正在退避（多半是被风控）的账号拉起来重试，
          只会加重风控。

        返回 ``(已重新排期的账号数, 因退避而保持原样的账号数)``。
        """
        now = time.time() if now is None else now
        interval = max(float(interval), 1.0)
        secs = list(self._data["accounts"])
        total = max(len(secs), 1)
        retimed = kept = 0
        for i, sec in enumerate(secs):
            state = self._account(sec)
            if skip_failing and int(state.get("fail_count", 0) or 0) > 0:
                kept += 1
                continue
            state["next_check"] = int(now + interval * (i + 1) / total)
            retimed += 1
        return retimed, kept

    # -- 订阅关系 -----------------------------------------------------------

    @property
    def sessions(self) -> dict[str, Any]:
        return self._data["sessions"]

    def add_sub(self, umo: str, sec_uid: str) -> bool:
        """新增订阅。已存在返回 False。"""
        entry = self._data["sessions"].setdefault(umo, {"subs": []})
        subs: list[str] = entry.setdefault("subs", [])
        if sec_uid in subs:
            return False
        subs.append(sec_uid)
        self._account(sec_uid)  # 确保账号状态存在
        return True

    def remove_sub(self, umo: str, sec_uid: str) -> bool:
        entry = self._data["sessions"].get(umo)
        if not entry:
            return False
        subs: list[str] = entry.get("subs", [])
        if sec_uid not in subs:
            return False
        subs.remove(sec_uid)
        done = entry.get("latest_synced")
        if isinstance(done, list) and sec_uid in done:
            done.remove(sec_uid)
        if not subs:
            self._data["sessions"].pop(umo, None)
        self._gc_account(sec_uid)
        return True

    def clear_subs(self, umo: str) -> int:
        entry = self._data["sessions"].pop(umo, None)
        if not entry:
            return 0
        subs = list(entry.get("subs", []))
        for sec in subs:
            self._gc_account(sec)
        return len(subs)

    def _gc_account(self, sec_uid: str) -> None:
        """没有任何会话再订阅该账号时，清掉它的轮询状态。"""
        if any(sec_uid in (e.get("subs") or []) for e in self._data["sessions"].values()):
            return
        self._data["accounts"].pop(sec_uid, None)

    def subs_of(self, umo: str) -> list[str]:
        entry = self._data["sessions"].get(umo) or {}
        return list(entry.get("subs") or [])

    def subscribers_of(self, sec_uid: str) -> list[str]:
        """返回订阅了该账号的所有会话。"""
        return [
            umo
            for umo, entry in self._data["sessions"].items()
            if sec_uid in (entry.get("subs") or [])
        ]

    def all_accounts(self) -> list[str]:
        return list(self._data["accounts"].keys())

    def subscription_count(self) -> int:
        return sum(len(e.get("subs") or []) for e in self._data["sessions"].values())

    def session_count(self) -> int:
        return len(self._data["sessions"])

    def find_account_by_keyword(self, umo: str, keyword: str) -> str | None:
        """在某个会话的订阅里按 sec_uid / 昵称 / 抖音号模糊查找。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return None
        subs = self.subs_of(umo)
        if keyword in subs:
            return keyword
        lowered = keyword.lower()
        for sec in subs:
            state = self._account(sec)
            if lowered in str(state.get("nickname", "")).lower():
                return sec
            if lowered == str(state.get("unique_id", "")).lower():
                return sec
        return None

    def dump(self) -> dict[str, Any]:
        """导出只读快照，供 WebUI 使用。"""
        return json.loads(json.dumps(self._data, ensure_ascii=False))

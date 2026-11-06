# -*- coding: utf-8 -*-
"""推送闸门 —— 全项目唯一决定「这条内容能不能发」的地方。

背景（2026-11-06 重构）：
    旧实现把「是否推送」的判定散落在 monitor 的首次运行两分支、常规轮询、
    backfill_feed 四处，且 /api/feeds/backfill、console add/backfill 三个入口
    都会先 clear_feed() 清空去重记录再重发近 24 小时。叠加云端 sandbox 的
    seen.db 被部署包里的旧快照覆盖、云库 cloud.enabled 未开启，结果每次
    发布/重启都会把过往推文重新补发一遍，造成机器人刷屏。

设计原则：
    * **状态不可信时宁可漏一轮，绝不补历史**。
    * 不依赖任何外部持久化也能防刷屏（基线 + 启动水位线都在进程内）。
    * 回测是「补漏」，不是「清空重发」，且带冷却与上限。

四道闸门（select 内按序执行）：
    1) 基线：某源去重记录为空 → 首轮把时间线全部标记为已处理，不推送。
    2) 启动水位线：内容时间早于 boot_at - boot_grace_seconds 一律不补发，
       无论去重是否命中（挡住 seen.db 被回滚成旧快照的情况）。
    3) 时间窗：常规运行只推 max_push_age_hours 内的新内容（防发送失败后无限重试）。
    4) 频控：单源单轮上限 + 全局每分钟上限，超出的留到下一轮而不是丢弃。

本模块只用标准库，可独立单测；不 import monitor/webui，避免循环依赖。
"""
from collections import deque
from dataclasses import dataclass, field
import logging
import threading
import time

DEFAULT_CFG = {
    "enabled": True,
    "boot_grace_seconds": 600,       # 启动前 10 分钟以外的内容一律不补发
    "max_push_age_hours": 2,        # 常规运行时的时间窗
    "max_push_per_cycle": 5,        # 单源单轮上限
    "max_push_per_minute": 10,      # 全局每分钟上限（跨所有源）
    "replay_cooldown_seconds": 300,  # 同一源两次强制回测的最小间隔
    "replay_max": 5,                # 单次回测最多重发条数
}


@dataclass
class Decision:
    """一次 select 的结果。调用方负责：to_mark 落库、to_push 发送、to_defer 留到下轮。"""
    to_push: list = field(default_factory=list)
    to_mark: list = field(default_factory=list)
    to_defer: list = field(default_factory=list)
    reason: str = ""
    skipped_old: int = 0
    skipped_known: int = 0

    @property
    def counts(self) -> dict:
        return {
            "push": len(self.to_push), "mark": len(self.to_mark),
            "defer": len(self.to_defer), "skipped_old": self.skipped_old,
            "skipped_known": self.skipped_known, "reason": self.reason,
        }


class PushGuard:
    """线程安全的推送决策器。

    :param store: 去重存储，或返回存储的 callable。
                  GHA 兜底会在 Monitor 构造后把 monitor.store 换成 CloudStore，
                  所以这里必须支持动态取，否则闸门会一直读构造时的空本地库。
    :param config_fn: callable -> 完整配置 dict（每次读最新，支持热重载）
    :param now_fn: 可注入的时钟，便于单测
    """

    def __init__(self, store, config_fn, now_fn=None):
        self._store_fn = store if callable(store) else (lambda: store)
        self._config_fn = config_fn
        self._now = now_fn or time.time
        self.boot_at = self._now()
        self._lock = threading.RLock()
        self._recent_selects = deque()   # 最近 60s 内放行的推送时刻（全局频控）
        self._replay_at = {}             # feed_key -> 上次强制回测时刻
        self._primed = set()             # 已建立基线的源
        self.stats = {"selects": 0, "primed": 0, "blocked_boot": 0,
                      "blocked_age": 0, "blocked_cap": 0,
                      "blocked_rate": 0, "allowed": 0, "replays": 0}

    @property
    def store(self):
        return self._store_fn()

    # ---------- 配置 ----------
    def cfg(self) -> dict:
        raw, full = {}, {}
        try:
            full = self._config_fn() or {}
            raw = dict(full.get("push_guard") or {})
        except Exception:
            pass
        # 兼容未升级的旧配置（如 GHA 精简副本）：顶层参数仍作为缺省值
        for key in ("max_push_age_hours", "max_push_per_cycle"):
            if key not in raw and key in full:
                raw[key] = full[key]
        return {**DEFAULT_CFG, **raw}

    def _enabled(self) -> bool:
        return bool(self.cfg().get("enabled", True))

    # ---------- 主决策 ----------
    def select(self, feed_key: str, tweets: list, *, replay: bool = False,
               hours: int = 24, force: bool = False) -> Decision:
        """决定一批抓取结果里哪些可以推送。

        :param replay: True = 回测模式（补推时间窗内漏掉的）；默认 False = 常规轮询
        :param hours:  回测时间窗（小时）
        :param force:  回测时是否允许重发「已推过」的内容（受冷却/条数限制）
        """
        d = Decision()
        if not tweets:
            d.reason = "empty"
            return d
        if not self._enabled():
            # 闸门关闭时退化为旧的宽松行为，但依然走去重，避免完全失控
            d.reason = "guard_disabled"
            known = self._known(feed_key)
            for t in tweets:
                (d.to_mark if t.get("id") in known else d.to_push).append(t)
            return d
        with self._lock:
            self.stats["selects"] += 1
            if replay:
                return self._replay(feed_key, tweets, hours, force, d)
            return self._normal(feed_key, tweets, d)

    def _known(self, feed_key: str) -> set:
        try:
            return set(self.store.known_ids(feed_key))
        except Exception as e:
            logging.warning("[push_guard] 读取去重记录失败(%s)，按空集合处理: %s",
                            feed_key, e)
            return set()

    def _normal(self, feed_key: str, tweets: list, d: Decision) -> Decision:
        c = self.cfg()
        now = self._now()
        known = self._known(feed_key)

        # 闸门 1：去重记录为空 = 状态不可信（新接入 / seen.db 被回滚 / 云库未启用）
        # 首轮只建立基线，绝不把时间线里的历史内容补发出去。
        if not known and feed_key not in self._primed:
            self._primed.add(feed_key)
            self.stats["primed"] += 1
            for t in tweets:
                d.to_mark.append(t)
            d.reason = "primed: 去重记录为空，首轮只建基线不推送"
            logging.info("[push_guard] [%s] 首轮基线：标记 %d 条历史，不推送"
                         "（防止部署/重启补发旧内容）", feed_key, len(tweets))
            return d

        # 基线已建但去重库仍为空 = 上一轮落库异常。此时收紧扣到 boot_at，
        # 只放行启动之后新产生的内容，绝不重推启动瞬间时间线上的任何一条。
        boot_floor = (self.boot_at if not known
                      else self.boot_at - float(c["boot_grace_seconds"]))
        age_floor = now - float(c["max_push_age_hours"]) * 3600
        cap = int(c["max_push_per_cycle"])
        rate = int(c["max_push_per_minute"])
        # 两个下界取更严格的那个：常规运行用 max_push_age，重启初期用 boot 水位线
        floor = max(boot_floor, age_floor)

        # 从旧到新处理，保证同时出现多条时按顺序发
        ordered = sorted(tweets, key=lambda t: (t.get("ts") or 0))
        for t in ordered:
            tid = t.get("id")
            if tid in known:
                d.skipped_known += 1
                continue
            ts = t.get("ts") or 0
            if not ts:
                d.to_mark.append(t)      # 无时间戳无法判断新旧，保守只标记
                d.skipped_old += 1
                continue
            if ts < boot_floor:
                d.to_mark.append(t)      # 启动前就存在的内容 → 永不补发
                d.skipped_old += 1
                self.stats["blocked_boot"] += 1
                continue
            if ts < age_floor:
                d.to_mark.append(t)
                d.skipped_old += 1
                self.stats["blocked_age"] += 1
                continue
            d.to_push.append(t)

        # 闸门 4a：单源单轮上限
        if len(d.to_push) > cap:
            d.to_defer.extend(d.to_push[cap:])
            d.to_push = d.to_push[:cap]
            self.stats["blocked_cap"] += len(d.to_defer)

        # 闸门 4b：全局每分钟上限（跨源，挡住多源并发刷屏）
        while self._recent_selects and now - self._recent_selects[0] > 60:
            self._recent_selects.popleft()
        room = max(0, rate - len(self._recent_selects))
        if len(d.to_push) > room:
            d.to_defer.extend(d.to_push[room:])
            d.to_push = d.to_push[:room]
            self.stats["blocked_rate"] += 1
        self._recent_selects.extend([now] * len(d.to_push))
        self.stats["allowed"] += len(d.to_push)

        d.reason = ("normal: push=%d mark=%d defer=%d floor=%s"
                    % (len(d.to_push), len(d.to_mark), len(d.to_defer),
                       time.strftime("%H:%M:%S", time.localtime(floor))))
        if d.skipped_old:
            logging.info("[push_guard] [%s] 拦截 %d 条启动前/超窗旧内容（只标记不补发）",
                         feed_key, d.skipped_old)
        return d

    def _replay(self, feed_key: str, tweets: list, hours: int,
                force: bool, d: Decision) -> Decision:
        """回测：只补时间窗内「没推过」的；force 才允许重发，且有冷却与条数上限。

        关键：不再清空去重记录，因此回测不会破坏水位线、不会让历史推文
        在下一轮常规轮询里被重新当成新内容。
        """
        c = self.cfg()
        now = self._now()
        known = self._known(feed_key)
        cap = int(c["replay_max"])
        cutoff = now - max(1, int(hours)) * 3600

        if force:
            last = self._replay_at.get(feed_key, 0)
            cooldown = float(c["replay_cooldown_seconds"])
            if now - last < cooldown:
                d.reason = "replay blocked: 冷却中（%d 秒内不重复强制回测）" % int(cooldown - (now - last))
                return d
            self._replay_at[feed_key] = now

        candidates = []
        no_ts = 0
        for t in tweets:
            ts = t.get("ts") or 0
            if not ts or ts < cutoff:
                no_ts += 1
                continue
            if not force and t.get("id") in known:
                d.skipped_known += 1
                continue
            candidates.append(t)
        candidates.sort(key=lambda t: t.get("ts") or 0)
        d.to_push = candidates[:cap]
        d.reason = ("replay: 近%d小时候选 %d 条，放行 %d 条（force=%s, 清库=否, 无时间戳/超窗 %d 条）"
                    % (hours, len(candidates), len(d.to_push), force, no_ts))
        self.stats["replays"] += 1
        self.stats["allowed"] += len(d.to_push)
        logging.info("[push_guard] [%s] %s", feed_key, d.reason)
        return d

    # ---------- 诊断 ----------
    def status(self) -> dict:
        with self._lock:
            now = self._now()
            while self._recent_selects and now - self._recent_selects[0] > 60:
                self._recent_selects.popleft()
            return {
                "enabled": self._enabled(),
                "boot_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(self.boot_at)),
                "boot_grace_seconds": self.cfg().get("boot_grace_seconds"),
                "primed_feeds": sorted(self._primed),
                "recent_minute": len(self._recent_selects),
                "stats": dict(self.stats),
            }

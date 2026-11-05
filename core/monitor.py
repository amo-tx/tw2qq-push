# -*- coding: utf-8 -*-
"""监控服务：被 main.py（前台运行）和 webui.py（Web 控制台）共用"""
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path

import yaml

from core import x_source
from core import net
from core.feed import fetch_feed
from core.formatter import format_message
from core.media import download_media, video_to_gif
from core.qq_bot import OneBotClient
from core.ssl_setup import enable_system_trust
from core.store import Store

# 尽早注入：让 requests 信任 Windows 系统证书库（兼容 Watt Toolkit 等 MITM 加速）
enable_system_trust()


class RingLog(logging.Handler):
    """保留最近 N 条日志的环形缓冲，供 Web 控制台实时展示"""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.buf = deque(maxlen=capacity)
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass

    def tail(self, n: int = 200) -> list:
        return list(self.buf)[-n:]


def setup_logging(log_dir: Path) -> RingLog:
    import sys
    import traceback
    log_dir.mkdir(exist_ok=True)
    ring = RingLog()
    handlers = [logging.FileHandler(log_dir / "run.log", encoding="utf-8"), ring]
    if sys.stderr is not None:  # --noconsole 冻结 exe 里 sys.stderr 为 None
        handlers.insert(0, logging.StreamHandler())
    # force=True：清掉可能被其他模块抢先注册的 root handler，保证文件日志生效
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    # 崩溃兜底：把未捕获异常（主线程/子线程）落到 crash.log，否则 noconsole 下无从排查
    crash_path = log_dir / "crash.log"

    def _write_crash(title: str, exc_info: tuple):
        try:
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + title + "\n")
                f.write("".join(traceback.format_exception(*exc_info)) + "\n")
        except Exception:
            pass

    def _sys_hook(tp, val, tb):
        _write_crash("unhandled exception (main)", (tp, val, tb))
        sys.__excepthook__(tp, val, tb)

    def _thread_hook(args):
        _write_crash(f"unhandled exception (thread {args.thread.name})",
                     (args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    return ring


class Monitor:
    """轮询 + 推送服务，线程安全，可在前台或后台线程运行"""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.config_path = self.base_dir / "config.yaml"

        self.ring = setup_logging(self.base_dir / "logs")
        self.config = {}
        self.store = None
        self.bot = None
        self.interval = 600
        self.reload_config()

        self._stop_event = threading.Event()
        self._thread = None
        self.lock = threading.Lock()
        self.checking = False
        self.last_check = None
        self.feeds_status = {}  # url -> {name, last_ok, last_error}
        self._busy_urls = set()  # 正在处理（常规轮询或手动回推）的源 url，二者互斥防重复推送
        self._cfg_mtime = None  # 配置文件 mtime，用于检测外部修改自动重载
        self._last_fetch = {}  # url -> 上次抓取时刻（快速轮询调度用）

    # ---------- 配置 ----------
    def reload_config(self):
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}
        if self.store is None:
            self.store = Store(self.base_dir / self.config.get("db_path", "data/seen.db"))
        qq_cfg = self.config.get("qq") or {}
        off = qq_cfg.get("official") or {}
        if off.get("enabled") and off.get("app_id") and off.get("client_secret"):
            from core.qq_official import OfficialBotClient
            same = (isinstance(self.bot, OfficialBotClient)
                    and self.bot.app_id == str(off["app_id"])
                    and self.bot.client_secret == off["client_secret"])
            if not same:
                # 配置变更才重建客户端；先停旧 WS，否则同一 AppID 同时挂多条
                # 连接会被平台互踢（create session error）。
                if isinstance(self.bot, OfficialBotClient):
                    try:
                        self.bot.stop_ws()
                    except Exception:
                        pass
                self.bot = OfficialBotClient(
                    off["app_id"], off["client_secret"], base_dir=self.base_dir)
                logging.info("QQ 通道：官方机器人（开放平台）%s", off["app_id"])
            if off.get("collect_groups", True):
                self.bot.start_ws()  # 内部幂等：WS 线程存活时直接返回
        else:
            if isinstance(self.bot, OfficialBotClient):
                try:
                    self.bot.stop_ws()
                except Exception:
                    pass
            self.bot = OneBotClient(
                qq_cfg["napcat"]["base_url"],
                qq_cfg["napcat"].get("access_token", ""),
            )
        self.interval = int(self.config.get("interval_minutes", 10)) * 60
        # 全局代理（x.com / GitHub 走 ChromeGo 等本地加速通道）
        net.apply_proxy((self.config.get("network") or {}).get("proxy", ""))

    # ---------- 状态 ----------
    def status(self) -> dict:
        bot_ok, bot_info = self.bot.get_status()
        return {
            "polling": self.is_running,
            "checking": self.checking,
            "last_check": self.last_check,
            "interval_minutes": self.interval // 60,
            "poll_seconds": int(self.config.get("poll_seconds") or 0),
            "bot": {"ok": bot_ok, "info": str(bot_info)[:100]},
            "feeds": [
                {
                    "name": f.get("name", f.get("handle") or f["url"]),
                    "handle": f.get("handle", ""),
                    "url": f.get("url", ""),
                    "groups": f.get("groups") or [],
                    **self.feeds_status.get(
                        f.get("url") or f"x://{f.get('handle')}",
                        {"last_ok": None, "last_error": None},
                    ),
                }
                for f in self.config["twitter"]["feeds"]
            ],
            "groups": self.config["qq"]["groups"],
            "pushed_total": self.store.total_pushed(),
        }

    # ---------- 核心检查 ----------
    def _check_one_feed(self, feed_cfg: dict) -> int:
        """抓取并处理单个博主，返回推送条数；异常不外抛（记录到 feeds_status）"""
        url = feed_cfg.get("url") or f"x://{feed_cfg.get('handle')}"
        name = feed_cfg.get("name", feed_cfg.get("handle") or url)
        self._last_fetch[url] = time.time()
        try:
            n = self._process_feed(feed_cfg)
            self.feeds_status[url] = {
                "name": name,
                "last_ok": time.strftime("%H:%M:%S"),
                "last_error": None,
            }
            return n
        except Exception as e:
            logging.error("处理 [%s] 出错: %s", name, e)
            self.feeds_status[url] = {
                "name": name,
                "last_ok": self.feeds_status.get(url, {}).get("last_ok"),
                "last_error": str(e)[:200],
            }
            return 0

    def check_once(self):
        """执行一轮全量检查，返回 (推送条数, 出错源数量)"""
        with self.lock:
            if self.checking:
                return 0, 0
            self.checking = True
        sent_total, errors = 0, 0
        try:
            for feed_cfg in self.config["twitter"]["feeds"]:
                before = self.feeds_status.get(
                    feed_cfg.get("url") or f"x://{feed_cfg.get('handle')}", {}
                ).get("last_error")
                n = self._check_one_feed(feed_cfg)
                sent_total += n
                after = self.feeds_status.get(
                    feed_cfg.get("url") or f"x://{feed_cfg.get('handle')}", {}
                ).get("last_error")
                if after and after != before:
                    errors += 1
        finally:
            self.checking = False
            self.last_check = time.strftime("%Y-%m-%d %H:%M:%S")
        return sent_total, errors

    # ---------- 抓取（RSS / X 通道自动路由） ----------
    def _fetch_tweets(self, feed_cfg: dict) -> list:
        """handle 存在走 X 公开接口（带 auth_token），否则走 RSS url"""
        handle = feed_cfg.get("handle")
        if handle:
            cookie = (self.config.get("x") or {}).get("auth_token", "")
            return x_source.fetch_user_tweets(handle, cookie)
        return fetch_feed(feed_cfg["url"])

    def _prepare_videos(self, tweet: dict) -> list:
        """下载推文视频并逐个转 GIF，返回本地 mp4 路径列表（GIF 存同目录同名）"""
        max_n = int(self.config.get("max_videos", 2))
        secs = int(self.config.get("video_gif_seconds", 10))
        media_dir = self.base_dir / "data" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^\w-]", "_", str(tweet["id"]))[-60:]
        paths = []
        for i, vurl in enumerate(tweet.get("videos", [])[:max_n]):
            dest = media_dir / f"{key}_{i}.mp4"
            try:
                download_media(vurl, dest)
                video_to_gif(dest, dest.with_suffix(".gif"), max_seconds=secs)
                paths.append(dest)
                logging.info("视频已处理: %s (+GIF)", dest.name)
            except Exception as e:
                logging.warning("视频处理失败 %s: %s", vurl, e)
        return paths

    def _push_one(self, url: str, name: str, t: dict, groups: list, feed_cfg: dict = None) -> bool:
        """格式化并发送一条推文；成功记入已推送和历史。失败返回 False（不记 id，下轮重试）"""
        ts = t.get("ts") or 0
        tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
        # @全体成员：博主级 at_all 优先，否则用全局开关（需机器人为群管理员）
        at_all = bool((feed_cfg or {}).get("at_all", self.config.get("at_all", False)))
        # 自动翻译（translate: true 时对外文推文附加中文译文）
        body = t["text"]
        if self.config.get("translate"):
            try:
                from core.translate import translate_for_push
                body = translate_for_push(body, engine=self.config.get("translate_engine", "llm"))
            except Exception as e:
                logging.warning("翻译模块异常（跳过翻译）: %s", e)
        text = format_message(
            self.config["template"],
            name=name, author=t["author"], text=body, link=t["link"], time=tstr,
        )
        images = (
            t.get("images", [])[: self.config.get("max_images", 4)]
            if self.config.get("send_images", True) else []
        )
        video_paths = []
        if self.config.get("send_videos", True) and t.get("videos"):
            video_paths = self._prepare_videos(t)
        try:
            ok_groups, failed = self.bot.send_post(groups, text, images, video_paths, at_all=at_all)
            for f in failed:
                logging.warning("[%s] 群 %s 未送达: %s", name, f["group_id"], f["error"])
            self.store.mark(url, t["id"])
            self.store.record_history(
                name, t["id"], t["author"], t["text"],
                ok_groups, len(images) + len(video_paths),
            )
            logging.info("已推送 [%s] %s -> 群 %s（图%d/视频%d，未送达%d个）",
                         name, t["id"], ok_groups, len(images), len(video_paths), len(failed))
            return True
        except Exception as e:
            logging.error("推送到 QQ 失败（[%s] %s）: %s", name, t["id"], e)
            return False

    def _process_feed(self, feed_cfg: dict) -> int:
        name = feed_cfg.get("name", feed_cfg.get("handle") or feed_cfg["url"])
        tweets = self._fetch_tweets(feed_cfg)  # 异常上抛，由 check_once 统一记录
        if not tweets:
            return 0

        url = feed_cfg["url"] if feed_cfg.get("url") else f"x://{feed_cfg['handle']}"
        # 与手动回推互斥：该博主正在回推时本轮跳过，避免双通道同时推送
        with self.lock:
            if url in self._busy_urls:
                logging.info("[%s] 正在手动回推中，本轮常规检查跳过", name)
                return 0
            self._busy_urls.add(url)
        try:
            return self._do_process_feed(url, name, tweets, feed_cfg)
        finally:
            with self.lock:
                self._busy_urls.discard(url)

    def _do_process_feed(self, url: str, name: str, tweets: list, feed_cfg: dict) -> int:
        known = self.store.known_ids(url)
        groups = feed_cfg.get("groups") or self.config["qq"]["groups"]  # 博主专属群优先
        if not groups:
            logging.warning("[%s] 未配置目标群（博主专属群和全局默认群都为空），本轮只记录不推送", name)
            for t in tweets:
                self.store.mark(url, t["id"])
            return 0
        # 首次运行且未开 backfill：只记录历史，不刷屏
        if not known and not self.config.get("backfill", False):
            for t in tweets:
                self.store.mark(url, t["id"])
            logging.info("首次运行 [%s]：记录 %d 条历史推文（不推送）", name, len(tweets))
            return 0
        # 首次运行且开了 backfill：同样只推时间窗口内的（无时间戳的一律只记录）
        if not known:
            hours = int(self.config.get("backfill_hours", 24))
            cutoff = time.time() - hours * 3600
            sent = 0
            for t in reversed(tweets):
                if not t.get("ts") or t["ts"] < cutoff:
                    self.store.mark(url, t["id"])
                    continue
                if self._push_one(url, name, t, groups, feed_cfg):
                    sent += 1
            logging.info("首次运行 [%s]（backfill 开）：推送近 %d 小时 %d 条", name, hours, sent)
            return sent

        # 常规轮询：双重防刷屏
        # 1) 时间窗：只推 max_push_age_hours 内的新推文，更早的积压（如曾因禁言/掉线
        #    发送失败而未标记的旧推文）直接标记跳过，不再补发
        # 2) 单轮上限：每源每轮最多推 max_push_per_cycle 条，超出留到下一轮
        max_age_hours = float(self.config.get("max_push_age_hours", 2))
        cutoff = time.time() - max_age_hours * 3600
        cap = int(self.config.get("max_push_per_cycle", 5))
        sent = 0
        skipped_old = 0
        for t in reversed(tweets):  # 从旧到新
            if t["id"] in known:
                continue
            if not t.get("ts"):
                # 无时间戳无法判断新旧，保守起见只标记不推送（避免老旧内容涌入刷屏）
                self.store.mark(url, t["id"])
                skipped_old += 1
                continue
            if t["ts"] < cutoff:
                self.store.mark(url, t["id"])
                skipped_old += 1
                continue
            if sent >= cap:
                logging.info("[%s] 本轮已达单轮推送上限 %d 条，其余留待下轮", name, cap)
                break
            if self._push_one(url, name, t, groups, feed_cfg):
                sent += 1
        if skipped_old:
            logging.info("[%s] 跳过 %d 条超出 %.1f 小时时间窗/无时间戳的推文（只标记不补发）",
                         name, skipped_old, max_age_hours)
        return sent

    def backfill_feed(self, feed_cfg: dict, hours: int = None) -> int:
        """回推最近 hours 小时的推文（供测试）；小时数默认取配置 backfill_hours"""
        if hours is None:
            hours = int(self.config.get("backfill_hours", 24))
        name = feed_cfg.get("name", feed_cfg.get("handle") or feed_cfg.get("url", "?"))
        url = feed_cfg.get("url") or f"x://{feed_cfg.get('handle')}"
        with self.lock:
            if url in self._busy_urls:
                logging.info("回推 [%s] 跳过：该博主正在常规检查中", name)
                return 0
            self._busy_urls.add(url)
        try:
            tweets = self._fetch_tweets(feed_cfg)
            cutoff = time.time() - hours * 3600
            known = self.store.known_ids(url)
            groups = feed_cfg.get("groups") or self.config["qq"]["groups"]
            if not groups:
                logging.warning("回推 [%s] 跳过：未指定目标群（博主专属群和全局默认群都为空）", name)
                return 0
            # 严格按时间窗口：没有时间戳的推文一律不回推（避免混入老旧内容）
            recent = [t for t in tweets if t.get("ts") and t["ts"] >= cutoff]
            no_ts = sum(1 for t in tweets if not t.get("ts"))
            if no_ts:
                logging.info("回推 [%s]：跳过 %d 条无时间戳的推文", name, no_ts)
            logging.info("回推 [%s]：近 %d 小时 %d 条（时间线共 %d 条）",
                         name, hours, len(recent), len(tweets))
            sent = 0
            for t in reversed(recent):  # 从旧到新
                if t["id"] in known:
                    continue
                if self._push_one(url, name, t, groups, feed_cfg):
                    sent += 1
            logging.info("回推 [%s] 完成：成功推送 %d 条到群 %s", name, sent, groups)
            return sent
        finally:
            with self.lock:
                self._busy_urls.discard(url)

    # ---------- 轮询线程 ----------
    def _fast_tick(self, poll_seconds: int):
        """快速轮询的一次心跳：挑出最久没抓的博主，超过 poll_seconds 就抓它。
        每次只抓一个源（错峰），避免同一时刻并发请求 X 接口触发限频。"""
        feeds = self.config.get("twitter", {}).get("feeds", [])
        if not feeds:
            return
        candidates = sorted(
            ((self._last_fetch.get(fc.get("url") or f"x://{fc.get('handle')}", 0), fc) for fc in feeds),
            key=lambda x: x[0],
        )
        oldest_ts, feed_cfg = candidates[0]
        url = feed_cfg.get("url") or f"x://{feed_cfg.get('handle')}"
        if time.time() - oldest_ts < poll_seconds:
            return  # 全部源都还新鲜
        with self.lock:
            if self.checking:
                return  # 手动检查进行中，让路
            self.checking = True
        try:
            self._check_one_feed(feed_cfg)
            self.last_check = time.strftime("%Y-%m-%d %H:%M:%S")
        finally:
            self.checking = False

    def _maybe_reload_config(self):
        """磁盘配置文件被外部修改（手动编辑/另一处保存）时自动重载，
        避免内存配置滞后于磁盘，随后整份写回把外部修改洗掉。"""
        try:
            mtime = self.config_path.stat().st_mtime
            if self._cfg_mtime is None:
                self._cfg_mtime = mtime
            elif mtime != self._cfg_mtime:
                self._cfg_mtime = mtime
                self.reload_config()
                logging.info("检测到配置文件变更，已自动重载")
        except Exception as e:
            logging.debug("配置重载检查失败: %s", e)

    def _run(self):
        poll_seconds = int(self.config.get("poll_seconds") or 0)
        if poll_seconds <= 0:
            # 传统整轮模式：每 interval_minutes 全量检查一次
            logging.info("轮询线程启动，间隔 %d 分钟", self.interval // 60)
            while not self._stop_event.is_set():
                self._maybe_reload_config()
                self.check_once()
                # 分片 sleep，便于快速响应停止
                for _ in range(max(self.interval // 5, 1)):
                    if self._stop_event.is_set():
                        return
                    self._stop_event.wait(5)
            return
        # 快速模式：每 poll_seconds 秒抓一次每个博主（错峰逐个抓，新推文平均 ~poll_seconds/2 内转发）
        logging.info("轮询线程启动（快速模式）：每个博主每 %d 秒抓取一次（错峰）", poll_seconds)
        while not self._stop_event.is_set():
            self._maybe_reload_config()
            self._fast_tick(poll_seconds)
            if self._stop_event.wait(5):
                return

    def start(self):
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="monitor")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

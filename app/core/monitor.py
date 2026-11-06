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
try:
    from core import tg_source          # 精简副本（GHA）没有 TG 源，可选
except ImportError:                     # pragma: no cover
    tg_source = None
from core import net
from core.feed import fetch_feed
from core.formatter import format_message
from core.media import download_media, video_to_gif, hls_url
from core.push_guard import PushGuard
from core.qq_bot import OneBotClient
from core.ssl_setup import enable_system_trust
from core.store import Store

# 尽早注入：让 requests 信任 Windows 系统证书库（兼容 Watt Toolkit 等 MITM 加速）
enable_system_trust()


def clean_handle(raw: str) -> str:
    """兼容 @name / t.me/name / https://t.me/s/name?before=1 等写法。

    放在 monitor 内而不是依赖 tg_source，让精简副本（GHA）也能安全 import。
    """
    import re as _re
    h = (raw or "").strip().lstrip("@")
    h = _re.sub(r"^https?://", "", h, flags=_re.I)
    h = _re.sub(r"^t\.me/(?:s/)?", "", h, flags=_re.I)
    return h.split("/")[0].split("?")[0].strip()


def feed_url(f: dict) -> str:
    """去重存储用的源唯一键。不同来源的 handle 可能重名，必须带协议前缀。"""
    if (f or {}).get("source") == "telegram":
        return "tg://" + clean_handle(f.get("handle", ""))
    return (f or {}).get("url") or f"x://{(f or {}).get('handle')}"


def iter_feeds(cfg: dict) -> list:
    """统一枚举 X 博主 + Telegram 频道（给后者自动打 source 标记与默认群）。

    所有轮询、状态、回测、前端列表都走这里，避免 TG 变成第二条独立支路。
    """
    cfg = cfg or {}
    out = list((cfg.get("twitter") or {}).get("feeds") or [])
    tg = cfg.get("telegram") or {}
    if tg.get("enabled", True):
        default_groups = (cfg.get("qq") or {}).get("groups") or []
        for c in (tg.get("channels") or []):
            c = dict(c)
            c.setdefault("source", "telegram")
            c.setdefault("groups", list(default_groups))
            out.append(c)
    return out


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
        self.manual_pause = False   # 群指令「暂停」：常驻守护不得自动拉起
        self.save_config_hook = None  # webui._save_config_key（供 @ 指令写配置）
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
        # 唯一权威的推送闸门：所有「该不该发」的判定都收敛到这里，
        # 防止部署/重启后补发历史推文刷屏（见 core/push_guard.py 说明）
        self.guard = PushGuard(lambda: self.store, lambda: self.config)
        # 发送串行锁：官方机器人把一条推文拆成「1 文本 + N 图片」多次 API 调用。
        # 常规轮询、手动补推、不同源之间若无锁会交错发送，转发端即使能区分
        # 也无从判断图片归属，必然串台丢图。整条推文的发送必须原子完成。
        self._push_lock = threading.Lock()

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
        # 官方通道：把 WS 的 @ 事件接到指令处理器。reload_config 可能重建
        # bot，因此每次重载都重新挂载（内部幂等，不会重复计数）。
        if hasattr(self.bot, "start_ws"):
            try:
                from core.commands import attach_official_commands
                attach_official_commands(self.bot, self, self.save_config_hook)
            except Exception as e:
                logging.debug("挂载官方 @指令 失败: %s", e)

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
                    "name": f.get("name", f.get("handle") or f.get("url") or "?"),
                    "handle": f.get("handle", ""),
                    "url": f.get("url", ""),
                    "source": f.get("source") or "x",
                    "groups": f.get("groups") or [],
                    **self.feeds_status.get(
                        feed_url(f),
                        {"last_ok": None, "last_error": None},
                    ),
                }
                for f in iter_feeds(self.config)
            ],
            "groups": self.config["qq"]["groups"],
            "pushed_total": self.store.total_pushed(),
            "push_guard": self.guard.status(),
        }

    # ---------- 核心检查 ----------
    def _check_one_feed(self, feed_cfg: dict) -> int:
        """抓取并处理单个博主，返回推送条数；异常不外抛（记录到 feeds_status）"""
        url = feed_url(feed_cfg)
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

    def check_once(self, force: bool = False):
        """执行一轮全量检查，返回 (推送条数, 出错源数量)。

        force=True 用于人工触发（WebUI 的「立即检查」、群指令「检查」），
        绕过群指令设置的暂停状态。
        """
        if self.manual_pause and not force:
            logging.info("自动推送已被群指令暂停，跳过本轮检查")
            return 0, 0
        with self.lock:
            if self.checking:
                return 0, 0
            self.checking = True
        sent_total, errors = 0, 0
        try:
            for feed_cfg in iter_feeds(self.config):
                before = self.feeds_status.get(
                    feed_url(feed_cfg), {}
                ).get("last_error")
                n = self._check_one_feed(feed_cfg)
                sent_total += n
                after = self.feeds_status.get(
                    feed_url(feed_cfg), {}
                ).get("last_error")
                if after and after != before:
                    errors += 1
        finally:
            self.checking = False
            self.last_check = time.strftime("%Y-%m-%d %H:%M:%S")
        return sent_total, errors

    # ---------- 抓取（RSS / X 通道自动路由） ----------
    def _fetch_tweets(self, feed_cfg: dict) -> list:
        """按 source 分派：telegram 走 t.me/s 预览页，X 走公开接口，其余走 RSS。"""
        if feed_cfg.get("source") == "telegram":
            if tg_source is None:
                raise RuntimeError("当前副本未包含 Telegram 源模块")
            return tg_source.fetch_channel(feed_cfg["handle"])
        handle = feed_cfg.get("handle")
        if handle:
            cookie = (self.config.get("x") or {}).get("auth_token", "")
            return x_source.fetch_user_tweets(handle, cookie)
        return fetch_feed(feed_cfg["url"])

    def _prepare_videos(self, tweet: dict) -> list:
        """下载视频并转 GIF，返回占位 mp4 路径列表（send_post 只按同名 .gif 取用）。

        兼容 X 只给 HLS(.m3u8) 的视频：交给 ffmpeg 直接读流，不落盘 mp4。
        """
        max_n = int(self.config.get("max_videos", 2))
        secs = int(self.config.get("video_gif_seconds", 10))
        media_dir = self.base_dir / "data" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^\w-]", "_", str(tweet["id"]))[-60:]
        paths = []
        for i, vurl in enumerate(tweet.get("videos", [])[:max_n]):
            dest = media_dir / f"{key}_{i}.mp4"
            gif = dest.with_suffix(".gif")
            try:
                if hls_url(vurl):
                    video_to_gif(vurl, gif, max_seconds=secs)
                else:
                    download_media(vurl, dest)
                    video_to_gif(dest, gif, max_seconds=secs)
                paths.append(dest)
                logging.info("视频/GIF 已处理: %s (%.2f MB)",
                             gif.name, gif.stat().st_size / 1048576)
            except Exception as e:
                logging.warning("视频处理失败 %s: %s", str(vurl)[:120], e)
                if gif.exists():       # 别把转了一半的 GIF 发出去
                    try:
                        gif.unlink()
                    except OSError:
                        pass
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
        # Telegram 频道用独立模板（带 (@author) 供 relay 订阅匹配）；
        # feed 级 template 优先，其次 telegram.template，最后全局 template。
        tpl = (feed_cfg or {}).get("template")
        if not tpl and (feed_cfg or {}).get("source") == "telegram":
            tpl = (self.config.get("telegram") or {}).get("template")
        text = format_message(
            tpl or self.config["template"],
            name=name, author=t["author"], text=body, link=t["link"], time=tstr,
        )
        images = (
            t.get("images", [])[: self.config.get("max_images", 4)]
            if self.config.get("send_images", True) else []
        )
        video_paths = []
        if self.config.get("send_videos", True) and t.get("videos"):
            video_paths = self._prepare_videos(t)
            if not video_paths:
                # GIF 全失败时至少把原视频直链放到消息最前面（QQ 按字节截尾，
                # 放前缀才不会被裁掉），保证群里还能点开看视频。
                link = str(t["videos"][0])[:180]
                text = f"🎬 视频/动图直链：{link}\n" + text
                logging.warning("[%s] %s 的 GIF 生成全部失败，改为附带视频直链",
                                name, t["id"])
        try:
            # 与「其它源 / 手动补推」互斥，保证 text → 图片 1..N 连续发完再让位。
            with self._push_lock:
                ok_groups, failed = self.bot.send_post(
                    groups, text, images, video_paths, at_all=at_all)
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

        url = feed_url(feed_cfg)
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
        """常规轮询：所有防刷屏判定统一交给 PushGuard，这里只负责落库与发送。"""
        groups = feed_cfg.get("groups") or self.config["qq"]["groups"]  # 博主专属群优先
        if not groups:
            logging.warning("[%s] 未配置目标群（博主专属群和全局默认群都为空），本轮只记录不推送", name)
            for t in tweets:
                self.store.mark(url, t["id"])
            return 0
        d = self.guard.select(url, tweets)
        for t in d.to_mark:
            self.store.mark(url, t["id"])
        sent = 0
        for t in d.to_push:
            if self._push_one(url, name, t, groups, feed_cfg):
                sent += 1
        logging.info("[%s] %s；实际发送 %d 条", name, d.reason, sent)
        return sent

    def backfill_feed(self, feed_cfg: dict, hours: int = None,
                      force: bool = False) -> int:
        """回测：补推近 hours 小时内「漏掉」的内容，不破坏去重水位线。

        与旧实现的关键区别：**不再清空去重记录**。旧版调用方先 clear_feed()
        再回推，等于把水位线抹掉，时间线里更老的内容会在下一轮常规轮询里
        被重新当成新内容补发 —— 这正是反复刷屏的来源。
        force=True 才允许重发已推过的，且受 PushGuard 冷却与条数上限约束。
        """
        if hours is None:
            hours = int(self.config.get("backfill_hours", 24))
        name = feed_cfg.get("name", feed_cfg.get("handle") or feed_cfg.get("url", "?"))
        url = feed_url(feed_cfg)
        with self.lock:
            if url in self._busy_urls:
                logging.info("回推 [%s] 跳过：该博主正在常规检查中", name)
                return 0
            self._busy_urls.add(url)
        try:
            tweets = self._fetch_tweets(feed_cfg)
            groups = feed_cfg.get("groups") or self.config["qq"]["groups"]
            if not groups:
                logging.warning("回推 [%s] 跳过：未指定目标群（博主专属群和全局默认群都为空）", name)
                return 0
            d = self.guard.select(url, tweets, replay=True, hours=hours, force=force)
            for t in d.to_mark:
                self.store.mark(url, t["id"])
            sent = 0
            for t in d.to_push:
                if self._push_one(url, name, t, groups, feed_cfg):
                    sent += 1
            logging.info("回推 [%s] 完成：%s；成功推送 %d 条到群 %s",
                         name, d.reason, sent, groups)
            return sent
        finally:
            with self.lock:
                self._busy_urls.discard(url)

    # ---------- 轮询线程 ----------
    def _fast_tick(self, poll_seconds: int):
        """快速轮询的一次心跳：挑出最久没抓的博主，超过 poll_seconds 就抓它。
        每次只抓一个源（错峰），避免同一时刻并发请求 X 接口触发限频。"""
        feeds = iter_feeds(self.config)
        if not feeds:
            return
        candidates = sorted(
            ((self._last_fetch.get(feed_url(fc), 0), fc) for fc in feeds),
            key=lambda x: x[0],
        )
        oldest_ts, feed_cfg = candidates[0]
        url = feed_url(feed_cfg)
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
            # 每轮重读配置：群指令「间隔 N」改完即刻生效，不必重启轮询线程
            cur = int(self.config.get("poll_seconds") or 0) or poll_seconds
            if cur != poll_seconds:
                logging.info("抓取间隔已更新：%d → %d 秒", poll_seconds, cur)
                poll_seconds = cur
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

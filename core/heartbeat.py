# -*- coding: utf-8 -*-
"""7x24 常驻守护：看门狗自愈 + 自我保活 + 云端心跳留痕。

云端 sandbox 可能因长时间无外部请求被回收 / 休眠。本模块做三件事：

1. watchdog 每 30s 巡检：轮询线程、代理内核、官方机器人 WS，异常自动拉起；
   轮询停滞超过 2.5 个周期时补跑一轮检查。
2. keepalive 每 60s 从进程内请求自身公网健康端点，维持网关流量活跃，
   降低空闲回收概率（不依赖外部账号、不依赖用户本地设备常开）。
3. heartbeat 每 10 分钟向云数据库 upsert 一行，事后可核验在线连续性。

只读依赖 monitor / chromego / clouddb 的公开属性，失败一律降级记日志，
绝不影响推送主流程。
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

BOOT_ID = uuid.uuid4().hex[:12]
BOOT_TS = time.time()

# 自身域名 / 云网关必须直连：程序内 net.apply_proxy 会把流量导向 Twitter 代理
_sess = requests.Session()
_sess.trust_env = False

_state = {
    "boot_id": BOOT_ID,
    "boot_at": datetime.fromtimestamp(BOOT_TS).strftime("%Y-%m-%d %H:%M:%S"),
    "loops": 0,
    "last_loop": None,
    "repairs": [],
    "self_ping_ok": None,
    "self_ping_ms": None,
    "cloud_heartbeat_ok": None,
    "last_check_age": None,
    "gha_fresh": False,
    "mode": "cloud-app",
}
_lock = threading.Lock()


def state() -> dict:
    with _lock:
        d = dict(_state)
        d["repairs"] = list(d["repairs"][-15:])
    d["uptime_seconds"] = int(time.time() - BOOT_TS)
    return d


def _repair(msg):
    with _lock:
        _state["repairs"].append(
            {"at": datetime.now().strftime("%H:%M:%S"), "what": msg})
    logging.warning("[守护] %s", msg)


def _age_of(last_check):
    """monitor.last_check 是本地时间字符串 'YYYY-MM-DD HH:MM:SS'。"""
    if not last_check:
        return None
    try:
        dt = datetime.strptime(str(last_check), "%Y-%m-%d %H:%M:%S")
        return max(0.0, (datetime.now() - dt).total_seconds())
    except Exception:
        return None


def _gha_fresh(cloud) -> bool:
    """GitHub Actions 是否在最近 15 分钟内跑过（service_heartbeat id=2）。

    GHA 每 10 分钟写一次心跳；留 5 分钟余量容忍 Actions 排队延迟。
    """
    if not cloud:
        return False
    try:
        rows = cloud._select("service_heartbeat",
                             {"select": "last_seen", "id": "eq.2", "limit": 1})
        if not rows or not rows[0].get("last_seen"):
            return False
        dt = datetime.fromisoformat(
            str(rows[0]["last_seen"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return -300 < age < 900
    except Exception as e:
        logging.debug("[守护] GHA 心跳检查失败: %s", e)
        return False


def _tick(monitor, chromego, cloud, keepalive_url, stall_seconds,
          need_ping, need_hb):
    with _lock:
        _state["loops"] += 1
        _state["last_loop"] = datetime.now().strftime("%H:%M:%S")

    # 0) 主源仲裁：GHA 在线则云端让位，GHA 失联则云端接管。
    #    两端共享云数据库 seen，因此任何时刻只有一个推送源，不会重复推送。
    gha = _gha_fresh(cloud)
    with _lock:
        prev = _state["gha_fresh"]
        _state["gha_fresh"] = gha
        _state["mode"] = "github-actions" if gha else "cloud-app"
    if gha:
        if monitor.is_running:
            monitor.stop()
            _repair("GitHub Actions 主推送源在线，云端轮询自动让位")
    else:
        if prev:
            _repair("GitHub Actions 超过 15 分钟未运行，云端轮询自动接管")
        if not monitor.is_running:
            try:
                monitor.start()
            except Exception as e:
                _repair("轮询线程重启失败: %s" % e)

    # 1) 轮询停滞补检（云端让位期间不检查，避免抢推）
    age = _age_of(getattr(monitor, "last_check", None))
    with _lock:
        _state["last_check_age"] = int(age) if age is not None else None
    if (age is not None and age > stall_seconds
            and not monitor.checking and not gha):
        def _catchup():
            try:
                monitor.check_once()
                _repair("轮询停滞 %ds，已补跑一轮检查" % int(age))
            except Exception as e:
                _repair("补检失败: %s" % e)
        threading.Thread(target=_catchup, daemon=True, name="catchup").start()

    # 3) 代理内核
    try:
        netcfg = monitor.config.get("network") or {}
        if (netcfg.get("chromego_auto_start", True) is not False
                and not chromego.is_running):
            r = chromego.start(netcfg.get("chromego_kernel", "clash.meta"))
            if r.get("ok"):
                _repair("代理内核已掉线，已重新拉起")
    except Exception as e:
        _repair("代理内核检查异常: %s" % e)

    # 4) 官方机器人 WebSocket
    try:
        bot = getattr(monitor, "bot", None)
        if bot is not None and hasattr(bot, "start_ws"):
            th = getattr(bot, "_ws_thread", None)
            if th is None or not th.is_alive():
                bot.start_ws()
                _repair("官方机器人 WS 已断开，已重连")
    except Exception as e:
        _repair("WS 重连失败: %s" % e)

    # 5) 自我保活：让网关持续看到流量，避免空闲回收
    if need_ping and keepalive_url:
        t0 = time.time()
        try:
            r = _sess.get(keepalive_url, timeout=12)
            ok = r.status_code < 500
            with _lock:
                _state["self_ping_ok"] = ok
                _state["self_ping_ms"] = int((time.time() - t0) * 1000)
            if not ok:
                logging.warning("[守护] 自我保活 HTTP %s", r.status_code)
        except Exception as e:
            with _lock:
                _state["self_ping_ok"] = False
            logging.debug("[守护] 自我保活失败: %s", e)

    # 6) 云端心跳留痕（事后可查过去 24h 是否连续在线）
    if need_hb and cloud:
        try:
            st = state()
            try:
                pushed = monitor.store.total_seen()
            except Exception:
                pushed = None
            ok = cloud.heartbeat({
                "boot_id": BOOT_ID,
                "boot_at": datetime.fromtimestamp(BOOT_TS, timezone.utc).isoformat(),
                "uptime_seconds": st["uptime_seconds"],
                "last_check_age_seconds": st["last_check_age"],
                "self_ping_ok": st["self_ping_ok"],
                "pushed_total": pushed,
            })
            with _lock:
                _state["cloud_heartbeat_ok"] = ok
        except Exception as e:
            logging.debug("[守护] 云端心跳失败: %s", e)


def start(monitor, chromego, cloud, config: dict):
    """启动守护线程；重复调用安全（幂等）。"""
    cloud_cfg = config.get("cloud") or {}
    keepalive_url = cloud_cfg.get("keepalive_url") or ""
    poll = int(config.get("poll_seconds") or 120)
    stall = max(int(poll * 2.5), 300)
    interval = 30

    def _loop():
        last_ping = 0.0
        last_hb = 0.0
        while True:
            now = time.time()
            need_ping = (now - last_ping) >= 60
            need_hb = (now - last_hb) >= 600
            try:
                _tick(monitor, chromego, cloud, keepalive_url, stall,
                      need_ping, need_hb)
            except Exception as e:
                logging.warning("[守护] 巡检异常: %s", e)
            if need_ping:
                last_ping = now
            if need_hb:
                last_hb = now
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="watchdog").start()
    logging.info("[守护] 常驻守护已启动：巡检 %ss / 保活 60s / 云心跳 600s，boot_id=%s",
                 interval, BOOT_ID)

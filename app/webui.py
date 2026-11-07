# -*- coding: utf-8 -*-
"""Web 可视化控制台

启动：python webui.py  →  浏览器打开 http://127.0.0.1:5800
功能：运行状态总览、7日推送统计、实时日志、最近推送记录、配置在线编辑、手动触发检查
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

from core import x_source
from core import tg_source
from core import net
from core import settings
from core import heartbeat
from core.chromego import ChromeGoManager
from core.monitor import Monitor, feed_url, iter_feeds
from core.qq_login import NapCatManager
from core.commands import (BUTTON_TEXT, COMMAND_CATALOG, PANEL_COMMON,
                           PANEL_MAIN, PANEL_MORE, official_keyboard_state,
                           panel_keyboard)

# 冻结为 exe 时：config/data/logs 放在 exe 同级目录（用户可编辑），
# 打包资源（templates）从解包临时目录读取
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).parent
    TEMPLATE_DIR = Path(getattr(sys, "_MEIPASS", str(BASE))) / "templates"
else:
    BASE = Path(__file__).parent
    TEMPLATE_DIR = BASE / "templates"

# 时区兜底：云主机/容器默认 UTC 时，控制台与推送文案里的时间会比北京时间慢 8 小时。
# 优先用 tzdata 的 Asia/Shanghai，没有 zoneinfo 就退回 POSIX 串 CST-8（不依赖任何包）。
if not os.environ.get("TZ"):
    os.environ["TZ"] = ("Asia/Shanghai"
                        if Path("/usr/share/zoneinfo/Asia/Shanghai").exists()
                        else "CST-8")
try:
    time.tzset()
except (AttributeError, OSError):
    pass

monitor = Monitor(BASE)
# 轮询延后到代理内核就绪后再启动（见文件末尾 _boot_sequence）。
# 原实现在模块加载时立即 start，而代理内核 4 秒后才起，导致冷启动首轮
# 抓取 / WS 全部因代理端口未监听而失败。

# WorkBuddy 云服务数据库：推送记录/历史/群 openid 云端持久化（部署更新不丢）
from core import clouddb
_cloud = clouddb.init(monitor.config)
if _cloud:
    monitor.store.cloud = _cloud

    def _cloud_restore():
        try:
            time.sleep(6)  # 等本地库初始化完成
            _cloud.restore(monitor.store, Path(BASE) / "data" / "official_groups.json")
            # 一次性迁移：本地既有记录灌入云端（云端已有数据时 upsert 幂等）
            if monitor.store.all_history():
                _cloud.migrate_history_from(monitor.store)
            _cloud.migrate_seen_from(monitor.store)
        except Exception as e:
            logging.warning("云端恢复线程异常: %s", e)

    threading.Thread(target=_cloud_restore, daemon=True, name="cloud-restore").start()

napcat = NapCatManager(BASE, lambda: monitor.config,
                       lambda patch: _save_config_key(patch))

# @机器人指令交互
#   · NapCat / OneBot：HTTP 上报 POST /onebot/event → commander
#   · QQ 官方机器人：WS 的 GROUP_AT_MESSAGE_CREATE → OfficialCommandHandler
#     （在下方 _attach_at_commands 挂载，模块加载时 _save_config_key 尚未定义）
from core.commands import CommandHandler
commander = CommandHandler(monitor, napcat, lambda patch: _save_config_key(patch))
monitor.save_config_hook = lambda patch: _save_config_key(patch)

# ChromeGo 内核管理器（程序内启动代理内核，只影响本程序流量）
chromego = ChromeGoManager(BASE, lambda: monitor.config,
                           lambda patch: _save_config_key(patch))

# QQ 小号转发链（拉不进官方机器人的群）：relay.py 转发器 + Linux 协议端
# engine=napcat（推荐，活跃维护）或 lagrange（官方签名服务已下线，仅备用）
from core.lagrange import LagrangeManager
from core.napcat_cloud import NapCatCloudManager
lagrange = LagrangeManager(BASE, lambda: monitor.config)
napcat_cloud = NapCatCloudManager(BASE, lambda: monitor.config)
_relay_proc = None


def _relay_engine() -> str:
    return (monitor.config.get("relay") or {}).get("engine") or "napcat"


def _relay_manager():
    return lagrange if _relay_engine() == "lagrange" else napcat_cloud

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

# ============ X 登录凭据独立备份 ============
# config.yaml 可能被模板同步/手动编辑覆盖，auth_token 单点存那里会丢。
# 冗余存 data/x_account.json，启动时发现 config 里没有就自动恢复。
X_TOKEN_FILE = Path(BASE) / "data" / "x_account.json"


def _backup_x_token(token: str):
    try:
        X_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        X_TOKEN_FILE.write_text(yaml.safe_dump(
            {"auth_token": token}, allow_unicode=True), encoding="utf-8")
    except Exception as e:
        logging.warning("备份 X 凭据失败: %s", e)


def _restore_x_token():
    try:
        if not X_TOKEN_FILE.exists():
            return
        saved = (yaml.safe_load(X_TOKEN_FILE.read_text(encoding="utf-8")) or {}).get("auth_token", "")
        cfg_token = ((monitor.config.get("x") or {}).get("auth_token") or "").strip()
        if saved and not cfg_token:
            _save_config_key({"x": {"auth_token": saved}})
            logging.info("已从 data/x_account.json 恢复 X 登录凭据")
    except Exception as e:
        logging.warning("恢复 X 凭据失败: %s", e)


# ============ QQ 自动快速登录 ============
def _auto_qq_login():
    try:
        time.sleep(3)  # 等控制台服务就绪
        saved = napcat.saved_qq()
        if not saved or not napcat.installed():
            return
        if monitor.config.get("auto_login_qq", True) is False:
            return
        logging.info("检测到已保存的 QQ 号 %s，自动快速登录中…", saved)
        r = napcat.start(saved)
        if r.get("ok"):
            logging.info("QQ 自动登录已启动（本地有快速登录票据时免扫码）")
        else:
            logging.warning("QQ 自动登录失败: %s", r.get("error"))
    except Exception as e:
        logging.warning("QQ 自动登录异常: %s", e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(monitor.status())


@app.route("/api/history")
def api_history():
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(monitor.store.recent_history(limit))


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "total": monitor.store.total_pushed(),
        "by_day": monitor.store.push_stats(7),
    })


@app.route("/api/logs")
def api_logs():
    return jsonify(monitor.ring.tail(min(int(request.args.get("n", 200)), 500)))


@app.route("/api/check", methods=["POST"])
def api_check():
    if monitor.checking:
        return jsonify({"ok": False, "error": "已有检查在进行中"})
    # WebUI 手动点击带 X-Tw2qq-Force: 1 → 绕过群指令「暂停」；
    # 外部看门狗（GitHub Actions）唤醒时不带此头，必须遵守暂停状态，
    # 否则「暂停」会被每 10 分钟一次的自动唤醒绕过。
    force = request.headers.get("X-Tw2qq-Force") == "1"
    sent, errors = monitor.check_once(force=force)
    return jsonify({"ok": True, "sent": sent, "errors": errors,
                    "paused": bool(getattr(monitor, "manual_pause", False))})


@app.route("/api/polling", methods=["POST"])
def api_polling():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "start":
        monitor.start()
    elif action == "stop":
        monitor.stop()
    else:
        return jsonify({"ok": False, "error": "action 需为 start/stop"}), 400
    return jsonify({"ok": True, "polling": monitor.is_running})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        with open(monitor.config_path, encoding="utf-8") as f:
            return jsonify({"content": f.read()})
    content = (request.get_json(silent=True) or {}).get("content", "")
    try:
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), "根节点必须是映射"
        assert "twitter" in parsed and "qq" in parsed, "缺少 twitter/qq 配置段"
    except Exception as e:
        return jsonify({"ok": False, "error": f"YAML 校验失败: {e}"}), 400
    with open(monitor.config_path, "w", encoding="utf-8") as f:
        f.write(content)
    monitor.reload_config()
    logging.info("配置已在 Web 控制台更新并重新加载")
    return jsonify({"ok": True})


@app.route("/api/config/quick", methods=["GET", "POST"])
def api_config_quick():
    """工具栏快捷开关（白名单字段），避免用户手编 YAML。"""
    if request.method == "GET":
        return jsonify({
            "at_all": bool(monitor.config.get("at_all", False)),
            "translate": bool(monitor.config.get("translate", True)),
        })
    body = request.get_json(silent=True) or {}
    patch = {k: body[k] for k in ("at_all", "translate") if k in body}
    if not patch:
        return jsonify({"ok": False, "error": "无可更新字段"}), 400
    _save_config_key(patch)
    return jsonify({"ok": True, **patch})


# ============ OneBot 事件上报（@机器人指令交互） ============
@app.route("/onebot/event", methods=["POST"])
def onebot_event():
    """NapCat 把收到的 QQ 消息事件 POST 到这里（见 qq_login._write_onebot_config）。"""
    ev = request.get_json(silent=True)
    if ev is not None:
        threading.Thread(target=commander.handle_event, args=(ev,),
                         daemon=True, name="ob-event").start()
    return jsonify({})


# ============ 网络代理（ChromeGo / Watt 等加速通道） ============
@app.route("/api/network", methods=["GET"])
def api_network_get():
    return jsonify({
        "proxy": (monitor.config.get("network") or {}).get("proxy", ""),
        "active": net.current_proxy(),
        "detected": net.detect_local_proxies(),
    })


@app.route("/api/network/proxy", methods=["POST"])
def api_network_set():
    proxy = (request.get_json(silent=True) or {}).get("proxy", "").strip()
    _save_config_key({"network": {"proxy": proxy}})
    return jsonify({"ok": True, "active": net.current_proxy()})


@app.route("/api/network/test", methods=["POST"])
def api_network_test():
    body = request.get_json(silent=True) or {}
    proxy = (body.get("proxy") or "").strip() or None
    results = net.test_proxy(proxy)
    return jsonify({"ok": all(r["ok"] for r in results), "results": results})


# ============ ChromeGo 集成（程序内启动代理内核） ============
@app.route("/api/chromego/status")
def api_chromego_status():
    return jsonify(chromego.status())


@app.route("/api/chromego/start", methods=["POST"])
def api_chromego_start():
    kernel = (request.get_json(silent=True) or {}).get("kernel", "").strip()
    return jsonify(chromego.start(kernel))


@app.route("/api/chromego/stop", methods=["POST"])
def api_chromego_stop():
    chromego.stop()
    return jsonify({"ok": True})


@app.route("/api/chromego/dir", methods=["POST"])
def api_chromego_dir():
    d = (request.get_json(silent=True) or {}).get("dir", "").strip()
    if not Path(d).exists():
        return jsonify({"ok": False, "error": "目录不存在"}), 400
    _save_config_key({"network": {"chromego_dir": d}})
    return jsonify({"ok": True, "dir": d})


# ============ X 账号登录通道 ============
@app.route("/api/x/validate", methods=["POST"])
def api_x_validate():
    token = (request.get_json(silent=True) or {}).get("token", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "请粘贴 auth_token Cookie 值"})
    result = x_source.validate_cookie(token)
    if result.get("ok"):
        _save_config_key({"x": {"auth_token": token}})
        _backup_x_token(token)  # 独立备份：config 被覆盖后可自动恢复
        logging.info("X 账号登录成功: @%s", result.get("user"))
    return jsonify(result)


def _deep_merge(dst: dict, patch: dict):
    """兼容旧调用；实现已收敛到 core.settings。"""
    return settings.deep_merge(dst, patch)


def _save_config_key(patch: dict):
    """定点更新配置键并热重载：先重读磁盘，避免覆盖手工编辑过的键。"""
    settings.patch_yaml(patch, monitor.config_path, fallback=monitor.config)
    monitor.reload_config()


# ============ ChromeGo 代理内核自动启动 ============
def _auto_chromego_start():
    """同步启动代理内核（由 _boot_sequence 调用；也保留给手动重试）。"""
    try:
        netcfg = monitor.config.get("network") or {}
        if netcfg.get("chromego_auto_start", True) is False:
            return
        if chromego.is_running:
            logging.info("ChromeGo 代理内核已在运行，无需自动启动")
            return
        key = netcfg.get("chromego_kernel", "clash.meta")
        logging.info("自动启动 ChromeGo 代理内核（%s）…", key)
        r = chromego.start(key)
        if r.get("ok"):
            logging.info("ChromeGo 自动启动成功（%s @ %s）", r.get("kernel"), r.get("proxy"))
        else:
            logging.warning("ChromeGo 自动启动失败: %s", r.get("error"))
    except Exception as e:
        logging.warning("ChromeGo 自动启动异常: %s", e)


# 模块级启动任务（_save_config_key 已定义，可安全调用）
from core import cleanup as _cleanup_mod
_cleanup_mod.run_all(BASE, monitor.config)

def _cleanup_loop():
    while True:
        time.sleep(24 * 3600)
        _cleanup_mod.run_all(BASE, monitor.config)

threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup-daily").start()
_restore_x_token()
threading.Thread(target=_auto_qq_login, daemon=True, name="auto-qq-login").start()


def _attach_at_commands():
    """把官方机器人的 WS @ 事件接到指令处理器（幂等）。

    Monitor 构造时已自动挂过一次，但那时 webui._save_config_key 还没定义，
    这里补上写配置的回调，保证「翻译 开/关」等指令能真正落盘。
    """
    try:
        from core.commands import attach_official_commands
        h = attach_official_commands(monitor.bot, monitor, _save_config_key)
        if h:
            logging.info("[启动] 官方机器人 @指令 已就绪")
    except Exception as e:
        logging.warning("[启动] 挂载官方 @指令 失败: %s", e)


_attach_at_commands()


# ---------- QQ 小号转发链（relay.py 转发器 + Lagrange 协议端） ----------

def _port_open(port, host="127.0.0.1", timeout=1.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _push_relay_targets(retries: int = 6, quiet: bool = False) -> bool:
    """把控制台解析出的订阅通过本地 HTTP 直接下发给转发器。

    云端部署时 webui 与 relay.py 可能从不同的目录解析 BASE，两边读到的
    relay.json / data/relay_targets.json 并不一定是同一份（实测表现为
    「前端 6 个订阅、转发器 0 个」）。用进程内下发替代文件对齐，最可靠。
    """
    import urllib.request as _ur
    try:
        targets = settings.normalize_targets(settings.read_relay_cfg())
    except Exception as e:
        logging.warning("[relay] 订阅读取失败: %s", e)
        return False
    if not targets:
        return False
    body = json.dumps({"target_groups": targets}, ensure_ascii=False).encode("utf-8")
    for _ in range(max(1, retries)):
        try:
            req = _ur.Request("http://127.0.0.1:5801/targets", data=body,
                              method="POST",
                              headers={"Content-Type": "application/json"})
            with _ur.build_opener(_ur.ProxyHandler({})).open(req, timeout=5) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
            if out.get("ok"):
                if not quiet:
                    logging.info("[relay] 订阅已下发给转发器: %s", targets)
                return True
        except Exception:
            pass
        time.sleep(1.0)
    if not quiet:
        logging.warning("[relay] 订阅下发失败（转发器可能仍用默认配置）")
    return False


def _relay_boot():
    """转发器(relay.py 子进程) + 协议端(Lagrange) 随云端冷启动拉起。"""
    global _relay_proc
    cfg = monitor.config.get("relay") or {}
    if not cfg.get("enabled"):
        logging.info("[启动] relay.enabled=false，转发链未启用")
        return
    # 重新部署后 webui 是新进程、_relay_proc 为 None，但旧 relay.py 往往还
    # 占着 5801/5802。旧实现直接「跳过启动」，结果是新代码和刚恢复的订阅
    # 永远不生效（每次都要手工点一次重启）。这里主动换代一次。
    if _relay_proc is None and _port_open(5801):
        try:
            if os.name == "posix":
                subprocess.run(["pkill", "-f", "relay.py"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(12):
                    if not _port_open(5801):
                        break
                    time.sleep(0.5)
                logging.info("[启动] 已换代部署前遗留的 relay.py 进程")
        except Exception as e:
            logging.warning("[启动] 换代转发器失败（沿用现有进程）: %s", e)
    if not _port_open(5801):
        _relay_proc = subprocess.Popen(
            [sys.executable, str(Path(BASE) / "relay.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        logging.info("[启动] relay.py 转发器已拉起 pid=%s", _relay_proc.pid)
    else:
        logging.info("[启动] 转发器 5801 已在运行，跳过")
    # 文件对齐不可靠，启动后立刻把订阅下发一次（进程起来需要几秒，内部重试）
    _push_relay_targets()
    engine = _relay_engine()
    try:
        r = _relay_manager().start()
        logging.info("[启动] %s 协议端: %s",
                     "Lagrange" if engine == "lagrange" else "NapCat", r)
    except Exception as e:
        logging.warning("[启动] %s 启动失败: %s", engine, e)
    _relay_watchdog()


def _relay_health_tick(state: dict):
    """每 60s 判定小号转发链是否在线；离线→在线时自动补转漏掉的推文。

    20:32 事故：小号登录态失效近 50 分钟，期间官方机器人照常推送，但小号
    收不到群消息，恢复后也不会补发。这里在恢复的那一刻补拉来源群历史，
    由 relay.py 复用 message_id 去重，只补真正漏掉的推文。
    """
    import urllib.request as _ur
    online = False
    try:
        with _ur.build_opener(_ur.ProxyHandler({})).open(
                "http://127.0.0.1:5801/status", timeout=4) as r:
            st = json.loads(r.read().decode("utf-8", "replace"))
        online = bool(st.get("ws_connected"))
    except Exception:
        online = False
    # NapCat WebUI 登录态比 WS 更权威：WS 可能仍连着，但小号已掉线等扫码
    try:
        probe = getattr(_relay_manager(), "login_state", None)
        if callable(probe):
            ls = probe() or {}
            if ls:
                online = (bool(ls.get("isLogin")) and not ls.get("isOffline")
                          and str(ls.get("loginPhase") or "ready") == "ready")
    except Exception:
        pass
    prev = state.get("online")
    state["online"] = online
    if not online:
        if prev is not False:
            state["offline_since"] = time.time()
            logging.warning("[relay] 小号转发链离线，恢复后将自动补转漏掉的推文")
        return
    if prev is False and state.get("offline_since"):
        since = state["offline_since"]
        gap = time.time() - since
        state["offline_since"] = 0.0
        if gap < 60 or time.time() - state.get("last_recover", 0) < 600:
            return
        state["last_recover"] = time.time()
        minutes = max(10, min(240, int(gap / 60) + 10))
        # 只补掉线开始之后的推文，掉线前已转发的绝不重发
        url = ("http://127.0.0.1:5801/recover?minutes=%d&since_ts=%d"
               % (minutes, int(since)))
        try:
            with _ur.build_opener(_ur.ProxyHandler({})).open(
                    url, timeout=40) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
            logging.warning("[relay] 掉线 %.0f 秒后恢复，自动补转: %s", gap, out)
        except Exception as e:
            logging.warning("[relay] 自动补转失败（可手动 relay_recover 重试）: %s", e)


def _relay_watchdog():
    def loop():
        global _relay_proc
        state = {"online": None, "offline_since": 0.0, "last_recover": 0.0}
        while True:
            time.sleep(60)
            try:
                cfg = monitor.config.get("relay") or {}
                if not cfg.get("enabled"):
                    continue
                if not _port_open(5801):
                    logging.warning("[relay] 转发器无响应，重启 relay.py 子进程")
                    if _relay_proc and _relay_proc.poll() is None:
                        _relay_proc.kill()
                    _relay_proc = subprocess.Popen(
                        [sys.executable, str(Path(BASE) / "relay.py")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                # 每 60s 静默对齐一次：转发器被重启/换代后也能自动找回订阅
                _push_relay_targets(retries=1, quiet=True)
                _relay_health_tick(state)
            except Exception as e:
                logging.warning("[relay] watchdog 异常: %s", e)
    threading.Thread(target=loop, daemon=True, name="relay-wd").start()


def _boot_sequence():
    """冷启动顺序：代理内核 → 监控轮询 → 常驻守护。

    原实现"轮询立即启动 + 代理 4 秒后才起"会造成冷启动首轮抓取全失败；
    现在先同步把内核拉起来再开轮询，最后挂上看门狗 / 保活 / 云心跳。
    Windows exe 与安卓 APK 同样受益。
    """
    # 订阅状态必须在拉起 relay.py 之前收敛：转发器启动时会读一次配置
    try:
        settings.ensure_relay_state()
    except Exception as e:
        logging.warning("[启动] 订阅状态迁移失败: %s", e)
    _auto_chromego_start()
    if not monitor.is_running:
        monitor.start()
        logging.info("[启动] 代理就绪，监控轮询已启动")
    try:
        heartbeat.start(monitor, chromego, clouddb.get(), monitor.config)
    except Exception as e:
        logging.warning("[启动] 常驻守护启动失败: %s", e)
    try:
        _relay_boot()
    except Exception as e:
        logging.warning("[启动] 转发链启动失败: %s", e)


threading.Thread(target=_boot_sequence, daemon=True, name="boot-sequence").start()


@app.route("/api/x/status")
def api_x_status():
    return jsonify({"auth_token_set": bool((monitor.config.get("x") or {}).get("auth_token"))})


@app.route("/api/cloud/status")
def api_cloud_status():
    c = clouddb.get()
    if not c:
        return jsonify({"enabled": False})
    st = c.status()
    try:  # 云端表行数实测（验证数据真的落库）
        st["cloud_seen_rows"] = len(c._select("pushed_seen", {"select": "feed_key"}))
    except Exception:
        st["cloud_seen_rows"] = None
    return jsonify(st)


@app.route("/api/push/guard")
def api_push_guard():
    """推送闸门诊断：启动水位线、基线源、拦截统计、去重库规模。

    packaged_runtime_files 非空 = 部署包里混入了运行时状态文件，
    每次发布都会把云端去重记录回滚，必须移出部署目录。
    """
    st = monitor.guard.status()
    try:
        st["seen_total"] = monitor.store.total_seen()
        st["pushed_total"] = monitor.store.total_pushed()
    except Exception:
        pass
    st["packaged_runtime_files"] = settings.packaged_runtime_files()
    return jsonify(st)


@app.route("/api/relay/status")
def api_relay_status():
    st = _relay_manager().status()
    try:
        import urllib.request as _ur
        with _ur.build_opener(_ur.ProxyHandler({})).open(
                "http://127.0.0.1:5801/status", timeout=4) as r:
            st["forwarder"] = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        st["forwarder"] = {"ok": False, "error": str(e)[:120]}
    fw = st.get("forwarder") or {}
    # manager.status() 的 need_qrcode 来自 NapCat WebUI 登录态：反向 WS 可能
    # 仍连着，但小号已经在等扫码了。此时不能再用 ws_connected 判成在线。
    login_offline = bool(st.get("need_qrcode"))
    st["online"] = (bool(fw.get("ws_connected")) or bool(st.get("online"))) \
        and not login_offline
    # 掉线（等待扫码）时给前端明确信号，避免用户误判成订阅/回测配置问题
    st["need_qrcode"] = login_offline or (
        st.get("process") == "running" and not st["online"])
    st["webui"] = "/napcat/"
    st["qrcode_page"] = "/relay-qrcode"
    return jsonify(st)


@app.route("/api/relay/groups")
def api_relay_groups():
    """小号加入的群聊列表（经反向WS向 OneBot 取 get_group_list）。"""
    import urllib.request as _ur
    try:
        with _ur.build_opener(_ur.ProxyHandler({})).open(
                "http://127.0.0.1:5801/groups", timeout=15) as r:
            return jsonify(json.loads(r.read().decode("utf-8", "replace")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


@app.route("/api/relay/targets", methods=["POST"])
def api_relay_targets_change():
    """增删小号转发目标群：{action: 'add'|'remove', id: 群号}；新群订阅默认为空（转发全部）。"""
    body = request.get_json(silent=True) or {}
    gid = str(body.get("id") or "").strip()
    action = body.get("action") or ""
    if not gid.isdigit():
        return jsonify({"ok": False, "error": "群号无效"}), 400
    rt = _relay_targets(_relay_cfg())
    if action == "add":
        if not any(str(t["id"]) == gid for t in rt):
            rt.append({"id": int(gid), "feeds": []})
    elif action == "remove":
        rt = [t for t in rt if str(t["id"]) != gid]
    else:
        return jsonify({"ok": False, "error": "action 无效"}), 400
    _save_relay_targets(rt)
    return jsonify({"ok": True, "targets": rt})


@app.route("/api/relay/export")
def api_relay_export():
    """导出当前小号转发订阅：重新部署前把它同步回本地 relay.json，避免订阅被覆盖。"""
    return jsonify({"ok": True, "targets": _relay_targets(_relay_cfg()),
                    "config": _relay_cfg()})


@app.route("/api/relay/log")
def api_relay_log():
    arg = request.args.get("n") or "120"
    try:
        n = max(10, min(500, int(arg)))
    except ValueError:
        n = 120
    return jsonify({"ok": True, "lines": _relay_manager().tail_log(n)})


@app.route("/api/relay/qrcode")
def api_relay_qrcode():
    """小号扫码登录二维码：直接返回 NapCat 生成的 qrcode.png 图片。

    二维码约 2 分钟刷新一次（NapCat 自动重写文件）；超过 3 分钟未刷新
    视为已过期，返回 JSON 提示刷新页面重取。
    """
    import time as _time
    from flask import Response
    mgr = _relay_manager()
    data = b""
    mtime = None
    try:
        qp = mgr.qrcode_file
        if qp.exists():
            mtime = qp.stat().st_mtime
        # mtime 超过 90 秒视为过期空档：主动调 NapCat API 强制换新码再读
        if mtime is None or (_time.time() - mtime) > 90:
            if hasattr(mgr, "refresh_qrcode"):
                mgr.refresh_qrcode()
                _time.sleep(1.5)
        data = mgr.qrcode_bytes()
        if qp.exists():
            mtime = qp.stat().st_mtime
    except Exception:
        data = b""
    if data:
        fresh = mtime is None or (_time.time() - mtime) < 180
        resp = Response(data, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Qrcode-Fresh"] = "1" if fresh else "0"
        return resp
    return jsonify({"ok": False,
                    "error": "二维码尚未生成：等待协议端启动或重新打开本页面"}), 404


@app.route("/relay-qrcode")
def relay_qrcode_page():
    """小号扫码登录页：每 25 秒自动拉取最新二维码并倒计时，避免 2 分钟过期。"""
    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小号扫码登录 - tw2qq</title>
<style>
:root{color-scheme:light dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:#f5f6fa;color:#1f2329}
.card{background:#fff;border-radius:16px;padding:32px 28px;max-width:420px;width:92%;
  text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 6px}
p.sub{font-size:13px;color:#4e5969;margin:0 0 18px;line-height:1.6}
#qr{width:260px;height:260px;border-radius:8px;background:#eee;display:block;margin:0 auto}
#bar{height:5px;background:#e5e6eb;border-radius:3px;margin:16px auto 8px;width:260px;overflow:hidden}
#bar>div{height:100%;background:#4096ff;width:100%}
#st{font-size:13px;color:#4e5969;min-height:20px}
.ok{color:#00b42a!important;font-weight:600}
.err{color:#f53f3f!important}
@media(prefers-color-scheme:dark){body{background:#17171a;color:#e5e6eb}
 .card{background:#23232a}#bar{background:#3a3a3f}p.sub,#st{color:#a9aeb8}#qr{background:#333}}
</style>
</head>
<body>
<div class="card">
  <h1>QQ 小号扫码登录</h1>
  <p class="sub">用<b>小号（3961685949）登录的手机 QQ</b> 扫下方二维码并授权。<br>
  二维码约 2 分钟自动更换，本页每 25 秒自动刷新，无需手动操作。</p>
  <img id="qr" alt="二维码加载中...">
  <div id="bar"><div id="fill"></div></div>
  <div id="st">正在获取二维码…</div>
</div>
<script>
const img=document.getElementById('qr'),st=document.getElementById('st'),
      fill=document.getElementById('fill');
const REFRESH=25;let left=REFRESH,timer;
function load(){
  st.textContent='正在获取二维码…';st.className='';
  img.src='/api/relay/qrcode?t='+Date.now();
}
img.onload=()=>{left=REFRESH;st.textContent='请用手机 QQ 扫码（'+left+' 秒后自动刷新）';
  st.className='ok';tick();};
img.onerror=()=>{st.textContent='二维码暂未生成，10 秒后重试…';st.className='err';
  left=10;tick();};
function tick(){clearInterval(timer);timer=setInterval(()=>{
  left--;fill.style.width=(left/REFRESH*100)+'%';
  if(img.complete&&img.naturalWidth){st.textContent='请用手机 QQ 扫码（'+left+' 秒后自动刷新）';}
  if(left<=0)load();
},1000);}
load();
</script>
</body>
</html>"""
    from flask import Response
    return Response(html, mimetype="text/html; charset=utf-8")


# NapCat 4.x WebUI 前端 axios 无 baseURL，静态资源(/webui/*)与 API(/auth/*、
# /QQLogin/* 等顶层路径)都按同源绝对路径请求；全部反代到云端 NapCat 6099。
# 本应用自身 API 全部带 /api/ 前缀，与这些顶层路径不冲突。
_NAPCAT_TOP_PREFIXES = (
    "auth", "base", "webui", "Log", "Process", "WebUIConfig", "QQLogin",
    "OB11Config", "ShareConfig", "Setting", "SelfInfo", "Group", "Message",
    "NapCatStatus", "SystemStatus", "UpdateNapCat", "GetList", "Friend",
    "File", "Debug", "NodeInfo", "Device",
)
_NAPCAT_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]


def _napcat_relay_proxy(subpath):
    """把请求原样转发到云端 NapCat WebUI(6099)。转发前剥离平台参数
    （sharecode/_t），否则 NapCat 鉴权中间件的 req.url 精确匹配会失效。"""
    import urllib.request as _ur
    from urllib.parse import parse_qsl, urlencode
    from flask import Response
    url = "http://127.0.0.1:6099/" + subpath
    qs = request.query_string.decode("utf-8", "replace")
    if qs:
        keep = [(k, v) for k, v in parse_qsl(qs, keep_blank_values=True)
                if k not in ("sharecode", "_t", "_r")]
        if keep:
            url += "?" + urlencode(keep)
    data = request.get_data() if request.method in ("POST", "PUT") else None
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "connection", "cookie")}
    req = _ur.Request(url, data=data, headers=headers, method=request.method)
    try:
        with _ur.build_opener(_ur.ProxyHandler({})).open(req, timeout=30) as r:
            resp = Response(r.read(), status=r.status)
            for k, v in r.headers.items():
                if k.lower() in ("content-type", "location", "content-encoding"):
                    resp.headers[k] = v
            return resp
    except Exception as e:
        st = napcat_cloud.status()
        return jsonify({"ok": False,
                        "error": "NapCat WebUI 不可达: %s" % str(e)[:120],
                        "napcat_process": st.get("process"),
                        "last_error": st.get("last_error")}), 502


# /napcat/ 兼容入口（页面直开，6099 根路径即页面）
app.add_url_rule("/napcat/", defaults={"subpath": ""},
                 endpoint="napcat_webui_root", view_func=_napcat_relay_proxy,
                 methods=_NAPCAT_METHODS)
app.add_url_rule("/napcat/<path:subpath>", endpoint="napcat_webui_sub",
                 view_func=_napcat_relay_proxy, methods=_NAPCAT_METHODS)


def _make_pfx_handler(pfx):
    def handler(subpath):
        return _napcat_relay_proxy(pfx + ("/" + subpath if subpath else ""))
    handler.__name__ = "napcat_px_" + pfx
    return handler


# 前端实际请求的顶层路径
for _pfx in _NAPCAT_TOP_PREFIXES:
    _h = _make_pfx_handler(_pfx)
    app.add_url_rule("/%s" % _pfx, defaults={"subpath": ""},
                     endpoint="napcat_px_%s" % _pfx, view_func=_h,
                     methods=_NAPCAT_METHODS)
    app.add_url_rule("/%s/<path:subpath>" % _pfx,
                     endpoint="napcat_px_sub_%s" % _pfx, view_func=_h,
                     methods=_NAPCAT_METHODS)


@app.route("/api/<seg>/<path:rest>", methods=_NAPCAT_METHODS)
def napcat_api_fallback(seg, rest):
    """NapCat WebUI 前端把 API 打到 /api/<Pascal>/<Action>；本应用自有 API
    第二段全为小写，故仅当第二段是 PascalCase 或 auth 时转交云端 NapCat。"""
    if seg[0].isupper() or seg in ("auth", "base"):
        return _napcat_relay_proxy("api/%s/%s" % (seg, rest))
    from flask import abort
    abort(404)


@app.route("/api/health")
def api_health():
    """轻量健康端点：供外部监控 / 进程内自我保活使用。

    永远返回 200（保活探针只看可达性），健康结论放在 body.ok 里。
    watchdog 状态含 boot_id / uptime，可据此判断实例是否发生过重启。
    """
    # 只有进程之外的请求才算「24x7 外部唤醒」证据；self-ping 带 source=self
    if request.args.get("source") != "self":
        heartbeat.note_external()
    wd = heartbeat.state()
    try:
        bot_ok, bot_info = monitor.bot.get_status()
    except Exception as e:
        bot_ok, bot_info = False, str(e)[:120]
    cloud = clouddb.get()
    try:
        proxy_ok = bool(chromego.is_running)
    except Exception:
        proxy_ok = None
    # Linux 云主机上不用 ChromeGo（X 走 WARP/系统代理），只看代理端口是否真能连上，
    # 否则 health 的 proxy_ok 会一直误报 false。
    if not proxy_ok:
        _pxy = str(((monitor.config.get("network") or {}).get("proxy")) or "").strip()
        if _pxy:
            try:
                from urllib.parse import urlparse as _up
                _u = _up(_pxy)
                with socket.create_connection(
                        (_u.hostname or "127.0.0.1", _u.port or 80), timeout=2):
                    proxy_ok = True
            except Exception:
                proxy_ok = False
    age = wd.get("last_check_age")
    gha = wd.get("gha_fresh", False)   # GHA 主推时云端轮询会主动让位
    paused = bool(getattr(monitor, "manual_pause", False))
    ok = bool(bot_ok and (gha or paused or monitor.is_running)
              and (gha or paused or age is None or age < max(
                  600, int(monitor.config.get("poll_seconds") or 120) * 3)))
    from core.commands import official_command_stats
    return jsonify({
        "ok": ok, "service": "twitter2qq",
        "polling": monitor.is_running, "paused": paused,
        "last_check": monitor.last_check,
        "bot_ok": bot_ok, "bot_info": bot_info,
        "proxy_ok": proxy_ok,
        "cloud_ok": (cloud.ok if cloud else None),
        "at_commands": official_command_stats(monitor),
        "buttons": official_keyboard_state(monitor),
        "x_backoff": x_source.x_backoff_state(),
        "watchdog": wd,
        "external_heartbeat": {
            "pings": wd["external_pings"],
            "last_at": wd["last_external_at"],
            "gap_seconds": wd["external_gap_seconds"],
            "max_gap_seconds": wd["max_external_gap_seconds"],
        },
    })


@app.route("/api/tg/probe")
def api_tg_probe():
    """只读诊断：云端代理能否访问 t.me/s 预览页、HTML 结构能否解析、图片 CDN 能否下载。

    用法：/api/tg/probe?handle=telegram（省略 handle 时用公开频道 telegram 做冒烟测试）
    """
    handle = request.args.get("handle", "telegram").strip().lstrip("@") or "telegram"
    media = str(request.args.get("media", "1")).lower() not in ("0", "false", "no")
    try:
        return jsonify(tg_source.probe(handle, check_media=media))
    except Exception as e:
        logging.warning("TG 频道探针异常: %s", e)
        return jsonify({"ok": False, "handle": handle,
                        "error": f"{type(e).__name__}: {str(e)[:200]}"})


@app.route("/api/x/raw")
def api_x_raw():
    """只读诊断：真实 X 响应里的媒体结构 + 视频 CDN 可达性。

    用法：/api/x/raw?handle=xxx&probe=1（probe=0 只做结构分析，不下载）
    """
    handle = request.args.get("handle", "").strip().lstrip("@")
    if not handle:
        return jsonify({"ok": False, "error": "需要 handle 参数"}), 400
    cookie = ((monitor.config.get("x") or {}).get("auth_token") or "").strip()
    if not cookie:
        return jsonify({"ok": False, "error": "云端未配置 X auth_token"}), 200
    probe = str(request.args.get("probe", "1")).lower() not in ("0", "false", "no")
    try:
        return jsonify(x_source.diagnose_timeline(handle, cookie, probe=probe))
    except Exception as e:
        logging.warning("X 时间线诊断失败: %s", e)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "backoff": x_source.x_backoff_state()})


# ============ 博主管理（面板选博主） ============
@app.route("/api/x/preview")
def api_x_preview():
    handle = request.args.get("handle", "").strip().lstrip("@")
    if not handle:
        return jsonify({"ok": False, "error": "请输入博主用户名"}), 400
    cookie = (monitor.config.get("x") or {}).get("auth_token", "")
    try:
        tweets = x_source.fetch_user_tweets(handle, cookie)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    return jsonify({
        "ok": True, "handle": handle,
        "tweets": [
            {"id": t["id"], "text": t["text"][:150],
             "images": len(t.get("images", [])), "videos": len(t.get("videos", []))}
            for t in tweets[:5]
        ],
    })


@app.route("/api/feeds", methods=["GET", "POST", "DELETE"])
def api_feeds():
    """博主/频道管理。GET 返回 X + Telegram 合并列表；POST/DELETE 按 source 落到对应配置段。"""
    if request.method == "GET":
        return jsonify(iter_feeds(monitor.config))
    body = request.get_json(silent=True) or {}
    is_tg = (body.get("source") or "x").strip().lower() == "telegram"
    cfg = _disk_config()
    section, list_key = (("telegram", "channels") if is_tg
                         else ("twitter", "feeds"))
    feeds = list(((cfg.get(section) or {}).get(list_key)) or [])

    if request.method == "DELETE":
        # 前端只传 handle/url，不带 source：两个配置段都要查，否则删不掉 TG 频道
        key = body.get("key")
        x_list = list((cfg.get("twitter") or {}).get("feeds") or [])
        tg_list = list((cfg.get("telegram") or {}).get("channels") or [])
        def _keep(f):
            return (f.get("handle") or f.get("url")) != key
        if any(not _keep(f) for f in x_list + tg_list):
            _save_config_key({
                "twitter": {"feeds": [f for f in x_list if _keep(f)]},
                "telegram": {**(cfg.get("telegram") or {}),
                             "channels": [f for f in tg_list if _keep(f)]},
            })
            return jsonify({"ok": True, "feeds": iter_feeds(monitor.config)})
        return jsonify({"ok": False, "error": "未找到该博主/频道"}), 404

    feed = None
    if request.method == "POST":
        handle = (body.get("handle") or "").strip().lstrip("@")
        url = (body.get("url") or "").strip()
        if not handle and not url:
            return jsonify({"ok": False, "error": "需要 handle 或 url"}), 400
        if is_tg and not handle:
            return jsonify({"ok": False, "error": "Telegram 频道需要填写频道用户名"}), 400
        if is_tg:
            from core import tg_source
            handle = tg_source.clean_handle(handle)
        key = handle or url
        # 大小写不敏感去重（X handle 不区分大小写；TG 频道名同理）
        existing = next((f for f in feeds
                         if handle and (f.get("handle") or "").lower() == handle.lower()
                         or (not handle and (f.get("url") or "") == url)), None)
        if existing:
            return jsonify({"ok": False, "error": "该博主/频道已在关注列表中"}), 400
        new_groups = [str(g) for g in body.get("groups", [])]
        if not new_groups:
            logging.warning("添加 [%s] 时未选择任何群，将只记录不推送；可稍后在「推送目标」页绑定", key)
        feed = {"name": body.get("name") or handle or url, "groups": new_groups}
        if is_tg:
            feed["source"] = "telegram"
        if handle:
            feed["handle"] = handle
        else:
            feed["url"] = url
        feeds.append(feed)
    _save_config_key({section: {list_key: feeds}})

    # 新增源默认**不**自动回推历史：以前一添加博主就把近 24 小时推文刷进群。
    # 需要测试时由调用方显式传 backfill=true，或单独调用回测接口。
    backfill = False
    if request.method == "POST" and body.get("backfill") is True and feed:
        try:
            backfill = monitor.bot.get_status()[0]
        except Exception:
            backfill = False
        if backfill:
            threading.Thread(target=monitor.backfill_feed, args=(feed,),
                             daemon=True, name="backfill").start()
    return jsonify({"ok": True, "backfill": backfill,
                    "auto_backfill": False,
                    "hours": int(monitor.config.get("backfill_hours", 24)),
                    "feeds": iter_feeds(monitor.config)})


@app.route("/api/x/following")
def api_x_following():
    """读取当前 X 账号的关注列表（供博主管理页一键导入）"""
    cookie = (monitor.config.get("x") or {}).get("auth_token", "")
    if not cookie:
        return jsonify({"ok": False, "error": "请先在「X 账号」页验证并保存 auth_token"})
    try:
        users = x_source.fetch_following(cookie)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})
    existing = [_feed_key(f).lstrip("@").lower()
                for f in iter_feeds(monitor.config)]
    return jsonify({"ok": True, "users": users, "existing": existing})


# ============ 推送目标管理（按群聊配置各自关注的博主） ============
def _feed_key(f: dict) -> str:
    return f.get("handle") or f.get("url") or ""


# 配置读写统一收敛到 core.settings：
#   relay.json            = 随包发布的静态配置（端口、关键词、限速…）
#   data/relay_targets.json = 运行时订阅状态，不随包发布（重新部署不丢）
# relay.py 与 webui 都按同一优先级读取，不再出现「前端绑了、转发器读不到」。
RELAY_CFG_PATH = settings.RELAY_CFG_PATH


def _write_relay_cfg(rj: dict):
    settings.write_relay_cfg(rj)


def _relay_cfg() -> dict:
    return settings.read_relay_cfg()


def _relay_targets(rj: dict) -> list:
    return settings.normalize_targets(rj)


def _save_relay_targets(targets: list):
    """写回静态配置 + 运行时订阅状态，并通知转发器热重载（不重启进程）。"""
    rj = _relay_cfg()
    rj["target_groups"] = targets
    _write_relay_cfg(rj)
    try:
        import urllib.request as _ur
        req = _ur.Request("http://127.0.0.1:5801/reload", data=b"{}", method="POST",
                          headers={"Content-Type": "application/json"})
        with _ur.build_opener(_ur.ProxyHandler({})).open(req, timeout=5):
            pass
    except Exception as e:
        logging.warning("转发器热重载失败（下次重启生效）: %s", e)
    # /reload 可能读到另一份文件，再直接下发一次确保生效
    _push_relay_targets(retries=1, quiet=True)


@app.route("/api/feeds/backfill", methods=["POST"])
def api_feeds_backfill():
    """回测：只取该博主时间线**最新一条**推文试推一次。

    规则（2026-11-07 起）：
      * 默认 latest=true，只看最新 1 条——回测是为了验证链路，不是重刷历史；
      * 这条若已在去重库里（官方机器人先推过 / 上一轮已发），直接跳过不重发；
      * 不清空去重记录，不改水位线；
      * force=true 才允许重发已推过的，且受 PushGuard 冷却与条数上限约束。
    小号直投通道不依赖官方机器人，所以它在不在线不影响能否回测。
    """
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip().lstrip("@")
    feeds = iter_feeds(monitor.config)
    # 博主 handle 大小写不固定（BB_33750 vs bb_33750），一律不敏感匹配
    feed = next((f for f in feeds
                 if (f.get("handle") or f.get("url") or "").lstrip("@").lower()
                 == key.lower()), None)
    if not feed:
        return jsonify({"ok": False, "error": f"未找到博主: {key}"}), 400
    default_hours = int(monitor.config.get("backfill_hours", 24))
    try:
        hours = max(1, min(int(body.get("hours") or default_hours), 168))
    except (TypeError, ValueError):
        hours = default_hours
    force = bool(body.get("force"))
    latest_only = body.get("latest", True) is not False
    ok, err = False, ""
    try:
        ok = monitor.bot.get_status()[0]
    except Exception as e:
        err = str(e)[:150]
    try:
        relay_ok = bool(monitor._relay_targets(feed))
    except Exception:
        relay_ok = False
    ok = ok or relay_ok         # 小号直投在线即可回测
    if not ok and not err:
        err = "QQ 通道未在线（官方机器人与小号都不可用）"
    if ok:
        threading.Thread(target=monitor.backfill_feed,
                         args=(feed, hours, force, latest_only),
                         daemon=True, name="backfill").start()
    return jsonify({"ok": ok, "mode": "replay", "force": force,
                    "latest_only": latest_only, "via_relay": relay_ok,
                    "hours": hours, "cleared": 0,
                    "error": err if not ok else ""})


@app.route("/api/chats")
def api_chats():
    """群列表 + 每个群绑定的博主 + 全局默认群

    群列表有两个来源，必须分开判断（曾把两者混在一起导致误报「无法从 QQ 获取群列表」）：
      1) 官方机器人（开放平台 q.qq.com）：平台没有「拉群列表」的 API，
         get_group_list 只能返回 WS 收到过群消息后收集到的 group_openid，
         刚上线 / 群里没人说话时就是空——这不代表机器人离线；
      2) 小号 OneBot（relay，:3001）：get_group_list 是真实全量群列表。
    因此「在线」看 get_status()，「群列表」取两者并集，小号群自带真实群名。
    """
    feeds = iter_feeds(monitor.config)
    feed_list = [{"key": _feed_key(f), "name": f.get("name") or _feed_key(f),
                  "groups": [str(g) for g in (f.get("groups") or [])]}
                 for f in feeds]
    global_groups = [str(g) for g in (monitor.config["qq"].get("groups") or [])]

    bot_ok, bot_info, bot_error = False, "", ""
    try:
        bot_ok, bot_info = monitor.bot.get_status()
    except Exception as e:
        bot_error = f"{type(e).__name__}: {e}"[:180]
    try:
        gl = monitor.bot.get_group_list() or []
    except Exception as e:
        gl = []
        bot_error = bot_error or f"{type(e).__name__}: {e}"[:180]

    # 小号（relay）侧的真实群列表：群号 → 群名，用于命名与「在线」提示
    relay_groups, rnames = [], {}
    try:
        import urllib.request as _ur
        with _ur.build_opener(_ur.ProxyHandler({})).open(
                "http://127.0.0.1:5801/groups", timeout=6) as r:
            relay_groups = json.loads(r.read().decode("utf-8", "replace")).get("groups") or []
        for g in relay_groups:
            rnames[str(g.get("group_id"))] = g.get("group_name") or ""
    except Exception:
        pass

    chats, known = [], set()
    for g in gl:
        gid = str(g.get("group_id") or "")
        known.add(gid)
        chats.append({
            "id": gid, "name": g.get("group_name") or str(gid),
            "member_count": g.get("member_count"), "official": True,
            "is_global": gid in global_groups,
            "feeds": [{"key": fl["key"], "name": fl["name"]}
                      for fl in feed_list if gid in fl["groups"]],
        })
    # 配置里有但官方机器人还没收集到的群（openid 已配置，等价于已在群）也展示出来
    configured = set(global_groups)
    for fl in feed_list:
        configured.update(fl["groups"])
    for gid in sorted(configured - known):
        chats.append({
            "id": gid,
            "name": rnames.get(gid) or f"官方机器人推送群（{gid[:8]}…）",
            "member_count": None, "official": True,
            "is_global": gid in global_groups,
            "feeds": [{"key": fl["key"], "name": fl["name"]}
                      for fl in feed_list if gid in fl["groups"]],
        })
    # 小号转发目标（relay.json）：同样是可订阅博主的推送目标
    rt = _relay_targets(_relay_cfg())
    if rt:
        lower_map = {fl["key"].lower(): fl for fl in feed_list}
        have = {c["id"] for c in chats}
        for t in rt:
            gid = str(t["id"])
            if gid in have:      # 两边都在同一个群时避免重复卡片
                continue
            fed = []
            for k in t["feeds"]:
                fl = lower_map.get(k)
                fed.append({"key": fl["key"], "name": fl["name"]} if fl
                           else {"key": k, "name": k})
            chats.append({"id": gid, "relay": True,
                          "name": rnames.get(gid) or ("群 %s（小号转发）" % gid),
                          "member_count": None, "is_global": False, "feeds": fed})
    return jsonify({"ok": True, "bot_online": bool(bot_ok), "bot_info": bot_info,
                    "error": bot_error, "chats": chats, "feeds": feed_list,
                    "relay_online": bool(rnames),
                    "relay_group_count": len(relay_groups)})


@app.route("/api/chats/bind", methods=["POST"])
def api_chats_bind():
    """绑定/解绑：{group_id, key, bind}；key='__global__' 表示设为/取消全局默认群"""
    body = request.get_json(silent=True) or {}
    gid = str(body.get("group_id") or "").strip()
    key = str(body.get("key") or "").strip()
    bind = bool(body.get("bind", True))
    if not gid:
        return jsonify({"ok": False, "error": "group_id 无效"}), 400

    # 小号转发目标：订阅写在 relay.json，热重载转发器
    rt = _relay_targets(_relay_cfg())
    hit = next((t for t in rt if str(t["id"]) == gid), None)
    if hit is not None and key != "__global__":
        feeds = list(hit["feeds"])
        kl = key.lower()
        if bind and kl not in feeds:
            feeds.append(kl)
        elif not bind:
            feeds = [f for f in feeds if f != kl]
        hit["feeds"] = feeds
        _save_relay_targets(rt)
        logging.info("转发目标订阅更新: 群 %s %s %s", gid,
                     "绑定" if bind else "解绑", key)
        return jsonify({"ok": True, "relay": True})

    if key == "__global__":
        # 从磁盘重读最新全局群（内存可能滞后，直接用会洗掉其他全局群）
        try:
            with open(monitor.config_path, "r", encoding="utf-8") as f:
                disk_cfg = yaml.safe_load(f) or {}
            cur = [str(g) for g in ((disk_cfg.get("qq") or {}).get("groups") or [])]
        except Exception:
            cur = [str(g) for g in (monitor.config["qq"].get("groups") or [])]
        groups = [g for g in cur if g != gid]
        if bind:
            groups.append(gid)
        _save_config_key({"qq": {"groups": groups}})
    else:
        cfg = _disk_config()
        x_feeds = list((cfg.get("twitter") or {}).get("feeds") or [])
        tg_channels = list((cfg.get("telegram") or {}).get("channels") or [])
        for f in x_feeds + tg_channels:
            if _feed_key(f) == key:
                gs = [str(g) for g in (f.get("groups") or [])]
                if bind and gid not in gs:
                    gs.append(gid)
                elif not bind:
                    gs = [g for g in gs if g != gid]
                f["groups"] = gs
                break
        else:
            return jsonify({"ok": False, "error": "未找到该博主/频道"}), 404
        # telegram 段要带上原有 enabled/template，否则 yaml 重写会把它们洗掉
        _save_config_key({
            "twitter": {"feeds": x_feeds},
            "telegram": {**(cfg.get("telegram") or {}), "channels": tg_channels},
        })
    logging.info("推送目标更新: 群 %s %s %s", gid, "绑定" if bind else "解绑",
                 "全局默认" if key == "__global__" else key)
    return jsonify({"ok": True})


# ============ QQ 登录通道（NapCat 集成启动器） ============
@app.route("/api/qq/status")
def api_qq_status():
    d = napcat.status_info()
    # Linux/容器里 NapCat 走「反向 WS」模式（relay 监听 5802），并不监听
    # OneBot HTTP 3000，qq_login 的 30s 心跳探测因此必然失败，会把在线的小号
    # 报成 offline —— 控制台就会误显示「已掉线·等待重连」，还连带弹出
    # 「安卓外部模式」的误导提示。这里用真实的反向 WS 连接纠正状态。
    if d.get("status") != "online":
        try:
            need_qr = bool(_relay_manager().status().get("need_qrcode"))
            fw = {}
            try:
                import urllib.request as _ur
                with _ur.build_opener(_ur.ProxyHandler({})).open(
                        "http://127.0.0.1:5801/status", timeout=4) as r:
                    fw = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                fw = {}
            if fw.get("ws_connected") and not need_qr:
                d["status"] = "online"
                d["status_source"] = "reverse-ws"
                d["running"] = True
                d["hint"] = ""
        except Exception as e:
            logging.debug("qq status 状态纠正失败: %s", e)
    return jsonify(d)


@app.route("/api/qq/download", methods=["POST"])
def api_qq_download():
    result = napcat.download()
    return jsonify(result)


@app.route("/api/qq/start", methods=["POST"])
def api_qq_start():
    qq = str((request.get_json(silent=True) or {}).get("qq", "")).strip()
    return jsonify(napcat.start(qq))


@app.route("/api/qq/stop", methods=["POST"])
def api_qq_stop():
    napcat.stop()
    return jsonify({"ok": True})


# ============ 云控制台：薄客户端「查看 + 控制」专用接口 ============
# 电脑端 / 安卓端不再运行抓取、代理、QQ 通道；本地 console.py 只调这几个接口。
def _disk_config() -> dict:
    """从磁盘重读最新配置（实现收敛到 core.settings）。"""
    return settings.read_yaml(monitor.config_path) or dict(monitor.config or {})


def _feed_brief(feeds) -> list:
    return [{"key": _feed_key(f), "name": f.get("name") or _feed_key(f),
             "groups": [str(g) for g in (f.get("groups") or [])]}
            for f in feeds]


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        logging.debug("快照字段读取失败: %s", e)
        return default


@app.route("/api/console/snapshot")
def api_console_snapshot():
    """一次拿到面板所需的全部数据（状态 / 统计 / 记录 / 日志 / 配置 / 指令表）。"""
    cfg = monitor.config or {}
    official = (cfg.get("qq") or {}).get("official") or {}
    quick = {
        "poll_seconds": int(cfg.get("poll_seconds") or 0),
        "translate": bool(cfg.get("translate", True)),
        "send_images": bool(cfg.get("send_images", True)),
        "send_videos": bool(cfg.get("send_videos", True)),
        "at_all": bool(cfg.get("at_all", False)),
        "buttons": official.get("buttons", True) is not False,
        "x_configured": bool((cfg.get("x") or {}).get("auth_token")),
        "groups": [str(g) for g in ((cfg.get("qq") or {}).get("groups") or [])],
        "feeds": _safe(lambda: _feed_brief(iter_feeds(cfg)), []),
        "paused": bool(getattr(monitor, "manual_pause", False)),
    }
    return jsonify({
        "ok": True,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "health": _safe(lambda: api_health().get_json(), {}),
        "status": _safe(lambda: monitor.status(), {}),
        "stats": _safe(lambda: {"total": monitor.store.total_pushed(),
                                "by_day": monitor.store.push_stats(7)}, {}),
        "history": _safe(lambda: monitor.store.recent_history(12), []),
        "logs": _safe(lambda: monitor.ring.tail(25), []),
        "config": quick,
        "commands": COMMAND_CATALOG,
    })


@app.route("/api/commands")
def api_commands():
    """群指令速查表（控制台按钮 / 复制指令用，与 QQ 按钮面板同源）。"""
    return jsonify({
        "ok": True, "catalog": COMMAND_CATALOG,
        "panels": {"main": PANEL_MAIN, "more": PANEL_MORE,
                   "common": PANEL_COMMON},
    })


def _button_panel_test(group=None, text=None) -> dict:
    """向目标群主动发一条带按钮的面板消息，返回平台是否接受按钮。"""
    groups = [str(g) for g in
              ((monitor.config.get("qq") or {}).get("groups") or [])]
    gid = str(group or (groups[0] if groups else "")).strip()
    if not gid:
        return {"ok": False, "error": "未配置目标群 openid"}
    before = official_keyboard_state(monitor)
    try:
        monitor.bot.send_group_msg(
            gid,
            [{"type": "text", "data": {"text": text or BUTTON_TEXT}}],
            keyboard=panel_keyboard("more"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}",
                "group": gid[:12], "before": before,
                "state": official_keyboard_state(monitor)}
    return {"ok": True, "group": gid[:12], "before": before,
            "state": official_keyboard_state(monitor)}


def _media_test(body: dict) -> dict:
    """端到端媒体自检：抓一条含视频的推文 → 下载 → 转 GIF → 上传 QQ 富媒体。

    默认只做上传（srv_send_msg=False，不在群里发消息）；send=1 时额外把这条
    推文连同 GIF 发到第一个绑定群，供人工确认群里确实能看到动图。
    不清理去重记录、只处理 1 条，避免像全量回推那样刷屏。
    """
    import re
    from core.media import download_media, hls_url, video_to_gif

    cookie = ((monitor.config.get("x") or {}).get("auth_token") or "").strip()
    if not cookie:
        return {"ok": False, "error": "云端未配置 X auth_token"}
    feeds = (monitor.config.get("twitter") or {}).get("feeds") or []
    handle = str(body.get("handle") or "").strip().lstrip("@")
    feed = next((f for f in feeds
                 if (f.get("handle") or "").lstrip("@") == handle), None) if handle else None
    if not feed:
        feed = next((f for f in feeds if f.get("handle")), None)
    if not feed:
        return {"ok": False, "error": "配置里没有可用的 X 博主"}
    handle = feed["handle"]
    groups = feed.get("groups") or (monitor.config.get("qq") or {}).get("groups") or []
    gid = str(groups[0]) if groups else ""
    if not gid:
        return {"ok": False, "error": "未配置目标群 openid"}

    tweets = x_source.fetch_user_tweets(handle, cookie)
    target = next((t for t in tweets if t.get("videos")), None)
    if not target:
        return {"ok": False,
                "error": f"[{handle}] 最近 {len(tweets)} 条推文里没有视频/动图",
                "tweets": len(tweets)}
    vurl = target["videos"][0]
    media_dir = monitor.base_dir / "data" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w-]", "_", "mediatest_" + str(target.get("id") or ""))[-60:]
    mp4, gif = media_dir / (stem + ".mp4"), media_dir / (stem + ".gif")
    secs = int(monitor.config.get("video_gif_seconds", 10))
    if hls_url(vurl):
        video_to_gif(vurl, gif, max_seconds=secs)
    else:
        download_media(vurl, mp4)
        video_to_gif(mp4, gif, max_seconds=secs)

    out = {
        "ok": True, "handle": handle, "tweet_id": str(target.get("id") or ""),
        "video_url": vurl[:160], "gif_bytes": gif.stat().st_size,
        "gif_magic": gif.read_bytes()[:3].decode("ascii", "ignore"),
        "group": gid[:12], "send": bool(body.get("send")),
    }
    uploader = getattr(monitor.bot, "_upload_group_image", None)
    if not uploader:
        out["upload"] = {"ok": False, "error": "当前 QQ 通道不支持富媒体上传"}
        return out
    try:
        fi = uploader(gid, "file://" + str(gif))
        out["upload"] = {"ok": bool(fi), "has_file_info": bool(fi)}
    except Exception as e:
        out["upload"] = {"ok": False,
                         "error": f"{type(e).__name__}: {str(e)[:150]}"}
        return out
    if body.get("send"):
        text = (f"🧪 媒体自检｜{handle}\n{(target.get('text') or '')[:180]}\n"
                f"{target.get('link') or ''}")
        try:
            # 诊断推送也要走同一把锁，否则会和真实推文的图片交错串台
            with monitor._push_lock:
                ok_groups, failed = monitor.bot.send_post(
                    [gid], text, [], [str(mp4)], at_all=False)
            out["sent"] = {"ok": True, "groups": ok_groups,
                           "failed": len(failed or [])}
        except Exception as e:
            out["sent"] = {"ok": False,
                           "error": f"{type(e).__name__}: {str(e)[:150]}"}
    return out


@app.route("/api/at/buttons/test", methods=["POST"])
def api_at_buttons_test():
    body = request.get_json(silent=True) or {}
    return jsonify(_button_panel_test(body.get("group"), body.get("text")))


def _control(action: str, body: dict) -> dict:
    """薄客户端的所有写操作都收敛到这里，复用 WebUI 原有逻辑。"""
    if action in ("status", "snapshot"):
        return api_console_snapshot().get_json()

    if action == "check":
        if monitor.checking:
            return {"ok": False, "error": "已有检查在进行中"}

        def run():
            try:
                monitor.check_once(force=True)
            except Exception as e:
                logging.warning("控制台触发检查异常: %s", e)

        threading.Thread(target=run, daemon=True, name="console-check").start()
        return {"ok": True, "message": "已触发立即检查"}

    if action == "pause":
        monitor.manual_pause = True
        monitor.stop()
        return {"ok": True, "message": "已暂停自动推送"}

    if action == "resume":
        monitor.manual_pause = False
        monitor.start()
        return {"ok": True, "message": "已恢复自动推送"}

    if action == "interval":
        sec = int(body.get("value") or body.get("seconds") or 0)
        if not (30 <= sec <= 3600):
            return {"ok": False, "error": "间隔需在 30~3600 秒之间"}
        _save_config_key({"poll_seconds": sec})
        return {"ok": True, "message": f"抓取间隔已设为 {sec} 秒"}

    if action == "switch":
        key = str(body.get("key") or "")
        if key not in ("translate", "send_images", "send_videos", "at_all"):
            return {"ok": False, "error": "不支持的开关"}
        value = bool(body.get("value"))
        _save_config_key({key: value})
        return {"ok": True, "message": f"{key} 已设为 {'开' if value else '关'}"}

    if action == "buttons":
        on = bool(body.get("value", True))
        _save_config_key({"qq": {"official": {"buttons": on}}})
        return {"ok": True, "message": f"群消息按钮已{'开启' if on else '关闭'}"}

    if action == "relay_debug":
        # 云端现场诊断：探测 OneBot 端口、读监听表、看配置文件
        import urllib.request as _urq
        out = {"tcp_listen": [], "probes": {}, "ob_conf": None}
        try:  # /proc/net/tcp 监听表（hex 端口）
            for src in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(src, encoding="ascii", errors="replace") as f:
                    for ln in f.readlines()[1:]:
                        parts = ln.split()
                        if len(parts) > 3 and parts[3] == "0A":  # LISTEN
                            port = int(parts[1].split(":")[1], 16)
                            out["tcp_listen"].append(port)
            out["tcp_listen"] = sorted(set(out["tcp_listen"]))
        except Exception as e:
            out["tcp_listen"] = "err: %s" % e
        mgr = _relay_manager()
        ob = (mgr.cfg.get("onebot_url") or "http://127.0.0.1:3000").rstrip("/")
        for name, url, method in (
                ("ob_get_login_info", ob + "/get_login_info", "POST"),
                ("ob_get_login_info_get", ob + "/get_login_info", "GET"),
                ("ob_api_prefix", ob + "/api/get_login_info", "POST"),
                ("ob_root", ob + "/", "GET"),
                ("webui_6099", "http://127.0.0.1:6099/", "GET")):
            try:
                req = _urq.Request(
                    url, data=b"{}" if method == "POST" else None,
                    method=method,
                    headers={"Content-Type": "application/json"})
                with _urq.build_opener(
                        _urq.ProxyHandler({})).open(req, timeout=5) as r:
                    out["probes"][name] = {
                        "http": r.status, "body": r.read(300).decode("utf-8", "replace")}
            except _urq.HTTPError as e:
                body = b""
                try:
                    body = e.read(300)
                except Exception:
                    pass
                out["probes"][name] = {
                    "http": e.code,
                    "body": body.decode("utf-8", "replace")}
            except Exception as e:
                out["probes"][name] = {"error": "%s: %s" % (type(e).__name__, str(e)[:150])}
        try:
            cf = mgr.napcat_dir / "config" / ("onebot11_%s.json" % (mgr.qq or "0"))
            out["ob_conf"] = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else "(missing)"
        except Exception as e:
            out["ob_conf"] = "err: %s" % e
        # 端口绑定实测：NapCat 到底能不能绑这些端口
        import socket as _sk
        binds = {}
        for hp in (("127.0.0.1", 3001), ("127.0.0.1", 3000), ("0.0.0.0", 3001)):
            s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
            s.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
            try:
                s.bind(hp)
                binds["%s:%d" % hp] = "OK"
            except OSError as e:
                binds["%s:%d" % hp] = "%s errno=%s" % (type(e).__name__, e.errno)
            finally:
                s.close()
        out["bind_test"] = binds
        # 从云内直调 NapCat WebUI API（无公网网关剥头问题）拿 OB11 运行时状态
        try:
            import hashlib as _hl
            tok = mgr.webui_token
            h = _hl.sha256(("%s.napcat" % tok).encode()).hexdigest()
            rq = _urq.Request("http://127.0.0.1:6099/api/auth/login",
                              data=json.dumps({"hash": h, "totpCode": ""}).encode(),
                              method="POST", headers={"Content-Type": "application/json"})
            with _urq.build_opener(_urq.ProxyHandler({})).open(rq, timeout=5) as r:
                login = json.loads(r.read().decode("utf-8", "replace"))
            cred = ((login.get("data") or {}).get("Credential")) or ""
            out["webui_login_code"] = login.get("code")
            if cred:
                # 1) 读运行中的 OB11 配置（POST）
                try:
                    rq2 = _urq.Request(
                        "http://127.0.0.1:6099/api/OB11Config/GetConfig",
                        data=b"{}", method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer " + cred})
                    with _urq.build_opener(_urq.ProxyHandler({})).open(rq2, timeout=5) as r2:
                        g = json.loads(r2.read().decode("utf-8", "replace"))
                    out["ob_config_read_code"] = g.get("code")
                    cfg_obj = (g.get("data") or {})
                except Exception as e2:
                    out["ob_config_read_code"] = "err: %s" % str(e2)[:120]
                    cfg_obj = None
                # 2) 热重载：给运行中的 NapCat 注入反向WS 配置（发送通道）
                if isinstance(cfg_obj, dict) and isinstance(
                        cfg_obj.get("network"), dict):
                    try:
                        wcl = cfg_obj["network"].setdefault("websocketClients", [])
                        if not any(w.get("enable") and "5802" in str(w.get("url"))
                                   for w in wcl):
                            wcl.append({"name": "tw2qq-relay-ws", "enable": True,
                                        "url": "ws://127.0.0.1:5802",
                                        "messagePostFormat": "array",
                                        "reportSelfMessage": False,
                                        "reconnectInterval": 5000,
                                        "token": "",
                                        "debug": False,
                                        "heartInterval": 30000})
                        rq3 = _urq.Request(
                            "http://127.0.0.1:6099/api/OB11Config/SetConfig",
                            data=json.dumps({"config": json.dumps(cfg_obj)}).encode(),
                            method="POST",
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + cred})
                        with _urq.build_opener(_urq.ProxyHandler({})).open(rq3, timeout=8) as r3:
                            out["ob_setconfig"] = r3.read(200).decode("utf-8", "replace")
                    except Exception as e3:
                        out["ob_setconfig"] = "err: %s" % str(e3)[:150]
                for ep, m, body in (("QQLogin/CheckLoginStatus", "POST", b"{}"),):
                    try:
                        rq4 = _urq.Request(
                            "http://127.0.0.1:6099/api/" + ep,
                            data=body, method=m,
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + cred})
                        with _urq.build_opener(_urq.ProxyHandler({})).open(rq4, timeout=5) as r4:
                            out.setdefault("webui_api", {})[ep] = json.dumps(
                                json.loads(r4.read().decode("utf-8", "replace")),
                                ensure_ascii=False)[:600]
                    except Exception as e2:
                        out.setdefault("webui_api", {})[ep] = "err: %s" % str(e2)[:120]
        except Exception as e:
            out["webui_login_code"] = "err: %s" % str(e)[:150]
        return {"ok": True, "debug": out}

    if action == "relay_sendtest":
        # 让转发器向目标群发一条测试消息，验证「WS 发送 → 小号发言 → 目标群收到」
        import urllib.request as _ur
        try:
            with _ur.build_opener(_ur.ProxyHandler({})).open(
                    "http://127.0.0.1:5801/sendtest", timeout=30) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
            return {"ok": bool(out.get("ok")), "result": out,
                    "message": "测试消息已发送到目标群" if out.get("ok")
                    else "发送失败，看 /api/relay/log"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    if action == "relay_recover":
        # 手动补转：小号掉线期间官方机器人已推送、但小号没转发的推文。
        # 复用 relay.py 的 message_id 去重，重复调用不会刷屏。
        try:
            minutes = max(10, min(240, int(body.get("minutes") or 120)))
        except (TypeError, ValueError):
            minutes = 120
        try:
            since_ts = float(body.get("since_ts") or 0)
        except (TypeError, ValueError):
            since_ts = 0
        import urllib.request as _ur
        url = ("http://127.0.0.1:5801/recover?minutes=%d&since_ts=%d"
               % (minutes, int(since_ts)))
        try:
            with _ur.build_opener(_ur.ProxyHandler({})).open(url, timeout=40) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
            return {"ok": bool(out.get("ok")), "result": out,
                    "message": "已扫描来源群历史，漏掉的推文会自动补转"
                    if out.get("ok") else "补转失败，见 result.error"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    if action == "relay_restart":
        def _relay_restart():
            # 0) 清场：杀掉残留的 QQ/xvfb 孤儿进程（部署重启后可能未被跟踪，
            #    否则新实例快速登录会报「当前账号已登录,无法重复登录」）
            try:
                subprocess.run(["pkill", "-f", "xvfb-run"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["pkill", "-f", "opt/QQ"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logging.info("[relay] 已清理残留 QQ/xvfb 进程")
            except Exception as e:
                logging.warning("[relay] 清理残留进程失败: %s", e)
            time.sleep(2.0)
            # 0.5) 重新部署后 webui 是新进程、_relay_proc 为 None，但旧 relay.py 仍
            #      占着 5801/5802；不清理则下面 _port_open 判断会跳过启动，旧代码继续跑。
            global _relay_proc
            if _relay_proc is None and _port_open(5801):
                try:
                    subprocess.run(["pkill", "-f", "relay.py"], timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logging.info("[relay] 已清理部署前遗留的转发器进程")
                    time.sleep(1.0)
                except Exception as e:
                    logging.warning("[relay] 清理遗留转发器失败: %s", e)
            # 1) 重启 relay.py 转发器（让它重读 relay.json 的 onebot_url 等配置）
            try:
                if _relay_proc and _relay_proc.poll() is None:
                    _relay_proc.terminate()
                    _relay_proc.wait(timeout=6)
            except Exception:
                pass
            _relay_proc = None
            time.sleep(1.0)
            try:
                if not _port_open(5801):
                    _relay_proc = subprocess.Popen(
                        [sys.executable, str(Path(BASE) / "relay.py")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                    logging.info("[relay] 转发器已重启 pid=%s", _relay_proc.pid)
            except Exception as e:
                logging.warning("[relay] 转发器重启失败: %s", e)
            # 2) 重启协议端（NapCat 会重写 OneBot 配置再启动）
            try:
                _relay_manager().stop()
            except Exception:
                pass
            time.sleep(1.5)
            try:
                _relay_manager().start()
            except Exception as e:
                logging.warning("[relay] 协议端重启失败: %s", e)
        threading.Thread(target=_relay_restart, daemon=True).start()
        return {"ok": True, "message": "转发器+协议端正在重启；若快登失败请到 /relay-qrcode 重新扫码"}

    if action == "add":
        cfg = _disk_config()
        feeds = list((cfg.get("twitter") or {}).get("feeds") or [])
        handle = str(body.get("handle") or "").strip().lstrip("@")
        url = str(body.get("url") or "").strip()
        if not handle and not url:
            return {"ok": False, "error": "需要 handle 或 url"}
        key = handle or url
        for f in feeds:
            fk = (f.get("handle") or f.get("url") or "").lstrip("@").lower()
            if fk == key.lstrip("@").lower():
                return {"ok": False, "error": f"@{key} 已在推送列表中"}
        groups = [str(g) for g in (body.get("groups") or [])]
        if not groups:
            groups = [str(g) for g in ((cfg.get("qq") or {}).get("groups") or [])]
        feed = {"name": body.get("name") or key, "groups": groups}
        feed["handle" if handle else "url"] = key
        feeds.append(feed)
        _save_config_key({"twitter": {"feeds": feeds}})
        # 默认不自动回推历史（旧版 True 会让「添加博主」直接刷近 24 小时推文）
        backfill = bool(body.get("backfill", False)) and handle
        if backfill:
            try:
                backfill = monitor.bot.get_status()[0]
            except Exception:
                backfill = False
            if backfill:
                threading.Thread(target=monitor.backfill_feed, args=(feed,),
                                 daemon=True, name="console-backfill").start()
        return {"ok": True, "message": f"已添加 @{key}", "backfill": backfill,
                "feeds": _feed_brief(iter_feeds(monitor.config))}

    if action == "remove":
        cfg = _disk_config()
        feeds = list((cfg.get("twitter") or {}).get("feeds") or [])
        idx = body.get("index")
        if idx:
            i = int(idx)
            if not (1 <= i <= len(feeds)):
                return {"ok": False, "error": "序号超出范围"}
            target = feeds[i - 1]
            feeds = [f for f in feeds if f is not target]
        else:
            key = str(body.get("key") or body.get("handle") or "").strip().lstrip("@")
            if not key:
                return {"ok": False, "error": "需要 key 或 index"}
            kept = [f for f in feeds
                    if (f.get("handle") or f.get("url") or "").lstrip("@") != key]
            if len(kept) == len(feeds):
                return {"ok": False, "error": f"未找到博主: {key}"}
            feeds = kept
        _save_config_key({"twitter": {"feeds": feeds}})
        return {"ok": True, "message": "已移除（不会取关 X）",
                "feeds": _feed_brief(iter_feeds(monitor.config))}

    if action == "backfill":
        feeds = iter_feeds(monitor.config)
        idx = body.get("index")
        feed = None
        if idx:
            i = int(idx)
            if 1 <= i <= len(feeds):
                feed = feeds[i - 1]
        else:
            key = str(body.get("key") or "").strip().lstrip("@")
            feed = next((f for f in feeds
                         if _feed_key(f).lstrip("@") == key), None)
        if not feed:
            return {"ok": False, "error": "未找到该博主"}
        hours = max(1, min(int(body.get("hours") or 24), 168))
        force = bool(body.get("force"))
        # 不再 clear_feed()：回测只补漏，不抹掉去重水位线
        threading.Thread(target=monitor.backfill_feed,
                         args=(feed, hours, force),
                         daemon=True, name="console-backfill").start()
        return {"ok": True, "mode": "replay", "force": force,
                "message": f"正在补推近 {hours} 小时内漏掉的推文（不清空去重记录）",
                "cleared": 0}

    if action == "following":
        cookie = ((monitor.config.get("x") or {}).get("auth_token") or "").strip()
        if not cookie:
            return {"ok": False, "error": "云端未配置 X auth_token"}
        users = x_source.fetch_following(cookie)
        existing = [_feed_key(f).lstrip("@").lower()
                    for f in iter_feeds(monitor.config)]
        return {"ok": True, "users": users, "existing": existing}

    if action == "media_test":
        return _media_test(body)

    if action == "buttons_test":
        return _button_panel_test(body.get("group"), body.get("text"))

    if action == "self_test":
        h = getattr(getattr(monitor, "bot", None), "_at_handler", None)
        if not h:
            return {"ok": False, "error": "@指令处理器未挂载"}
        return {"ok": True, "text": h._dispatch("自检", {})}

    if action == "logs":
        n = max(1, min(int(body.get("n") or 50), 300))
        return {"ok": True, "lines": monitor.ring.tail(n)}

    return {"ok": False, "error": f"未知操作: {action}"}


@app.route("/api/control", methods=["POST"])
def api_control():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").strip().lower()
    try:
        return jsonify(_control(action, body))
    except Exception as e:
        logging.warning("控制台操作 %s 失败: %s", action, e)
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {str(e)[:160]}"})


if __name__ == "__main__":
    # 云端部署（发布为应用）：sandbox 注入 PORT 环境变量，须绑定 0.0.0.0
    port = int(os.environ.get("PORT", "5800"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print("=" * 50)
    print(f" twitter2qq Web 控制台:  http://{host}:{port}")
    print("=" * 50)
    app.run(host=host, port=port, debug=False)

# -*- coding: utf-8 -*-
"""NapCat 云端管理器 —— 在 Linux sandbox 里 rootless 安装并运行 QQ 小号协议端。

背景：Lagrange.OneBot v1 的官方签名服务器已永久下线（404），云端转发改用
NapCat（活跃维护、真实 QQNT 协议、风控更低）。参照 NapCat-Installer 的
rootless 流程：全部装到 $HOME/Napcat，不碰系统目录。

流程（全部自动）：
  1. 下载 linuxqq_3.2.34-53644_amd64.deb（腾讯 CDN）→ dpkg -x 到 ~/Napcat/opt/QQ
     （无 dpkg 时用纯标准库解析 deb：ar 头 + data.tar.xz）
  2. 下载 NapCat.Shell.zip（GitHub，多源+镜像+本机代理）→ 解包到 app_launcher/napcat/
  3. 写 loadNapCat.js、改 package.json main（QQ 启动即加载 NapCat）
  4. Xvfb 缺失时 apt-get 安装 xvfb xauth libnss3 libgbm1 libasound2（sandbox 已确认 root）
  5. 写 OneBot 配置（HTTP API 3000 + 事件上报 127.0.0.1:5801/onebot/event）
     与 WebUI 配置（6099，经 webui 反代 /napcat/ 供浏览器扫码登录）
  6. 启动：xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox -q <QQ号>，watchdog 30s
"""
from __future__ import annotations

import json
import logging
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

LOG = logging.getLogger("napcat-cloud")

QQ_DEB_URL = ("https://qqdl.gtimg.cn/qqfile/QQNT/9.9.36/beta/9ee04bef/"
              "linuxqq_3.2.34-53644_amd64.deb")
QQ_VERSION = "3.2.34-53644"
NAPCAT_ZIP_URL = ("https://github.com/NapNeko/NapCatQQ/releases/latest/"
                  "download/NapCat.Shell.zip")
APT_PKGS = "xvfb xauth libnss3 libgbm1 libasound2"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _download_urls(url: str) -> list:
    return [url,
            "https://ghfast.top/" + url,
            "https://gh-proxy.com/" + url]


class NapCatCloudManager:
    def __init__(self, base_dir, get_config):
        self.base = Path(base_dir)
        self.get_config = get_config
        self.proc = None
        self.lock = threading.RLock()
        self.last_error = ""
        self._stop = threading.Event()
        self._wd = None
        # 登录态健康：小号掉线时 NapCat 进程往往仍活着（卡在等待扫码），
        # 旧 watchdog 只看 proc.poll()，因此永远不会自愈，必须人工扫码。
        self.login = {}                  # 最近一次 CheckLoginStatus 结果
        self.offline_since = 0.0        # 掉线起始时间（0 = 在线）
        self.auto_recoveries = 0        # 自动快登重启次数
        self.last_recovery_at = 0.0     # 上次自动恢复时间（冷却用）
        self.recovery_cooldown = 600    # 两次自动重启最小间隔（秒）
        self.max_auto_recoveries = 5    # 每进程最多自动重启次数，防死循环

    # ---------- 配置 ----------
    @property
    def cfg(self) -> dict:
        return dict(self.get_config().get("relay") or {})

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.get("enabled")) and (c.get("engine") or "napcat") == "napcat"

    @property
    def qq(self) -> str:
        return str(self.cfg.get("qq") or "")

    @property
    def install_dir(self) -> Path:
        """rootless 安装根：$HOME/Napcat（与官方 installer 一致）。"""
        return Path(self.cfg.get("dir") or (Path.home() / "Napcat"))

    @property
    def qq_dir(self) -> Path:
        return self.install_dir / "opt" / "QQ"

    @property
    def napcat_dir(self) -> Path:
        return self.qq_dir / "resources" / "app" / "app_launcher" / "napcat"

    @property
    def qq_pkg(self) -> Path:
        return self.qq_dir / "resources" / "app" / "package.json"

    @property
    def log_file(self) -> Path:
        return self.base / "logs" / "napcat_cloud.log"

    @property
    def webui_token(self) -> str:
        return self.cfg.get("webui_token") or "tw2qq-relay"

    @property
    def qrcode_file(self) -> Path:
        """QQ 扫码登录二维码（NapCat 每次刷新二维码时覆盖该文件）。"""
        return self.napcat_dir / "cache" / "qrcode.png"

    def qrcode_bytes(self) -> bytes:
        """读取当前登录二维码 PNG；未生成或已失效返回空 bytes。"""
        try:
            p = self.qrcode_file
            if p.exists() and p.stat().st_size > 100:
                data = p.read_bytes()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    return data
        except Exception:
            pass
        return b""

    def _webui_cred(self) -> str:
        """云内直调 NapCat WebUI 取 Bearer 凭证（公网网关会剥 Authorization）。"""
        try:
            import hashlib as _hl
            body = json.dumps({
                "hash": _hl.sha256((self.webui_token + ".napcat").encode()).hexdigest(),
                "totpCode": "",
            }).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:6099/api/auth/login", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with OPENER.open(req, timeout=6) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("code") == 0:
                return str(((d.get("data") or {}).get("Credential")) or "")
        except Exception as e:
            LOG.debug("[napcat] WebUI 登录失败: %s", e)
        return ""

    def _webui_post(self, path: str, cred: str, timeout: float = 6) -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:6099/api/" + path, data=b"{}", method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cred})
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def login_state(self) -> dict:
        """读 NapCat 登录态；WebUI 未就绪/探测失败返回 {}（不判为离线）。

        ready 示例：{"isLogin": true, "isOffline": false,
                    "loginPhase": "ready", "qrLoginAccepted": false, ...}
        掉线时 isLogin=false / isOffline=true / loginPhase=wait_qr。
        """
        cred = self._webui_cred()
        if not cred:
            return {}
        try:
            d = self._webui_post("QQLogin/CheckLoginStatus", cred)
            if d.get("code") == 0:
                return dict(d.get("data") or {})
        except Exception as e:
            LOG.debug("[napcat] 登录态探测失败: %s", e)
        return {}

    @staticmethod
    def _login_ok(st: dict) -> bool:
        return bool(st.get("isLogin")) and not st.get("isOffline") and \
            str(st.get("loginPhase") or "ready") == "ready"

    def refresh_qrcode(self) -> str:
        """云端内部直调 NapCat WebUI API 强制刷新登录二维码。

        返回 qrcodeurl（空串=失败）。NapCat 只在检测到二维码过期后才
        自行重生成，主动调用可消除「扫码时图已过期」的空档。
        """
        cred = self._webui_cred()
        if not cred:
            return ""
        try:
            d = self._webui_post("QQLogin/RefreshQRcode", cred, timeout=8)
            if d.get("code") == 0:
                return str((d.get("data") or {}).get("qrcodeurl") or "")
        except Exception:
            pass
        return ""

    # ---------- 下载 ----------
    @staticmethod
    def _fetch(url: str, dest: Path, proxy: str = ""):
        handler = (urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                   if proxy else urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, headers={"User-Agent": "tw2qq/1.0"})
        with opener.open(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    def _download(self, url: str, dest: Path, min_bytes: int):
        if dest.exists() and dest.stat().st_size >= min_bytes:
            return
        proxy = self.cfg.get("http_proxy") or ""
        last = ""
        for i, u in enumerate(_download_urls(url)):
            use_proxy = proxy if (proxy and i == len(_download_urls(url)) - 1) else ""
            try:
                LOG.info("[napcat] 下载 (%d/%d): %s", i + 1, len(_download_urls(url)), u)
                self._fetch(u, dest, use_proxy)
                if dest.stat().st_size >= min_bytes:
                    return
                last = "文件不完整 (%d bytes)" % dest.stat().st_size
            except Exception as e:
                last = str(e)[:160]
                LOG.warning("[napcat] 下载失败: %s", last)
        dest.unlink(missing_ok=True)
        raise RuntimeError("下载失败: " + last)

    # ---------- 安装 ----------
    def ensure_installed(self) -> bool:
        if self._qq_ready() and self._napcat_ready() and shutil.which("Xvfb"):
            return True
        self.install_dir.mkdir(parents=True, exist_ok=True)
        if not self._qq_ready():
            self._install_qq()
        if not self._napcat_ready():
            self._install_napcat()
        self._patch_entry()
        if not shutil.which("Xvfb"):
            self._install_xvfb()
            if not shutil.which("Xvfb"):
                self.last_error = "Xvfb 不可用且 apt 安装失败（无头运行必需）"
                return False
        return True

    def _qq_ready(self) -> bool:
        try:
            if not self.qq_pkg.exists():
                return False
            return json.loads(self.qq_pkg.read_text(encoding="utf-8")).get("version") == QQ_VERSION
        except Exception:
            return False

    def _napcat_ready(self) -> bool:
        return (self.napcat_dir / "napcat.mjs").exists()

    def _install_qq(self):
        deb = self.install_dir / "QQ.deb"
        self._download(QQ_DEB_URL, deb, 100_000_000)
        LOG.info("[napcat] 解包 QQ (%d MB)...", deb.stat().st_size // 1000000)
        staging = self.install_dir / ".qq-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        self._extract_deb(deb, staging)
        src = staging / "opt" / "QQ"
        if not src.exists():
            raise RuntimeError("deb 里没有 /opt/QQ 结构")
        if self.qq_dir.exists():
            shutil.move(str(self.qq_dir), str(staging / "previous-QQ"))
        shutil.move(str(src), str(self.qq_dir))
        shutil.rmtree(staging, ignore_errors=True)
        deb.unlink(missing_ok=True)
        LOG.info("[napcat] QQ %s 安装完成 → %s", QQ_VERSION, self.qq_dir)

    @staticmethod
    def _extract_deb(deb: Path, dest: Path):
        """dpkg -x 优先；缺失时纯标准库解析 ar + data.tar.xz。"""
        if shutil.which("dpkg"):
            subprocess.run(["dpkg", "-x", str(deb), str(dest)], check=True)
            return
        LOG.info("[napcat] 无 dpkg，用标准库解析 deb...")
        with open(deb, "rb") as f:
            magic = f.read(8)
            if magic != b"!<arch>\n":
                raise RuntimeError("不是 deb(ar) 格式: %s" % magic)
            data_member = None
            while True:
                hdr = f.read(60)
                if len(hdr) < 60:
                    break
                name = hdr[:16].decode("ascii", "replace").strip()
                size = int(hdr[48:58].decode("ascii", "replace").strip())
                body = f.read(size)
                if size % 2:
                    f.read(1)
                if name.startswith("data.tar"):
                    data_member = (name, body)
            if not data_member:
                raise RuntimeError("deb 中未找到 data.tar*")
            name, body = data_member
            import io
            if name.endswith(".xz"):
                raw = lzma.decompress(body)
            elif name.endswith(".gz"):
                import gzip
                raw = gzip.decompress(body)
            else:
                raw = body  # 未压缩
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
                t.extractall(dest)

    def _install_napcat(self):
        z = self.install_dir / "NapCat.Shell.zip"
        prebuilt = self.base / "napcat_pkg" / "NapCat.Shell.zip"
        if (prebuilt.exists() and prebuilt.stat().st_size >= 10_000_000
                and not (z.exists() and z.stat().st_size >= 10_000_000)):
            shutil.copy(prebuilt, z)
            LOG.info("[napcat] 使用部署包内置 NapCat.Shell.zip（跳过 GitHub 下载）")
        self._download(NAPCAT_ZIP_URL, z, 10_000_000)
        LOG.info("[napcat] 解包 NapCat.Shell...")
        import zipfile
        with zipfile.ZipFile(z) as f:
            f.extractall(self.napcat_dir)
        z.unlink(missing_ok=True)
        if not self._napcat_ready():
            raise RuntimeError("NapCat.Shell 解包后未找到 napcat.mjs")
        LOG.info("[napcat] NapCat 安装完成 → %s", self.napcat_dir)

    def _patch_entry(self):
        app = self.qq_dir / "resources" / "app"
        loader = app / "loadNapCat.js"
        loader.write_text(
            "const path = require('node:path');\n"
            "const { pathToFileURL } = require('node:url');\n"
            "import(pathToFileURL(path.join(__dirname, 'app_launcher/napcat/napcat.mjs')).href);\n",
            encoding="utf-8")
        pkg = json.loads(self.qq_pkg.read_text(encoding="utf-8"))
        if pkg.get("main") != "./loadNapCat.js":
            pkg["main"] = "./loadNapCat.js"
            self.qq_pkg.write_text(json.dumps(pkg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        LOG.info("[napcat] 入口已 patch（package.json main → loadNapCat.js）")

    def _install_xvfb(self):
        LOG.info("[napcat] 安装无头运行依赖: apt-get install %s", APT_PKGS)
        for cmd in (["apt-get", "update", "-y"], ["apt-get", "install", "-y"] + APT_PKGS.split()):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if r.returncode:
                    LOG.warning("[napcat] %s 退出码 %s: %s",
                                cmd[0:2], r.returncode, (r.stderr or "")[-300:])
            except Exception as e:
                LOG.warning("[napcat] apt 异常: %s", e)
        if shutil.which("Xvfb"):
            LOG.info("[napcat] Xvfb 就绪")
        else:
            self.last_error = ("apt 安装 xvfb 失败（详见日志）。可手动安装：apt-get install -y "
                               + APT_PKGS)

    # ---------- 配置 ----------
    def write_onebot_config(self):
        relay = self.cfg
        ob = urlparse(relay.get("onebot_url") or "http://127.0.0.1:3000")
        rp = urlparse(relay.get("report_url") or "http://127.0.0.1:5801/onebot/event")
        conf_dir = self.napcat_dir / "config"
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf = {
            "network": {
                "httpServers": [{"name": "tw2qq-relay", "enable": True,
                                 "host": ob.hostname or "127.0.0.1",
                                 "port": ob.port or 3000, "enableCors": False,
                                 "enableWebsocket": False,
                                 "messagePostFormat": "array",
                                 "token": relay.get("onebot_token") or "",
                                 "debug": False}],
                "httpClients": [{"name": "tw2qq-relay", "enable": True,
                                 "url": "http://%s:%s%s" % (rp.hostname, rp.port,
                                                            rp.path or "/onebot/event"),
                                 "messagePostFormat": "array",
                                 "reportSelfMessage": False,
                                 "token": relay.get("onebot_token") or ""}],
                # 反向 WS：NapCat 主动连入转发器(5802)发送动作。
                # 云端 sandbox 里 OneBot HTTP 服务端绑不上端口（原因未明），
                # 发送通道改走反向 WS，HTTP 服务端保留作为本地模式兜底。
                "websocketClients": [{"name": "tw2qq-relay-ws", "enable": True,
                                      "url": "ws://127.0.0.1:%d" % int(
                                          relay.get("ws_port") or 5802),
                                      "messagePostFormat": "array",
                                      "reportSelfMessage": False,
                                      "reconnectInterval": 5000,
                                      "token": relay.get("onebot_token") or "",
                                      "debug": False,
                                      "heartInterval": 30000}],
            },
            "http": {"enable": True, "host": "127.0.0.1", "port": ob.port or 3000,
                     "secret": relay.get("onebot_token") or "", "enableCors": False},
            "musicSignUrl": "", "reportSelfMessage": False,
            "token": relay.get("onebot_token") or "",
        }
        (conf_dir / ("onebot11_%s.json" % (self.qq or "0"))).write_text(
            json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        webui = {"port": 6099, "host": "0.0.0.0", "enable": True,
                 "token": self.webui_token, "login": True}
        (conf_dir / "webui.json").write_text(
            json.dumps(webui, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("[napcat] OneBot(HTTP %s → 上报 %s) 与 WebUI(6099) 配置已写入",
                 ob.port, rp.port)

    # ---------- 启停 ----------
    def start(self) -> dict:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return {"ok": True, "already": True, "pid": self.proc.pid}
            if not self.qq:
                self.last_error = "config.yaml relay.qq 未配置"
                return {"ok": False, "error": self.last_error}
            if os.name != "posix":
                self.last_error = "云端 NapCat 仅支持 Linux（本地 Windows 请用 NapCat.Shell + relay.py）"
                return {"ok": False, "error": self.last_error}
            try:
                if not self.ensure_installed():
                    return {"ok": False, "error": self.last_error}
            except Exception as e:
                self.last_error = "安装失败: %s" % str(e)[:220]
                LOG.exception("[napcat] 安装异常")
                return {"ok": False, "error": self.last_error}
            self.write_onebot_config()
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            qq_bin = self.qq_dir / "qq"
            env = dict(os.environ)
            env.setdefault("NAPCAT_PATH", str(self.napcat_dir))
            logf = open(self.log_file, "ab", buffering=0)
            cmd = ["xvfb-run", "-a", str(qq_bin), "--no-sandbox", "-q", self.qq]
            self.proc = subprocess.Popen(cmd, cwd=str(self.install_dir), env=env,
                                         stdout=logf, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
            self._start_watchdog()
            LOG.info("[napcat] 已启动 pid=%s qq=%s（首次需在 /napcat/ 页面扫码，"
                     "token=%s）", self.proc.pid, self.qq, self.webui_token)
            return {"ok": True, "pid": self.proc.pid, "webui": "/napcat/",
                    "webui_token": self.webui_token}

    def stop(self):
        self._stop.set()
        with self.lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _start_watchdog(self):
        if self._wd and self._wd.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self._watchdog, daemon=True, name="napcat-cloud-wd")
        self._wd = t
        t.start()

    def restart(self, reason: str = "") -> dict:
        """登录态掉线时重启协议端尝试快登。

        不调用 stop()：stop() 会 set _stop，正在跑的 watchdog 线程下一轮就
        退出，而 _start_watchdog() 见旧线程还活着又直接 return，导致看门狗
        永久失效。清孤儿 QQ/xvfb 是必须的，否则新实例快登会报「当前账号已
        登录，无法重复登录」并卡死在等待扫码。
        """
        with self.lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if os.name == "posix":
            for pat in ("xvfb-run", "opt/QQ/qq"):
                try:
                    subprocess.run(["pkill", "-f", pat], timeout=8,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            time.sleep(2)
        LOG.warning("[napcat] 自动快登重启（原因: %s）", reason or "login-offline")
        return self.start()

    def _watchdog(self):
        fails = 0
        offline_ticks = 0
        webui_blind = 0
        while not self._stop.is_set():
            self._stop.wait(30)
            try:
                if not self.enabled:
                    continue
                p = self.proc
                if not p or p.poll() is not None:
                    fails += 1
                    if fails <= 5:
                        LOG.warning("[napcat] 进程退出（code=%s，第 %d 次），30s 后自动重启",
                                    p.poll() if p else None, fails)
                        time.sleep(30)
                        self.start()
                    else:
                        LOG.error("[napcat] 连续退出 %d 次，暂停自动重启（查看 /api/relay/log）", fails)
                    continue
                fails = 0
                # 进程活着 ≠ 登录在线。20:32 事故里 NapCat 进程一直 running、
                # 卡在等待扫码，旧 watchdog 完全没动作，只能人工扫码才恢复。
                st = self.login_state()
                if not st:
                    # WebUI 未就绪：冷启动留足 10 分钟宽限；若持续不可达
                    # 说明 NapCat 已卡死，也尝试重启一次（受冷却与次数限制）。
                    webui_blind += 1
                    if (webui_blind >= 20
                            and time.time() - self.last_recovery_at > self.recovery_cooldown
                            and self.auto_recoveries < self.max_auto_recoveries):
                        self.auto_recoveries += 1
                        self.last_recovery_at = time.time()
                        webui_blind = 0
                        self.restart("WebUI 登录态持续不可达 10 分钟")
                    continue
                webui_blind = 0
                self.login = st
                if self._login_ok(st):
                    if self.offline_since:
                        LOG.info("[napcat] 登录态恢复（离线 %.0f 秒）",
                                 time.time() - self.offline_since)
                        self.offline_since = 0.0
                    offline_ticks = 0
                    continue
                if not self.offline_since:
                    self.offline_since = time.time()
                offline_ticks += 1
                LOG.warning("[napcat] 登录态离线（第 %d 次, phase=%s, 已 %.0fs）",
                            offline_ticks, st.get("loginPhase"),
                            time.time() - self.offline_since)
                # 用户已扫码、只等手机端确认时绝不重启，否则会打断登录。
                if st.get("qrLoginAccepted") or str(st.get("loginPhase") or "") in (
                        "confirm", "qr_scanned", "wait_confirm"):
                    LOG.info("[napcat] 二维码已扫描，等待用户确认，暂不重启")
                    continue
                now = time.time()
                if (offline_ticks >= 3
                        and now - self.last_recovery_at > self.recovery_cooldown
                        and self.auto_recoveries < self.max_auto_recoveries):
                    self.auto_recoveries += 1
                    self.last_recovery_at = now
                    offline_ticks = 0
                    self.restart("登录态离线 90s 未恢复")
            except Exception as e:
                LOG.warning("[napcat] watchdog 异常: %s", e)

    # ---------- 状态 ----------
    def probe(self, timeout: float = 4) -> bool:
        try:
            base = (self.cfg.get("onebot_url") or "http://127.0.0.1:3000").rstrip("/")
            req = urllib.request.Request(base + "/get_login_info", data=b"{}",
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            with OPENER.open(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return bool((d.get("data") or {}).get("user_id"))
        except Exception:
            return False

    def tail_log(self, n: int = 120) -> list:
        try:
            lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-max(10, min(500, n)):]
        except Exception as e:
            return ["(暂无日志: %s)" % e]

    def status(self) -> dict:
        p = self.proc
        proc_state = ("running" if (p and p.poll() is None)
                      else ("exited(code=%s)" % p.poll() if p else "stopped"))
        held = self.lock.acquire(blocking=False)
        if held:
            self.lock.release()
        elif proc_state == "stopped":
            proc_state = "preparing(installing)"
        # OneBot HTTP 3001 在云端 sandbox 偶尔绑不上（实际发送走反向 WS），
        # 所以有登录态缓存时以 WebUI 为准；仅在缓存还没建立（冷启动）时用
        # probe() 兜底。明确离线时绝不能被 probe() 判成在线。
        login_ok = self._login_ok(self.login)
        online = login_ok if self.login else self.probe()
        return {"engine": "napcat", "enabled": self.enabled, "qq": self.qq,
                "qq_ready": self._qq_ready(), "napcat_ready": self._napcat_ready(),
                "xvfb": bool(shutil.which("Xvfb")),
                "installed": self._qq_ready() and self._napcat_ready(),
                "process": proc_state, "pid": p.pid if p else None,
                "online": bool(online),
                "login_state": dict(self.login),
                "need_qrcode": bool(self.login) and not login_ok
                               and not self.login.get("qrLoginAccepted"),
                "offline_since": self.offline_since,
                "auto_recoveries": self.auto_recoveries,
                "last_recovery_at": self.last_recovery_at,
                "install_dir": str(self.install_dir),
                "last_error": self.last_error}

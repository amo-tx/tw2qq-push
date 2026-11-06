# -*- coding: utf-8 -*-
"""Lagrange.OneBot 进程管理 —— 云端 Linux 无头跑 QQ 小号协议端（转发链）。

背景：QQ 官方机器人进不了没有管理员的群；转发用的小号需要一个 OneBot v11
协议端。Lagrange.OneBot 是自包含 .NET 单文件（无需 QQ 客户端 / 系统依赖，
约 38MB），是受限 sandbox 里唯一能无头跑的方案。

说明：Lagrange.OneBot(v1) 2025-10 起停止积极维护，nightly 资产仍可用；
协议失效时本模块会在日志里给出明确错误，转发链自动降级为不可用。

职责：
  - 首次启动自动从 GitHub（含镜像/本机代理）下载并解包 linux-x64 包
  - 生成 appsettings.json：HTTP API(3000) + 事件上报到 relay.py(5801)
  - 拉起 / 看门狗 / 日志尾部（扫码二维码就在日志里，经 /api/relay/log 展示）
  - 登录态持久化在 lagrange/data/keystore.json，进程重启自动快登
"""
from __future__ import annotations

import hashlib
import json
import logging
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

LOG = logging.getLogger("lagrange")

# nightly linux-x64 SelfContained（2025-08 构建，Lagrange.OneBot v1 最终版）
_ASSET_URL = ("https://github.com/LagrangeDev/Lagrange.Core/releases/download/"
              "nightly/Lagrange.OneBot_linux-x64_net9.0_SelfContained.tar.gz")
_ASSET_SHA256 = "c6d7704e58a5e6b7a57a0ad2d21786a168bf60a435951b5137320c8bcb4dad3c"
_ASSET_MIN_BYTES = 30_000_000

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机探测不走代理


def _download_urls(extra_proxy: str = "") -> list:
    """多源下载：直连 → 镜像 → 本机代理（云端 mihomo 7890 可出境）。"""
    urls = [_ASSET_URL,
            "https://ghfast.top/" + _ASSET_URL,
            "https://gh-proxy.com/" + _ASSET_URL]
    if extra_proxy:
        urls.append(_ASSET_URL)  # 最后由 _fetch 带 proxy 重试直连源
    return urls


class LagrangeManager:
    def __init__(self, base_dir, get_config):
        self.base = Path(base_dir)
        self.get_config = get_config
        self.proc = None
        self.lock = threading.RLock()
        self.last_error = ""
        self._stop = threading.Event()
        self._wd = None

    # ---------- 配置 ----------
    @property
    def cfg(self) -> dict:
        return dict(self.get_config().get("relay") or {})

    @property
    def enabled(self) -> bool:
        c = self.cfg
        # engine=napcat 时 lagrange 完全让位（否则其 watchdog 会反复重启失败进程）
        return bool(c.get("enabled")) and (c.get("engine") or "napcat") == "lagrange"

    @property
    def qq(self) -> str:
        return str(self.cfg.get("qq") or "")

    @property
    def dir(self) -> Path:
        d = self.base / (self.cfg.get("dir") or "lagrange")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def data_dir(self) -> Path:
        d = self.dir / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_file(self) -> Path:
        return self.base / "logs" / "lagrange.log"

    # ---------- 安装 ----------
    def ensure_installed(self) -> bool:
        if self._find_bin():
            return True
        dl = self.dir / "lagrange.tar.gz"
        proxy = self.cfg.get("http_proxy") or ""
        ok = False
        for attempt, url in enumerate(_download_urls(proxy)):
            try:
                LOG.info("[lagrange] 下载协议端 (%d/%d): %s",
                         attempt + 1, len(_download_urls(proxy)), url)
                self._fetch(url, dl, proxy if attempt == len(_download_urls(proxy)) - 1 and proxy else "")
                ok = True
                break
            except Exception as e:
                LOG.warning("[lagrange] 下载失败: %s", str(e)[:160])
        if not ok:
            self.last_error = "Lagrange 所有下载源均失败（见上方日志）"
            return False
        size = dl.stat().st_size
        if size < _ASSET_MIN_BYTES:
            self.last_error = "下载不完整 (%d bytes)" % size
            dl.unlink(missing_ok=True)
            return False
        digest = hashlib.sha256(dl.read_bytes()).hexdigest()
        if digest != _ASSET_SHA256:
            LOG.warning("[lagrange] sha256 与记录不符（资产可能已更新）: %s", digest)
        LOG.info("[lagrange] 解包 (%d bytes)...", size)
        with tarfile.open(dl, "r:gz") as t:
            t.extractall(self.dir)
        dl.unlink(missing_ok=True)
        bin_path = self._find_bin()
        if not bin_path:
            self.last_error = "解包完成但未找到 Lagrange.OneBot 可执行文件"
            return False
        bin_path.chmod(0o755)
        LOG.info("[lagrange] 就绪: %s", bin_path)
        return True

    def _find_bin(self):
        hits = [p for p in self.dir.rglob("Lagrange.OneBot") if p.is_file()]
        return hits[0] if hits else None

    @property
    def bin(self):
        return self._find_bin()

    @staticmethod
    def _fetch(url: str, dest: Path, proxy: str = ""):
        handler = (urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                   if proxy else urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, headers={"User-Agent": "tw2qq/1.0"})
        with opener.open(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    # ---------- 配置文件 ----------
    def write_config(self):
        relay = self.cfg
        ob = urlparse(relay.get("onebot_url") or "http://127.0.0.1:3000")
        rp = urlparse(relay.get("report_url") or "http://127.0.0.1:5801/onebot/event")
        token = relay.get("onebot_token") or ""
        conf = {
            "Logging": {"LogLevel": {"Default": "Information", "Microsoft": "Warning"}},
            "SignServerUrl": relay.get("sign_server") or "",
            "SignProxyUrl": "",
            "Account": {"Uin": int(self.qq or 0), "Protocol": "Linux",
                        "AutoReconnect": True, "AutoReLogin": True,
                        "GetOptimumServer": True},
            "Message": {"IgnoreSelf": True, "StringPost": False},
            "QrCode": {"ConsoleCompatibilityMode": True},
            "ConfigPath": {"DeviceInfo": "data/device.json",
                           "Keystore": "data/keystore.json",
                           "Database": "data/lagrange-db"},
            "Implementations": [
                {"Type": "Http", "Host": ob.hostname or "127.0.0.1",
                 "Port": ob.port or 3000, "AccessToken": token},
                {"Type": "HttpPost", "Host": "%s://%s" % (rp.scheme, rp.hostname),
                 "Port": rp.port or 80, "Suffix": rp.path or "/onebot/event",
                 "HeartBeatEnable": True, "HeartBeatInterval": 30000,
                 "AccessToken": token},
            ],
        }
        (self.dir / "appsettings.json").write_text(
            json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("[lagrange] appsettings.json 已生成 (Uin=%s, Http:%s, 上报:%s)",
                 self.qq, ob.port, rp.port)

    # ---------- 启停 ----------
    def start(self) -> dict:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return {"ok": True, "already": True, "pid": self.proc.pid}
            if not self.qq:
                self.last_error = "config.yaml relay.qq 未配置"
                return {"ok": False, "error": self.last_error}
            try:
                if not self.ensure_installed():
                    return {"ok": False, "error": self.last_error}
            except Exception as e:
                self.last_error = "安装失败: %s" % str(e)[:200]
                LOG.exception("[lagrange] 安装异常")
                return {"ok": False, "error": self.last_error}
            self.write_config()
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            # .NET HttpClient 的系统代理用于 GitHub（自动更新/签名），本机回环绕开
            proxy = self.cfg.get("http_proxy") or "http://127.0.0.1:7890"
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[k] = proxy
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
            logf = open(self.log_file, "ab", buffering=0)
            self.proc = subprocess.Popen(
                [str(self.bin)], cwd=str(self.dir), env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            self._start_watchdog()
            LOG.info("[lagrange] 已启动 pid=%s qq=%s（登录态：%s）",
                     self.proc.pid, self.qq,
                     "已有 keystore，自动快登" if self.has_keystore else "需要扫码")
            return {"ok": True, "pid": self.proc.pid,
                    "need_qr": not self.has_keystore}

    def stop(self):
        self._stop.set()
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

    def _start_watchdog(self):
        if self._wd and self._wd.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self._watchdog, daemon=True, name="lagrange-wd")
        self._wd = t
        t.start()

    def _watchdog(self):
        while not self._stop.is_set():
            self._stop.wait(30)
            try:
                if not self.enabled:
                    continue
                p = self.proc
                if p and p.poll() is None:
                    continue  # 活着（是否已登录由 probe/status 反映，扫码期不重启）
                LOG.warning("[lagrange] 进程退出（code=%s），30s 后自动重启",
                            p.poll() if p else None)
                time.sleep(30)
                self.start()
            except Exception as e:
                LOG.warning("[lagrange] watchdog 异常: %s", e)

    # ---------- 状态 ----------
    @property
    def has_keystore(self) -> bool:
        return (self.data_dir / "keystore.json").exists()

    def probe(self, timeout: float = 4) -> bool:
        """OneBot HTTP 是否在线且已登录（get_login_info 有 user_id）。"""
        try:
            relay = self.cfg
            base = (relay.get("onebot_url") or "http://127.0.0.1:3000").rstrip("/")
            req = urllib.request.Request(
                base + "/get_login_info", data=b"{}", method="POST",
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

    def env_summary(self) -> dict:
        out = {"python": sys.version.split()[0],
               "euid": getattr(os, "geteuid", lambda: -1)(),
               "bin_exists": bool(self._find_bin())}
        try:
            mu = shutil.disk_usage(self.base)
            out["disk_free_mb"] = int(mu.free / 1e6)
        except Exception:
            pass
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable"):
                        out["mem_available_mb"] = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass
        return out

    def status(self) -> dict:
        # 不持锁：boot 线程下载协议端期间会长时间持有 self.lock，
        # status 若等锁会把 /api/relay/status 卡到超时。读 proc 引用是原子的。
        p = self.proc
        proc_state = ("running" if (p and p.poll() is None)
                      else ("exited(code=%s)" % p.poll() if p else "stopped"))
        held = self.lock.acquire(blocking=False)
        if held:
            self.lock.release()
        if not held:
            proc_state = "preparing(downloading)" if proc_state == "stopped" else proc_state
        return {"enabled": self.enabled, "qq": self.qq,
                "installed": bool(self._find_bin()), "process": proc_state,
                "pid": p.pid if p else None, "online": self.probe(),
                "keystore": self.has_keystore, "last_error": self.last_error,
                "env": self.env_summary()}

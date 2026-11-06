# -*- coding: utf-8 -*-
"""ChromeGo 内核集成：在程序内直接启动/停止代理内核

- 流量隔离：代理只写入本进程环境变量（net.apply_proxy），绝不修改 Windows
  系统代理，程序外的网络环境完全不受影响；QQ 群转发(NapCat 127.0.0.1)
  由 NO_PROXY 排除，始终直连。
- 启动命令与 ChromeGo 官方 .cmd 脚本一致（已逐一核对）。
"""
import atexit
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import net
from core.platform import IS_ANDROID as _IS_ANDROID

# 与 ChromeGo 翻墙.cmd 逐一对应的内核启动定义
# scheme: requests 代理协议；port: 本地监听端口
# 安卓端(APK)：assets 内置 linux-arm64 内核，随程序文件释放到容器 /root/tw2qq/chromego
if _IS_ANDROID:
    KERNELS = {
        "clash.meta": {"label": "Clash.Meta（推荐，混合端口）", "dir": "clash.meta",
                       "exe": "mihomo-linux-arm64", "args": ["-d", "{dir}"],
                       "port": 7890, "scheme": "http"},
    }
elif sys.platform.startswith("linux"):
    # 云端/Linux 服务器：仅 clash.meta（mihomo linux-amd64 内核随部署包携带）
    KERNELS = {
        "clash.meta": {"label": "Clash.Meta（推荐，混合端口）", "dir": "clash.meta",
                       "exe": "mihomo-linux-amd64", "args": ["-d", "{dir}"],
                       "port": 7890, "scheme": "http"},
    }
else:
    KERNELS = {
        "clash.meta": {"label": "Clash.Meta（推荐，混合端口）", "dir": "clash.meta",
                       "exe": "clash.meta-windows-386.exe", "args": ["-d", "{dir}"],
                       "port": 7890, "scheme": "http"},
        "xray": {"label": "Xray", "dir": "Xray", "exe": "xray.exe",
                 "args": ["-c", "{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "sing-box": {"label": "Sing-Box", "dir": "singbox", "exe": "sing-box.exe",
                     "args": ["run", "-c", "{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "hysteria": {"label": "Hysteria", "dir": "hysteria",
                     "exe": "hysteria-tun-windows-6.0-386.exe",
                     "args": ["-c", "{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "hysteria2": {"label": "Hysteria2", "dir": "hysteria2", "exe": "hysteria2.exe",
                      "args": ["-c", "{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "juicity": {"label": "Juicity", "dir": "juicity", "exe": "juicity-client.exe",
                    "args": ["run", "-c", "{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "naive": {"label": "NaiveProxy", "dir": "naiveproxy", "exe": "naive.exe",
                  "args": ["{dir}\\config.json"], "port": 1080, "scheme": "socks5"},
        "mieru": {"label": "Mieru", "dir": "mieru", "exe": "mieru.exe",
                  "args": ["start"], "port": 3080, "scheme": "socks5",
                  "pre_args": ["apply", "config", "{dir}\\config.json"]},
        "shadowquic": {"label": "ShadowQuic", "dir": "shadowquic", "exe": "shadowquic.exe",
                       "args": ["-c", "{dir}\\client.yaml"], "port": 4080, "scheme": "socks5"},
    }

DEFAULT_DIR = r"C:\Users\Admin\Desktop\ChromeGo"
# 安卓端内核在 proot 容器内的固定位置（由 BotService 从 APK assets 释放）
ANDROID_CHROMEGO_DIR = "/root/tw2qq/chromego"


def _bundled_source() -> Path | None:
    """打包进 exe 的内置内核资源目录（PyInstaller _MEIPASS/chromego）"""
    if getattr(sys, "frozen", False):
        d = Path(getattr(sys, "_MEIPASS", "")) / "chromego"
        if d.exists():
            return d
    return None


def _hidden_startupinfo():
    """让子进程窗口彻底不可见（部分内核会忽略 CREATE_NO_WINDOW）"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _hide_process_windows(pid: int) -> int:
    """隐藏某进程的全部可见顶层窗口。

    ChromeGo 的 clash 内核会自我复制成 ._cache_*.exe 再重新执行（孙进程），
    启动标志管不到孙进程，只能事后把它的窗口藏起来。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnds = []

        def _cb(h, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid:
                hwnds.append(h)
            return True

        CMPFUNC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(CMPFUNC(_cb), 0)
        n = 0
        for h in hwnds:
            if user32.IsWindowVisible(h) and user32.ShowWindow(h, 0):  # SW_HIDE
                n += 1
        return n
    except Exception:
        return 0


def _hide_later(get_pid, seconds: float = 4.0):
    """后台线程持续隐藏内核窗口（内核派生孙进程略有延迟）"""
    def _run():
        deadline = time.time() + seconds
        while time.time() < deadline:
            pid = get_pid()
            if pid and _hide_process_windows(pid):
                return
            time.sleep(0.3)
    threading.Thread(target=_run, daemon=True, name="chromego-hide").start()


def _port_open(port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class ChromeGoManager:
    """管理一个 ChromeGo 内核子进程的生命周期"""

    def __init__(self, base_dir, get_config, save_config_fn=None):
        self.base_dir = Path(base_dir)
        self._get_config = get_config          # -> 全局 config dict
        self._save_config_fn = save_config_fn  # 可选：持久化配置回调
        self.proc: subprocess.Popen | None = None
        self.kernel: str | None = None
        self.port: int | None = None
        self.pid: int | None = None
        self.last_error: str | None = None
        atexit.register(self._stop_on_exit)

    # ---------- 路径 ----------
    @property
    def chrome_dir(self) -> Path:
        # 0) 安卓端(APK)：容器内固定位置，由 BotService 从 assets 释放（每次启动刷新）
        if _IS_ANDROID:
            return Path(ANDROID_CHROMEGO_DIR)
        # 1) exe 旁释放好的内置内核（单文件版自带代理，无需外部 ChromeGo）
        if getattr(sys, "frozen", False):
            persist = Path(sys.executable).parent / "chromego"
            if persist.exists():
                return persist
        # 2) 配置指定的外部目录（源码运行或内置释放前）
        d = (self._get_config().get("network") or {}).get("chromego_dir", "")
        if d and Path(d).exists():
            return Path(d)
        # 3) 打包内置资源目录（首次运行直接用，同时异步释放到 exe 旁）
        bundled = _bundled_source()
        if bundled is not None:
            self._kick_release(bundled)
            return bundled
        return Path(DEFAULT_DIR)

    def _kick_release(self, src: Path):
        """把内置内核复制到 exe 旁（只做一次，异步），此后内核配置可持久修改"""
        if getattr(self, "_release_started", False):
            return
        self._release_started = True

        def _copy():
            dest = Path(sys.executable).parent / "chromego"
            try:
                if not dest.exists():
                    shutil.copytree(src, dest)
                    logging.info("内置 ChromeGo 内核已释放到 %s", dest)
            except Exception as e:
                logging.warning("释放内置内核失败（继续使用临时目录内的内核）: %s", e)

        threading.Thread(target=_copy, daemon=True, name="chromego-release").start()

    # ---------- 状态 ----------
    def status(self) -> dict:
        base = self.chrome_dir
        kernels = []
        for key, k in KERNELS.items():
            exe = base / k["dir"] / k["exe"]
            kernels.append({
                "key": key, "label": k["label"], "port": k["port"],
                "scheme": k["scheme"],
                "available": exe.exists(),
                "running": self.kernel == key and self.is_running,
            })
        return {
            "dir": str(base),
            "dir_exists": base.exists(),
            "kernels": kernels,
            "running": self.is_running,
            "kernel": self.kernel,
            "port": self.port,
            "pid": self.pid,
            "proxy_active": net.current_proxy(),
            "stop_on_exit": bool((self._get_config().get("network") or {})
                                 .get("chromego_stop_on_exit", True)),
            "last_error": self.last_error,
        }

    @property
    def is_running(self) -> bool:
        """以「所选内核的代理端口在监听」为准（内核可能派生子进程）"""
        return self.kernel is not None and self.port is not None and _port_open(self.port)

    # ---------- 启动 / 停止 ----------
    def start(self, kernel_key: str) -> dict:
        if self.is_running or _port_open(KERNELS.get(kernel_key, {}).get("port", 0)):
            if self.kernel == kernel_key:
                return {"ok": False, "error": f"内核 {self.kernel} 已在运行，请先停止"}
        k = KERNELS.get(kernel_key)
        if not k:
            return {"ok": False, "error": f"未知内核: {kernel_key}"}
        # exe/kwdir 必须绝对路径：Linux 下 Popen(cwd=相对目录) 时，
        # execve 在新 cwd 里解析相对 exe 路径 → ENOENT（Windows 无此问题）
        base = self.chrome_dir
        exe = (base / k["dir"] / k["exe"]).resolve()
        # 云端/Linux：上传过程不保留执行权限 → 每次启动前修正；
        # 二进制可能被上传过滤 → 从随包 zip 自愈解压
        if sys.platform.startswith("linux"):
            if not exe.exists():
                import zipfile
                z = base / k["dir"] / (k["exe"] + ".zip")
                if z.exists():
                    try:
                        with zipfile.ZipFile(z) as zf:
                            zf.extractall(base / k["dir"])
                        logging.info("已从 zip 解压内核 %s", k["exe"])
                    except Exception as e:
                        logging.warning("内核 zip 解压失败: %s", e)
            if exe.exists():
                try:
                    os.chmod(str(exe), 0o755)
                except Exception as e:
                    logging.warning("内核 chmod 失败: %s", e)
        if not exe.exists():
            self.last_error = f"未找到 {exe}，请确认 ChromeGo 目录"
            return {"ok": False, "error": self.last_error}

        # 端口已被占用：视为已有实例（用户手动启动的），直接接管
        if _port_open(k["port"]):
            self.kernel, self.port = kernel_key, k["port"]
            proxy = f'{k["scheme"]}://127.0.0.1:{k["port"]}'
            net.apply_proxy(proxy)
            self._persist(kernel_key, proxy)
            logging.info("检测到 127.0.0.1:%d 已有 %s 实例，直接接管", k["port"], kernel_key)
            return {"ok": True, "kernel": kernel_key, "port": k["port"], "proxy": proxy}

        kwdir = (base / k["dir"]).resolve()
        argv = [str(exe)] + [a.format(dir=str(kwdir)) for a in k["args"]]
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        si = _hidden_startupinfo() if sys.platform == "win32" else None
        # 无桌面环境（安卓容器/Linux 云端）：内核日志落文件，便于排查
        out = subprocess.DEVNULL
        if _IS_ANDROID or sys.platform.startswith("linux"):
            try:
                out = open(kwdir / "kernel.log", "ab")
            except Exception:
                pass

        try:
            # mieru 需要先应用配置再启动（两步，同官方脚本）
            if k.get("pre_args"):
                pre = [str(exe)] + [a.format(dir=str(kwdir)) for a in k["pre_args"]]
                pr = subprocess.run(pre, cwd=str(kwdir), capture_output=True,
                                    timeout=20, creationflags=flags, startupinfo=si)
                if pr.returncode != 0:
                    self.last_error = pr.stderr.decode("utf-8", "ignore")[:200] or "apply config 失败"
                    return {"ok": False, "error": self.last_error}
            self.proc = subprocess.Popen(
                argv, cwd=str(kwdir), creationflags=flags, startupinfo=si,
                stdout=out, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            self.last_error = str(e)
            return {"ok": False, "error": f"启动失败: {e}"}

        # 等待本地代理端口就绪（最长 20 秒）。
        # 注意：部分内核（如 clash.meta）会自我派生子进程后主进程退出，
        # 因此只以端口就绪为准，不依赖子进程存活。
        self.kernel, self.port = kernel_key, k["port"]
        deadline = time.time() + 20
        while time.time() < deadline:
            if _port_open(k["port"]):
                self.pid = self._find_port_pid(k["port"]) or (self.proc.pid if self.proc else None)
                proxy = f'{k["scheme"]}://127.0.0.1:{k["port"]}'
                net.apply_proxy(proxy)
                self._persist(kernel_key, proxy)
                # 内核可能派生孙进程（clash.meta 复制自身重启），其窗口启动标志管不到，事后隐藏
                if sys.platform == "win32":
                    _hide_later(lambda: self._find_port_pid(k["port"]) or self.pid)
                logging.info("ChromeGo 内核 [%s] 已启动 (127.0.0.1:%d, pid=%s)，程序流量走代理: %s",
                             kernel_key, k["port"], self.pid, proxy)
                return {"ok": True, "kernel": kernel_key, "port": k["port"], "proxy": proxy}
            time.sleep(0.5)

        self.stop()
        self.last_error = "启动超时：20 秒内代理端口未就绪（配置可能失效，可换其他内核试试）"
        return {"ok": False, "error": self.last_error}

    @staticmethod
    def _find_port_pid(port: int):
        """用 netstat 找到监听该端口的进程 PID（内核派生场景下与子进程不同）"""
        if sys.platform != "win32":
            return None  # 非 Windows 直接用子进程 PID
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True, timeout=10,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
                startupinfo=_hidden_startupinfo() if sys.platform == "win32" else None,
            ).stdout.decode("gbk", "ignore")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        except Exception:
            pass
        return None

    def stop(self):
        stopped = False
        # 1) 按端口找到真实监听进程（内核可能已派生，与 self.proc 不同）并杀进程树
        if self.port and sys.platform == "win32":
            pid = self._find_port_pid(self.port) or self.pid
            if pid:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=10)
                    stopped = True
                except Exception as e:
                    logging.warning("taskkill 异常: %s", e)
        # 2) 直接子进程兜底
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                stopped = True
            except Exception as e:
                logging.warning("停止内核异常: %s", e)
        if stopped or self.kernel:
            logging.info("ChromeGo 内核 [%s] 已停止", self.kernel)
        self.proc = None
        self.kernel = None
        self.port = None
        self.pid = None
        # 内核停了，代理自然失效，切回直连
        net.apply_proxy("")
        if self._save_config_fn:
            self._save_config_fn({"network": {"proxy": ""}})

    def _stop_on_exit(self):
        """程序退出时按配置决定是否连带停止内核（默认停止）"""
        try:
            if (self._get_config().get("network") or {}).get("chromego_stop_on_exit", True):
                self.stop()
        except Exception:
            self.stop()

    def _persist(self, kernel_key: str, proxy: str):
        """记住所选内核与代理地址，重启程序后面板可见"""
        if self._save_config_fn:
            self._save_config_fn({"network": {
                "chromego_kernel": kernel_key, "proxy": proxy,
            }})

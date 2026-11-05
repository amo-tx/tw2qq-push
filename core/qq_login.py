# -*- coding: utf-8 -*-
"""QQ 登录通道：NapCat.Shell 集成启动器

- 下载/安装 NapCat.Shell（GitHub Releases，失败可手动解压到 napcat/ 目录）
- 启动前自动写入 OneBot HTTP Server 配置（端口 3000，与本程序 config 对齐）
- 子进程捕获 stdout：提取登录二维码（字符画）与登录状态，供面板展示
"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

import requests

from core.platform import IS_WINDOWS, EXTERNAL_NAPCAT

RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
QR_CHARS = ("█", "▄", "▀")


def child_env_no_proxy() -> dict:
    """QQ/NapCat 流量强制直连：剥离全部代理环境变量并设 NO_PROXY=*

    本程序为了加速 x.com/GitHub 会设置 HTTP_PROXY/HTTPS_PROXY，子进程默认
    会继承；NapCat 内部用的 HTTP 库（axios 等）会读取这些变量，导致 QQ 登录
    流量被送进代理节点（国外 IP 连腾讯服务器）→ 被风控/超时。故剥离。
    """
    strip = {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
    env = {k: v for k, v in os.environ.items() if k.lower() not in strip}
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _kill_tree(pid: int) -> None:
    """杀掉整棵进程树（terminate 只杀 cmd 外壳，QQ/node 子进程会变孤儿）"""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            # Linux/Termux：进程组整组杀（Popen 以 start_new_session 启动）
            import signal
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def _ps_lines(script: str, timeout: int = 20) -> list:
    """用 PowerShell 查询进程信息，返回 stdout 行（仅 Windows 使用）"""
    if not IS_WINDOWS:
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def qq_running() -> bool:
    """官方 QQ 客户端是否正在运行（NapCat 与之冲突会导致卡在“等待网络连接”）
    仅 Windows 桌面环境有意义；Termux/proot 环境没有官方 QQ 桌面客户端"""
    if not IS_WINDOWS:
        return False
    out = _ps_lines('tasklist /FI "IMAGENAME eq QQ.exe" /NH', timeout=10)
    return any("QQ.exe" in ln for ln in out)


class NapCatManager:
    def __init__(self, base_dir: Path, get_config, save_config=None):
        self.base_dir = Path(base_dir)
        self.get_config = get_config  # 返回 monitor.config
        self._save_config = save_config  # 定点写配置的回调（webui._save_config_key）
        self.proc = None
        self.status = "stopped"       # stopped/starting/wait_qr/online/error
        self.qr_lines = []
        self.recent_lines = deque(maxlen=30)  # NapCat 输出尾巴（报错时面板可见）
        self.webui_url = ""
        self.qq = ""
        self._lock = threading.Lock()
        self._wait_net_since = 0.0  # 首次出现“等待网络连接”的时间戳
        self._qq_cache = (0.0, False)  # (时刻, 官方QQ是否在运行) 缓存
        self._wd_gen = 0        # 看门狗代数（stop/重启后旧看门狗自动退出）
        self.reconnects = 0     # 本次会话自动重连次数（UI 可见）
        self.dropped_from_online = False  # 曾在线后掉线（服务端踢下线/票据失效），UI 提示用
        self.drop_reconnects = 0  # 掉线自动恢复（快速重登）次数，上限 3
        self.external = EXTERNAL_NAPCAT  # 安卓/Linux：NapCat 由 proot 容器自行管理，仅探测端口

    # ---------- 路径 ----------
    @property
    def napcat_dir(self) -> Path:
        d = self.base_dir / (self.get_config().get("qq", {}).get("napcat", {}).get("dir") or "napcat")
        return d

    def launch_cmd(self):
        """在解压目录里定位启动器（新版 NapCat 是 napcat.bat，旧版为 napcat.cmd）"""
        for name in ("napcat.bat", "napcat.cmd"):
            hits = list(self.napcat_dir.rglob(name))
            if hits:
                return min(hits, key=lambda p: len(p.parts))  # 优先最浅层（Shell 入口）
        return None

    def installed(self) -> bool:
        if self.external:
            return True  # 外部模式：NapCat 在 proot 容器内自行管理
        return self.launch_cmd() is not None

    # ---------- 下载安装 ----------
    MIRRORS = [
        "",                                   # 直连（走环境变量代理，如 ChromeGo）
        "https://ghfast.top/",                # GitHub 加速镜像 1
        "https://gh-proxy.com/",              # GitHub 加速镜像 2
    ]

    def _download_zip(self, url: str, dest: Path) -> None:
        """带断点续传的单 URL 下载，失败抛异常"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        headers = {"User-Agent": "Mozilla/5.0"}
        for attempt in range(3):  # 断点续传最多 3 次
            done = part.stat().st_size if part.exists() else 0
            if done:
                headers["Range"] = f"bytes={done}-"
            with requests.get(url, stream=True, timeout=(15, 60),
                              headers=headers) as resp:
                if resp.status_code == 416:  # 已下完
                    break
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0)) + done
                mode = "ab" if done and resp.status_code == 206 else "wb"
                got = done if mode == "ab" else 0
                with open(part, mode) as f:
                    for chunk in resp.iter_content(1 << 17):
                        f.write(chunk)
                        got += len(chunk)
                        if total and got // (20 << 20) != (got - len(chunk)) // (20 << 20):
                            logging.info("NapCat 下载进度: %.1f / %.1f MB",
                                         got / 1048576, total / 1048576)
            if not total or got >= total:
                part.replace(dest)
                return
        raise RuntimeError("重试 3 次仍未下完")

    def download(self) -> dict:
        """从 GitHub 下载最新 NapCat.Shell 并解压，返回 {ok, error}"""
        try:
            r = requests.get(RELEASE_API, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            assets = [a for a in r.json().get("assets", [])
                      if "Shell" in a.get("name", "") and a["name"].endswith(".zip")]
            if not assets:
                return {"ok": False, "error": "未在最新 Release 中找到 Shell zip"}
            asset = assets[0]
            zip_path = self.base_dir / "napcat_shell.zip"
            errors = []
            for mirror in self.MIRRORS:
                url = mirror + asset["browser_download_url"] if mirror else asset["browser_download_url"]
                try:
                    logging.info("开始下载 NapCat.Shell: %s (%s)",
                                 asset["name"], mirror or "直连/代理")
                    self._download_zip(url, zip_path)
                    break
                except Exception as e:
                    errors.append(f"{mirror or '直连'}: {type(e).__name__}: {e}"[:150])
                    logging.warning("下载失败(%s): %s", mirror or "直连", e)
                    continue
            else:
                return {"ok": False,
                        "error": "所有通道均失败：" + " | ".join(errors)}

            # zip 完整性校验
            try:
                with zipfile.ZipFile(zip_path) as z:
                    bad = z.testzip()
                    if bad:
                        return {"ok": False, "error": f"下载包损坏({bad})，请重试"}
                    z.extractall(self.napcat_dir)
            except zipfile.BadZipFile:
                return {"ok": False, "error": "下载包不是有效 zip（连接中断），请重试"}
            zip_path.unlink(missing_ok=True)
            ok = self.installed()
            return {"ok": ok, "error": None if ok else "解压完成但未找到 napcat.cmd"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- OneBot 配置 ----------
    def _android_napcat_conf_dir(self):
        """安卓外部模式：在 proot-distro rootfs 里定位 NapCat 的 config 目录
        （proot 不隔离文件系统，宿主 Termux 可直接按路径读写容器内文件）"""
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        roots = Path(prefix) / "var/lib/proot-distro/installed-rootfs"
        if not roots.is_dir():
            return None
        for distro_root in sorted(roots.glob("*/root")):
            # 官方 NapCat-Installer 的常见布局
            for cand in (
                distro_root / "Napcat/opt/QQ/resources/app/app_launcher/napcat/config",
                distro_root / "NapCat/opt/QQ/resources/app/app_launcher/napcat/config",
            ):
                if cand.is_dir():
                    return cand
            # 兜底：按深度逐层找 napcat.mjs（最多 6 层，避免全量 rglob 扫几千个文件）
            found = None
            for depth in range(1, 7):
                for mjs in distro_root.glob("*/" * depth + "napcat.mjs"):
                    found = mjs.parent / "config"
                    break
                if found:
                    break
            if found and found.is_dir():
                return found
        return None

    def _write_onebot_config(self, qq: str):
        """写入 HTTP Server(3000) 配置，token 与本程序 config 对齐。
        同时配置 HTTP 上报（httpClients）：NapCat 把收到的群消息事件
        POST 到本程序 5800 端口，实现 @机器人指令交互。"""
        cfg = self.get_config().get("qq", {}).get("napcat", {})
        token = cfg.get("access_token", "")
        webui_port = self.get_config().get("port", 5800)
        conf = {
            "network": {
                "httpServers": [{
                    "name": "twitter2qq", "enable": True,
                    "host": "127.0.0.1", "port": 3000,
                    "enableCors": False, "enableWebsocket": False,
                    "messagePostFormat": "array",
                    "token": token, "debug": False,
                }],
                "httpClients": [{
                    "name": "twitter2qq-report", "enable": True,
                    "url": f"http://127.0.0.1:{webui_port}/onebot/event",
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": token,
                }],
            },
            # 兼容旧版扁平结构
            "http": {"enable": True, "host": "127.0.0.1", "port": 3000,
                     "secret": token, "enableCors": False},
            "musicSignUrl": "", "reportSelfMessage": False,
            "token": token,
        }
        # NapCat 实际读取的配置目录是 napcat.mjs 所在目录下的 config/
        # （Shell 包结构：根目录 napcat.bat 只是启动器，napcat/ 子目录才是应用本体）
        if self.external:
            conf_dir = self._android_napcat_conf_dir()
            if conf_dir is None:
                logging.warning("安卓外部模式：未在 proot rootfs 中找到 NapCat，"
                                "请先运行 android/install.sh 安装，或在 NapCat WebUI (6099) "
                                "手动添加 HTTP Server(3000) 与 HTTP 上报(http://127.0.0.1:5800/onebot/event)")
                return
        else:
            mjs = list(self.napcat_dir.rglob("napcat.mjs"))
            conf_dir = (mjs[0].parent / "config") if mjs else (self.launch_cmd().parent / "config")
        conf_dir.mkdir(exist_ok=True)
        import json
        path = conf_dir / f"onebot11_{qq}.json"
        path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("已写入 NapCat OneBot 配置: %s", path.name)

    # ---------- 启动/停止 ----------
    def _kill_leftovers(self):
        """清理上次异常退出遗留的 NapCat 进程（可执行文件在 napcat 目录里的 node）"""
        ndir = str(self.napcat_dir).replace("'", "''")
        pids = _ps_lines(
            "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
            f"Where-Object {{ $_.ExecutablePath -like '*{ndir}*' }} | "
            "Select-Object -ExpandProperty ProcessId")
        for ln in pids:
            if ln.isdigit():
                logging.info("清理遗留 NapCat 进程 PID=%s", ln)
                _kill_tree(int(ln))

    def _persist_qq(self, qq: str):
        """把 QQ 号写进配置持久化，下次启动自动快速登录（免重新输入/扫码）"""
        if not self._save_config:
            return
        try:
            self._save_config({"qq": {"napcat": {"qq": qq}}})
            logging.info("已保存 QQ 号 %s（下次启动自动快速登录）", qq)
        except Exception as e:
            logging.warning("保存 QQ 号失败: %s", e)

    def saved_qq(self) -> str:
        """上次登录保存的 QQ 号（启动自动登录/输入框预填用）"""
        try:
            return str((self.get_config().get("qq", {}).get("napcat", {}) or {}).get("qq", "") or "")
        except Exception:
            return ""

    def start(self, qq: str) -> dict:
        with self._lock:
            if self.external:
                # 安卓/Linux 外部模式：不拉进程，写好 OneBot 配置后周期探测 3000 端口
                if self.status == "online":
                    return {"ok": False, "error": "NapCat 已在线"}
                self.qq = qq or self.saved_qq() or ""
                self.status = "starting"
                self.dropped_from_online = False
                self.drop_reconnects = 0
            else:
                if self.proc and self.proc.poll() is None:
                    return {"ok": False, "error": "NapCat 已在运行"}
                if not qq or not qq.isdigit():
                    return {"ok": False, "error": "请输入正确的 QQ 号（纯数字）"}
        if self.external:
            self._write_onebot_config(self.qq)
            self._wd_gen += 1
            threading.Thread(target=self._watchdog, args=(self._wd_gen,),
                             daemon=True, name="napcat-watchdog").start()
            logging.info("外部模式启动探测：每 30 秒探测 OneBot 3000 端口；"
                         "请在 NapCat WebUI (http://127.0.0.1:6099) 启动并扫码登录")
            return {"ok": True, "warning": "安卓外部模式：QQ 登录请在 NapCat WebUI (http://127.0.0.1:6099) 扫码，"
                                           "本程序自动探测连接状态"}
        with self._lock:
            cmd = self.launch_cmd()
            if not cmd:
                return {"ok": False, "error": "未安装 NapCat（先点击下载安装，或手动解压 Shell 包到 napcat/ 目录）"}
            self._kill_leftovers()
            warn = None
            if qq_running():
                warn = ("检测到官方 QQ 客户端正在运行：NapCat 与官方 QQ 同时运行可能争抢数据目录"
                        "（卡在“等待网络连接”），且若官方 QQ 登录的是同一个 QQ 号，会互踢导致机器人反复掉线。"
                        "建议机器人用小号，或登录机器人期间完全退出官方 QQ（含托盘图标）。")
                logging.warning(warn)
            self._write_onebot_config(qq)
            self.qq = qq
            self.qr_lines = []
            self.webui_url = ""
            self.status = "starting"
            self.recent_lines.clear()
            self._wait_net_since = 0.0
            self.proc = subprocess.Popen(
                ["cmd", "/c", str(cmd), "-q", qq],
                cwd=cmd.parent,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=child_env_no_proxy(),  # QQ 流量强制直连，不走加速代理
            )
            self._wd_gen += 1
            self._persist_qq(qq)
            threading.Thread(target=self._pump, daemon=True, name="napcat-log").start()
            threading.Thread(target=self._watchdog, args=(self._wd_gen,),
                             daemon=True, name="napcat-watchdog").start()
            return {"ok": True, "warning": warn}

    def stop(self):
        self._wd_gen += 1  # 让看门狗退出
        if self.proc and self.proc.poll() is None:
            _kill_tree(self.proc.pid)  # 连同 QQ/node 子进程整树杀掉，不留孤儿
        self.proc = None
        self.status = "stopped"

    # ---------- 看门狗：自动探测掉线并重连 ----------
    def _probe_onebot(self) -> bool:
        """探测 OneBot HTTP 是否在线（强制直连，不走环境代理）"""
        token = self.get_config().get("qq", {}).get("napcat", {}).get("access_token", "") or ""
        try:
            r = requests.get(
                "http://127.0.0.1:3000/get_status",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=6,
                proxies={"http": None, "https": None},
            )
            return r.status_code == 200 and r.json().get("retcode", 0) == 0
        except Exception:
            return False

    def _watchdog(self, gen: int):
        """每 30s 探测一次：进程退出或 OneBot 连续无响应 → 自动快速登录重启。
        掉线恢复不再需要人工扫码（本地有快速登录票据）；票据失效时 NapCat 会重新出二维码。
        外部模式（安卓/Linux）：只探测 3000 端口并同步状态，不管理进程。"""
        if self.external:
            while True:
                time.sleep(30)
                with self._lock:
                    if gen != self._wd_gen:
                        return
                if self._probe_onebot():
                    with self._lock:
                        if self.status != "online":
                            logging.info("探测到 NapCat OneBot 在线（外部模式）")
                            self.status = "online"
                            self.dropped_from_online = False
                            self.drop_reconnects = 0
                else:
                    with self._lock:
                        if self.status == "online":
                            logging.warning("NapCat OneBot 掉线（外部模式）：proot 容器可能被系统杀掉，"
                                            "请检查 Termux 后台权限（wakelock）并在 NapCat WebUI 重启")
                            self.dropped_from_online = True
                        if self.status in ("starting", "online"):
                            self.status = "offline"
            return
        fails = 0
        while True:
            time.sleep(30)
            with self._lock:
                if gen != self._wd_gen:      # 用户已停止/外部重启
                    return
                alive = bool(self.proc and self.proc.poll() is None)
                pid = self.proc.pid if self.proc else None
                qq = self.qq
            if not alive:
                if not self.get_config().get("auto_reconnect", True):
                    return
                if self.reconnects >= 5:
                    logging.error("NapCat 自动重连已达 5 次上限，停止尝试（请到 QQ 登录页手动启动）")
                    return
                self.reconnects += 1
                logging.warning("检测到 NapCat 进程已退出，自动重启（第 %d/5 次，快速登录 %s）...",
                                self.reconnects, qq)
                self.stop()          # 清理残留；gen 变化让本看门狗退出
                r = self.start(qq)   # start 内部会拉起新看门狗
                if not r.get("ok"):
                    logging.error("NapCat 自动重启失败: %s", r.get("error"))
                return
            if self._probe_onebot():
                fails = 0
                self.reconnects = 0
                self.drop_reconnects = 0
                self.dropped_from_online = False
                with self._lock:
                    if gen != self._wd_gen:
                        return
                    if self.status != "online":
                        logging.info("看门狗探测到 OneBot 在线，状态 %s → online", self.status)
                        self.status = "online"
                continue
            fails += 1
            with self._lock:
                st = self.status
            if st == "wait_qr":
                # 正在等待扫码：不是僵死（二维码会自动刷新），不重启也不刷心跳日志
                if fails == 3:
                    if self.dropped_from_online:
                        logging.info("QQ 曾在线后被服务端下线（风控/网络原因），NapCat 出码中。"
                                     "将自动尝试快速登录恢复（票据有效时免扫码）")
                    else:
                        logging.info("QQ 等待扫码中（快速登录票据已失效）。请到控制台 QQ 登录页扫码一次，之后日常启动免扫码")
                # 掉线自动恢复：曾被服务端踢下线时，快速登录票据通常仍有效，
                # 重启 NapCat 快速登录即可免扫码回到在线。退避：每多失败一次多等 ~1.5 分钟
                if (self.dropped_from_online and self.drop_reconnects < 3
                        and fails >= 4 + self.drop_reconnects * 3
                        and self.get_config().get("auto_reconnect", True)):
                    self.drop_reconnects += 1
                    logging.warning("QQ 掉线自动恢复（第 %d/3 次）：重启 NapCat 尝试快速登录…",
                                    self.drop_reconnects)
                    with self._lock:
                        self.status = "reconnecting"
                    if pid:
                        _kill_tree(pid)
                    self.stop()
                    r = self.start(qq)
                    if not r.get("ok"):
                        logging.error("QQ 掉线自动恢复失败: %s", r.get("error"))
                    return
                continue
            logging.warning("NapCat 心跳探测失败（连续 %d 次）", fails)
            if fails >= 3 and self.get_config().get("auto_reconnect", True):
                if self.reconnects >= 5:
                    logging.error("NapCat 自动重连已达 5 次上限，停止尝试")
                    return
                self.reconnects += 1
                logging.warning("OneBot 连续无响应，判定进程僵死，自动重启（第 %d/5 次）...",
                                self.reconnects)
                with self._lock:
                    self.status = "reconnecting"
                if pid:
                    _kill_tree(pid)
                self.stop()
                r = self.start(qq)
                if not r.get("ok"):
                    logging.error("NapCat 自动重启失败: %s", r.get("error"))
                return

    def _pump(self):
        """读取 NapCat stdout：提取二维码块 / 登录状态 / WebUI 地址"""
        gen = self._wd_gen
        try:
            in_qr = False
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                with self._lock:
                    if line.strip():
                        self.recent_lines.append(line[:200])
                        if any(c in line for c in QR_CHARS):
                            if not in_qr:
                                self.qr_lines = []
                                in_qr = True
                            self.qr_lines.append(line)
                            if self.status == "online":
                                # 曾在线 → 出码：会话被服务端下线（风控/网络原因），不是首次登录
                                logging.warning(
                                    "检测到机器人会话掉线（QQ 服务端踢下线或票据失效）。"
                                    "NapCat 已重新出码，程序将自动尝试快速登录恢复"
                                    "（票据有效时免扫码）；若持续出码请到控制台扫码一次")
                                self.dropped_from_online = True
                            elif self.status != "wait_qr":
                                logging.info("QQ 快速登录票据未生效/已失效，出二维码等待扫码（扫码一次后日常启动免扫码）")
                            self.status = "wait_qr"
                        else:
                            in_qr = False
                            if "等待网络连接" in line and not self._wait_net_since:
                                self._wait_net_since = time.time()
                            if re.search(r"登录成功|Login successful|Bot.{0,8}开始运行|online", line, re.I):
                                self.status = "online"
                            m = re.search(r"https?://[^\s\"']*token=[\w-]+", line)
                            if m:
                                self.webui_url = m.group(0)
                            if re.search(r"失败|错误|ERROR|exited", line, re.I) and self.status not in ("online",):
                                self.status = "error" if self.status in ("starting",) else self.status
        except Exception:
            pass
        finally:
            with self._lock:
                if gen == self._wd_gen:  # 仅当非人为停止时才标记退出状态
                    if self.status == "online":
                        self.status = "offline"  # 进程已退出，等看门狗自动重启
                    elif self.status != "stopped":
                        self.status = "error"

    # ---------- 状态 ----------
    def _qq_running_cached(self) -> bool:
        now = time.time()
        ts, val = self._qq_cache
        if now - ts > 15:  # 15s 缓存，避免每次轮询都起进程
            val = qq_running()
            self._qq_cache = (now, val)
        return val

    def status_info(self) -> dict:
        with self._lock:
            stuck = (self._wait_net_since
                     and self.status not in ("online", "stopped")
                     and time.time() - self._wait_net_since > 60)
            hint = ""
            if stuck:
                hint = ("卡在“等待网络连接”超过 1 分钟：通常因为官方 QQ 客户端正在运行，"
                        "与 NapCat 争抢数据目录。请完全退出官方 QQ（含托盘图标）后，"
                        "先点停止再重新启动登录。")
            return {
                "installed": self.installed(),
                "dir": str(self.napcat_dir),
                "running": bool(self.proc and self.proc.poll() is None),
                "status": self.status,
                "qq": self.qq or self.saved_qq(),
                "webui_url": self.webui_url,
                "qr": "\n".join(self.qr_lines),
                "log_tail": list(self.recent_lines)[-12:],
                "proxy_direct": True,
                "official_qq_running": self._qq_running_cached(),
                "reconnects": self.reconnects,
                "dropped_from_online": self.dropped_from_online,
                "drop_reconnects": self.drop_reconnects,
                "external": self.external,
                "napcat_webui": "http://127.0.0.1:6099" if self.external else "",
                "hint": hint,
            }

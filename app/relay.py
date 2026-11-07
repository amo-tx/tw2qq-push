# -*- coding: utf-8 -*-
"""QQ 小号转发器 —— 把官方机器人发的推文搬到「拉不进机器人」的群。

背景：QQ 官方机器人必须由群主/管理员邀请才能进群。对于拿不到管理员的群，
改由个人 QQ 小号（NapCat / 任意 OneBot v11 端）监听「机器人能发言的群」，
把机器人发的推文消息自动转发到目标群。

链路（全部在本机/常开设备上跑，不改云端、不需重新部署）：

    QQ 官方机器人在 UPC 群发推文
        │  (小号也在 UPC 群，NapCat 收到群消息)
        ▼
    NapCat HTTP 上报 → relay.py :5801/onebot/event
        │  过滤 + 文本/图片/GIF 合并 + 去重 + 限速
        ▼
    NapCat /send_group_msg → 目标群(1059831074)   ← 小号是普通成员即可

纯 Python 标准库，Windows / Termux(Android) 都能跑。

用法：
    python relay.py                 启动转发服务（前台常驻）
    python relay.py probe           只观察不转发，打印收到的群消息（找群号/机器人QQ）
    python relay.py setup           自动把 NapCat 上报地址改到本转发器（先备份）
    python relay.py status          查看转发统计
    python relay.py sendtest        向目标群发一条测试文本（验证小号在群里）
    python relay.py selftest        本地自检（模拟上报，验证过滤/合并/去重/清洗）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import queue
import random
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent

# 时区兜底：容器默认 UTC，转发器日志/去重时间戳都会比北京时间慢 8 小时。
if not os.environ.get("TZ"):
    os.environ["TZ"] = ("Asia/Shanghai"
                        if Path("/usr/share/zoneinfo/Asia/Shanghai").exists()
                        else "CST-8")
try:
    time.tzset()
except (AttributeError, OSError):
    pass
# 掉线恢复需要「来源群 + 机器人 QQ」才能精确补拉历史。转发器进程重启后
# 内存里的 recent 是空的（恰好也是掉线恢复最常见的时刻），所以把这对信息
# 落到运行时状态文件；不随部署包发布，sandbox 重启后仍在。
SOURCE_HINT_PATH = BASE / "data" / "relay_source.json"
DEFAULT_CONFIG = {
    "onebot_url": "http://127.0.0.1:3000",   # NapCat OneBot HTTP Server
    "onebot_token": "",                      # 与 NapCat access_token 一致（空则不带）
    "listen_host": "127.0.0.1",
    "listen_port": 5801,                     # 避开 console.py 的 5800
    "report_path": "/onebot/event",
    "target_groups": [1059831074],           # 要转发到的群（Monyami 群）
    # direct=True：内容由服务端 POST /push 直投（推荐，少一层依赖）；
    # direct=False：老行为，监听 source 群里官方机器人发的消息再转发。
    "direct": True,
    "source_groups": [],                     # 只监听这些群；空 = 监听所有群
    "source_qq": [],                         # 只转发这些 QQ 发的消息；空 = 靠关键词识别
    "keywords": ["更新推文", "🔗 原文："],
    "drop_keywords": ["菜单", "指令面板", "指令速查", "当前状态", "自检报告",
                      "操作成功", "推送已暂停", "推送已恢复", "关注列表", "群指令"],
    "bundle_wait_seconds": 10,               # 文本后等待图片/GIF 到齐的时间
    "bundle_max_seconds": 30,                # 单个 bundle 最长收集时间
    "min_interval_seconds": 3,               # 每条实际发送之间的最小间隔（防风控）
    "max_per_minute": 10,
    "dedup_minutes": 60,
    "prefix": "",                            # 可选转发前缀，如「【转】」
    "split_media_on_fail": True,             # 合并消息发送失败时，退化成逐段发送
    "db_path": "data/relay.db",
    "log_path": "logs/relay.log",
    "ws_port": 5802,                         # OneBot 反向WS 端口（NapCat 连入，发送通道）
}

# 一条推文在官方通道里会被拆成「1 条文本 + N 条图片/GIF」独立消息，
# 所以必须把同一发送者的后续媒体消息并入同一个 bundle 一起转发。
MEDIA_TYPES = {"image", "video", "record", "mface", "file"}
KEEP_TYPES = {"text", "face", "dice", "rps", "shake", "poke"} | MEDIA_TYPES
DROP_TYPES = {"at", "reply", "forward", "anonymous", "json", "xml",
              "contact", "location", "music", "node", "redbag"}

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机请求不走系统代理
LOG = logging.getLogger("relay")


def setup_logging(log_path: str, verbose: bool = False):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        p = BASE / log_path
        p.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(p), encoding="utf-8"))
    except Exception as e:
        print("日志文件不可用:", e)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S", handlers=handlers,
    )


def normalize_targets(raw) -> list:
    """target_groups 兼容两种写法：[群号] 或 [{"id": 群号, "feeds": ["博主handle"]}]

    feeds 空 = 转发全部推文；非空 = 只转发这些博主（handle 不分大小写）。
    """
    targets = []
    for x in (raw or []):
        if isinstance(x, dict):
            try:
                gid = int(x.get("id") or 0)
            except (TypeError, ValueError):
                continue
            feeds = [str(f).strip().lstrip("@").lower()
                     for f in (x.get("feeds") or []) if str(f).strip()]
        else:
            try:
                gid = int(x)
            except (TypeError, ValueError):
                continue
            feeds = []
        if gid > 0 and all(t["id"] != gid for t in targets):
            targets.append({"id": gid, "feeds": feeds})
    return targets


def load_config(path: str = "") -> dict:
    p = Path(path) if path else BASE / "relay.json"
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    # 订阅写在运行时状态文件里（与 core/settings.py 同一约定）。它会随云端
    # sandbox 保留，而 relay.json 每次部署都会被包里的版本覆盖；优先读状态
    # 文件，重新发布后就不必再手工重新绑定博主。
    state = BASE / "data" / "relay_targets.json"
    if p.resolve() == (BASE / "relay.json").resolve() and state.exists():
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
            # 只认带版本标记的状态文件：旧版备份可能是过期的空订阅
            if s.get("schema") == 2 and "target_groups" in s:
                cfg["target_groups"] = s["target_groups"]
        except Exception as e:
            LOG.warning("订阅状态读取失败，回退到 relay.json: %s", e)
    cfg["_path"] = str(p)
    cfg["targets"] = normalize_targets(cfg.get("target_groups"))
    cfg["target_groups"] = [t["id"] for t in cfg["targets"]]
    for k in ("source_groups",):
        cfg[k] = [int(x) for x in (cfg.get(k) or [])]
    cfg["source_qq"] = [str(x) for x in (cfg.get("source_qq") or [])]
    cfg["keywords"] = list(cfg.get("keywords") or [])
    cfg["drop_keywords"] = list(cfg.get("drop_keywords") or [])
    return cfg


def post_json(url: str, payload: dict, token: str = "", timeout: float = 25) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_json(url: str, timeout: float = 5) -> dict:
    with OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------- OneBot v11 反向 WebSocket（NapCat 主动连入，发送动作通道） ----------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class OneBotWS:
    """极简反向 WS 服务端：NapCat 按 websocketClients 配置连入 ws://host:port。

    背景：NapCat 的 OneBot HTTP 服务端在云端 sandbox 绑不上端口（原因未明），
    改走反向 WS 发送 send_group_msg 等动作；事件上报仍走 HTTP。
    只实现握手 / 文本帧 / ping-pong / close，纯标准库。
    """

    def __init__(self, host: str, port: int, on_event=None):
        self.host, self.port = host, int(port)
        self.conn = None                      # 当前 NapCat 连接
        self.lock = threading.Lock()
        self._echo_n = 0
        self._echo_ev = threading.Event()
        self._echo_result = None
        self.stop_ev = threading.Event()
        self.connected_since = 0.0
        # NapCat 的 websocketClients 也会把群消息事件推过来；旧版只收 echo 响应，
        # 普通事件全部丢弃 → 转发器 matched 永远为 0。这里交给 Relay.on_event 处理，
        # 与 HTTP 上报形成双通道（重复事件由 Dedup 按 message_id 去重）。
        self.on_event = on_event
        self._evq: queue.Queue = queue.Queue()
        threading.Thread(target=self._event_worker, daemon=True,
                         name="relay-ws-events").start()

    def _event_worker(self):
        while not self.stop_ev.is_set():
            try:
                ev = self._evq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self.on_event:
                    self.on_event(ev)
            except Exception:
                LOG.exception("处理反向WS 上报事件异常")

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="relay-ws").start()

    def _serve(self):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(2)
            s.settimeout(0.5)
        except OSError as e:
            LOG.error("反向WS 监听失败 %s:%s: %s", self.host, self.port, e)
            return
        LOG.info("反向WS 服务端就绪 ws://%s:%s（等待 NapCat 连入）", self.host, self.port)
        while not self.stop_ev.is_set():
            try:
                c, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(c, addr),
                             daemon=True, name="relay-ws-cli").start()
        try:
            s.close()
        except Exception:
            pass

    @staticmethod
    def _recv_exact(c: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return buf

    def _read_frame(self, c: socket.socket):
        h = self._recv_exact(c, 2)
        op = h[0] & 0x0F
        masked = bool(h[1] & 0x80)
        ln = h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(c, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(c, 8))[0]
        mask = self._recv_exact(c, 4) if masked else None
        data = self._recv_exact(c, ln) if ln else b""
        if masked and data:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return op, data

    def _send_frame(self, c: socket.socket, op: int, data: bytes):
        h = bytearray([0x80 | op])
        n = len(data)
        if n < 126:
            h.append(n)
        elif n < 65536:
            h.append(126)
            h += struct.pack(">H", n)
        else:
            h.append(127)
            h += struct.pack(">Q", n)
        with self.lock:
            c.sendall(bytes(h) + data)

    def _client(self, c: socket.socket, addr):
        try:
            c.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    c.close()
                    return
                data += chunk
                if len(data) > 65536:
                    break
            key = ""
            for line in data.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            if not key:
                c.close()
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            c.settimeout(None)
            LOG.info("NapCat 反向WS 已连入 %s（发送通道就绪）", addr[0])
            with self.lock:
                self.conn = c
                self.connected_since = time.time()
            while not self.stop_ev.is_set():
                op, payload = self._read_frame(c)
                if op == 8:                       # close
                    break
                if op == 9:                       # ping → pong
                    self._send_frame(c, 10, payload)
                    continue
                if op != 1:
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                if "echo" in obj and ("retcode" in obj or "status" in obj):
                    self._echo_result = obj       # 动作响应
                    self._echo_ev.set()
                else:
                    self._evq.put(obj)            # 群消息/心跳等事件 → 转发器
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            with self.lock:
                if self.conn is c:
                    self.conn = None
            try:
                c.close()
            except Exception:
                pass
            LOG.info("NapCat 反向WS 断开 %s", addr[0])

    @property
    def connected(self) -> bool:
        with self.lock:
            c = self.conn
        return bool(c)

    def call(self, action: str, params: dict, token: str = "",
             timeout: float = 20) -> dict:
        """发送 OneBot 动作并等待响应。token 留空时依赖 NapCat 侧同样为空。"""
        with self.lock:
            c = self.conn
        if not c:
            raise OSError("反向WS 未连接")
        self._echo_n += 1
        echo = "relay-%s" % self._echo_n
        with self.lock:
            self._echo_result = None
        self._echo_ev.clear()
        self._send_frame(c, 1, json.dumps(
            {"action": action, "params": params, "echo": echo},
            ensure_ascii=False).encode("utf-8"))
        if not self._echo_ev.wait(timeout):
            raise TimeoutError("反向WS 动作超时: %s" % action)
        return self._echo_result or {}


# ---------- 消息段处理 ----------

def normalize_segments(message) -> list:
    """把 OneBot 的 array 格式或 CQ 码字符串统一成 [{'type','data'}]。"""
    if isinstance(message, str):
        out = []
        for part in re.split(r"(\[CQ:[^\]]+\])", message):
            if not part:
                continue
            m = re.match(r"\[CQ:([a-zA-Z_]+)(?:,(.*))?\]", part, re.S)
            if not m:
                out.append({"type": "text", "data": {"text": part}})
                continue
            typ, raw = m.group(1), m.group(2) or ""
            data = {}
            for kv in raw.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    data[k] = v.replace("&amp;", "&").replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
            out.append({"type": typ, "data": data})
        return out
    if isinstance(message, list):
        out = []
        for s in message:
            if isinstance(s, dict) and s.get("type"):
                d = s.get("data") if isinstance(s.get("data"), dict) else {}
                out.append({"type": str(s["type"]), "data": dict(d)})
        return out
    return []


def plain_text(segments: list) -> str:
    return "".join(str(s.get("data", {}).get("text", ""))
                   for s in segments if s.get("type") == "text")


# ---------- 直投（服务端 → 转发器 → 群，不经过官方机器人） ----------
MAX_DIRECT_MEDIA_MB = 5      # 与 QQ 官方机器人富媒体上限一致


def _file_segment(value, typ: str):
    """把「本地路径 / http 直链 / base64://」统一成 OneBot 媒体段。

    本地文件优先转 base64：NapCat 跑在容器里、并不走系统代理，X 的图片
    CDN（pbs.twimg.com）在墙内直连多半拉不到，交给 NapCat 自己下载会失败
    且失败得很难排查。服务端已经把图落到 data/media/ 了，直接回传字节最稳。
    """
    v = str(value or "").strip()
    if not v:
        return None
    if v.startswith("base64://"):
        return {"type": typ, "data": {"file": v}}
    if v.startswith(("http://", "https://")):
        return {"type": typ, "data": {"file": v}}
    try:
        p = Path(v)
        if p.is_file():
            size = p.stat().st_size
            if size > MAX_DIRECT_MEDIA_MB * 1048576:
                LOG.warning("直投媒体超过 %dMB，跳过: %s (%.1f MB)",
                            MAX_DIRECT_MEDIA_MB, p.name, size / 1048576)
                return None
            return {"type": typ,
                    "data": {"file": "base64://" + base64.b64encode(p.read_bytes()).decode()}}
        LOG.warning("直投媒体不存在，跳过: %s", v)
    except Exception as e:
        LOG.warning("直投媒体读取失败 %s: %s", v, e)
    return None


def build_push_segments(obj: dict) -> list:
    """POST /push 的 body → OneBot 消息段。text → images → videos 顺序发送。"""
    segs = []
    text = str(obj.get("text") or "")
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    for u in (obj.get("images") or [])[:6]:
        s = _file_segment(u, "image")
        if s:
            segs.append(s)
    for v in (obj.get("videos") or [])[:3]:
        s = _file_segment(v, "video")
        if s:
            segs.append(s)
    return segs


AUTHOR_RE = re.compile(r"\(@([A-Za-z0-9_]{1,20})\)")


def msg_seq(key) -> int | None:
    """OneBot message_id 是同一群里单调递增的整数，用来判断媒体属于哪条文本。

    官方机器人把一条推文拆成「1 条文本 + N 条独立图片」，图片消息本身没有
    作者/推文 ID。多源并发时只靠 user_id 关联必然串台，message_id 的先后
    关系是唯一可靠的归属线索。非数字 id（自建端/旧版本）退化为时间序。
    """
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def parse_author(text: str) -> str:
    """从推送文本「🐦 名字 (@handle) 更新推文」解析博主 handle（小写）。"""
    m = AUTHOR_RE.search(text or "")
    return m.group(1).lower() if m else ""


def clean_segments(segments: list) -> list:
    """清洗跨群无效段：去掉 at/reply，卡片降级成文本，媒体段只保留必要字段。"""
    out = []
    for s in segments:
        typ, d = s.get("type"), s.get("data", {})
        if typ in ("at", "reply", "forward", "anonymous"):
            continue
        if typ in DROP_TYPES or typ not in KEEP_TYPES:
            txt = d.get("text") or d.get("summary") or d.get("content") or ""
            if txt:
                out.append({"type": "text", "data": {"text": ("[%s] %s" % (typ, txt))[:300]}})
            continue
        if typ == "text":
            txt = str(d.get("text") or "").strip()
            if txt:
                out.append({"type": "text", "data": {"text": txt}})
            continue
        nd = {k: d[k] for k in ("file", "url", "text", "name", "id", "type",
                                "sub_type", "summary", "file_id", "path", "qq", "magic")
              if k in d}
        if typ in MEDIA_TYPES and not (nd.get("file") or nd.get("url") or nd.get("path")):
            continue
        out.append({"type": typ, "data": nd})
    return out


class Dedup:
    """sqlite 持久化去重：转发器重启也不会把重放的消息再发一遍。"""

    def __init__(self, db_path: str):
        if db_path == ":memory:":
            self.path = ":memory:"
        else:
            self.path = str(BASE / db_path)
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, ts REAL)")
        self.db.commit()

    def seen(self, key: str, window_minutes: float) -> bool:
        now = time.time()
        cutoff = now - max(1, window_minutes) * 60
        with self.lock:
            try:
                self.db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
                self.db.execute("INSERT INTO seen(k, ts) VALUES(?, ?)", (key, now))
                self.db.commit()
                return False
            except sqlite3.IntegrityError:
                return True
            except Exception as e:
                LOG.warning("去重库异常（放行）: %s", e)
                return False


# 同时最多保留多少个「还没到齐」的 bundle，防止异常情况下无限堆积
MAX_OPEN_BUNDLES = 8


class Bundle:
    def __init__(self, gid: int, uid: int, segments: list, key: str,
                 wait: float, max_wait: float):
        now = time.time()
        self.group_id = gid
        self.user_id = uid
        self.segments = segments
        self.keys = [key]
        self.deadline = now + max(0.2, wait)
        self.hard_deadline = now + max(1.0, max_wait)
        self.created = now
        self.author = parse_author(plain_text(segments))
        self.seq = msg_seq(key)          # 首条文本消息的序号
        self.last_seq = self.seq         # 已并入的最后一条消息序号

    def summary(self):
        return {"group_id": self.group_id, "user_id": self.user_id,
                "segments": len(self.segments),
                "author": self.author, "seq": self.seq,
                "age": round(time.time() - self.created, 1)}


class Relay:
    def __init__(self, cfg: dict, probe: bool = False):
        self.cfg = cfg
        # load_config() 已把订阅归一化到 cfg["targets"]，并把 target_groups
        # 拍平成纯群号列表供 sendtest/status 使用。这里若再按 target_groups
        # 归一化一次，feeds 会被清空 —— 正是「每次部署后订阅回退成空、
        # 必须手工热重载一次才恢复」的真根因。
        base = cfg.get("targets")
        if base is None:                    # selftest 等直接传原始 dict
            base = cfg.get("target_groups")
        cfg["targets"] = normalize_targets(base)
        cfg["target_groups"] = [t["id"] for t in cfg["targets"]]
        self.probe = probe
        self.dedup = Dedup(cfg["db_path"])
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        # 多个 bundle 并存：多源并发推送时，新文本不再把上一条还没收完图片
        # 的 bundle 提前冲掉（旧实现会丢图，且把图片并进错误的推文）。
        self.bundles: list = []
        self.stop_ev = threading.Event()
        self.threads = []
        self._serve_thread = None
        self._last_send = 0.0
        self._send_times = []
        self.stats = {"received": 0, "matched": 0, "forwarded": 0,
                      "failed": 0, "skipped": 0, "dup": 0, "sent_messages": 0,
                      "direct": 0, "direct_failed": 0}
        self.recent = []
        self.recent_events = []          # 最近收到的原始事件摘要（排障用）
        self.ws = OneBotWS(cfg.get("ws_host", "127.0.0.1"),
                           cfg.get("ws_port", 5802), on_event=self.on_event)

    # ---------- 事件入口 ----------
    def reload_config(self) -> bool:
        """重读 relay.json（目标群/订阅变更后热生效，不重启不丢状态）。"""
        try:
            new_cfg = load_config(self.cfg.get("_path") or "")
        except Exception as e:
            LOG.warning("配置重载失败: %s", e)
            return False
        with self.lock:
            self.cfg = new_cfg
        LOG.info("配置已热重载：转发目标 %s", new_cfg["targets"])
        return True

    def on_event(self, ev):
        if not isinstance(ev, dict):
            return
        with self.lock:
            self.stats["received"] += 1
            try:
                _t = ev.get("message")
                if isinstance(_t, list):
                    _t = "".join(str(s.get("data", {}).get("text", ""))
                                 for s in _t if isinstance(s, dict) and s.get("type") == "text")
                self.recent_events.insert(0, {
                    "time": time.strftime("%H:%M:%S"),
                    "post_type": ev.get("post_type"), "message_type": ev.get("message_type"),
                    "group_id": ev.get("group_id"), "user_id": ev.get("user_id"),
                    "text": str(_t or "")[:60]})
                del self.recent_events[20:]
            except Exception:
                pass
        if ev.get("post_type") != "message" or ev.get("message_type") != "group":
            return
        if self.cfg.get("direct") and not self.probe:
            # 直投模式：内容由服务端 POST /push 直接送达转发器，不再监听群消息。
            # 否则「官方机器人发群」+「服务端直投」会把同一条推文发两遍。
            return
        try:
            gid = int(ev.get("group_id") or 0)
            uid = int(ev.get("user_id") or 0)
        except (TypeError, ValueError):
            return
        if self.cfg["source_groups"] and gid not in self.cfg["source_groups"]:
            return

        segs = clean_segments(normalize_segments(ev.get("message")))
        text = plain_text(segs)
        if self.probe:
            sender = ev.get("sender") or {}
            LOG.info("[PROBE] 群=%s 群名=%s QQ=%s 昵称=%s 段=%s 文本=%r",
                     gid, ev.get("group_name") or "-", uid,
                     sender.get("nickname") or sender.get("card") or "-",
                     [s["type"] for s in segs], text[:110])
            return
        if self.cfg["source_qq"] and str(uid) not in self.cfg["source_qq"]:
            return

        key = str(ev.get("message_id") or
                  hashlib.md5(("%s|%s|%s" % (gid, uid, text)).encode("utf-8")).hexdigest())
        seq = msg_seq(key)
        matched = (not self.cfg["keywords"] or
                   any(k in text for k in self.cfg["keywords"])) and \
                  not any(k in text for k in self.cfg["drop_keywords"])
        media = [s for s in segs if s["type"] in MEDIA_TYPES]

        with self.lock:
            # 先判断这条事件有没有可能进 bundle，再查去重库：小号加的所有群
            # 都会上报（含闲聊），否则每次闲聊都白白写一次 sqlite。
            b = None if matched else self._pick_bundle_locked(gid, uid, seq)
            if not matched and (not media or b is None):
                self.stats["skipped"] += 1
                return
            if self.dedup.seen(key, self.cfg["dedup_minutes"]):
                self.stats["dup"] += 1
                return

            if matched:
                self._prune_locked()
                if len(self.bundles) >= MAX_OPEN_BUNDLES:
                    self._flush_locked(self.bundles[0])
                self.bundles.append(Bundle(
                    gid, uid, segs, key,
                    self.cfg["bundle_wait_seconds"],
                    self.cfg["bundle_max_seconds"]))
                self.stats["matched"] += 1
                LOG.info("命中推文 [群%s QQ%s] seq=%s 段=%s 文本=%r",
                         gid, uid, seq, [s["type"] for s in segs], text[:70])
                return

            # 媒体消息：按 message_id 邻近度归到对应文本，允许晚到的图片
            # 仍然追上自己的 bundle（即使期间已经来了别的推文文本）。
            b.segments.extend(media)
            b.keys.append(key)
            if seq is not None:
                b.last_seq = max(b.last_seq or seq, seq)
            b.deadline = min(b.hard_deadline,
                             time.time() + max(0.5, self.cfg["bundle_wait_seconds"] / 2))
            LOG.info("并入媒体 [群%s QQ%s] seq=%s → 推文 seq=%s +%d 段",
                     gid, uid, seq, b.seq, len(media))

    def _pick_bundle_locked(self, gid: int, uid: int, seq):
        """挑出这条媒体最可能属于的 bundle：同群同发送者、未超时、序号最近的前一条文本。"""
        live = [x for x in self.bundles
                if x.group_id == gid and x.user_id == uid
                and time.time() < x.hard_deadline]
        if not live:
            return None
        if seq is None:
            return live[-1]
        prior = [x for x in live if x.last_seq is not None and x.last_seq < seq]
        if prior:
            return max(prior, key=lambda x: x.last_seq)
        return min(live, key=lambda x: abs((x.last_seq if x.last_seq is not None else seq) - seq))

    def _prune_locked(self):
        """清掉已经超过硬截止时间的 bundle（正常由 _bundle_loop 处理，这里是兜底）。"""
        now = time.time()
        for x in list(self.bundles):
            if now >= x.hard_deadline:
                self._flush_locked(x)

    def _flush_locked(self, b=None):
        """b=None 表示全部 flush；传入具体 bundle 只发它，其它继续收图。"""
        if b is None:
            pending, self.bundles = self.bundles, []
        else:
            if b not in self.bundles:
                return
            self.bundles.remove(b)
            pending = [b]
        for x in pending:
            if x.segments:
                self.q.put(x)

    # ---------- 后台线程 ----------
    def start(self, serve: bool = True):
        self.stop_ev.clear()
        self.ws.stop_ev.clear()
        self.ws.start()
        for name, fn in (("bundle", self._bundle_loop), ("sender", self._worker)):
            t = threading.Thread(target=fn, name="relay-" + name, daemon=True)
            t.start()
            self.threads.append(t)
        self.server = None
        if serve:
            self._serve_thread = threading.current_thread()
            self.server = ThreadingHTTPServer(
                (self.cfg["listen_host"], int(self.cfg["listen_port"])), Handler)
            self.server.daemon_threads = True
            self.server.relay = self
            LOG.info("转发器已启动：上报 http://%s:%s%s | OneBot %s | 目标群 %s | 模式=%s",
                     self.cfg["listen_host"], self.cfg["listen_port"],
                     self.cfg["report_path"], self.cfg["onebot_url"],
                     self.cfg["target_groups"], "probe" if self.probe else "转发")
            try:
                self.server.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        return self

    def stop(self):
        self.stop_ev.set()
        self.ws.stop_ev.set()
        with self.lock:
            self._flush_locked()
        self.q.put(None)
        # shutdown() 必须由 serve_forever 之外的线程调用，否则会死锁
        if self.server and threading.current_thread() is not self._serve_thread:
            try:
                self.server.shutdown()
            except Exception:
                pass

    def _bundle_loop(self):
        while not self.stop_ev.is_set():
            with self.lock:
                now = time.time()
                for b in [x for x in self.bundles if now >= x.deadline]:
                    LOG.info("bundle 到齐，开始转发（seq=%s %d 段，仍有 %d 个收集中）",
                             b.seq, len(b.segments), len(self.bundles) - 1)
                    self._flush_locked(b)
            self.stop_ev.wait(0.2)

    def _worker(self):
        while True:
            try:
                b = self.q.get(timeout=0.5)
            except queue.Empty:
                if self.stop_ev.is_set():
                    break
                continue
            if b is None:
                break
            try:
                self._forward(b)
            except Exception as e:
                LOG.exception("转发异常: %s", e)
                with self.lock:
                    self.stats["failed"] += 1

    def _throttle(self):
        now = time.time()
        gap = float(self.cfg["min_interval_seconds"])
        if gap > 0 and self._last_send:
            wait = gap - (now - self._last_send)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.5))
        now = time.time()
        self._send_times = [t for t in self._send_times if now - t < 60]
        limit = int(self.cfg["max_per_minute"])
        if limit > 0 and len(self._send_times) >= limit:
            nap = 60 - (now - self._send_times[0]) + 1
            LOG.warning("达到每分钟上限 %d，休眠 %.1fs（防风控）", limit, nap)
            time.sleep(max(1, nap))
        self._last_send = time.time()
        self._send_times.append(self._last_send)

    def _send_one(self, gid: int, segments: list) -> bool:
        payload = {"group_id": gid, "message": segments}
        url = self.cfg["onebot_url"].rstrip("/") + "/send_group_msg"
        err = ""
        for attempt in range(3):
            try:
                self._throttle()
                r = None
                if self.ws.connected:
                    # 首选反向 WS（NapCat HTTP 服务端在云端绑不上端口）
                    try:
                        r = self.ws.call("send_group_msg", payload,
                                         self.cfg["onebot_token"])
                    except Exception as e:
                        LOG.warning("反向WS 发送失败，转 HTTP: %s", e)
                        r = None
                if r is None:
                    r = post_json(url, payload, self.cfg["onebot_token"])
                if r.get("status") in ("ok", "async") or r.get("retcode") == 0:
                    with self.lock:
                        self.stats["sent_messages"] += 1
                    return True
                err = str(r)[:160]
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:160])
            if attempt < 2:
                LOG.warning("发送失败(第%d次) 群%s: %s", attempt + 1, gid, err)
                time.sleep(3 * (attempt + 1))
        LOG.error("发送失败，已放弃 群%s: %s", gid, err)
        return False

    def _forward(self, b: Bundle):
        segments = list(b.segments)
        if self.cfg.get("prefix"):
            segments.insert(0, {"type": "text", "data": {"text": self.cfg["prefix"]}})
        # 按目标群各自的博主订阅过滤（feeds 空 = 全部；解析不到博主 = 只发订阅空的群）
        with self.lock:
            targets = [dict(t) for t in self.cfg["targets"]]
        sel = [t["id"] for t in targets
               if not t["feeds"] or (b.author and b.author in t["feeds"])]
        if not sel:
            with self.lock:
                self.stats["skipped"] += 1
            LOG.info("博主 %s 不在任何转发目标的订阅里，跳过", b.author or "未知")
            return
        ok_any = False
        for gid in sel:
            ok = self._send_one(gid, segments)
            if not ok and self.cfg["split_media_on_fail"] and len(segments) > 1:
                LOG.info("合并发送失败，改为逐段发送 群%s", gid)
                text_segs = [s for s in segments if s["type"] == "text"]
                media_segs = [s for s in segments if s["type"] in MEDIA_TYPES]
                ok = self._send_one(gid, text_segs or segments)
                for s in media_segs:
                    ok = self._send_one(gid, [s]) and ok
            if ok:
                ok_any = True
            else:
                self._note(gid, b, "failed")
        with self.lock:
            if ok_any:
                self.stats["forwarded"] += 1
                self.recent.insert(0, {"time": time.strftime("%H:%M:%S"),
                                       "group": b.group_id, "user": b.user_id,
                                       "segments": len(segments), "ok": True})
            else:
                self.stats["failed"] += 1
                self.recent.insert(0, {"time": time.strftime("%H:%M:%S"),
                                       "group": b.group_id, "user": b.user_id,
                                       "segments": len(segments), "ok": False})
            del self.recent[20:]
        if ok_any:
            self._note_source(b.group_id, b.user_id)
        LOG.info("转发%s：%d 段 → 群 %s", "成功" if ok_any else "失败",
                 len(segments), sel)

    def _note(self, gid, b, tag):
        LOG.warning("目标群 %s %s（检查小号是否在群里/是否被禁言）", gid, tag)

    # ---------- 掉线恢复：补拉来源群历史，补转漏掉的推文 ----------
    def _note_source(self, gid: int, uid: int):
        """成功转发后落盘来源提示，供进程重启后的 recover 使用。"""
        try:
            SOURCE_HINT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SOURCE_HINT_PATH.write_text(json.dumps(
                {"group_id": gid, "user_id": uid, "updated_at": time.time()},
                ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            LOG.debug("[recover] 来源提示写入失败: %s", e)

    def _source_hint(self):
        """取「来源群 / 机器人 QQ」：内存记录 > 运行时状态 > relay.json 兜底。

        relay.json 的 source_groups/source_qq 在云端通常留空（监听所有群），
        补拉历史必须精确限定到「机器人推文所在的那个群和那个 QQ」，否则会
        把无关群聊也扫一遍。部署后首次 recover 内存为空，靠后两级兜底。
        """
        with self.lock:
            for r in self.recent:
                if r.get("ok"):
                    return int(r.get("group") or 0), int(r.get("user") or 0)
        try:
            h = json.loads(SOURCE_HINT_PATH.read_text(encoding="utf-8"))
            if h.get("group_id"):
                return int(h["group_id"]), int(h.get("user_id") or 0)
        except Exception:
            pass
        gid = int(self.cfg.get("source_group_hint") or 0)
        uid = int(self.cfg.get("source_qq_hint") or 0)
        if not gid:
            groups = self.cfg.get("source_groups") or []
            gid = int(groups[0]) if groups else 0
        return gid, uid

    def recover(self, minutes: int = 120, limit: int = 100,
                since_ts: float = 0) -> dict:
        """小号重新登录后，把掉线窗口内漏转的推文补发一次。

        直接复用 on_event 的过滤 / bundle / sqlite 去重：已经转发过的
        message_id 会被 Dedup 拦掉，只有掉线期间 NapCat 没上报过的消息才
        真正发出，因此重复调用也不会刷屏。since_ts 可把窗口下限定在「掉线
        开始的那一刻」，避免补发掉线前已经转发过的推文。NapCat 不支持该
        扩展动作时返回 ok=False，主链路不受影响（可手工回推补漏）。
        """
        if self.cfg.get("direct"):
            # 直投模式没有「来源群监听」，补拉群历史只会在小号恢复时
            # 把机器人早先发过的推文重转一遍 —— 正是要避免的重复。
            return {"ok": True, "skipped": "direct 模式不做群历史补拉"}
        if not self.ws.connected:
            return {"ok": False, "error": "反向WS 未连接，稍后重试"}
        gid, uid = self._source_hint()
        if not gid:
            return {"ok": False, "error": "未知来源群（尚无成功转发记录）"}
        r, err = {}, ""
        # 不同 NapCat 版本的参数名不一致，逐个尝试
        for params in ({"group_id": gid, "message_seq": 0, "count": limit,
                        "reverseOrder": False},
                       {"group_id": gid, "message_id": 0, "count": limit},
                       {"group_id": gid, "count": limit}):
            try:
                r = self.ws.call("get_group_msg_history", params,
                                 self.cfg.get("onebot_token") or "", timeout=15)
                if r.get("retcode") == 0 or r.get("status") == "ok":
                    err = ""
                    break
                err = str(r.get("message") or r.get("wording") or r)[:120]
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:120])
        data = r.get("data")
        msgs = (data.get("messages") if isinstance(data, dict) else data) or []
        if not isinstance(msgs, list) or not msgs:
            return {"ok": False, "group_id": gid, "scanned": 0,
                    "error": "get_group_msg_history 无消息: " + err}
        cutoff = max(time.time() - max(10, minutes) * 60,
                     float(since_ts or 0))
        picked = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            try:
                ts = int(m.get("time") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts and ts < cutoff:
                continue
            try:
                if uid and int(m.get("user_id") or 0) != uid:
                    continue
            except (TypeError, ValueError):
                continue
            picked.append(m)
        # 正序喂入：文本在前、媒体在后，bundle 才能按 message_id 邻近度合并
        picked.sort(key=lambda m: (int(m.get("time") or 0) if str(
            m.get("time") or "").isdigit() else 0,
            msg_seq(str(m.get("message_id") or "")) or 0))
        for i, m in enumerate(picked):
            ev = dict(m)
            ev.setdefault("post_type", "message")
            ev.setdefault("message_type", "group")
            ev["group_id"] = gid
            if not ev.get("message_id"):
                ev["message_id"] = "recover-%s-%s-%s" % (gid, m.get("time") or 0, i)
            self.on_event(ev)
        LOG.info("[recover] 来源群%s 扫描 %d 条，回放 %d 条（%d 分钟内，QQ=%s）",
                 gid, len(msgs), len(picked), minutes, uid or "不限")
        return {"ok": True, "group_id": gid, "scanned": len(msgs),
                "replayed": len(picked), "minutes": minutes,
                "source_qq": uid, "since_ts": float(since_ts or 0)}

    def status(self) -> dict:
        with self.lock:
            return {"ok": True, "probe": self.probe,
                    "ws_connected": self.ws.connected,
                    "direct": bool(self.cfg.get("direct")),
                    "onebot_url": self.cfg["onebot_url"],
                    "report": "http://%s:%s%s" % (self.cfg["listen_host"],
                                                  self.cfg["listen_port"],
                                                  self.cfg["report_path"]),
                    "target_groups": self.cfg["target_groups"],
                    "targets": [dict(t) for t in self.cfg["targets"]],
                    "source_groups": self.cfg["source_groups"],
                    "source_qq": self.cfg["source_qq"],
                    "config_path": self.cfg.get("_path"),
                    "stats": dict(self.stats), "queue": self.q.qsize(),
                    "bundle": self.bundles[-1].summary() if self.bundles else None,
                    "bundles": [x.summary() for x in self.bundles],
                    "recent": list(self.recent),
                    "recent_events": list(self.recent_events)}


class Handler(BaseHTTPRequestHandler):
    @property
    def relay(self) -> Relay:
        return self.server.relay

    def _reply(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        """兼容 NapCat 的 chunked 上报：只读 Content-Length 会拿到空 body，
        事件被解析成 {} 后静默丢弃（received 会涨但 matched 永远为 0）。"""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            buf = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";", 1)[0], 16)
                except ValueError:
                    break
                if size == 0:      # 读完 trailer
                    while True:
                        t = self.rfile.readline()
                        if not t or t in (b"\r\n", b"\n"):
                            break
                    break
                buf += self.rfile.read(size)
                self.rfile.read(2)  # CRLF
            return buf
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/push":
            # 直投：服务端把推文直接交给转发器，由小号发到目标群。
            # 不再依赖「官方机器人先发一遍群、小号监听再抄」这条链。
            try:
                raw = self._read_body()
                obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                segs = build_push_segments(obj)
                if not segs:
                    self._reply(400, {"ok": False, "error": "内容为空（text/images/videos 都没有）"})
                    return
                raw_groups = obj.get("groups") or self.relay.cfg["target_groups"]
                gids = []
                for g in raw_groups:
                    try:
                        gids.append(int(g))
                    except (TypeError, ValueError):
                        continue
                if not gids:
                    self._reply(400, {"ok": False, "error": "没有目标群"})
                    return
                results, ok_any = {}, False
                for gid in gids:
                    ok = self.relay._send_one(gid, segs)
                    results[str(gid)] = ok
                    ok_any = ok or ok_any
                with self.relay.lock:
                    self.relay.stats["direct" if ok_any else "direct_failed"] += 1
                    self.relay.recent.insert(0, {
                        "time": time.strftime("%H:%M:%S"), "group": gids[0],
                        "user": 0, "segments": len(segs), "ok": ok_any,
                        "via": "direct"})
                    del self.relay.recent[20:]
                LOG.info("直投%s：%d 段 → 群 %s", "成功" if ok_any else "失败",
                         len(segs), gids)
                self._reply(200, {"ok": ok_any, "results": results,
                                  "segments": len(segs)})
            except Exception as e:
                LOG.exception("直投异常: %s", e)
                self._reply(500, {"ok": False, "error": str(e)[:200]})
            return
        if path == "/targets":
            # 控制台直接下发订阅：两进程若因部署目录/cwd 不同读到了不同的
            # relay.json，靠文件同步永远对不齐；这里以控制台解析结果为准。
            try:
                raw = self._read_body()
                obj = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                targets = normalize_targets(obj.get("target_groups"))
                if not targets:
                    self._reply(400, {"error": "target_groups 为空"})
                    return
                with self.relay.lock:
                    self.relay.cfg["targets"] = targets
                    self.relay.cfg["target_groups"] = [t["id"] for t in targets]
                self._reply(200, {"ok": True, "targets": targets})
            except Exception as e:
                self._reply(400, {"error": str(e)})
            return
        if path == "/reload":
            ok = self.relay.reload_config()
            self._reply(200, {"ok": ok,
                              "targets": [dict(t) for t in self.relay.cfg["targets"]]})
            return
        if urlparse(self.path).path != self.relay.cfg["report_path"]:
            self._reply(404, {"error": "not found"})
            return
        try:
            raw = self._read_body()
            ev = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception as e:
            self._reply(400, {"error": str(e)})
            return
        # NapCat 等待响应，必须立刻回 200；重活放后台
        self._reply(200, {"status": "ok"})
        try:
            self.relay.on_event(ev)
        except Exception as e:
            LOG.exception("处理上报事件异常: %s", e)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/reload":
            ok = self.relay.reload_config()
            self._reply(200, {"ok": ok,
                              "targets": [dict(t) for t in self.relay.cfg["targets"]]})
            return
        if path == "/groups":
            try:
                r = self.relay.ws.call("get_group_list", {}, timeout=10)
                groups = [{"group_id": g.get("group_id"),
                           "group_name": g.get("group_name")}
                          for g in (r.get("data") or [])]
                tmap = {int(t["id"]): (t.get("feeds") or [])
                        for t in self.relay.cfg.get("targets") or []}
                targets = []
                for g in groups:
                    try:
                        gid = int(g.get("group_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if gid in tmap:
                        g["feeds"] = tmap[gid]
                        targets.append(g)
                self._reply(200, {"ok": True, "ws": self.relay.ws.connected,
                                  "groups": groups, "targets": targets})
            except Exception as e:
                self._reply(200, {"ok": False, "ws": self.relay.ws.connected,
                                  "groups": [], "targets": [],
                                  "error": str(e)[:150]})
            return
        if path == "/sendtest":
            ok = self.relay._send_one(int(self.relay.cfg["target_groups"][0]),
                                      [{"type": "text",
                                        "data": {"text": "转发器 sendtest ✓（反向WS 通道）"}}])
            self._reply(200, {"ok": ok, "ws": self.relay.ws.connected})
            return
        if path == "/recover":
            q = parse_qs(urlparse(self.path).query)
            try:
                minutes = max(10, min(240, int((q.get("minutes") or [120])[0])))
            except (TypeError, ValueError):
                minutes = 120
            try:
                since_ts = float((q.get("since_ts") or [0])[0])
            except (TypeError, ValueError):
                since_ts = 0
            self._reply(200, self.relay.recover(minutes, since_ts=since_ts))
            return
        if path not in ("/", "/status"):
            self._reply(404, {"error": "not found"})
            return
        st = self.relay.status()
        if path == "/status":
            self._reply(200, st)
            return
        html = ("<!doctype html><meta charset=utf-8><title>小号转发器</title>"
                "<meta http-equiv=refresh content=10>"
                "<style>body{font:15px/1.6 system-ui;padding:24px;max-width:760px}"
                "pre{background:#f5f5f5;padding:14px;border-radius:8px;overflow:auto}</style>"
                "<h2>QQ 小号转发器</h2><pre>" +
                json.dumps(st, ensure_ascii=False, indent=2).replace("<", "&lt;") +
                "</pre>")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ---------- 子命令 ----------

def cmd_serve(args, cfg):
    setup_logging(cfg["log_path"], args.verbose)
    Relay(cfg, probe=(args.cmd == "probe")).start(serve=True)


def cmd_status(args, cfg):
    url = "http://%s:%s/status" % (cfg["listen_host"], cfg["listen_port"])
    try:
        print(json.dumps(get_json(url), ensure_ascii=False, indent=2))
    except Exception as e:
        print("转发器未运行或无法连接 %s: %s" % (url, e))
        print("先执行: python relay.py")


def cmd_setup(args, cfg):
    setup_logging(cfg["log_path"])
    roots = [BASE / "napcat" / "napcat" / "config", BASE / "napcat" / "config"]
    found = []
    for d in roots:
        if d.exists():
            found += [p for p in sorted(d.glob("onebot11_*.json")) if "10000" not in p.name]
    if not found:
        print("未找到 NapCat 的 onebot11_<QQ>.json，请确认 NapCat 已安装并登录过一次。")
        print("也可在 NapCat WebUI(6099) 手动把 HTTP 上报地址改成：\n  %s" %
              ("http://%s:%s%s" % (cfg["listen_host"], cfg["listen_port"], cfg["report_path"])))
        return 1
    url = "http://%s:%s%s" % (cfg["listen_host"], cfg["listen_port"], cfg["report_path"])
    for p in found:
        try:
            conf = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print("跳过 %s（解析失败 %s）" % (p, e))
            continue
        bak = p.with_suffix(p.suffix + ".bak." + time.strftime("%Y%m%d%H%M%S"))
        bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        net = conf.setdefault("network", {})
        net["httpClients"] = [{
            "name": "tw2qq-relay", "enable": True, "url": url,
            "messagePostFormat": "array", "reportSelfMessage": False,
            "token": cfg["onebot_token"],
        }]
        conf["reportSelfMessage"] = False
        p.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已修改 %s" % p)
        print("  上报地址 → %s" % url)
        print("  原配置备份 → %s" % bak.name)
    print("\n请重启 NapCat 使配置生效，然后运行：python relay.py probe")
    return 0


def cmd_sendtest(args, cfg):
    setup_logging(cfg["log_path"])
    segs = [{"type": "text", "data": {"text": "【转发器测试】小号转发通道已连通，稍后会自动搬运推文。"}}]
    for gid in cfg["target_groups"]:
        try:
            d = post_json(cfg["onebot_url"].rstrip("/") + "/send_group_msg",
                          {"group_id": gid, "message": segs}, cfg["onebot_token"])
            print("群 %s → %s" % (gid, str(d)[:200]))
        except Exception as e:
            print("群 %s 发送失败: %s" % (gid, e))
    print("若报「不在群内/被禁言」，先把小号拉进目标群。")
    return 0


def cmd_selftest(args, cfg):
    """本地 mock OneBot，验证过滤 / 合并 / 去重 / 清洗。"""
    setup_logging(cfg["log_path"], verbose=True)
    # 自检会触发「成功转发 → 落盘来源提示」，别污染本地运行时状态
    import tempfile
    globals()["SOURCE_HINT_PATH"] = Path(tempfile.mkdtemp()) / "relay_source.json"

    class Mock(BaseHTTPRequestHandler):
        calls = []

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            self.server.calls.append((self.path, body))
            out = json.dumps({"status": "ok", "retcode": 0,
                              "data": {"message_id": 12345}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    srv.calls = Mock.calls
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = dict(cfg)
    cfg.update({"onebot_url": "http://127.0.0.1:%d" % srv.server_address[1],
                "bundle_wait_seconds": 0.4, "bundle_max_seconds": 2,
                "min_interval_seconds": 0, "max_per_minute": 1000,
                "split_media_on_fail": False, "db_path": ":memory:",
                "target_groups": [999001],
                # 自检要覆盖监听/合并/补转这些老路径，直投模式会提前 return
                "direct": False,
                # load_config() 会带上真实订阅；自检要覆盖「全部转发」场景
                "targets": [{"id": 999001, "feeds": []}]})

    def ev(mid, gid, uid, segs):
        return {"post_type": "message", "message_type": "group", "sub_type": "normal",
                "message_id": mid, "group_id": gid, "user_id": uid,
                "message": segs, "sender": {"nickname": "tester"},
                "time": int(time.time())}

    r = Relay(cfg)
    r.start(serve=False)
    gid, bot, human = 888001, 10001, 10002
    tweet = "🐦 BB (@BB_33750) 更新推文\n⏱ 12:00\n────────\nhello\n────────\n🔗 原文：https://x.com/i/status/1"

    r.on_event(ev(1, gid, human, [{"type": "text", "data": {"text": "群友闲聊 https://x.com/abc"}}]))
    r.on_event(ev(2, gid, 10003, [{"type": "text", "data": {"text": "✅ 操作成功：已暂停"}}]))
    r.on_event(ev(3, gid, bot, [
        {"type": "reply", "data": {"id": "xyz"}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": tweet}},
    ]))
    r.on_event(ev(4, gid, bot, [{"type": "image", "data": {"file": "https://example.com/a.png"}}]))
    r.on_event(ev(5, gid, bot, [{"type": "image", "data": {"file": "https://example.com/b.gif"}}]))
    r.on_event(ev(3, gid, bot, [{"type": "text", "data": {"text": tweet}}]))  # 重复 message_id
    r.on_event(ev(6, gid, 10004, [{"type": "image", "data": {"file": "https://example.com/noise.png"}}]))
    r.on_event({"post_type": "meta_event", "meta_event_type": "heartbeat"})
    time.sleep(1.6)
    base = len(srv.calls)

    # ---- 阶段 2：多源并发交错（旧单 bundle 会冲掉上一条并丢图/串台）----
    tweet_a = ("🐦 AAA (@AAA_Author) 更新推文\n────────\nA 正文\n────────\n"
               "🔗 原文：https://x.com/i/status/10")
    tweet_b = ("🐦 BBB (@BBB_Author) 更新推文\n────────\nB 正文\n────────\n"
               "🔗 原文：https://x.com/i/status/20")
    r.on_event(ev(10, gid, bot, [{"type": "text", "data": {"text": tweet_a}}]))
    r.on_event(ev(11, gid, bot, [{"type": "image", "data": {"file": "https://example.com/a1.png"}}]))
    r.on_event(ev(20, gid, bot, [{"type": "text", "data": {"text": tweet_b}}]))
    r.on_event(ev(21, gid, bot, [{"type": "image", "data": {"file": "https://example.com/b1.png"}}]))
    r.on_event(ev(12, gid, bot, [{"type": "image", "data": {"file": "https://example.com/a2.png"}}]))
    time.sleep(1.6)

    # ---- 阶段 3：小号掉线恢复后补拉群历史（时间窗/机器人过滤 + 幂等去重）----
    stats_before_recover = dict(r.stats)
    now = int(time.time())
    hist = [
        {"message_id": 8101, "user_id": bot, "time": now - 300,
         "message": [{"type": "text", "data": {"text": tweet_a}}]},
        {"message_id": 8102, "user_id": bot, "time": now - 299,
         "message": [{"type": "image", "data": {"file": "https://example.com/r1.png"}}]},
        {"message_id": 8103, "user_id": human, "time": now - 298,
         "message": [{"type": "text", "data": {"text": "群友闲聊"}}]},
        {"message_id": 8104, "user_id": bot, "time": now - 9000,
         "message": [{"type": "text", "data": {"text": tweet_b}}]},
    ]

    class _FakeWS:
        connected = True

        def call(self, action, params, token="", timeout=20):
            if action == "get_group_msg_history":
                return {"status": "ok", "retcode": 0,
                        "data": {"messages": hist}}
            return {"status": "ok", "retcode": 0, "data": {}}

    real_ws, real_send = r.ws, r._send_one
    recovered = []
    r._send_one = lambda g, s: (recovered.append((g, list(s))) or True)
    r.ws = _FakeWS()
    r.recent.insert(0, {"time": "", "group": gid, "user": bot,
                        "segments": 1, "ok": True})
    out1 = r.recover(minutes=120)
    time.sleep(1.6)
    first = len(recovered)
    r.recover(minutes=120)          # 同一批历史再回放一次，必须不重复发
    time.sleep(1.6)
    second = len(recovered)
    out_since = r.recover(minutes=120, since_ts=float(now + 60))
    r.ws, r._send_one = real_ws, real_send
    r.stop()
    time.sleep(0.3)
    srv.shutdown()

    sends = [b for path, b in srv.calls[:base] if path.endswith("/send_group_msg")]
    sends2 = [b for path, b in srv.calls[base:] if path.endswith("/send_group_msg")]
    checks = []

    def check(name, cond, extra=""):
        checks.append((name, bool(cond), extra))

    check("推文+2张图合并成 1 条转发", len(sends) == 1, "实际 %d 条" % len(sends))
    if sends:
        segs = sends[0].get("message", [])
        types = [s.get("type") for s in segs]
        check("目标群正确", sends[0].get("group_id") == 999001)
        check("保留文本段", "text" in types)
        check("合并 2 个媒体段", types.count("image") == 2, str(types))
        check("清除 at/reply 段", "at" not in types and "reply" not in types, str(types))
        check("文本含推文特征", "更新推文" in plain_text(segs))
    check("闲聊/指令回复/重复消息被过滤",
          stats_before_recover["matched"] == 3 and stats_before_recover["dup"] >= 1,
          str(stats_before_recover))
    check("无发送失败", r.stats["failed"] == 0, str(r.stats))

    def media_files(b):
        return [s.get("data", {}).get("file") for s in b.get("message", [])
                if s.get("type") == "image"]

    got = {}
    for b in sends2:
        txt = plain_text(b.get("message", []))
        tag = "A" if "AAA_Author" in txt else ("B" if "BBB_Author" in txt else "?")
        got.setdefault(tag, []).append(b)
    check("并发两条推文各自成 bundle（不串台）", len(sends2) == 2,
          "实际 %d 条 %s" % (len(sends2), {k: [media_files(x) for x in v] for k, v in got.items()}))
    check("A 的文本+2张图（含晚到的）都保住",
          len(got.get("A", [])) == 1 and len(media_files(got["A"][0])) == 2,
          str({k: [media_files(x) for x in v] for k, v in got.items()}))
    check("B 的图并到 B 而不是 A",
          len(got.get("B", [])) == 1
          and media_files(got["B"][0]) == ["https://example.com/b1.png"],
          str({k: [media_files(x) for x in v] for k, v in got.items()}))
    check("所有 bundle 已收敛", r.status()["bundles"] == [], str(r.status()["bundles"]))

    # 回归：load_config() → Relay() 不得丢订阅（旧版双重归一化会把 feeds 清空）
    _lc = dict(load_config())
    _lc.update({"db_path": ":memory:", "ws_port": 0})
    _expected = [dict(t) for t in _lc["targets"]]   # Relay 会原地改 cfg，先快照
    _r2 = Relay(_lc)
    check("启动时订阅不丢失（load_config → Relay）",
          _r2.cfg["targets"] == _expected,
          "%s vs %s" % (_r2.cfg["targets"], _expected))
    _g0, _u0 = _r2._source_hint()
    check("重启后来源提示可从运行时状态恢复",
          _g0 == gid and _u0 == bot, "%s / %s" % (_g0, _u0))
    globals()["SOURCE_HINT_PATH"] = Path(tempfile.mkdtemp()) / "empty.json"
    _g, _u = _r2._source_hint()
    check("来源状态缺失时可从 relay.json 兜底",
          _g == 894183442 and _u == 4015477545, "%s / %s" % (_g, _u))
    check("恢复补转只回放时间窗内机器人消息",
          out1.get("ok") and out1.get("replayed") == 2, str(out1))
    check("补转合并文本+图片后发送 1 条", first == 1, "实际 %d 条" % first)
    check("补转图片归位", bool(recovered) and any(
        s.get("type") == "image" for s in recovered[0][1]))
    check("重复补转幂等不刷屏", second == first,
          "%d -> %d" % (first, second))
    check("补转时间下限生效（不补掉线前内容）",
          out_since.get("replayed") == 0, str(out_since))

    print("\n=== SELFTEST ===")
    for name, ok, extra in checks:
        print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + extra if extra and not ok else ""))
    print("  stats =", json.dumps(r.stats, ensure_ascii=False))
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="QQ 小号转发器（把官方机器人推文搬到无管理员权限的群）")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, help_text, extra=None):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", default="", help="指定 relay.json 路径")
        p.add_argument("--verbose", action="store_true")
        return p

    add("serve", "启动转发服务（默认）")
    add("probe", "只观察不转发，打印收到的群消息")
    add("status", "查看转发统计")
    add("setup", "修改 NapCat 上报地址指向本转发器")
    add("sendtest", "向目标群发一条测试文本")
    add("selftest", "本地自检")
    add("run", "同 serve")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "serve"
        args.config, args.verbose = "", False
    cfg = load_config(getattr(args, "config", ""))

    if args.cmd in ("serve", "probe", "run"):
        return cmd_serve(args, cfg)
    return {"status": cmd_status, "setup": cmd_setup, "sendtest": cmd_sendtest,
            "selftest": cmd_selftest}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main() or 0)

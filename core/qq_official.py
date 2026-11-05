# -*- coding: utf-8 -*-
"""QQ 官方机器人（开放平台 q.qq.com）客户端。

与 core.qq_bot.OneBotClient 接口对齐（鸭子类型），可无缝替换 monitor.bot：
  get_status() / get_group_list() / send_group_msg() / send_post()

特点：
- 机器人是独立身份，AppID+AppSecret 鉴权，**不占用/不登录任何个人 QQ 号**，
  无个人账号风控/封号风险（官方合规渠道）。
- 群主动消息 2026-06-22 起恢复：需群主在手机 QQ 机器人设置页打开"主动发言"开关。
  频控：20 条/分钟/群，1000 条/天/群（个人认证 60/qpm bot 维度）。
- group_openid 无法通过 API 枚举，由 WebSocket 事件（机器人入群 / 群内 @ 消息）
  自动收集持久化到 data/official_groups.json。
- 图片：先由程序自行下载，再 base64 二进制直传（≤5MB，已真机验证 2026-11-05）；
  失败回退平台 URL 拉取，再失败降级为"纯文本 + 图片直链"（保证推文不丢）。
"""
import base64
import json
import logging
import threading
import time
from pathlib import Path

import requests

# QQ 官方 API（token / 发消息 / 富媒体上传）国内可直连，且必须在 ChromeGo
# 代理内核启动之前就能用。独立 Session 忽略进程代理，避免冷启动竞态。
_DIRECT = requests.Session()
_DIRECT.trust_env = False

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
WS_URL = "wss://api.sgroup.qq.com/websocket"
INTENT_GROUP_AND_C2C = 1 << 25  # GROUP_AND_C2C_EVENT：群@消息、机器人入群等


class OfficialBotClient:
    def __init__(self, app_id: str, client_secret: str, base_dir=None,
                 api_base: str = API_BASE, timeout: int = 20):
        self.app_id = str(app_id)
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._token = None
        self._token_expire = 0.0
        self._token_lock = threading.Lock()
        self._last_error = None

        # 已发现的群 openid 持久化：openid -> {"alias": 群名/备注, "added_at": ts}
        self.groups_file = None
        self.c2c_file = None
        if base_dir:
            self.groups_file = Path(base_dir) / "data" / "official_groups.json"
            self.groups_file.parent.mkdir(parents=True, exist_ok=True)
            self.c2c_file = Path(base_dir) / "data" / "official_c2c.json"
        self._groups = self._load_groups()
        self._groups_lock = threading.Lock()

        self._ws_thread = None
        self._ws_stop = threading.Event()
        self._ws_sock = None
        self._ws_last_seq = None
        self._ws_ok = False
        self._ws_info = "未启动"

    # ---------- Token ----------
    def _get_token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expire:
                return self._token
            r = _DIRECT.post(TOKEN_URL, json={
                "appId": self.app_id, "clientSecret": self.client_secret,
            }, timeout=self.timeout)
            data = r.json()
            token = data.get("access_token")
            if not token:
                self._last_error = f"获取 access_token 失败: HTTP {r.status_code} {data}"
                raise RuntimeError(self._last_error)
            self._token = token
            # 官方 expires_in 单位秒（通常 ~2 小时），提前 120 秒刷新
            self._token_expire = time.time() + int(data.get("expires_in", 7200)) - 120
            return self._token

    def _api(self, method: str, path: str, payload=None) -> dict:
        url = self.api_base + path
        headers = {
            "Authorization": "QQBot " + self._get_token(),
            "Content-Type": "application/json",
        }
        r = _DIRECT.request(method, url, json=payload, headers=headers,
                            timeout=self.timeout)
        if r.status_code >= 300:
            self._last_error = f"{method} {path}: HTTP {r.status_code} {r.text[:200]}"
            raise RuntimeError(self._last_error)
        return r.json() if r.content else {}

    # ---------- 与 OneBotClient 对齐的接口 ----------
    def get_status(self, timeout: int = 8):
        """(在线?, 描述)。token 能正常获取即视为可用。"""
        try:
            self._get_token()
            ws = self._ws_info if self._ws_thread else "WS未启动(openid仅能手动登记)"
            return True, f"官方机器人 {self.app_id}（{ws}）"
        except Exception as e:
            return False, str(e)[:120]

    def get_group_list(self) -> list:
        """官方 API 无群列表接口，返回 WS 收集到的群（openid）。"""
        with self._groups_lock:
            items = list(self._groups.items())
        return [{"group_id": oid, "group_name": v.get("alias") or f"官方群 {oid[:12]}…"}
                for oid, v in items]

    def _upload_group_image(self, group_openid: str, img: str) -> str:
        """上传图片返回 file_info。优先二进制直传（≤5MB，已真机验证），
        失败回退平台 URL 拉取。"""
        raw = None
        url = str(img)
        if url.startswith("base64://"):
            raw = base64.b64decode(url[9:])
        elif url.startswith("file://"):
            raw = Path(url[7:]).read_bytes()
        elif url.startswith("http"):
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.content
        else:  # 本地路径
            raw = Path(url).read_bytes()

        if raw and len(raw) <= 5 * 1024 * 1024:
            try:
                media = self._api(
                    "POST", f"/v2/groups/{group_openid}/files",
                    {"file_type": 1, "file_name": "tweet.png",
                     "file_data": base64.b64encode(raw).decode(),
                     "srv_send_msg": False})
                fi = media.get("file_info")
                if fi:
                    return fi
                logging.warning("二进制直传未返回 file_info: %s", media)
            except Exception as e:
                logging.warning("二进制直传失败，尝试 URL 拉取: %s", e)

        # 回退：平台 URL 拉取（仅 http 链接可用）
        if url.startswith("http"):
            media = self._api("POST", f"/v2/groups/{group_openid}/files",
                              {"file_type": 1, "url": url, "srv_send_msg": False})
            fi = media.get("file_info")
            if fi:
                return fi
        raise RuntimeError("图片上传失败（二进制与 URL 两种方式均未成功）")

    @staticmethod
    def _clip(text: str, limit: int = 1000) -> str:
        """按 UTF-8 字节裁剪（QQ content 限长按字节算，中文 3 字节/字）。"""
        raw = str(text).encode("utf-8")
        if len(raw) <= limit:
            return str(text)
        return raw[:limit].decode("utf-8", "ignore")

    def reply_group_msg(self, group_openid: str, msg_id: str, text: str,
                        msg_seq: int = 1) -> dict:
        """被动回复群内 @ 消息。msg_id 必须来自 GROUP_AT_MESSAGE_CREATE 事件，
        有效期 5 分钟；被动回复不占用主动消息配额。"""
        payload = {"msg_type": 0, "content": self._clip(text),
                   "msg_id": str(msg_id), "msg_seq": msg_seq}
        path = f"/v2/groups/{group_openid}/messages"
        try:
            return self._api("POST", path, payload)
        except Exception as e:
            # 部分版本不接受 msg_seq，去掉后再试一次
            logging.warning("带 msg_seq 的被动回复失败，重试: %s", e)
            payload.pop("msg_seq", None)
            return self._api("POST", path, payload)

    def send_group_msg(self, group_openid: str, segments: list) -> dict:
        """发送一条群消息。segments 为 OneBot v11 消息段数组（text/image）。"""
        text = "".join(s["data"].get("text", "") for s in segments
                       if s.get("type") == "text")
        images = [s["data"].get("file") or s["data"].get("url")
                  for s in segments if s.get("type") == "image"]
        images = [u for u in images if u]

        results = []
        # 1) 文本
        if text:
            results.append(self._api("POST", f"/v2/groups/{group_openid}/messages",
                                     {"msg_type": 0, "content": self._clip(text),
                                      "msg_seq": 1}))
        # 2) 图片：本地下载后 base64 直传（腾讯服务器拉不到 X 图床，需自己下载）
        for img in images:
            url = img[8:] if str(img).startswith("base64://") else str(img)
            try:
                fi = self._upload_group_image(group_openid, img)
                results.append(self._api(
                    "POST", f"/v2/groups/{group_openid}/messages",
                    {"msg_type": 7, "media": {"file_info": fi}, "msg_seq": 1}))
            except Exception as e:
                logging.warning("官方通道图片上传失败: %s", e)
                # 降级：文本里带上图片直链
                if url.startswith("http") and url not in text:
                    results.append(self._api(
                        "POST", f"/v2/groups/{group_openid}/messages",
                        {"msg_type": 0, "content": (text + f"\n🖼 {url}")[:2000],
                         "msg_seq": 1}))
        return {"status": "ok", "ret": results}

    def send_post(self, group_ids, text: str, images=None, video_paths=None,
                  at_all=False) -> tuple:
        """向多个群推送，语义与 OneBotClient.send_post 一致，返回 (成功, 失败)。"""
        if at_all:
            logging.info("官方机器人不支持 @全体成员，已忽略")
        # 官方富媒体不支持视频直发；推文视频已生成同名 GIF（≤5MB，官方按图片收），
        # 故将视频替换为其 GIF 版本随消息发送，GIF 缺失时跳过该视频
        gifs = []
        for vp in (video_paths or []):
            gp = Path(vp).with_suffix(".gif")
            if gp.exists():
                gifs.append(str(gp))
            else:
                logging.info("官方通道视频 %s 无同名 GIF，跳过", Path(vp).name)
        all_images = list(images or []) + gifs
        ok_groups, failed = [], []
        for gid in group_ids:
            try:
                self.send_group_msg(gid, [{"type": "text", "data": {"text": text}}]
                                    + [{"type": "image", "data": {"file": i}}
                                       for i in all_images])
                ok_groups.append(gid)
            except Exception as e:
                failed.append({"group_id": gid, "error": str(e)[:150]})
                logging.warning("官方通道推送到群 %s 失败: %s", gid, str(e)[:150])
            time.sleep(0.6)  # 保守限速（官方 20/qpm/群）
        if failed and not ok_groups:
            raise RuntimeError(f"所有目标群发送失败: {failed}")
        return ok_groups, failed

    # ---------- group_openid 收集 ----------
    def _load_groups(self) -> dict:
        try:
            if self.groups_file and self.groups_file.exists():
                return json.loads(self.groups_file.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning("读取官方群列表失败: %s", e)
        return {}

    def _save_group(self, openid: str, extra: dict = None):
        with self._groups_lock:
            entry = self._groups.setdefault(
                openid, {"alias": "", "added_at": int(time.time())})
            if extra:
                entry.update({k: v for k, v in extra.items() if v})
            self._groups[openid] = entry
        try:
            self.groups_file.write_text(
                json.dumps(self._groups, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logging.warning("保存官方群列表失败: %s", e)
        # 云端备份（WorkBuddy 云数据库）：重新部署后群 openid 不丢失
        try:
            from core import clouddb
            c = clouddb.get()
            if c:
                c.sync_group(openid, alias=(extra or {}).get("alias", ""),
                             extra=extra, added_at=(extra or {}).get("added_at", 0))
        except Exception:
            pass
        logging.info("官方机器人发现群 openid: %s %s", openid, extra or "")

    def _save_c2c(self, openid: str, data: dict = None):
        """持久化私聊 openid（通常为管理员），供主动通知/调试用。"""
        self._c2c = getattr(self, "_c2c", {}) or {}
        if openid in self._c2c:
            return
        self._c2c[openid] = {"added_at": int(time.time()),
                             "last_content": (data or {}).get("content", "")[:20]}
        if not self.c2c_file:
            return
        try:
            self.c2c_file.write_text(
                json.dumps(self._c2c, ensure_ascii=False, indent=2),
                encoding="utf-8")
            logging.info("官方机器人发现私聊 openid: %s", openid)
        except Exception as e:
            logging.warning("保存私聊 openid 失败: %s", e)

    def start_ws(self):
        """启动 WebSocket 线程：自动收集群 openid（入群事件 / 群内@消息）。"""
        try:
            import websocket  # websocket-client
        except ImportError:
            self._ws_info = "未安装 websocket-client，无法自动收集群"
            logging.warning(self._ws_info)
            return
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True,
                                           name="qq-official-ws")
        self._ws_thread.start()

    def stop_ws(self):
        self._ws_stop.set()
        try:
            if self._ws_sock:
                self._ws_sock.close()
        except Exception:
            pass

    def _ws_send(self, obj: dict):
        self._ws_sock.send(json.dumps(obj))

    def _ws_loop(self):
        import websocket
        backoff = 5
        while not self._ws_stop.is_set():
            try:
                token = self._get_token()
                sock = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._ws_on_open,
                    on_message=self._ws_on_message,
                    on_error=lambda s, e: logging.warning("官方WS错误: %s", e),
                )
                self._ws_sock = sock
                self._ws_ok = True
                self._ws_info = "已连接"
                sock.run_forever(ping_interval=0)  # 心跳由我们手动发 op1
                self._ws_ok = False
                self._ws_info = "连接断开，重连中"
                backoff = min(backoff * 2, 300)
            except Exception as e:
                self._ws_info = f"WS异常: {e}"
                backoff = min(backoff * 2, 300)
            self._ws_stop.wait(backoff)

    def _ws_on_open(self, ws):
        logging.info("官方机器人 WebSocket 已连接，发送 Identify")

    def _ws_on_message(self, ws, message):
        try:
            frame = json.loads(message)
        except ValueError:
            return
        op = frame.get("op")
        d = frame.get("d") or {}
        if op == 10:  # Hello
            interval = d.get("heartbeat_interval", 30000) / 1000
            self._ws_send({"op": 2, "d": {
                "token": "QQBot " + self._get_token(),
                "intents": INTENT_GROUP_AND_C2C,
                "shard": [0, 1],
            }})
            threading.Thread(target=self._ws_heartbeat, args=(ws, interval),
                             daemon=True).start()
        elif op == 0:  # Dispatch
            self._ws_last_seq = frame.get("s")
            t = frame.get("t")
            logging.info("官方WS事件: %s", t)
            if t in ("GROUP_ADD_ROBOT", "GROUP_AT_MESSAGE_CREATE",
                     "GROUP_MSG_RECEIVE"):
                oid = d.get("group_openid")
                # 只在首次发现群时落盘；@ 消息不再覆盖已有群别名，
                # 也避免每条群消息都写一次文件 + 云数据库。
                if oid and oid not in self._groups:
                    self._save_group(oid)
            elif t == "C2C_MESSAGE_CREATE":
                oid = d.get("user_openid") or d.get("openid")
                if oid:
                    self._save_c2c(oid, d)
            elif t == "READY":
                logging.info("官方机器人 WS Ready: %s", d.get("user", {}).get("id"))
            # @ 机器人指令：独立线程处理，避免指令动作（抓取/配置写入）
            # 阻塞 WS 的收包与心跳，导致平台判定掉线。
            at_hook = getattr(self, "on_group_at", None)
            if t == "GROUP_AT_MESSAGE_CREATE" and callable(at_hook):
                threading.Thread(target=self._safe_at_hook, args=(at_hook, d),
                                 daemon=True, name="official-at").start()
            # 外部事件钩子（测试/扩展用）
            hook = getattr(self, "on_event", None)
            if callable(hook):
                try:
                    hook(t, d)
                except Exception as e:
                    logging.warning("on_event 钩子异常: %s", e)
        elif op == 11:  # Heartbeat ACK
            pass

    @staticmethod
    def _safe_at_hook(hook, d: dict):
        try:
            hook("GROUP_AT_MESSAGE_CREATE", d)
        except Exception as e:
            logging.warning("官方 @指令 钩子异常: %s", e)

    def _ws_heartbeat(self, ws, interval: float):
        while not self._ws_stop.is_set() and self._ws_ok:
            try:
                self._ws_send({"op": 1, "d": self._ws_last_seq})
            except Exception:
                return
            time.sleep(interval)

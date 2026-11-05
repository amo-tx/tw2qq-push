# -*- coding: utf-8 -*-
"""WorkBuddy 云服务数据库接入：推送记录 / 推送历史 / 群 openid 云端持久化

设计（本地为主、云端备份、启动恢复）：
- 运行时全部走本地 SQLite/JSON（性能与可靠性不受云网络影响）；
- mark/record_history/收集群 时异步双写云端（队列 + 后台线程，失败不影响推送）；
- 启动时若本地为空（新部署/重新发布后），从云端恢复 → 部署更新不丢数据。

REST 形态：PostgREST（与 workbuddy-cloud-sdk 同一数据面），
鉴权 header：x-wb-webapp-access-key: <publishable_key>。
配置在 config.yaml：
  cloud:
    enabled: true
    endpoint: https://xxx.app.workbuddy.host
    publishable_key: wbpk_...
"""
import json
import logging
import queue
import threading
import time
from pathlib import Path

import requests

_TABLE_SEEN = "pushed_seen"
_TABLE_HISTORY = "push_history"
_TABLE_GROUPS = "official_groups"
_TABLE_HEARTBEAT = "service_heartbeat"


class CloudDB:
    def __init__(self, endpoint: str, publishable_key: str):
        self.base = (endpoint or "").rstrip("/") + "/.cloud/database/rest"
        self.headers = {
            "x-wb-webapp-access-key": publishable_key,
            "Content-Type": "application/json",
        }
        # 独立会话并忽略环境/进程代理：云数据面必须直连，
        # 不受程序内 ChromeGo 代理（net.apply_proxy 设的 HTTP_PROXY）影响
        self.sess = requests.Session()
        self.sess.trust_env = False
        self.sess.headers.update(self.headers)
        self.q: "queue.Queue" = queue.Queue(maxsize=2000)
        self.ok = False          # 最近一次云端写入是否成功（供状态页）
        self.last_error = ""
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="clouddb-worker")
        self._worker.start()

    # ---------- REST 基础 ----------
    def _insert(self, table: str, rows: list) -> bool:
        r = self.sess.post(f"{self.base}/{table}", json=rows, timeout=15)
        return r.status_code in (200, 201)

    def _select(self, table: str, params: dict = None) -> list:
        r = self.sess.get(f"{self.base}/{table}", params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json() or []

    def _delete(self, table: str, filter_qs: str) -> bool:
        r = self.sess.delete(f"{self.base}/{table}?{filter_qs}", timeout=15)
        return r.status_code == 204

    # ---------- 写队列（后台线程消费） ----------
    def enqueue(self, table: str, row: dict):
        try:
            self.q.put_nowait((table, row))
        except queue.Full:
            pass  # 队列满说明云端长期不可达，丢弃最旧的备份价值有限的新数据

    def _run(self):
        fail_streak = 0
        while not self._stop.is_set():
            try:
                table, row = self.q.get(timeout=2)
            except queue.Empty:
                continue
            try:
                if self._insert(table, [row]):
                    fail_streak = 0
                    self.ok = True
                    self.last_error = ""
                else:
                    fail_streak += 1
                    self.last_error = f"HTTP {table}"
            except Exception as e:
                fail_streak += 1
                self.last_error = str(e)[:120]
                time.sleep(min(2 * fail_streak, 30))  # 指数退避，防雪崩

    # ---------- 业务双写 ----------
    def push_seen(self, feed_key: str, tweet_id: str):
        self.enqueue(_TABLE_SEEN, {"feed_key": feed_key, "tweet_id": str(tweet_id)})

    def push_history(self, feed_name, tweet_id, author, text, groups, image_count):
        self.enqueue(_TABLE_HISTORY, {
            "feed_name": feed_name, "tweet_id": str(tweet_id),
            "author": author, "body": (text or "")[:300],
            "groups": json.dumps(groups, ensure_ascii=False),
            "image_count": int(image_count or 0),
        })

    def remove_seen(self, feed_key: str):
        """重新回推测试清记录时，同步清云端"""
        try:
            self._delete(_TABLE_SEEN, f"feed_key=eq.{feed_key}")
        except Exception as e:
            self.last_error = str(e)[:120]

    def sync_group(self, openid: str, alias: str = "", extra: dict = None,
                   added_at: int = 0):
        row = {"openid": openid, "alias": alias or "",
               # JSONB 列必须传对象；传 json.dumps 字符串会被存成 JSON 字符串，
               # restore 时 extra.items() 直接 AttributeError
               "extra": extra or {},
               "added_at": int(added_at or 0),
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")}
        try:
            # upsert：PK 冲突时更新
            r = self.sess.post(
                f"{self.base}/{_TABLE_GROUPS}?on_conflict=openid",
                json=row, headers={"Prefer": "resolution=merge-duplicates"},
                timeout=15)
            if r.status_code not in (200, 201):
                self.last_error = f"group sync HTTP {r.status_code}"
        except Exception as e:
            self.last_error = str(e)[:120]

    # ---------- 启动恢复 ----------
    def restore(self, store, groups_file: Path) -> dict:
        """本地为空时从云端恢复。返回统计 {seen, groups}"""
        stat = {"seen": 0, "groups": 0}
        try:
            local_n = store.total_seen()
            if local_n == 0:
                rows = self._select(_TABLE_SEEN, {"select": "*"})
                # 恢复的数据本就在云端，临时摘掉双写钩子避免逐条回写（幂等但浪费请求）
                _saved_cloud, store.cloud = store.cloud, None
                try:
                    for r in rows:
                        store.mark(r["feed_key"], r["tweet_id"])
                finally:
                    store.cloud = _saved_cloud
                stat["seen"] = len(rows)
                if rows:
                    logging.info("云端恢复推送记录 %d 条", len(rows))
        except Exception as e:
            logging.warning("云端恢复推送记录失败: %s", e)
        try:
            if not groups_file.exists() or not groups_file.read_text(encoding="utf-8").strip():
                rows = self._select(_TABLE_GROUPS, {"select": "*"})
                data = {}
                for r in rows:
                    raw = r.get("extra")
                    extra = raw if isinstance(raw, dict) else {}
                    if isinstance(raw, str):
                        try:
                            extra = json.loads(raw or "{}")
                        except Exception:
                            extra = {}
                    if not isinstance(extra, dict):
                        extra = {}
                    data[r["openid"]] = {"alias": r.get("alias") or "",
                                         **{k: v for k, v in extra.items() if k != "alias"},
                                         "added_at": r.get("added_at") or 0}
                if data:
                    groups_file.parent.mkdir(parents=True, exist_ok=True)
                    groups_file.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    stat["groups"] = len(data)
                    logging.info("云端恢复群 openid %d 个", len(data))
        except Exception as e:
            logging.warning("云端恢复群列表失败: %s", e)
        # 反向补云：本地有群列表但云端表为空（首次接入 / 表被清空）时，
        # 把本地文件灌回云端，确保日后重新部署仍能恢复群 openid。
        try:
            if groups_file.exists():
                local = json.loads(groups_file.read_text(encoding="utf-8") or "{}")
                if local and not self._select(_TABLE_GROUPS,
                                              {"select": "openid", "limit": 1}):
                    for oid, info in local.items():
                        info = info if isinstance(info, dict) else {}
                        self.sync_group(
                            oid, info.get("alias", ""),
                            {k: v for k, v in info.items()
                             if k not in ("alias", "added_at")},
                            info.get("added_at", 0))
                    stat["groups"] = len(local)
                    logging.info("本地群列表 %d 个已反向补入云端", len(local))
        except Exception as e:
            logging.debug("群列表反向补云失败: %s", e)
        return stat

    # ---------- 历史迁移（一次性：本地已有历史灌入云端） ----------
    def migrate_history_from(self, store) -> int:
        """把本地 SQLite 既有 history 灌入云端（仅云端为空时执行）。

        历史表无业务唯一键，重复迁移会让云端行数随每次重启线性增长，
        因此先看云端是否已有数据，有则跳过。
        """
        n = 0
        try:
            if self._select(_TABLE_HISTORY, {"select": "id", "limit": 1}):
                logging.info("云端已有推送历史，跳过本地历史迁移")
                return 0
        except Exception as e:
            logging.debug("云端历史检查失败（继续迁移）: %s", e)
        try:
            for r in store.all_history():
                self.enqueue(_TABLE_HISTORY, {
                    "feed_name": r["feed"], "tweet_id": str(r["tweet_id"]),
                    "author": r["author"], "body": r["text"],
                    "groups": r["groups"], "image_count": int(r["images"] or 0)})
                n += 1
            logging.info("本地历史 %d 条已入云端同步队列", n)
        except Exception as e:
            logging.warning("历史迁移失败: %s", e)
        return n

    def migrate_seen_from(self, store) -> int:
        """把本地 pushed 去重记录批量 upsert 到云端（ignore-duplicates 幂等）"""
        try:
            rows = store.all_seen()
            if not rows:
                return 0
            r = self.sess.post(
                f"{self.base}/{_TABLE_SEEN}?on_conflict=feed_key,tweet_id",
                json=rows,
                headers={"Prefer": "resolution=ignore-duplicates"},
                timeout=30)
            if r.status_code in (200, 201):
                logging.info("本地去重记录 %d 条已同步云端", len(rows))
                return len(rows)
            self.last_error = f"seen migrate HTTP {r.status_code}"
            logging.warning("去重记录云端迁移失败: HTTP %s %s",
                            r.status_code, r.text[:120])
        except Exception as e:
            self.last_error = str(e)[:120]
            logging.warning("去重记录云端迁移失败: %s", e)
        return 0

    def heartbeat(self, payload: dict) -> bool:
        """云端心跳留痕：单行 upsert（id=1），失败不影响主流程。

        表结构由迁移脚本创建；表不存在时静默降级，仅看门狗状态里显示 false。
        """
        try:
            row = dict(payload)
            row["id"] = 1
            # merge-duplicates 只更新 payload 中出现的列，故 last_seen 必须显式带上
            from datetime import datetime, timezone
            row["last_seen"] = datetime.now(timezone.utc).isoformat()
            r = self.sess.post(
                f"{self.base}/{_TABLE_HEARTBEAT}?on_conflict=id",
                json=row, headers={"Prefer": "resolution=merge-duplicates"},
                timeout=15)
            return r.status_code in (200, 201, 204)
        except Exception as e:
            logging.debug("云端心跳写入失败: %s", e)
            return False

    def status(self) -> dict:
        return {"enabled": True, "ok": self.ok, "pending": self.q.qsize(),
                "last_error": self.last_error}

    def stop(self):
        self._stop.set()


# ---------- 模块级单例 ----------
_instance: "CloudDB | None" = None


def init(config: dict) -> "CloudDB | None":
    """按 config.cloud 初始化；未启用返回 None。重复调用幂等。"""
    global _instance
    if _instance is not None:
        return _instance
    cfg = (config or {}).get("cloud") or {}
    if not cfg.get("enabled") or not cfg.get("endpoint") or not cfg.get("publishable_key"):
        return None
    _instance = CloudDB(cfg["endpoint"], cfg["publishable_key"])
    logging.info("云服务数据库已接入: %s", cfg["endpoint"])
    return _instance


def get() -> "CloudDB | None":
    return _instance

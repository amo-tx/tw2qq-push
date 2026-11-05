# -*- coding: utf-8 -*-
"""以 WorkBuddy 云数据库为唯一存储的 Store 替身（GitHub Actions one-shot 用）。

与 core.store.Store 鸭子类型对齐，让 Monitor.check_once() 原封不动复用全部
抓取 / 翻译 / 媒体 / 推送逻辑；区别只是去重记录与历史落在云数据库，
因此 Actions runner 完全无状态、每次从干净环境启动也不会重推旧推文。

同时与云端 Web 应用共享同一份 seen：任一端推送成功后立即写云，另一端下一轮
就能看到，配合 service_heartbeat(id=2) 的让位机制避免双份推送。
"""
import json
import logging
from datetime import datetime, timezone

import requests


class CloudStore:
    def __init__(self, endpoint: str, key: str, dry_run: bool = False,
                 timeout: int = 20):
        self.base = (endpoint or "").rstrip("/") + "/.cloud/database/rest"
        self.sess = requests.Session()
        # 云数据面直连：不受 runner 环境代理 / 程序内 net.apply_proxy 影响
        self.sess.trust_env = False
        self.sess.headers.update({
            "x-wb-webapp-access-key": key,
            "Content-Type": "application/json",
        })
        self.timeout = timeout
        self.dry_run = dry_run
        self.cloud = None          # 与 Store 对齐：禁止二次双写
        self.writes = 0
        self._seen = {}            # feed_key -> set(tweet_id)
        self._load()

    # ---------- REST ----------
    def _get(self, table, params=None):
        r = self.sess.get(f"{self.base}/{table}", params=params or {},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json() or []

    def _post(self, table, rows, query="", prefer=None):
        if self.dry_run:
            return True
        headers = {"Prefer": prefer} if prefer else None
        r = self.sess.post(f"{self.base}/{table}{query}", json=rows,
                           headers=headers, timeout=30)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"{table} 写入 HTTP {r.status_code}: {r.text[:160]}")
        self.writes += 1
        return True

    def _load(self):
        rows = self._get("pushed_seen", {"select": "feed_key,tweet_id"})
        for r in rows:
            self._seen.setdefault(r["feed_key"], set()).add(str(r["tweet_id"]))
        logging.info("云端载入去重记录 %d 条 / %d 个博主", len(rows), len(self._seen))

    # ---------- Store 兼容接口 ----------
    def known_ids(self, feed_key: str) -> set:
        return set(self._seen.get(feed_key, ()))

    def mark(self, feed_key: str, tweet_id: str):
        tweet_id = str(tweet_id)
        bucket = self._seen.setdefault(feed_key, set())
        if tweet_id in bucket:
            return
        # 同步写入（非队列）：必须保证进程退出前落库，下次运行才不会重推
        self._post("pushed_seen", {"feed_key": feed_key, "tweet_id": tweet_id},
                   "?on_conflict=feed_key,tweet_id",
                   "resolution=ignore-duplicates")
        bucket.add(tweet_id)

    def record_history(self, feed_name, tweet_id, author, text, groups,
                       image_count):
        self._post("push_history", {
            "feed_name": feed_name, "tweet_id": str(tweet_id),
            "author": author, "body": (text or "")[:300],
            "groups": json.dumps(groups, ensure_ascii=False),
            "image_count": int(image_count or 0),
        })

    def clear_feed(self, feed_key: str) -> int:
        n = len(self._seen.get(feed_key, ()))
        self._seen.pop(feed_key, None)
        if not self.dry_run:
            r = self.sess.delete(
                f"{self.base}/pushed_seen?feed_key=eq.{feed_key}", timeout=20)
            r.raise_for_status()
        return n

    def total_seen(self) -> int:
        return sum(len(v) for v in self._seen.values())

    def all_seen(self) -> list:
        return [{"feed_key": k, "tweet_id": i}
                for k, v in self._seen.items() for i in v]

    def all_history(self) -> list:
        return self._get("push_history", {"select": "*"})

    def recent_history(self, limit: int = 50) -> list:
        return self._get("push_history",
                         {"select": "*", "order": "id.desc", "limit": limit})

    def push_stats(self, days: int = 7) -> list:
        return []

    def total_pushed(self) -> int:
        try:
            return len(self._get("push_history", {"select": "id"}))
        except Exception:
            return 0

    def close(self):
        pass

    # ---------- GitHub Actions 心跳 ----------
    def write_heartbeat(self, boot_id: str, extra: dict = None) -> bool:
        """写 id=2：云端 Web 应用看到新鲜心跳会自动暂停自己的轮询。"""
        now = datetime.now(timezone.utc).isoformat()
        row = {"id": 2, "boot_id": boot_id, "boot_at": now, "last_seen": now,
               "uptime_seconds": 0, "self_ping_ok": True}
        row.update(extra or {})
        return self._post("service_heartbeat", row, "?on_conflict=id",
                          "resolution=merge-duplicates")

    def clear_heartbeat(self) -> bool:
        """把 id=2 心跳置为过期：本轮失败时调用，云端 watchdog 立即接管轮询。

        GHA 失败（QQ 凭据失效 / 网络异常）若留下新鲜心跳，云端会持续让位，
        形成「GHA 挂了、云端也停了」的推送空窗，必须显式释放。
        """
        if self.dry_run:
            return True
        r = self.sess.patch(
            f"{self.base}/service_heartbeat?id=eq.2",
            json={"last_seen": "1970-01-01T00:00:00+00:00"},
            headers={"Prefer": "return=minimal"}, timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"清除心跳 HTTP {r.status_code}: {r.text[:160]}")
        return True

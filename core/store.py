# -*- coding: utf-8 -*-
"""已推送记录（SQLite 去重存储，线程安全）"""
import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：允许轮询线程 / Flask 线程共用，所有操作加锁串行化
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.cloud = None  # core.clouddb.CloudDB，由 webui 启动时注入；None=未启用
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS pushed ("
                "feed_key TEXT NOT NULL, "
                "tweet_id TEXT NOT NULL, "
                "pushed_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (feed_key, tweet_id))"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "feed_name TEXT, tweet_id TEXT, author TEXT, text TEXT, "
                "groups TEXT, image_count INTEGER, "
                "pushed_at TEXT DEFAULT (datetime('now','localtime')))"
            )
            self.conn.commit()

    def known_ids(self, feed_key: str) -> set:
        with self.lock:
            rows = self.conn.execute(
                "SELECT tweet_id FROM pushed WHERE feed_key=?", (feed_key,)
            )
            return {r[0] for r in rows}

    def mark(self, feed_key: str, tweet_id: str):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO pushed (feed_key, tweet_id) VALUES (?,?)",
                (feed_key, tweet_id),
            )
            self.conn.commit()
        if self.cloud:
            try:
                self.cloud.push_seen(feed_key, tweet_id)
            except Exception:
                pass

    def clear_feed(self, feed_key: str) -> int:
        """清掉某博主的全部推送记录（供"重新回推测试"），返回清除条数"""
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM pushed WHERE feed_key=?", (feed_key,))
            self.conn.commit()
            n = cur.rowcount
        if self.cloud:
            try:
                self.cloud.remove_seen(feed_key)
            except Exception:
                pass
        return n

    # ---------- 推送历史（供 Web 控制台展示） ----------
    def record_history(self, feed_name: str, tweet_id: str, author: str,
                       text: str, groups: list, image_count: int):
        with self.lock:
            self.conn.execute(
                "INSERT INTO history (feed_name, tweet_id, author, text, groups, image_count) "
                "VALUES (?,?,?,?,?,?)",
                (feed_name, tweet_id, author, (text or "")[:300],
                 json.dumps(groups, ensure_ascii=False), image_count),
            )
            self.conn.commit()
        if self.cloud:
            try:
                self.cloud.push_history(feed_name, tweet_id, author, text,
                                        groups, image_count)
            except Exception:
                pass

    def total_seen(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM pushed").fetchone()[0]

    def all_seen(self) -> list:
        """全量去重记录（云端迁移用）"""
        with self.lock:
            rows = self.conn.execute("SELECT feed_key, tweet_id FROM pushed")
            return [{"feed_key": r[0], "tweet_id": r[1]} for r in rows]

    def all_history(self) -> list:
        """全量历史（云端迁移用）"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT pushed_at, feed_name, tweet_id, author, text, "
                "image_count, groups FROM history ORDER BY rowid")
            return [
                {"time": r[0], "feed": r[1], "tweet_id": r[2], "author": r[3],
                 "text": r[4], "images": r[5], "groups": r[6]}
                for r in rows
            ]

    def recent_history(self, limit: int = 50) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT pushed_at, feed_name, author, text, image_count, groups "
                "FROM history ORDER BY rowid DESC LIMIT ?", (limit,)
            )
            return [
                {"time": r[0], "feed": r[1], "author": r[2], "text": r[3],
                 "images": r[4], "groups": r[5]}
                for r in rows
            ]

    def push_stats(self, days: int = 7) -> list:
        """最近 N 天每日推送量（用于柱状图）"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT date(pushed_at), COUNT(*) FROM history "
                "WHERE pushed_at >= date('now', 'localtime', ?) "
                "GROUP BY date(pushed_at) ORDER BY date(pushed_at)",
                (f"-{days} days",),
            )
            return [{"date": r[0], "count": r[1]} for r in rows]

    def total_pushed(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()

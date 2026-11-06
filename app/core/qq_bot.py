# -*- coding: utf-8 -*-
"""OneBot v11 HTTP API 客户端（NapCat / Lagrange / LLOneBot 均兼容）"""
import base64
import logging

import requests


class OneBotClient:
    def __init__(self, base_url: str, access_token: str = "", timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"

    def _post(self, endpoint: str, payload: dict) -> dict:
        r = requests.post(
            f"{self.base_url}/{endpoint}",
            json=payload,
            headers=self.headers,
            timeout=self.timeout,
        )
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"OneBot 返回非 JSON: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code != 200 or data.get("status") not in ("ok", "async"):
            raise RuntimeError(f"OneBot 调用失败 {endpoint}: HTTP {r.status_code} {data}")
        return data

    def get_status(self, timeout: int = 5):
        """查询 OneBot 运行状态，返回 (是否在线, 描述)"""
        try:
            r = requests.get(f"{self.base_url}/get_status",
                             headers=self.headers, timeout=timeout)
            data = r.json()
            if r.status_code == 200 and data.get("status") == "ok":
                return True, data.get("data", {}).get("nick", "在线")
            return False, data.get("wording") or f"HTTP {r.status_code}"
        except Exception as e:
            return False, f"连接失败: {type(e).__name__}"

    def get_group_list(self) -> list:
        """机器人所在的群列表 [{group_id, group_name, member_count}, ...]"""
        return (self._post("get_group_list", {}).get("data") or [])

    def get_login_info(self) -> dict:
        """当前登录账号 {user_id, nickname}"""
        return (self._post("get_login_info", {}).get("data") or {})

    def send_group_msg(self, group_id: int, segments: list) -> dict:
        return self._post("send_group_msg", {"group_id": group_id, "message": segments})

    def send_post(self, group_ids: list, text: str, images=None, video_paths=None, at_all=False) -> tuple:
        """向多个群推送一条内容，返回 (成功群列表, 失败列表)。

        分开多条消息发送：文本+图片一条、每个视频一条、每张 GIF 一条——
        合并在一条里时大 base64 会撑爆消息上限，导致整条静默丢失。
        at_all: True 时首条消息前插入 @全体成员 段（需要机器人为群管理员）。
        """
        from pathlib import Path

        text_segs = [{"type": "text", "data": {"text": text}}]
        if at_all:
            text_segs.insert(0, {"type": "at", "data": {"qq": "all"}})
        image_segs = []
        for img in images or []:
            file_ref = img
            if img.startswith("http"):
                try:
                    r = requests.get(
                        img, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    r.raise_for_status()
                    file_ref = "base64://" + base64.b64encode(r.content).decode()
                except Exception as e:
                    logging.warning("图片下载失败，改用直链 %s: %s", img, e)
            image_segs.append({"type": "image", "data": {"file": file_ref}})

        video_segs, gif_segs = [], []
        for vp in video_paths or []:
            p = Path(vp).resolve()
            if not p.exists():
                logging.warning("视频文件不存在，跳过: %s", p)
                continue
            video_segs.append({"type": "video", "data": {"file": p.as_uri()}})
            # 同条视频的 GIF 版本单独发（GIF 动图更直观）
            gif = p.with_suffix(".gif")
            if gif.exists():
                b64 = base64.b64encode(gif.read_bytes()).decode()
                gif_segs.append({"type": "image", "data": {"file": "base64://" + b64}})

        # 逐群发送：单群失败（如被禁言）不影响其他群，全部失败才抛异常
        ok_groups, failed = [], []
        for gid in group_ids:
            try:
                self.send_group_msg(gid, text_segs + image_segs)
                for vs in video_segs:
                    self.send_group_msg(gid, [vs])
                for gs in gif_segs:
                    self.send_group_msg(gid, [gs])
                ok_groups.append(gid)
            except Exception as e:
                failed.append({"group_id": gid, "error": str(e)[:150]})
                logging.warning("推送到群 %s 失败（可能被禁言/已退群/号被风控）: %s",
                                gid, str(e)[:150])
        if failed and not ok_groups:
            raise RuntimeError(f"所有目标群发送失败: {failed}")
        return ok_groups, failed

    # 兼容旧调用
    def send_tweet(self, group_ids, text, images=None):
        return self.send_post(group_ids, text, images=images)

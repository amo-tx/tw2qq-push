# -*- coding: utf-8 -*-
"""@机器人指令交互：群成员 @机器人 + 指令 → 菜单/操作。

事件来源有两条，共用同一套指令分发逻辑：
  1. NapCat / OneBot：HTTP 上报 POST /onebot/event → CommandHandler.handle_event
  2. QQ 官方机器人：WebSocket 的 GROUP_AT_MESSAGE_CREATE →
     OfficialCommandHandler.handle_official（由 qq_official.on_group_at 钩子驱动）

支持的指令（大小写/空格不敏感）：
  菜单 / help          显示指令菜单
  检查                 立即检查所有博主并推送新推文
  状态                 查看运行状态
  暂停 / 恢复          暂停或恢复自动轮询推送
  @全体 开|关          推送时是否 @全体成员（仅 NapCat 通道支持）
  翻译 开|关           开关推文自动翻译
"""
import logging
import re
import threading
import time

MENU_TEXT = (
    "🤖 推送机器人指令（@我 + 指令）\n"
    "• 菜单 — 显示本菜单\n"
    "• 检查 — 立即检查并推送新推文\n"
    "• 状态 — 查看运行状态\n"
    "• 暂停 / 恢复 — 暂停或恢复自动推送\n"
    "• @全体 开/关 — 推送时是否@全体成员\n"
    "• 翻译 开/关 — 开关自动翻译"
)


class CommandHandler:
    def __init__(self, monitor, napcat, save_config):
        self.monitor = monitor          # core.monitor.Monitor
        self.napcat = napcat            # core.qq_login.NapCatManager（官方通道为 None）
        self.save_config = save_config  # webui._save_config_key

    # ---------- 事件入口（NapCat / OneBot） ----------
    def handle_event(self, ev: dict):
        if not isinstance(ev, dict):
            return
        if ev.get("post_type") != "message" or ev.get("message_type") != "group":
            return
        bot_qq = str(self.napcat.qq or self.napcat.saved_qq() or "")
        if not bot_qq:
            return
        user_id = str(ev.get("user_id") or "")
        if user_id == bot_qq:  # 自己的消息不响应
            return
        group_id = int(ev.get("group_id") or 0)
        if not group_id:
            return

        cmd = self._extract_command(ev.get("message"), bot_qq)
        if cmd is None:  # 没有 @ 机器人
            return
        logging.info("收到群指令 [群%s %s]: %s", group_id, user_id, cmd or "(菜单)")
        reply = self._dispatch(cmd.strip())
        if reply:
            segs = [{"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": "\n" + reply}}]
            try:
                self.monitor.bot.send_group_msg(group_id, segs)
            except Exception as e:
                logging.warning("回复群指令失败: %s", e)

    # ---------- 解析 ----------
    @staticmethod
    def _extract_command(message, bot_qq: str):
        """返回 @机器人 之后的指令文本；未 @ 机器人返回 None。

        兼容 array 与 string（CQ 码）两种消息格式。"""
        if isinstance(message, list):
            ated = any(
                seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == bot_qq
                for seg in message if isinstance(seg, dict)
            )
            if not ated:
                return None
            texts = [seg.get("data", {}).get("text", "")
                     for seg in message if isinstance(seg, dict) and seg.get("type") == "text"]
            return "".join(texts).strip()
        if isinstance(message, str):
            # [CQ:at,qq=3961685949] 指令
            m = re.search(r"\[CQ:at,qq=(\d+)[^\]]*\]", message)
            if not m or m.group(1) != bot_qq:
                return None
            return message[m.end():].strip()
        return None

    # ---------- 指令分发（两条通道共用） ----------
    def _dispatch(self, cmd: str) -> str:
        c = re.sub(r"\s+", "", cmd).lower()
        c = c.lstrip("/＠@")
        if not c or c in ("菜单", "help", "菜单帮助", "帮助", "指令"):
            return MENU_TEXT
        if c in ("检查", "立即检查", "刷新", "更新"):
            threading.Thread(target=self._do_check, daemon=True).start()
            return "正在检查所有博主的新推文，有更新会直接推送到群…"

        if c in ("状态", "status"):
            return self._status_text()

        if c in ("暂停", "暂停推送", "stop"):
            self.monitor.manual_pause = True
            self.monitor.stop()
            return "已暂停自动推送（常驻守护不会再自动拉起）。发送「恢复」重新开启。"
        if c in ("恢复", "继续", "start"):
            self.monitor.manual_pause = False
            self.monitor.start()
            return "已恢复自动推送。"

        m = re.fullmatch(r"@?全体(开|关)", c)
        if m:
            return self._set_at_all(m.group(1) == "开")

        m = re.fullmatch(r"翻译(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"translate": on})
            return f"自动翻译已{'开启' if on else '关闭'}。"

        return f"未识别指令：「{cmd}」\n发送「菜单」查看支持的指令。"

    # ---------- 通道差异钩子 ----------
    def _set_at_all(self, on: bool) -> str:
        self.save_config({"at_all": on})
        if on:
            return "已开启：推送时 @全体成员（需机器人为群管理员，普通群有每日次数限制）。"
        return "已关闭 @全体成员。"

    def _channel_status(self) -> str:
        status = getattr(self.napcat, "status", "未启用")
        if status == "online":
            return "在线 ✅"
        if status == "wait_qr":
            return "等待扫码（控制台扫码恢复）"
        return str(status)

    # ---------- 具体动作 ----------
    def _do_check(self):
        try:
            sent, errors = self.monitor.check_once(force=True)
            if errors:
                logging.info("群指令触发检查完成：推送 %d 条，%d 个源出错", sent, errors)
        except Exception as e:
            logging.warning("群指令触发检查异常: %s", e)

    def _status_text(self) -> str:
        m = self.monitor
        cfg = m.config
        running = m.is_running and not getattr(m, "manual_pause", False)
        lines = [
            f"• 自动推送：{'运行中' if running else '已暂停'}",
            f"• 博主：{len(cfg['twitter']['feeds'])} 个",
            f"• 上次检查：{m.last_check or '尚未检查'}",
            f"• @全体成员：{'开' if cfg.get('at_all') else '关'}",
            f"• 自动翻译：{'开' if cfg.get('translate') else '关'}（{cfg.get('translate_engine', 'llm')} 引擎）",
        ]
        lines.insert(0, "• QQ：" + self._channel_status())
        return "当前状态：\n" + "\n".join(lines)


class OfficialCommandHandler(CommandHandler):
    """QQ 官方机器人通道的 @ 指令处理器。

    WS 事件 GROUP_AT_MESSAGE_CREATE 只在群成员 @机器人 时下发，
    因此不存在"未 @ 机器人"的误触发。回复优先用带 msg_id 的被动消息
    （不受主动消息配额限制），失败再回退主动消息。
    """

    def __init__(self, monitor, bot, save_config=None):
        super().__init__(monitor, None, save_config or (lambda patch: None))
        self.bot = bot
        self.events = 0            # 收到的 @ 事件总数
        self.replies = 0           # 成功回复数
        self.last_event = None
        self.last_reply = None
        self.last_error = None
        self._seen = []            # 事件 id 去重（防止 WS 重投重复回复）
        self._seen_set = set()
        self._lock = threading.Lock()

    # ---------- 事件入口（由 qq_official.on_group_at 在独立线程调用） ----------
    def handle_official(self, t: str, d: dict):
        if t != "GROUP_AT_MESSAGE_CREATE" or not isinstance(d, dict):
            return
        self.events += 1
        self.last_event = time.strftime("%Y-%m-%d %H:%M:%S")
        msg_id = str(d.get("id") or "")
        group_openid = str(d.get("group_openid") or "")
        if not group_openid:
            return
        if msg_id:
            with self._lock:
                if msg_id in self._seen_set:
                    return
                self._seen.append(msg_id)
                self._seen_set.add(msg_id)
                if len(self._seen) > 300:
                    old = self._seen.pop(0)
                    self._seen_set.discard(old)

        content = self._clean_content(str(d.get("content") or ""))
        # 机器人自己发出的消息不会被 @ 事件回传，这里再兜一层防回环
        if content.startswith("🤖 推送机器人指令") or content.startswith("当前状态："):
            return
        logging.info("收到官方群指令 [群%s]: %s", group_openid[:12], content or "(菜单)")
        try:
            reply = self._dispatch(content)
        except Exception as e:
            logging.warning("官方群指令分发异常: %s", e)
            self.last_error = str(e)[:120]
            reply = f"指令处理失败：{type(e).__name__}，请稍后重试。"
        if reply:
            self._reply(group_openid, msg_id, reply)

    @staticmethod
    def _clean_content(raw: str) -> str:
        """去掉官方事件 content 里的 @ 标记与各种空白，留下纯指令文本。"""
        s = re.sub(r"<@!?[^>]*>", " ", raw)        # <@!member_openid>
        s = re.sub(r"\[CQ:at[^\]]*\]", " ", s)     # 兼容 CQ 码
        s = s.replace("\u2005", " ").replace("\xa0", " ").replace("\u3000", " ")
        s = re.sub(r"^[\s@＠/．.、,，:：]+", "", s)
        return s.strip()

    def _set_at_all(self, on: bool) -> str:
        return "QQ 官方机器人不支持 @全体成员（平台限制），该设置未修改。"

    def _channel_status(self) -> str:
        try:
            ok, info = self.bot.get_status()
            return (str(info) if ok else "异常：" + str(info)[:60])
        except Exception as e:
            return "未知（%s）" % type(e).__name__

    def _reply(self, group_openid: str, msg_id: str, text: str):
        try:
            if msg_id:
                try:
                    self.bot.reply_group_msg(group_openid, msg_id, text)
                    self.replies += 1
                    self.last_reply = time.strftime("%Y-%m-%d %H:%M:%S")
                    return
                except Exception as e:
                    self.last_error = str(e)[:120]
                    logging.warning("被动回复失败，回退主动消息: %s", e)
            self.bot.send_group_msg(group_openid,
                                    [{"type": "text", "data": {"text": text}}])
            self.replies += 1
            self.last_reply = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            self.last_error = str(e)[:120]
            logging.warning("回复官方群指令失败: %s", e)

    def stats(self) -> dict:
        return {
            "enabled": True,
            "events": self.events,
            "replies": self.replies,
            "last_event": self.last_event,
            "last_reply": self.last_reply,
            "last_error": self.last_error,
        }


def attach_official_commands(bot, monitor, save_config=None):
    """幂等挂载：官方机器人 WS 事件 → @ 指令处理器。

    Monitor.reload_config() 可能重建 bot，故每次重载都会调用本函数；
    已挂载时只刷新 save_config 回调，不重复创建处理器（保留计数）。
    """
    if bot is None or not hasattr(bot, "start_ws"):
        return None
    cur = getattr(bot, "_at_handler", None)
    if cur is not None:
        if save_config is not None:
            cur.save_config = save_config
        return cur
    h = OfficialCommandHandler(monitor, bot, save_config)
    bot._at_handler = h
    bot.on_group_at = h.handle_official
    logging.info("QQ 官方机器人 @指令 已挂载")
    return h


def official_command_stats(monitor) -> dict:
    h = getattr(getattr(monitor, "bot", None), "_at_handler", None)
    return h.stats() if h else {"enabled": False}

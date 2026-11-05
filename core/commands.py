"""@机器人指令交互：群成员 @机器人 + 指令 → 菜单/操作。

事件来自 NapCat 的 HTTP 上报（httpClients → POST /onebot/event）。
支持的指令（大小写/空格不敏感）：
  菜单 / help          显示指令菜单
  检查                 立即检查所有博主并推送新推文
  状态                 查看运行状态
  暂停 / 恢复          暂停或恢复自动轮询推送
  @全体 开|关          推送时是否 @全体成员（需机器人为群管理员）
  翻译 开|关           开关推文自动翻译
"""
import logging
import re
import threading

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
        self.napcat = napcat            # core.qq_login.NapCatManager
        self.save_config = save_config  # webui._save_config_key

    # ---------- 事件入口 ----------
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

    # ---------- 指令分发 ----------
    def _dispatch(self, cmd: str) -> str:
        c = re.sub(r"\s+", "", cmd).lower()
        if not c or c in ("菜单", "help", "菜单帮助", "指令"):
            return MENU_TEXT
        if c in ("检查", "立即检查", "刷新"):
            threading.Thread(target=self._do_check, daemon=True).start()
            return "正在检查所有博主的新推文，有更新会直接推送到群…"

        if c in ("状态", "status"):
            return self._status_text()

        if c in ("暂停", "暂停推送", "stop"):
            self.monitor.stop()
            return "已暂停自动推送。发送「恢复」重新开启。"
        if c in ("恢复", "继续", "start"):
            self.monitor.start()
            return "已恢复自动推送。"

        m = re.fullmatch(r"@全体(开|关)|全体(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"at_all": on})
            if on:
                return "已开启：推送时 @全体成员（需机器人为群管理员，普通群有每日次数限制）。"
            return "已关闭 @全体成员。"

        m = re.fullmatch(r"翻译(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"translate": on})
            return f"自动翻译已{'开启' if on else '关闭'}。"

        return f"未识别指令：「{cmd}」\n发送「菜单」查看支持的指令。"

    # ---------- 具体动作 ----------
    def _do_check(self):
        try:
            sent, errors = self.monitor.check_once()
            if errors:
                logging.info("群指令触发检查完成：推送 %d 条，%d 个源出错", sent, errors)
        except Exception as e:
            logging.warning("群指令触发检查异常: %s", e)

    def _status_text(self) -> str:
        m = self.monitor
        cfg = m.config
        lines = [
            f"• 自动推送：{'运行中' if m.is_running else '已暂停'}",
            f"• 博主：{len(cfg['twitter']['feeds'])} 个",
            f"• 上次检查：{m.last_check or '尚未检查'}",
            f"• @全体成员：{'开' if cfg.get('at_all') else '关'}",
            f"• 自动翻译：{'开' if cfg.get('translate') else '关'}（{cfg.get('translate_engine', 'llm')} 引擎）",
        ]
        nap_status = self.napcat.status
        if nap_status == "online":
            lines.insert(0, "• QQ：在线 ✅")
        elif nap_status == "wait_qr":
            lines.insert(0, "• QQ：等待扫码（控制台扫码恢复）")
        else:
            lines.insert(0, f"• QQ：{nap_status}")
        return "当前状态：\n" + "\n".join(lines)

# -*- coding: utf-8 -*-
"""@机器人指令交互：群成员 @机器人 + 指令 → 查询 / 管理 / 配置 / 诊断。

事件来源有两条，共用同一套指令分发逻辑：
  1. NapCat / OneBot：HTTP 上报 POST /onebot/event → CommandHandler.handle_event
  2. QQ 官方机器人：WebSocket 的 GROUP_AT_MESSAGE_CREATE →
     OfficialCommandHandler.handle_official（由 qq_official.on_group_at 钩子驱动）

常用指令见 MENU_TEXT，完整清单见 MORE_TEXT。大小写/空格不敏感，
handle 可写 @xxx 或完整 x.com 链接；「序号」按各自列表的 1-based 编号。
"""
import logging
import re
import threading
import time

MENU_TEXT = (
    "🤖 推送机器人指令（@我 + 指令）\n"
    "• 菜单 — 本菜单　• 更多 — 全部指令\n"
    "• 关注列表 — 列出你 X 账号关注的人\n"
    "• 关注 @handle / 关注 序号 — 加博主\n"
    "• 列表 / 移除 序号 — 管理推送列表\n"
    "• 检查 — 立即检查并推送新推文\n"
    "• 状态 / 自检 / 日志 — 运行情况\n"
    "• 暂停 / 恢复 · 翻译 开|关 · 间隔 秒数"
)

MORE_TEXT = (
    "🧰 全部指令（@我 + 指令）\n"
    "【X 关注列表】\n"
    "• 关注列表 [页码] — 分页列出你关注的博主\n"
    "• 刷新关注 — 强制重新抓取\n"
    "• 找 关键词 — 按 handle/昵称搜索\n"
    "• 关注 序号 / 关注 @handle — 加入推送\n"
    "• 批量 1 3 5 或 批量 1-5 — 一次加多位(≤10)\n"
    "【推送列表】\n"
    "• 列表 — 当前推送的博主\n"
    "• 移除 序号/@handle — 停止推送(不取关 X)\n"
    "• 回推 序号/@handle [小时] — 补发近 N 小时\n"
    "• 最新 @handle — 预览最近 3 条(不推送)\n"
    "【配置】\n"
    "• 间隔 60 — 每博主抓取间隔(秒, 30~3600)\n"
    "• 图片 开|关 · 视频 开|关 · 翻译 开|关 · @全体 开|关\n"
    "【诊断】\n"
    "• 自检 — QQ / X 登录态 / 云库 / 守护\n"
    "• 日志 — 最近 5 条日志 · 检查 — 全量抓取\n"
    "• 暂停 / 恢复 — 停开自动推送"
)

FOLLOWING_PAGE = 12          # 关注列表每页条数（被动回复按 UTF-8 字节限长）
FOLLOWING_TTL = 600          # 关注列表缓存 10 分钟
MAX_BATCH = 10               # 单次批量添加上限
_BOT_REPLY_PREFIXES = ("🤖", "🧰", "👥", "✅", "❌", "🕒", "🔎", "📋", "📡",
                       "当前状态：", "正在检查", "未识别指令")


class CommandHandler:
    def __init__(self, monitor, napcat, save_config):
        self.monitor = monitor          # core.monitor.Monitor
        self.napcat = napcat            # core.qq_login.NapCatManager（官方通道为 None）
        self.save_config = save_config or self._default_save
        self._following = None          # X 关注列表缓存
        self._following_ts = 0.0
        self._cache_lock = threading.Lock()

    # ---------- 写配置（webui 未注入回调时的兜底） ----------
    def _default_save(self, patch: dict):
        import yaml

        def merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v

        p = self.monitor.config_path
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        merge(cfg, patch)
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        self.monitor.reload_config()

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
        reply = self._dispatch(cmd.strip(), {"group_id": group_id, "user_id": user_id})
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
    def _dispatch(self, cmd: str, ctx: dict = None) -> str:
        ctx = ctx or {}
        raw = (cmd or "").strip().lstrip("/＠@").strip()
        c = re.sub(r"\s+", "", raw).lower()
        try:
            return self._dispatch_inner(raw, c, ctx) or ""
        except Exception as e:
            logging.warning("群指令执行异常: %s", e, exc_info=True)
            return f"❌ 操作失败：{type(e).__name__}: {str(e)[:110]}"

    def _dispatch_inner(self, raw: str, c: str, ctx: dict) -> str:
        # ---- 无参指令 ----
        if not c or c in ("菜单", "help", "菜单帮助", "帮助", "指令"):
            return MENU_TEXT
        if c in ("更多", "全部指令", "高级", "进阶", "more"):
            return MORE_TEXT
        if c in ("检查", "立即检查", "刷新", "更新"):
            threading.Thread(target=self._do_check, daemon=True).start()
            return "📡 正在检查所有博主的新推文，有更新会直接推送到群…"
        if c in ("状态", "status"):
            return self._status_text()
        if c in ("列表", "博主", "推送列表", "我的博主"):
            return self._cmd_feeds()
        if c in ("自检", "诊断", "check", "selftest"):
            return self._cmd_self_test()
        if c in ("日志", "log", "logs"):
            return self._cmd_logs()
        if c in ("刷新关注", "刷新关注列表", "重抓关注"):
            self._x_users(refresh=True)
            return self._cmd_following(1)
        if c in ("暂停", "暂停推送", "stop"):
            self.monitor.manual_pause = True
            self.monitor.stop()
            return "⏸ 已暂停自动推送（常驻守护不会再自动拉起）。发送「恢复」重新开启。"
        if c in ("恢复", "继续", "start"):
            self.monitor.manual_pause = False
            self.monitor.start()
            return "▶️ 已恢复自动推送。"

        m = re.fullmatch(r"@?全体(开|关)", c)
        if m:
            return self._set_at_all(m.group(1) == "开")
        m = re.fullmatch(r"翻译(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"translate": on})
            return f"✅ 自动翻译已{'开启' if on else '关闭'}。"
        m = re.fullmatch(r"图片(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"send_images": on})
            return f"✅ 推文配图已{'开启' if on else '关闭'}。"
        m = re.fullmatch(r"视频(开|关)", c)
        if m:
            on = m.group(1) == "开"
            self.save_config({"send_videos": on})
            return f"✅ 视频/GIF 已{'开启' if on else '关闭'}。"

        # ---- 带参指令（顺序敏感：长关键词在前） ----
        m = re.match(r"^(?:关注列表|我关注的|x关注|following)\s*(\d*)\s*$",
                     raw, re.I)
        if m:
            return self._cmd_following(int(m.group(1) or 1))

        m = re.match(r"^(?:批量|批量关注|批量添加|导入)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_batch(m.group(1), ctx)

        m = re.match(r"^(?:关注|添加|加博主|加|\+)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_add(m.group(1), ctx)

        m = re.match(r"^(?:移除|删除|删|取关|取消关注|-)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_remove(m.group(1), ctx)

        m = re.match(r"^(?:找|搜索|搜)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_find(m.group(1))

        m = re.match(r"^(?:最新|预览|看看)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_latest(m.group(1))

        m = re.match(r"^(?:回推|补发|补推)\s*(.+)$", raw, re.I)
        if m:
            return self._cmd_backfill(m.group(1), ctx)

        m = re.fullmatch(r"间隔(\d+)(秒|s|分钟|分|m)?", c)
        if m:
            return self._cmd_interval(int(m.group(1)), m.group(2))
        if c in ("间隔", "频率"):
            p = int(self.monitor.config.get("poll_seconds") or 0)
            return (f"当前每 {p} 秒抓取一次每个博主；发「间隔 60」修改（30~3600 秒）"
                    if p else f"当前每 {self.monitor.interval // 60} 分钟全量检查一次")

        return f"未识别指令：「{raw}」\n发送「菜单」或「更多」查看支持的指令。"

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

    # ---------- 通用小工具 ----------
    @staticmethod
    def _parse_handle(arg: str) -> str:
        """从 @handle、x.com/handle、纯文本里提取合法 handle。"""
        s = (arg or "").strip()
        s = re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "", s, flags=re.I)
        s = s.split("?")[0].split("/")[0].strip().lstrip("@＠")
        s = re.sub(r"\s+", "", s)
        return s if re.fullmatch(r"[A-Za-z0-9_]{1,15}", s) else ""

    @staticmethod
    def _feed_key(f: dict) -> str:
        return (f.get("handle") or f.get("url") or "").lstrip("@")

    def _feeds(self) -> list:
        return list((self.monitor.config.get("twitter") or {}).get("feeds") or [])

    def _save_feeds(self, feeds: list):
        self.save_config({"twitter": {"feeds": feeds}})

    def _find_feed(self, arg: str, feeds: list):
        """按 1-based 序号或 handle 找推送列表里的博主。"""
        s = (arg or "").strip()
        if re.fullmatch(r"\d+", s):
            i = int(s)
            return feeds[i - 1] if 1 <= i <= len(feeds) else None
        h = self._parse_handle(s).lower()
        if not h:
            return None
        for f in feeds:
            key = self._feed_key(f).lower()
            if key == h or h in key:
                return f
        return None

    def _x_cookie(self) -> str:
        return ((self.monitor.config.get("x") or {}).get("auth_token") or "").strip()

    def _x_users(self, refresh: bool = False) -> list:
        """读取当前 X 账号关注列表（带 10 分钟缓存）。"""
        with self._cache_lock:
            cached, age = self._following, time.time() - self._following_ts
        if cached is not None and not refresh and age < FOLLOWING_TTL:
            return cached
        cookie = self._x_cookie()
        if not cookie:
            raise RuntimeError("未配置 X auth_token，请先在 Web 控制台「X 账号」页保存")
        from core import x_source
        users = x_source.fetch_following(cookie, max_pages=5)
        if not users:
            raise RuntimeError("关注列表为空，可能被 X 限流，稍后再试")
        with self._cache_lock:
            self._following, self._following_ts = users, time.time()
        return users

    def _default_groups(self, ctx: dict) -> list:
        groups = [str(g) for g in ((self.monitor.config.get("qq") or {}).get("groups") or [])]
        if not groups:
            g = ctx.get("group_openid") or ctx.get("group_id")
            groups = [str(g)] if g else []
        return groups

    def _log_write(self, action: str, detail: str, ctx: dict):
        who = str(ctx.get("member_openid") or ctx.get("user_id") or "?")[:10]
        logging.info("群指令写操作: %s %s by %s", action, detail, who)

    # ---------- 关注列表 / 检索 ----------
    def _cmd_following(self, page: int) -> str:
        users = self._x_users()
        existing = {self._feed_key(f).lower() for f in self._feeds()}
        pages = max(1, (len(users) + FOLLOWING_PAGE - 1) // FOLLOWING_PAGE)
        page = max(1, min(page, pages))
        chunk = users[(page - 1) * FOLLOWING_PAGE: page * FOLLOWING_PAGE]
        lines = [f"👥 你 X 账号关注 {len(users)} 人（第 {page}/{pages} 页，✅=已推送）"]
        for i, u in enumerate(chunk, (page - 1) * FOLLOWING_PAGE + 1):
            mark = "✅" if (u.get("handle") or "").lower() in existing else "　"
            name = (u.get("name") or "")[:10]
            lines.append(f"{i:>3}.{mark}@{u.get('handle', '')} {name}")
        lines.append("「关注列表 2」翻页 ·「找 关键词」搜索 ·「关注 序号」加入推送 ·「刷新关注」重抓")
        return "\n".join(lines)

    def _cmd_find(self, kw: str) -> str:
        users = self._x_users()
        low = (kw or "").strip().lower()
        if not low:
            return "用法：找 关键词（按 handle 或昵称搜索你的 X 关注列表）"
        pos = {(u.get("handle") or "").lower(): i + 1 for i, u in enumerate(users)}
        hits = [u for u in users
                if low in (u.get("handle") or "").lower()
                or low in (u.get("name") or "").lower()]
        if not hits:
            return f"🔍 关注列表里没找到「{kw}」（共 {len(users)} 人）"
        existing = {self._feed_key(f).lower() for f in self._feeds()}
        lines = [f"🔍 找到 {len(hits)} 位（最多列 15 位）"]
        for u in hits[:15]:
            h = (u.get("handle") or "").lower()
            mark = "✅" if h in existing else "　"
            lines.append(f"{pos.get(h, 0):>3}.{mark}@{u.get('handle', '')} {(u.get('name') or '')[:14]}")
        lines.append("发「关注 序号」加入推送")
        return "\n".join(lines)

    # ---------- 增删博主 ----------
    def _cmd_add(self, arg: str, ctx: dict) -> str:
        arg = (arg or "").strip()
        if not arg:
            return "用法：关注 @handle 或 关注 序号（序号来自「关注列表」）"
        name = ""
        if re.fullmatch(r"\d+", arg):
            users = self._x_users()
            idx = int(arg)
            if not (1 <= idx <= len(users)):
                return f"❌ 序号超出范围（1–{len(users)}），先发「关注列表」查看"
            u = users[idx - 1]
            handle, name = u.get("handle") or "", u.get("name") or ""
        else:
            handle = self._parse_handle(arg)
        if not handle:
            return "❌ handle 不合法（1–15 位字母/数字/下划线），例：关注 @meicyououenbu"

        feeds = self._feeds()
        if any(self._feed_key(f).lower() == handle.lower() for f in feeds):
            return f"@{handle} 已在推送列表中（共 {len(feeds)} 位）"
        groups = self._default_groups(ctx)
        feeds.append({"name": name or handle, "handle": handle, "groups": groups})
        self._save_feeds(feeds)
        self._log_write("添加博主", handle, ctx)
        bound = f"已绑定 {len(groups)} 个群" if groups else "⚠️ 未绑定群，请到 Web 控制台绑定"
        shown = f"（{name}）" if name and name != handle else ""
        return (f"✅ 已添加 @{handle}{shown}\n{bound}，之后的新推文会自动推送。\n"
                f"发「回推 {handle}」可补发近 24 小时推文。")

    def _cmd_batch(self, arg: str, ctx: dict) -> str:
        nums = []
        for part in re.split(r"[\s,，、]+", (arg or "").strip()):
            if not part:
                continue
            m = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", part)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if b < a or b - a > 50:
                    return "❌ 区间写法有误（例：批量 1-5）"
                nums.extend(range(a, b + 1))
            elif re.fullmatch(r"\d+", part):
                nums.append(int(part))
            else:
                return f"❌ 批量只接受关注列表序号，无法识别「{part}」"
        nums = list(dict.fromkeys(nums))[:MAX_BATCH]
        if not nums:
            return "用法：批量 1 3 5 或 批量 1-5（序号来自「关注列表」，单次最多 10 位）"
        users = self._x_users()
        feeds = self._feeds()
        have = {self._feed_key(f).lower() for f in feeds}
        groups = self._default_groups(ctx)
        added, skipped, invalid = [], [], []
        for n in nums:
            if not (1 <= n <= len(users)):
                invalid.append(str(n))
                continue
            h = users[n - 1].get("handle") or ""
            if not h or h.lower() in have:
                skipped.append(h or str(n))
                continue
            feeds.append({"name": users[n - 1].get("name") or h,
                          "handle": h, "groups": groups})
            have.add(h.lower())
            added.append(h)
        if added:
            self._save_feeds(feeds)
            self._log_write("批量添加", ",".join(added), ctx)
        parts = [f"✅ 批量添加 {len(added)} 位：" + " ".join("@" + h for h in added)] if added else []
        if skipped:
            parts.append("已在列表/无效：" + " ".join(skipped))
        if invalid:
            parts.append("序号越界：" + " ".join(invalid))
        parts.append(f"当前共推送 {len(feeds)} 位；发「列表」查看")
        return "\n".join(parts)

    def _cmd_remove(self, arg: str, ctx: dict) -> str:
        feeds = self._feeds()
        target = self._find_feed(arg, feeds)
        if not target:
            return f"❌ 推送列表里没找到「{arg}」，发「列表」看当前博主"
        key = self._feed_key(target)
        self._save_feeds([f for f in feeds if f is not target])
        self._log_write("移除博主", key, ctx)
        return f"✅ 已移除 @{key}（当前 {len(feeds) - 1} 位）\n仅停止推送，不会取关你的 X 账号。"

    def _cmd_feeds(self) -> str:
        feeds = self._feeds()
        if not feeds:
            return "📋 推送列表为空。发「关注 @handle」或「关注列表」挑选博主。"
        lines = [f"📋 当前推送 {len(feeds)} 位博主（序号可用于「移除」「回推」）"]
        for i, f in enumerate(feeds, 1):
            key = self._feed_key(f)
            g = len(f.get("groups") or [])
            lines.append(f"{i:>2}. @{key} — {str(f.get('name') or key)[:16]}（{g} 个群）")
        lines.append("发「关注列表」看你的 X 关注，「关注 序号」加入推送")
        return "\n".join(lines)

    # ---------- 内容：预览 / 回推 ----------
    def _cmd_latest(self, arg: str) -> str:
        feed = self._find_feed(arg, self._feeds())
        handle = self._feed_key(feed) if feed else self._parse_handle(arg)
        if not handle:
            return "用法：最新 @handle（预览该博主最近 3 条，不推送）"
        from core import x_source
        tweets = x_source.fetch_user_tweets(handle, self._x_cookie())
        if not tweets:
            return f"🕒 暂时抓不到 @{handle} 的推文（可能无新内容、账号不可见或被限流）"
        lines = [f"🕒 @{handle} 最近 {min(3, len(tweets))} 条（仅预览，不推送）"]
        for t in tweets[:3]:
            ts = t.get("ts")
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) \
                if isinstance(ts, (int, float)) and ts > 0 else "--"
            text = re.sub(r"\s+", " ", str(t.get("text") or ""))[:60]
            lines.append(f"· {when} {text}")
        return "\n".join(lines)

    def _cmd_backfill(self, arg: str, ctx: dict) -> str:
        parts = (arg or "").split()
        hours = 24
        if len(parts) >= 2 and re.fullmatch(r"\d+", parts[-1]):
            hours = max(1, min(int(parts[-1]), 168))
            parts = parts[:-1]
        feed = self._find_feed(" ".join(parts), self._feeds())
        if not feed:
            return f"❌ 推送列表里没找到「{' '.join(parts)}」，发「列表」查看"
        key = self._feed_key(feed)

        def run():
            try:
                n = self.monitor.backfill_feed(feed, hours)
                logging.info("群指令回推 @%s 近 %d 小时：%d 条", key, hours, n)
            except Exception as e:
                logging.warning("群指令回推 @%s 异常: %s", key, e)

        threading.Thread(target=run, daemon=True, name="at-backfill").start()
        self._log_write("回推", f"{key}/{hours}h", ctx)
        return f"📡 正在回推 @{key} 近 {hours} 小时的推文（只推未发过的），稍后到群里看。"

    # ---------- 配置 / 诊断 ----------
    def _cmd_interval(self, num: int, unit: str) -> str:
        sec = num * 60 if unit in ("分钟", "分", "m") else num
        if not (30 <= sec <= 3600):
            return "❌ 间隔范围 30~3600 秒（例：间隔 60 或 间隔 2m）"
        self.save_config({"poll_seconds": sec})
        # monitor._run() 每轮重读该值，无需重启线程（避免抓取途中停摆）
        return f"✅ 抓取间隔已设为每博主 {sec} 秒（轮询下一轮自动生效）"

    def _cmd_self_test(self) -> str:
        lines = ["🔎 自检结果："]
        try:
            ok, info = self.monitor.bot.get_status()
            lines.append(f"{'✅' if ok else '❌'} QQ 通道：{str(info)[:60]}")
        except Exception as e:
            lines.append(f"❌ QQ 通道：{type(e).__name__}")
        cookie = self._x_cookie()
        if not cookie:
            lines.append("❌ X 登录态：未配置 auth_token")
        else:
            try:
                from core import x_source
                r = x_source.validate_cookie(cookie)
                lines.append(f"{'✅' if r.get('ok') else '❌'} X 登录态："
                             f"{str(r.get('user') or r.get('error'))[:60]}")
            except Exception as e:
                lines.append(f"❌ X 登录态：{type(e).__name__}: {str(e)[:60]}")
        try:
            lines.append(f"✅ 去重记录：{self.monitor.store.total_seen()} 条")
        except Exception:
            lines.append("⚠️ 去重记录：读取失败")
        try:
            from core import clouddb
            lines.append(f"{'✅' if clouddb.get() else '⚠️'} 云数据库："
                         f"{'已连接' if clouddb.get() else '未启用'}")
        except Exception:
            pass
        try:
            from core import heartbeat
            st = heartbeat.state()
            lines.append(f"✅ 守护：{st.get('mode')} / {st.get('loops')} 轮")
        except Exception:
            pass
        return "\n".join(lines)

    def _cmd_logs(self) -> str:
        try:
            lines = self.monitor.ring.tail(300)
        except Exception:
            return "⚠️ 日志缓冲不可用"
        if not lines:
            return "📋 暂无日志"
        bad = [x for x in lines if any(k in x for k in ("ERROR", "WARNING", "失败", "异常"))]
        picked = (bad or lines)[-5:]
        return "📋 最近日志（优先显示告警）：\n" + "\n".join(x[:90] for x in picked)

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
        feeds = self._feeds()
        running = m.is_running and not getattr(m, "manual_pause", False)
        poll = int(cfg.get("poll_seconds") or 0)
        try:
            seen = m.store.total_seen()
        except Exception:
            seen = "?"
        lines = [
            f"• 自动推送：{'运行中' if running else '已暂停'}",
            f"• 博主：{len(feeds)} 位｜抓取：每 {poll} 秒" if poll
            else f"• 博主：{len(feeds)} 位｜全量检查每 {m.interval // 60} 分钟",
            f"• 上次检查：{m.last_check or '尚未检查'}",
            f"• 去重记录：{seen} 条",
            f"• 开关：翻译{'开' if cfg.get('translate') else '关'}"
            f" / 图片{'开' if cfg.get('send_images') else '关'}"
            f" / 视频{'开' if cfg.get('send_videos') else '关'}"
            f" / @全体{'开' if cfg.get('at_all') else '关'}",
            f"• X 登录态：{'已配置' if self._x_cookie() else '未配置'}",
            "• 发「更多」看全部指令",
        ]
        lines.insert(0, "• QQ：" + self._channel_status())
        return "当前状态：\n" + "\n".join(lines)


class OfficialCommandHandler(CommandHandler):
    """QQ 官方机器人通道的 @ 指令处理器。

    WS 事件 GROUP_AT_MESSAGE_CREATE 只在群成员 @机器人 时下发，
    因此不存在「未 @ 机器人」的误触发。回复优先用带 msg_id 的被动消息
    （不受主动消息配额限制），失败再回退主动消息。
    """

    def __init__(self, monitor, bot, save_config=None):
        super().__init__(monitor, None, save_config)
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
        if content.startswith(_BOT_REPLY_PREFIXES) or content.startswith("当前状态："):
            return
        logging.info("收到官方群指令 [群%s]: %s", group_openid[:12], content or "(菜单)")
        ctx = {
            "group_openid": group_openid,
            "member_openid": str((d.get("author") or {}).get("member_openid") or ""),
            "msg_id": msg_id,
        }
        reply = self._dispatch(content, ctx)
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
    已挂载时只刷新 save_config 回调，不重复创建处理器（保留计数与缓存）。
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

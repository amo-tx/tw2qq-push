# -*- coding: utf-8 -*-
"""Telegram 公开频道内容源：t.me/s/<channel> 预览页轮询

为什么不用 Telethon（参考项目普遍做法）：云端是按需唤醒的 sandbox，长连接
会随进程回收断开，且 session 文件在重新部署后丢失、需要人工验证码登录。
公开预览页是纯 HTTP、无登录、无 api_id、无会话状态，与 monitor.py 现有的
「轮询 -> seen 去重 -> 推送」模型完全同构，重启后自动续上。

输出与 x_source / feed 一致的统一消息字典：
    {id, author, link, text, images, videos, ts}
其中 author 为频道 handle（不含 @），可直接用于模板与 relay 的 (@author) 订阅匹配。

公开预览页只能读公开频道：拿不到私有频道/群组、评论、投票结果，也不反映
删除与编辑历史。图片是 CDN 预览直链（通常足够清晰），视频可能是缩略图。
"""
import calendar
import html as html_lib
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
TEXT_RE = re.compile(
    r'<div[^>]*class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.S | re.I)
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)
PHOTO_URL_RE = re.compile(
    r'background-image\s*:\s*url\((?:[\'"]|&#39;|&quot;)?([^\'")]+)', re.I)
VIDEO_RE = re.compile(r'<video[^>]*\ssrc=["\']([^"\']+)["\']', re.I)
SOURCE_RE = re.compile(r'<source[^>]*\ssrc=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', re.I)
TAG_RE = re.compile(r'<[^>]+>')


def clean_handle(raw: str) -> str:
    """兼容 @name / t.me/name / https://t.me/s/name?before=1 等写法。"""
    h = (raw or "").strip().lstrip("@")
    h = re.sub(r"^https?://", "", h, flags=re.I)
    h = re.sub(r"^t\.me/(?:s/)?", "", h, flags=re.I)
    return h.split("/")[0].split("?")[0].strip()


def relay_key(raw: str) -> str:
    """生成 relay.py AUTHOR_RE 可匹配的作者 key。

    relay.py 的作者正则是 ``\\(@([A-Za-z0-9_]{1,20})\\)``：TG 频道名可能含
    连字符、点或超长，不规范化会在小号订阅过滤时被静默丢掉。
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", raw or "").strip("_")[:20]


def _session() -> requests.Session:
    """复用全局代理环境变量（net.apply_proxy 设置），带连接类短重试。

    云端冷启动时 mihomo 内核可能比 Flask 晚几秒监听 7890，与 x_source 同样
    需要兜住这个窗口；只重试连接错误，不重试 429/5xx。
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    retry = Retry(total=3, connect=3, read=0, status=0, backoff_factor=0.7)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    return s


def _plain(raw: str) -> str:
    """把消息文本 div 的 HTML 转成纯文本，保留换行。"""
    s = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    s = TAG_RE.sub("", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return "\n".join(ln.strip() for ln in s.split("\n")).strip()


def _is_video_poster(page: str, pos: int) -> bool:
    """判断一个 background-image 是不是视频封面，而不是真正的图片消息。

    TG 预览页把视频封面也写成 style="background-image:url(...)"，与图片帖
    用的是同一套写法。旧版会把封面同时算进 images，导致视频帖先发一次封面
    图、再转一个 GIF，群里出现重复画面。视频节点的 class 一定带 video。
    """
    start = page.rfind("<", 0, pos)
    end = page.find(">", pos)
    if start < 0 or end < 0:
        return False
    tag = page[start:end + 1]
    m = re.search(r'class=["\']([^"\']+)', tag, re.I)
    return bool(m and "video" in m.group(1).lower())


def _ts(value: str) -> int:
    """ISO 8601（t.me 用 +00:00）转 Unix 秒；解析失败返回 0。"""
    try:
        v = (value or "").strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return calendar.timegm(dt.utctimetuple())
    except Exception:
        return 0


def parse_messages(page: str, handle: str = "") -> list:
    """从预览页 HTML 解析消息列表（旧的在前），输出统一消息字典。

    预览页每条消息带 data-post="<channel>/<id>"，按该标记切块可避开页头/页脚。
    """
    posts = list(POST_RE.finditer(page or ""))
    out = []
    for i, m in enumerate(posts):
        channel, msg_id = m.group(1), m.group(2)
        end = posts[i + 1].start() if i + 1 < len(posts) else len(page)
        block = page[m.start():end]

        tm = TEXT_RE.search(block)
        text = _plain(tm.group(1)) if tm else ""
        dt = TIME_RE.search(block)
        ts = _ts(dt.group(1)) if dt else 0

        images = []
        for im in PHOTO_URL_RE.finditer(block):
            if _is_video_poster(block, im.start()):
                continue
            u = html_lib.unescape(im.group(1)).strip()
            # 只保留 TG CDN 媒体直链，排除头像/emoji 等背景图
            if ("telesco.pe" in u or "/file/" in u) and u not in images:
                images.append(u)

        videos = []
        for pat in (VIDEO_RE, SOURCE_RE):
            for u in pat.findall(block):
                u = html_lib.unescape(u).strip()
                if u.startswith("http") and u not in videos:
                    videos.append(u)

        # 无正文且无媒体的服务消息（置顶、改头像等）直接跳过
        if not text and not images and not videos:
            continue
        out.append({
            "id": f"{channel}/{msg_id}",
            # 模板里的 (@author) 会被 relay.py 用来匹配小号群订阅
            "author": relay_key(handle or channel),
            "link": f"https://t.me/{channel}/{msg_id}",
            "text": text,
            "images": images,
            "videos": videos,
            "ts": ts,
        })
    return out


def fetch_channel(handle: str, timeout: int = 25, session=None) -> list:
    """抓取一个公开频道最近约 20 条消息（旧的在前）。异常上抛由 monitor 统一处理。"""
    handle = clean_handle(handle)
    if not handle:
        raise ValueError("频道 handle 为空")
    s = session or _session()
    r = s.get(f"https://t.me/s/{handle}", timeout=timeout)
    r.raise_for_status()
    return parse_messages(r.text, handle)


def probe(handle: str = "telegram", timeout: int = 25, check_media: bool = True) -> dict:
    """云端可达性诊断：确认代理能到 t.me、HTML 结构可解析、图片 CDN 可下载。

    只读，不写任何配置、不推任何消息。异常一律转成结构化结果返回。
    """
    from core import net

    handle = clean_handle(handle) or "telegram"
    res = {
        "ok": False, "handle": handle,
        "proxy": net.current_proxy(), "url": f"https://t.me/s/{handle}",
        "http_status": 0, "bytes": 0, "elapsed_ms": 0,
        "message_count": 0, "has_preview": False,
        "channel_title": "", "samples": [], "media": {}, "error": "",
    }
    t0 = time.time()
    try:
        s = _session()
        r = s.get(res["url"], timeout=timeout)
        res["elapsed_ms"] = int((time.time() - t0) * 1000)
        res["http_status"] = r.status_code
        res["bytes"] = len(r.content)
        page = r.text
        title = TITLE_RE.search(page)
        res["channel_title"] = html_lib.unescape(title.group(1)) if title else ""
        if r.status_code >= 400:
            res["error"] = f"HTTP {r.status_code}"
            return res

        msgs = parse_messages(page, handle)
        res["message_count"] = len(msgs)
        # 空预览页与非公开频道都会返回 200，必须靠消息节点区分
        res["has_preview"] = bool(POST_RE.search(page)) and bool(msgs)
        res["samples"] = [
            {"id": m["id"], "ts": m["ts"], "link": m["link"],
             "text": m["text"][:100], "images": len(m["images"]),
             "videos": len(m["videos"])}
            for m in msgs[-3:]
        ]
        first_img = next((m["images"][0] for m in msgs if m["images"]), "")
        res["media"]["first_image"] = first_img[:200]
        res["media"]["first_video"] = next(
            (m["videos"][0] for m in msgs if m["videos"]), "")[:200]

        if check_media and first_img:
            try:
                mr = s.get(first_img, timeout=15, stream=True,
                           headers={"Range": "bytes=0-2047"})
                chunk = next(mr.iter_content(2048), b"")
                res["media"]["image_http"] = mr.status_code
                res["media"]["image_bytes"] = len(chunk)
                res["media"]["image_type"] = mr.headers.get("Content-Type", "")
                res["media"]["image_ok"] = mr.status_code < 400 and bool(chunk)
                mr.close()
            except Exception as e:
                res["media"]["image_ok"] = False
                res["media"]["image_error"] = f"{type(e).__name__}: {e}"[:150]

        res["ok"] = res["has_preview"]
        if not res["ok"]:
            res["error"] = ("页面可访问但没有 data-post 消息节点："
                            "频道可能不是公开频道，或 Telegram 改版了 HTML 结构")
        return res
    except Exception as e:
        res["elapsed_ms"] = int((time.time() - t0) * 1000)
        res["error"] = f"{type(e).__name__}: {e}"[:250]
        logging.warning("TG 探针失败 [%s]: %s", handle, res["error"])
        return res


_SAMPLE = """<html><head>
<meta property="og:title" content="Telegram Tips">
</head><body>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message"
 data-post="testchannel/101">
<div class="tgme_widget_message_text">第一条消息<br>第二行 &amp; 链接</div>
<a class="tgme_widget_message_date" href="https://t.me/testchannel/101">
<time datetime="2026-11-06T08:00:00+00:00">08:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message"
 data-post="testchannel/102">
<a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/a.jpg')"></a>
<a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/b.jpg')"></a>
<div class="tgme_widget_message_text">带两张图的消息</div>
<time datetime="2026-11-06T09:30:00+00:00">09:30</time>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message"
 data-post="testchannel/103">
<video src="https://cdn4.telesco.pe/file/c.mp4"></video>
<div class="tgme_widget_message_text">带视频</div>
<time datetime="2026-11-06T10:00:00+00:00">10:00</time>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message"
 data-post="testchannel/104">
<a class="tgme_widget_message_video_wrap" style="background-image:url('https://cdn4.telesco.pe/file/poster.jpg')"></a>
<a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/real.jpg')"></a>
<video src="https://cdn4.telesco.pe/file/d.webm"></video>
<div class="tgme_widget_message_text">视频加真图</div>
<time datetime="2026-11-06T11:00:00+00:00">11:00</time>
</div></div>
</body></html>"""


def selftest() -> bool:
    """离线解析自检（不联网），覆盖文本、时间、多图、视频、服务消息过滤。"""
    msgs = parse_messages(_SAMPLE, "testchannel")
    assert len(msgs) == 4, f"应解析 4 条，实际 {len(msgs)}"
    assert msgs[0]["id"] == "testchannel/101"
    assert msgs[0]["text"] == "第一条消息\n第二行 & 链接", msgs[0]["text"]
    assert msgs[0]["ts"] == calendar.timegm(
        datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc).utctimetuple())
    assert len(msgs[1]["images"]) == 2, msgs[1]["images"]
    assert msgs[1]["link"] == "https://t.me/testchannel/102"
    assert len(msgs[2]["videos"]) == 1, msgs[2]["videos"]
    assert msgs[2]["images"] == [], msgs[2]["images"]
    # 视频封面不能算成图片，但同一条里的真实图片必须保留
    assert msgs[3]["images"] == ["https://cdn4.telesco.pe/file/real.jpg"], msgs[3]["images"]
    assert msgs[3]["videos"] == ["https://cdn4.telesco.pe/file/d.webm"], msgs[3]["videos"]
    assert parse_messages("<html>no messages here</html>") == []
    assert parse_messages("") == []
    assert msgs[0]["author"] == "testchannel"
    assert relay_key("Seele_WW_Leak") == "Seele_WW_Leak"
    assert relay_key("some-channel.name") == "some_channel_name"
    assert relay_key("a" * 30) == "a" * 20
    print("tg_source selftest: 16/16 PASS (4 条消息 / 文本 / 时间 / 多图 / 视频 / "
          "视频封面过滤 / 空页 / relay_key)")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    elif len(sys.argv) > 1 and sys.argv[1] == "probe":
        print(json.dumps(probe(sys.argv[2] if len(sys.argv) > 2 else "telegram"),
                         ensure_ascii=False, indent=2))
    else:
        print("用法: python -m core.tg_source selftest | probe [@handle]")

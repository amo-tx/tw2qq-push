# -*- coding: utf-8 -*-
"""RSS 抓取与推文解析（兼容 RSSHub 生成的 Twitter 订阅源）"""
import html
import re
from html.parser import HTMLParser

import feedparser
import time


class _TextExtractor(HTMLParser):
    """把推文 HTML 转成纯文本，保留换行结构"""

    _IGNORE = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORE:
            self._skip += 1
        elif tag in ("br", "p", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._IGNORE and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw or "")
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n")).strip()


def extract_media(raw_html: str) -> tuple:
    """从推文 HTML 提取 (图片, 视频) URL；过滤头像 profile_images"""
    urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', raw_html or "", re.I)
    images = [html.unescape(u) for u in urls
              if "profile_images" not in u and not u.startswith("data:")]
    videos = []
    for pat in (r'<video[^>]+src=["\']([^"\']+)["\']',
                r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']',
                r'<a[^>]+href=["\']([^"\']+video\.twimg\.com[^"\']+)["\']'):
        videos += [html.unescape(u) for u in re.findall(pat, raw_html or "", re.I)]
    # 去重保序
    return list(dict.fromkeys(images)), list(dict.fromkeys(videos))


def fetch_feed(url: str, timeout: int = 30) -> list:
    """抓取一个 RSS 源，返回推文字典列表（旧的在前）"""
    d = feedparser.parse(
        url,
        request_headers={"User-Agent": "Mozilla/5.0 twitter2qq/1.0"},
    )
    if d.bozo and not d.entries:
        raise RuntimeError(f"RSS 解析失败: {getattr(d, 'bozo_exception', d.bozo)}")

    feed_title = d.feed.get("title", "") if hasattr(d.feed, "get") else ""
    out = []
    for e in d.entries:
        raw = ""
        if getattr(e, "content", None):
            raw = e.content[0].get("value", "")
        elif e.get("summary"):
            raw = e.summary

        text = html_to_text(raw) or e.get("title", "")
        images, videos = extract_media(raw)
        pp = e.get("published_parsed")
        out.append({
            "id": e.get("id") or e.get("link", ""),
            "author": e.get("author") or feed_title,
            "link": e.get("link", ""),
            "text": text,
            "images": images,
            "videos": videos,
            "ts": int(time.mktime(pp)) if pp else 0,
        })
    return out

# -*- coding: utf-8 -*-
"""推文自动翻译引擎链：
1. LLM（Pollinations 免费 OpenAI 兼容接口，走程序代理）——质量最好，保留语气
2. Google 免费端点（走程序代理）——快但直译
3. MyMemory 兜底（免代理）
结果带 24h 内存缓存。
"""
import logging
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

_cache: dict = {}          # text -> 译文
_CACHE_TTL = 24 * 3600
_cache_ts: dict = {}       # text -> 缓存时间
_TIMEOUT = 30

_PROMPT = (
    "你是推文翻译器。把下面的社交媒体帖子翻译成简体中文："
    "保留原文语气（吐槽/玩梗/感叹等）、颜文字和 emoji；"
    "人名/专有名词保留常用译名或原文；不要解释，不要加引号，只输出译文。\n\n"
)


def _looks_foreign(text: str) -> bool:
    """粗判是否需要翻译：含日文假名/谚文，或非 CJK 字母占比高"""
    if not text or len(text) < 2:
        return False
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text):
        return True
    # 统计拉丁字母占比（排除 URL、@提及、话题标签）
    stripped = re.sub(r"https?://\S+|@\w+|#\w+", "", text)
    letters = len(re.findall(r"[A-Za-z]", stripped))
    return letters >= max(8, int(len(stripped) * 0.5)) and not re.search(r"[\u4e00-\u9fff]", stripped)


def _llm(text: str) -> str:
    """Pollinations 免费 LLM（OpenAI 兼容），翻译质量最好"""
    last_err = None
    for attempt in range(3):  # 免费额度按 IP 限流，402/429 时重试
        try:
            r = requests.post(
                "https://text.pollinations.ai/openai",
                json={"model": "openai", "referrer": "twitter2qq",
                      "messages": [{"role": "user", "content": _PROMPT + text}]},
                headers=UA, timeout=90,
            )
            if r.status_code == 200:
                break
            last_err = f"HTTP {r.status_code}"
            time.sleep(4)
        except Exception as e:
            last_err = str(e)[:120]
            time.sleep(4)
    else:
        raise RuntimeError(last_err or "LLM 不可用")
    data = r.json()
    out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    # 有些模型会带前后缀说明，截掉明显不是译文的引号包裹
    if out and len(out) < len(text) * 4:
        return out
    raise RuntimeError("LLM 响应异常")


def _google(text: str) -> str:
    """Google translate 免费端点（无需 key），需能访问 translate.googleapis.com"""
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text},
        headers=UA, timeout=_TIMEOUT,
    )
    r.raise_for_status()
    segs = r.json()[0] or []
    return "".join(s[0] for s in segs if s and s[0])


def _mymemory(text: str, src: str = "") -> str:
    """MyMemory 兜底（免代理，500 字符/次，分段）"""
    out = []
    for chunk in re.findall(r".{1,480}(?:\s|$)", text, re.S):
        chunk = chunk.strip()
        if not chunk:
            continue
        pair = f"{src}|zh-CN" if src else "auto|zh-CN"
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": chunk, "langpair": pair}, headers=UA, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        out.append((r.json().get("responseData") or {}).get("translatedText", ""))
    return "\n".join(out)


_ENGINES = {"llm": _llm, "google": _google}


def translate(text: str, target: str = "zh-CN", engine: str = "llm") -> str:
    """翻译推文文本；全部引擎失败或无需翻译时返回空串（调用方自行决定是否附加）"""
    text = (text or "").strip()
    if not text:
        return ""
    hit = _cache.get(text)
    if hit and time.time() - _cache_ts.get(text, 0) < _CACHE_TTL:
        return hit
    result = ""
    # 引擎链：首选引擎 → 其余引擎 → MyMemory
    chain = []
    primary = _ENGINES.get(engine, _llm)
    chain.append(primary)
    chain += [e for e in (_llm, _google) if e is not primary]
    for fn in chain:
        try:
            result = fn(text)
            if result and result.strip() and result.strip() != text:
                break
            result = ""
        except Exception as e:
            logging.warning("%s 翻译失败: %s", fn.__name__, str(e)[:120])
    if not result:
        try:
            result = _mymemory(text)
        except Exception as e2:
            logging.warning("MyMemory 翻译也失败: %s", str(e2)[:120])
    result = (result or "").strip()
    if result and result != text:
        _cache[text], _cache_ts[text] = result, time.time()
        return result
    return ""


def translate_for_push(text: str, engine: str = "llm") -> str:
    """推送用：需要翻译则返回「原文\\n\\n🌐 翻译：译文」格式，否则返回原文"""
    if not _looks_foreign(text):
        return text
    zh = translate(text, engine=engine)
    if not zh:
        return text
    return f"{text}\n\n🌐 翻译：{zh}"

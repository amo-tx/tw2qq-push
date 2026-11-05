# -*- coding: utf-8 -*-
"""X（Twitter）内容源：auth_token Cookie 登录通道 + 博主时间线抓取

两条通道：
1. 公开 syndication 接口：无需登录即可按 handle 拉取博主时间线（受限但稳定）
2. 登录校验：auth_token Cookie + 动态提取 GraphQL Viewer queryId 验证账号有效性
统一输出与 RSS 通道相同的推文字典：{id, author, link, text, images, videos}
"""
import calendar
import json
import logging
import time
import re
import secrets
import threading

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
WEB_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAnAAnAAAAnAAAAAAAAAAAAAAAAAAA"  # 网页版公共 Bearer 占位，动态校验失败不影响公开通道

# GraphQL Following 接口的 features 声明（X 服务端要求，缺失会报错）
GQL_FEATURES = json.dumps({
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "rweb_downloads_video_enabled": False,
})


def _session(auth_token: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    if auth_token:
        s.cookies.set("auth_token", auth_token, domain=".x.com")
    # X 的 GraphQL 要求 x-csrf-token 头与 ct0 cookie 一致（双重提交）。
    # 部分网络环境下 x.com 不会主动下发 ct0，导致登录态被误判失效；
    # 这里主动生成一枚，服务端只比对两者是否相同，不校验来源。
    if not s.cookies.get("ct0"):
        s.cookies.set("ct0", secrets.token_hex(16), domain=".x.com")
    return s


def _api_headers(bearer: str, ct0: str, referer: str = "https://x.com/home") -> dict:
    return {
        "Authorization": f"Bearer {bearer or WEB_BEARER}",
        "X-Csrf-Token": ct0,
        "X-Twitter-Auth-Type": "WebClient",
        "X-Twitter-Active-User": "yes",
        "Content-Type": "application/json",
        "Referer": referer,
    }


def _extract_api_creds(s: requests.Session, html: str, limit: int = 10) -> tuple:
    """从页面引用的 JS 里提取 Bearer 与若干 GraphQL 操作的 queryId"""
    bearer, qids = None, {}
    ops = ("Viewer", "Following", "Followers", "UserTweets", "UserByScreenName")
    js_urls = []
    for u in re.findall(r'https://abs\.twimg\.com/[^\s"\'<>]+?\.js', html):
        if u not in js_urls:
            js_urls.append(u)
    for u in js_urls[:limit]:
        try:
            js = s.get(u, timeout=30).text
        except Exception:
            continue
        if not bearer:
            m = re.search(r'Bearer\s+([A-Za-z0-9%\-_]{80,})', js)
            bearer = m.group(1) if m else None
        for op in ops:
            if op in qids:
                continue
            for pat in (
                rf'queryId\s*:\s*"([^"]+)"\s*,\s*operationName\s*:\s*"{op}"',
                rf'operationName\s*:\s*"{op}"[^{{}}]{{0,200}}?queryId\s*:\s*"([^"]+)"',
                rf'"queryId"\s*:\s*"([^"]+)"\s*,\s*"operationName"\s*:\s*"{op}"',
            ):
                m = re.search(pat, js)
                if m:
                    qids[op] = m.group(1)
                    break
        if bearer and all(op in qids for op in ops):
            break
    return bearer, qids


_CTX_CACHE = {"key": None, "ts": 0.0, "ctx": None}   # 登录态上下文缓存（避免每轮轮询重复抓页面/JS）
_UID_CACHE = {}                                       # handle -> rest_id
_CTX_TTL = 900                                        # 15 分钟
_TWEETS_CACHE = {}                                    # handle -> (ts, tweets)：限流时复用
_X_LOCK = threading.Lock()
_X_LAST_REQ = 0.0                                     # 全局最小请求间隔，避免多博主并发把 X 打到 429
_X_BACKOFF = {"until": 0.0, "reason": ""}
_SYND_BACKOFF = {"until": 0.0}
MIN_REQ_GAP = 1.2


def _backoff(seconds: int, reason: str):
    with _X_LOCK:
        _X_BACKOFF.update(until=max(_X_BACKOFF["until"], time.time() + seconds),
                          reason=reason[:120])


def _x_get(s: requests.Session, url: str, **kw):
    """带全局节流与限流退避的 GET（只用于 X GraphQL / REST 接口）。"""
    global _X_LAST_REQ
    with _X_LOCK:
        now = time.time()
        left = _X_BACKOFF["until"] - now
        if left > 0:
            raise RuntimeError(f"X 限流退避中（{_X_BACKOFF['reason']}），{int(left)} 秒后自动重试")
        gap = MIN_REQ_GAP - (now - _X_LAST_REQ)
        if gap > 0:
            time.sleep(min(gap, 3.0))
        _X_LAST_REQ = time.time()
    return s.get(url, **kw)


def _json_response(r, label: str) -> dict:
    """把「空响应 / HTML 风控页」变成可读错误，并触发退避，避免每轮硬刷。"""
    ct = (r.headers.get("Content-Type") or "").lower()
    body = (r.text or "").strip()
    if r.status_code in (420, 429):
        _backoff(300, f"{label} HTTP 429 限流")
        raise RuntimeError(f"{label} 被 X 限流（HTTP 429），已自动退避 5 分钟")
    if r.status_code >= 500:
        _backoff(120, f"{label} HTTP {r.status_code}")
        raise RuntimeError(f"{label} 服务端错误 HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"{label} HTTP {r.status_code} ({ct}) {body[:100]!r}")
    if "json" not in ct and not body.startswith(("{", "[")):
        _backoff(90, f"{label} 返回非 JSON")
        raise RuntimeError(f"{label} 返回非 JSON（HTTP {r.status_code} {ct}）: {body[:100]!r}")
    try:
        return r.json()
    except ValueError:
        _backoff(90, f"{label} 响应无法解析")
        raise RuntimeError(f"{label} 响应不是合法 JSON: {body[:100]!r}")


def x_backoff_state() -> dict:
    remain = _X_BACKOFF["until"] - time.time()
    left = max(0, int(remain))
    return {"active": remain > 0, "seconds_left": left,
            "reason": _X_BACKOFF["reason"] if remain > 0 else ""}


def _authed_context(auth_token: str, force: bool = False) -> tuple:
    """登录态上下文：(session, bearer, ct0, uid, queryIds)，失效抛 RuntimeError（带缓存）"""
    key = auth_token
    now = time.time()
    if not force and _CTX_CACHE["key"] == key and _CTX_CACHE["ctx"] and now - _CTX_CACHE["ts"] < _CTX_TTL:
        return _CTX_CACHE["ctx"]
    s = _session(auth_token)
    r = s.get("https://x.com/home", timeout=25, allow_redirects=True)
    url = r.url
    if "/i/jf/onboarding" in url or "mode=login" in url or url.rstrip("/").endswith("/login"):
        raise RuntimeError("auth_token 已失效（被踢回登录页），请重新登录 x.com 并复制最新值")
    ct0 = s.cookies.get("ct0") or ""
    bearer, qids = _extract_api_creds(s, r.text)
    uid = ""
    m = re.search(r"u%3D(\d+)|u=(\d+)", s.cookies.get("twid") or "")
    if m:
        uid = m.group(1) or m.group(2) or ""
    if not uid and bearer and ct0:
        try:
            d = s.get("https://api.x.com/1.1/account/verify_credentials.json",
                      headers=_api_headers(bearer, ct0),
                      params={"skip_status": "true"}, timeout=20).json()
            uid = d.get("id_str") or ""
        except Exception:
            pass
    if not uid:
        # 登录态页面里通常还嵌着当前用户 id，再从 HTML 兜一次
        for pat in (r'"id_str"\s*:\s*"(\d{5,})"',
                    r'data-user-id="(\d{5,})"',
                    r'"rest_id"\s*:\s*"(\d{5,})"'):
            m = re.search(pat, r.text or "")
            if m:
                uid = m.group(1)
                break
    if not uid:
        # 关键：GraphQL UserTweets 只需要「目标博主」的 uid，当前账号 uid 并不参与请求。
        # 共享出口 IP（如 CI runner）常被 verify_credentials 拦截，旧实现在这里直接抛错，
        # 会把整条登录态主通道废掉、只留下容易被 429 的公开兜底。故降为告警并继续。
        logging.warning("未取到当前账号 user id（GraphQL 时间线不依赖它，继续）")
        uid = ""
    logging.info("X 登录态上下文: bearer=%s ct0=%s uid=%s qids=%s",
                 bool(bearer), bool(ct0), bool(uid), ",".join(sorted(qids)))
    ctx = (s, bearer, ct0, uid, qids)
    _CTX_CACHE.update(key=key, ts=now, ctx=ctx)
    return ctx


def validate_cookie(auth_token: str) -> dict:
    """验证 auth_token Cookie 是否有效，返回 {ok, user, error}

    X 已改版为 x-web 新架构（旧 responsive-web/main.js 的 queryId 方案失效）。
    这里做多路兜底：登录页重定向检测 → 页面 JS 提取 Bearer 调经典验证接口
    → GraphQL Viewer（旧架构兜底）→ twid Cookie 判定。
    """
    s = _session(auth_token)
    try:
        r = s.get("https://x.com/home", timeout=25, allow_redirects=True)
        url = r.url
        if "/i/jf/onboarding" in url or "mode=login" in url or url.rstrip("/").endswith("/login"):
            return {"ok": False,
                    "error": "auth_token 已失效（x.com 把会话踢回了登录页）。请重新登录 x.com 并复制新的 auth_token"}
        html = r.text
        twid = s.cookies.get("twid") or ""
        ct0 = s.cookies.get("ct0")

        # 收集页面引用的所有 JS（新旧架构都可能），从中提取 Bearer / Viewer queryId
        js_urls = []
        for u in re.findall(r'https://abs\.twimg\.com/[^\s"\'<>]+?\.js', html):
            if u not in js_urls:
                js_urls.append(u)
        bearer = None
        qid = None
        for u in js_urls[:8]:
            try:
                js = s.get(u, timeout=30).text
            except Exception:
                continue
            if not bearer:
                m = re.search(r'Bearer\s+([A-Za-z0-9%\-_]{80,})', js)
                bearer = m.group(1) if m else None
            if not qid:
                for pat in (
                    r'queryId\s*:\s*"([^"]+)"\s*,\s*operationName\s*:\s*"Viewer"',
                    r'operationName\s*:\s*"Viewer"[^{}]{0,200}?queryId\s*:\s*"([^"]+)"',
                    r'"queryId"\s*:\s*"([^"]+)"\s*,\s*"operationName"\s*:\s*"Viewer"',
                ):
                    m = re.search(pat, js)
                    if m:
                        qid = m.group(1)
                        break
            if bearer and qid:
                break

        # 路径 A：经典验证接口（bearer + ct0 + auth_token cookie）
        if bearer and ct0:
            try:
                r2 = s.get(
                    "https://api.x.com/1.1/account/verify_credentials.json",
                    headers={"Authorization": f"Bearer {bearer}",
                             "X-Csrf-Token": ct0,
                             "Referer": "https://x.com/home"},
                    params={"skip_status": "true", "include_entities": "false"},
                    timeout=20,
                )
                d = r2.json()
                sn = d.get("screen_name")
                if sn and not d.get("errors"):
                    return {"ok": True, "user": sn}
            except Exception:
                pass

        # 路径 B：GraphQL Viewer（旧架构 queryId 还在时）
        if qid and ct0:
            try:
                api = f"https://x.com/i/api/graphql/{qid}/Viewer"
                r2 = s.get(api, params={"variables": '{"withCommunitiesMemberships":true,"withSubscribedTasks":true}'},
                           headers={"Authorization": f"Bearer {bearer or WEB_BEARER}",
                                    "X-Csrf-Token": ct0,
                                    "Content-Type": "application/json",
                                    "Referer": "https://x.com/home"},
                           timeout=20)
                viewer = (r2.json().get("data") or {}).get("viewer", {}) \
                    .get("user_results", {}).get("result", {})
                screen = (viewer.get("legacy") or {}).get("screen_name")
                if screen:
                    return {"ok": True, "user": screen}
            except Exception:
                pass

        # 路径 C：twid Cookie 只在会话被服务端认可时下发（形如 u%3D<uid>）
        if twid.startswith(("u%3D", "u=")):
            m = re.search(r'"screen_name"\s*:\s*"([A-Za-z0-9_]{1,15})"', html)
            return {"ok": True, "user": m.group(1) if m else twid}

        return {"ok": False,
                "error": "无法确认登录态（页面未见登录标记）。auth_token 可能已失效，请重新复制最新值；若确认有效请重试一次"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _media_kind(m: dict) -> str:
    """兼容新旧 X 响应：type=photo/video/animated_gif 或 __typename=ApiVideo 等。"""
    t = (m.get("type") or "").lower().replace(" ", "")
    if t:
        return t
    tn = (m.get("__typename") or "").lower()
    if "animatedgif" in tn or tn.endswith("gif"):
        return "animated_gif"
    if "video" in tn:
        return "video"
    return "photo"


def _video_urls(m: dict) -> list:
    """从一条 media 对象里找出可播放地址（优先最高码率 mp4，其次 HLS）。

    新版 X 会把变体放在 video_info / videoInfo / variants / media_info 里，
    且 animated_gif 的 media_url_https 只是 ext_tw_video_thumb 的 jpg 封面，
    必须靠变体才拿得到真正的 mp4 —— 这是以前 GIF 一条都发不出来的主因。
    """
    urls = []
    raw = m.get("media_url_https") or m.get("media_url") or m.get("url") or ""
    if isinstance(raw, str) and re.search(r"\.(mp4|m4v|mov|m3u8)(\?|$)", raw, re.I):
        urls.append(raw)

    vi = m.get("video_info") or m.get("videoInfo") or {}
    if not isinstance(vi, dict):
        vi = {}
    variants = list(vi.get("variants") or [])
    for extra in (m.get("variants"), (m.get("media_info") or {}).get("variants")):
        if isinstance(extra, list):
            variants += [v for v in extra if isinstance(v, dict)]
    mp4, hls = [], []
    for v in variants:
        if not isinstance(v, dict):
            continue
        u = v.get("url") or v.get("src") or ""
        ct = (v.get("content_type") or v.get("type") or "").lower()
        if not u:
            continue
        if "mp4" in ct or re.search(r"\.mp4(\?|$)", u, re.I):
            mp4.append((int(v.get("bitrate") or 0), u))
        elif "mpegurl" in ct or "m3u8" in ct or re.search(r"\.m3u8(\?|$)", u, re.I):
            hls.append(u)
    if mp4:
        mp4.sort(key=lambda x: x[0], reverse=True)
        urls.append(mp4[0][1])
    elif hls:
        urls.append(hls[0])

    for key in ("video_url", "videoUrl", "playback_url"):
        u = m.get(key)
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
    return list(dict.fromkeys(urls))


def _parse_media(media_list: list) -> tuple:
    """从 media 数组提取 (图片列表, 视频/动图列表)，兼容 syndication 与 GraphQL。"""
    images, videos = [], []
    for m in media_list or []:
        if not isinstance(m, dict):
            continue
        vurls = _video_urls(m)
        kind = _media_kind(m)
        if vurls:
            videos.extend(vurls)
            # 视频/动图的封面缩略图不再当图片发送，避免群里多一张静态封面
            continue
        url = m.get("media_url_https") or m.get("media_url") or m.get("url") or ""
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        is_videoish = kind in ("video", "animated_gif") or "video.twimg.com" in url
        if is_videoish and "ext_tw_video_thumb" in url:
            continue
        images.append(url)
    return list(dict.fromkeys(images)), list(dict.fromkeys(videos))


def _collect_media(res: dict, legacy: dict) -> list:
    """汇总一条推文里所有可能存放媒体的位置（X 改版后位置不固定）。"""
    pools = []
    for src in (legacy.get("extended_entities"), legacy.get("entities"),
                res.get("extended_entities"), res.get("entities")):
        if isinstance(src, dict):
            pools += [x for x in (src.get("media") or []) if isinstance(x, dict)]
    for key in ("media", "mediaEntities", "media_entities"):
        extra = res.get(key)
        if isinstance(extra, list):
            pools += [x for x in extra if isinstance(x, dict)]
    # 去重：优先 media_key，其次 id_str，最后 url；同一媒体保留信息更全的那份
    # （entities 里的通常只有封面，extended_entities 里才带 video_info）。
    out, seen = [], {}
    for m in pools:
        k = (str(m.get("media_key") or "") or str(m.get("id_str") or "")
             or str(m.get("media_url_https") or m.get("media_url") or ""))
        if k and k in seen:
            idx = seen[k]
            if not _video_urls(out[idx]) and _video_urls(m):
                out[idx] = m
            continue
        if k:
            seen[k] = len(out)
        out.append(m)
    return out


def _parse_x_time(s: str) -> int:
    """X 的 created_at（如 'Wed Nov 02 12:34:56 +0000 2026'）→ unix 秒；失败返回 0"""
    try:
        return calendar.timegm(time.strptime(s, "%a %b %d %H:%M:%S %z %Y"))
    except Exception:
        return 0


def _parse_tweet(entry: dict, handle: str) -> dict:
    tw = (entry.get("content") or {}).get("tweet") or entry.get("tweet") or {}
    tid = tw.get("id_str") or tw.get("id") or ""
    if isinstance(tid, str) and tid.startswith("tweet-"):
        tid = tid.split("-", 1)[1]
    images, videos = _parse_media(tw.get("media"))
    user = tw.get("user") or {}
    return {
        "id": f"x_{tid}",
        "author": user.get("screen_name") or handle,
        "link": f"https://x.com/{handle}/status/{tid}",
        "text": tw.get("full_text") or tw.get("text") or "",
        "images": images,
        "videos": videos,
        "ts": _parse_x_time(tw.get("created_at") or ""),
    }


def _target_uid(s: requests.Session, bearer: str, ct0: str, handle: str, qids: dict | None = None) -> str:
    """目标博主的 rest_id：GraphQL UserByScreenName 优先（1.1 users/show 已被 X 废弃），带缓存"""
    if handle in _UID_CACHE:
        return _UID_CACHE[handle]
    qid = (qids or {}).get("UserByScreenName")
    if qid:
        variables = {"screen_name": handle, "withGrokTranslatedBio": False}
        r = s.get(f"https://x.com/i/api/graphql/{qid}/UserByScreenName",
                  params={"variables": json.dumps(variables), "features": GQL_FEATURES},
                  headers=_api_headers(bearer, ct0),
                  timeout=20)
        try:
            res = ((r.json().get("data") or {}).get("user") or {}).get("result") or {}
            uid = res.get("rest_id") or ""
        except ValueError:
            uid = ""
        if uid:
            _UID_CACHE[handle] = uid
            return uid
    # 兜底：1.1 经典接口（可能已废弃，但留着无妨）
    r = s.get("https://api.x.com/1.1/users/show.json",
              headers=_api_headers(bearer, ct0),
              params={"screen_name": handle}, timeout=20)
    d = r.json()
    uid = d.get("id_str") or ""
    if not uid:
        raise RuntimeError(f"找不到博主 @{handle}（{str(d.get('errors'))[:120]}）")
    _UID_CACHE[handle] = uid
    return uid


def _gql_timeline(auth_token: str, handle: str, count: int = 20) -> tuple:
    """请求 UserTweets，返回 (data, meta)；meta 只含可诊断的非敏感信息。"""
    s, bearer, ct0, uid, qids = _authed_context(auth_token)
    qid = qids.get("UserTweets")
    if not qid:
        raise RuntimeError("页面 JS 中未找到 UserTweets queryId")
    tuid = _target_uid(s, bearer, ct0, handle, qids)
    variables = {"userId": tuid, "count": count,
                 "includePromotedContent": False,
                 "withQuickPromoteEligibilityTweetFields": True,
                 "withVoice": False}
    r = _x_get(
        s, f"https://x.com/i/api/graphql/{qid}/UserTweets",
        params={"variables": json.dumps(variables), "features": GQL_FEATURES},
        headers=_api_headers(bearer, ct0, f"https://x.com/{handle}"),
        timeout=25,
    )
    meta = {"http": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "bytes": len(r.content or b""),
            "query_id": qid, "target_uid": tuid,
            "body_head": (r.text or "")[:160]}
    data = _json_response(r, f"UserTweets[@{handle}]")
    errors = data.get("errors")
    if errors:
        meta["errors"] = str(errors)[:200]
        raise RuntimeError(f"UserTweets 返回错误: {str(errors)[:160]}")
    meta["body_head"] = ""      # 成功响应不需要回传正文片段
    return data, meta


def _tweet_text(res: dict, legacy: dict) -> str:
    """长推文（note_tweet）正文不在 legacy.full_text 里。"""
    note = (((res.get("note_tweet") or {}).get("note_tweet_results") or {})
            .get("result") or {})
    return note.get("text") or legacy.get("full_text", "")


def _fetch_tweets_graphql(auth_token: str, handle: str, count: int = 20) -> list:
    """登录态 GraphQL UserTweets 时间线（syndication 改版后的主通道）"""
    data, _meta = _gql_timeline(auth_token, handle, count)
    tl = (((data.get("data") or {}).get("user") or {}).get("result") or {})
    tl = (tl.get("timeline_v2") or tl.get("timeline") or {}).get("timeline") or {}

    # 收集条目：AddEntries 的 entries + 置顶推文（TimelinePinEntry）
    raw_entries = []
    for ins in tl.get("instructions") or []:
        if ins.get("type") == "TimelineAddEntries":
            raw_entries += ins.get("entries") or []
        elif ins.get("type") == "TimelinePinEntry" and ins.get("entry"):
            raw_entries.append(ins["entry"])

    out = []
    for e in raw_entries:
        content = e.get("content") or {}
        # 普通条目 + 线程模块（TimelineTimelineModule 内含多条）
        items = [content]
        for mod in content.get("moduleItems") or []:
            items.append(mod.get("item") or mod)
        for it in content.get("items") or []:
            items.append(it.get("item") or it)
        for item in items:
            tc = item.get("itemContent") or item.get("tweetResults") or {}
            res = (tc.get("tweet_results") or {}).get("result") or {}
            if not res or res.get("__typename") == "TweetTombstone":
                continue
            # 转推：取被转推的原推
            if res.get("retweeted_status_result"):
                res = (res["retweeted_status_result"].get("result") or res)
            # 新版 X 把可见性受限推文包一层 TweetWithVisibilityResults，
            # 正文在 result.tweet 里；不解包会整条跳过（视频推文常属于这类）。
            if not res.get("legacy") and isinstance(res.get("tweet"), dict):
                res = res["tweet"]
            if not isinstance(res.get("legacy"), dict):
                continue
            legacy = res.get("legacy") or {}
            core = ((res.get("core") or {}).get("user_results") or {}).get("result") or {}
            sn = (core.get("legacy") or {}).get("screen_name") or handle
            tid = legacy.get("id_str") or res.get("rest_id") or ""
            if not tid:
                continue
            out.append({
                "id": f"x_{tid}",
                "author": sn,
                "link": f"https://x.com/{sn}/status/{tid}",
                "text": _tweet_text(res, legacy),
                "images": [], "videos": [],  # 下面统一从 media 提取
                "ts": _parse_x_time(legacy.get("created_at") or ""),
                "_media": _collect_media(res, legacy),
            })
    for t in out:
        media = t.pop("_media", [])
        t["images"], t["videos"] = _parse_media(media)
    n_img = sum(len(t["images"]) for t in out)
    n_vid = sum(len(t["videos"]) for t in out)
    logging.info("X 通道 GraphQL [%s] %d 条推文（图片 %d / 视频·动图 %d）",
                 handle, len(out), n_img, n_vid)
    return out


def fetch_user_tweets(handle: str, auth_token: str = "", timeout: int = 30) -> list:
    """拉取某博主最近推文：登录态 GraphQL 优先，公开 syndication 兜底"""
    handle = handle.strip().lstrip("@")
    reason = ""

    # 主通道：GraphQL UserTweets（auth_token 有效时稳定，syndication 已被 X 改版为返回 HTML）
    if auth_token:
        try:
            tweets = _fetch_tweets_graphql(auth_token, handle)
            _TWEETS_CACHE[handle] = (time.time(), tweets)
            return tweets
        except Exception as e:
            reason = str(e)
            logging.warning("GraphQL 时间线 [%s] 失败: %s", handle, reason)
            if "auth_token 已失效" in reason:
                raise
            # 限流期间复用上次成功的时间线：seen 去重表保证不会重复推送，
            # 也避免 10 个博主 × 每 2 分钟持续撞 X 的 429，越刷越封。
            cached = _TWEETS_CACHE.get(handle)
            if cached and time.time() - cached[0] < 900:
                logging.warning("[%s] X 暂不可用，复用 %d 分钟前的时间线缓存",
                                handle, int((time.time() - cached[0]) / 60))
                return cached[1]

    with _X_LOCK:
        cooldown_left = _SYND_BACKOFF["until"] - time.time()
    if cooldown_left > 0:
        raise RuntimeError(
            f"X 时间线暂不可用：{reason or '未配置 auth_token'}；公开通道冷却中（{int(cooldown_left)} 秒后重试）")

    s = _session(auth_token)
    r = s.get(
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}",
        params={"showreplies": "false"},
        timeout=timeout,
    )
    if r.status_code in (420, 429):
        with _X_LOCK:
            _SYND_BACKOFF["until"] = time.time() + 600
        raise RuntimeError("X 公开时间线被限流（HTTP 429），已冷却 10 分钟")
    if r.status_code != 200:
        raise RuntimeError(f"X 时间线拉取失败 HTTP {r.status_code}（博主不存在或接口受限）")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            "X 接口返回非 JSON（X 已改版限制公开通道）。请在「X 账号」页配置有效的 auth_token，程序将改用登录态接口")

    entries = (data.get("timeline") or {}).get("entries") or data.get("entries") or []
    out = [_parse_tweet(e, handle) for e in entries if e.get("type", "tweet") == "tweet" or (e.get("content") or {}).get("tweet")]
    logging.info("X 通道 syndication [%s] 获取 %d 条推文", handle, len(out))
    return out


def diagnose_timeline(handle: str, auth_token: str, probe: bool = True,
                      count: int = 8) -> dict:
    """线上只读诊断：返回真实 UserTweets 响应里媒体字段的结构与视频地址可达性。

    不返回 auth_token / Bearer / Cookie；用于确认「视频到底有没有解析出来、
    解析出来后 CDN 能不能下载」，避免靠猜改代码。
    """
    handle = (handle or "").strip().lstrip("@")
    out = {"ok": True, "handle": handle, "backoff": x_backoff_state(),
           "tweets": [], "counts": {"tweets": 0, "images": 0, "videos": 0}}
    try:
        data, meta = _gql_timeline(auth_token, handle, count)
    except Exception as e:
        out.update(ok=False, error=str(e)[:220], backoff=x_backoff_state())
        return out
    out["meta"] = meta
    tl = (((data.get("data") or {}).get("user") or {}).get("result") or {})
    tl = (tl.get("timeline_v2") or tl.get("timeline") or {}).get("timeline") or {}
    entries = []
    for ins in tl.get("instructions") or []:
        if ins.get("type") == "TimelineAddEntries":
            entries += ins.get("entries") or []
        elif ins.get("type") == "TimelinePinEntry" and ins.get("entry"):
            entries.append(ins["entry"])

    all_videos = []
    for e in entries:
        content = e.get("content") or {}
        items = [content]
        for mod in content.get("moduleItems") or []:
            items.append(mod.get("item") or mod)
        for it in content.get("items") or []:
            items.append(it.get("item") or it)
        for item in items:
            tc = item.get("itemContent") or item.get("tweetResults") or {}
            res = (tc.get("tweet_results") or {}).get("result") or {}
            if not isinstance(res, dict) or not res:
                continue
            tn = res.get("__typename") or ""
            if res.get("retweeted_status_result"):
                res = (res["retweeted_status_result"].get("result") or res)
            if not res.get("legacy") and isinstance(res.get("tweet"), dict):
                res = res["tweet"]
            legacy = res.get("legacy") or {}
            if not isinstance(legacy, dict) or not legacy:
                continue
            media = _collect_media(res, legacy)
            imgs, vids = _parse_media(media)
            all_videos += vids
            out["tweets"].append({
                "id": legacy.get("id_str") or res.get("rest_id") or "",
                "typename": tn,
                "media": len(media),
                "media_kinds": [_media_kind(m) for m in media],
                "media_fields": [sorted(m.keys())[:20] for m in media],
                "variant_types": [
                    [str(v.get("content_type")) for v in
                     ((m.get("video_info") or {}).get("variants") or [])]
                    for m in media],
                "images": len(imgs), "videos": len(vids),
                "video_urls": [u[:120] for u in vids[:2]],
            })
            if len(out["tweets"]) >= count:
                break
        if len(out["tweets"]) >= count:
            break
    out["counts"] = {
        "tweets": len(out["tweets"]),
        "images": sum(t["images"] for t in out["tweets"]),
        "videos": sum(t["videos"] for t in out["tweets"]),
    }
    if probe and all_videos:
        url = all_videos[0]
        host = (re.match(r"https?://([^/]+)", url) or [None, ""])[1]
        try:
            r = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://x.com/",
                              "Range": "bytes=0-1023"},
                stream=True, timeout=20)
            out["media_probe"] = {
                "host": host, "http": r.status_code,
                "content_type": r.headers.get("Content-Type", ""),
                "content_length": r.headers.get("Content-Length", ""),
                "accept_ranges": r.headers.get("Accept-Ranges", ""),
            }
            r.close()
        except Exception as e:
            out["media_probe"] = {"host": host, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return out


def fetch_following(auth_token: str, max_pages: int = 5) -> list:
    """读取当前账号的关注列表 [{handle, name, avatar}]

    优先 GraphQL Following（分页 cursor），失败兜底 1.1 friends/list.json。
    最多 max_pages 页，约 500 个账号。
    """
    s, bearer, ct0, uid, qids = _authed_context(auth_token)
    if not uid:
        raise RuntimeError("无法获取当前账号 user id，读取关注列表需要该值")
    users, seen = [], set()

    def _add(sn, name="", avatar=""):
        sn = (sn or "").strip().lstrip("@")
        if sn and sn.lower() not in seen:
            seen.add(sn.lower())
            users.append({"handle": sn, "name": name or sn, "avatar": avatar or ""})

    # 路径 1：GraphQL Following
    qid = qids.get("Following")
    if qid and ct0:
        try:
            cursor = None
            for _ in range(max_pages):
                variables = {"userId": uid, "count": 100, "includePromotedContent": False}
                if cursor:
                    variables["cursor"] = cursor
                r = s.get(
                    f"https://x.com/i/api/graphql/{qid}/Following",
                    params={"variables": json.dumps(variables), "features": GQL_FEATURES},
                    headers=_api_headers(bearer, ct0, f"https://x.com/i/user/{uid}"),
                    timeout=25,
                )
                data = r.json()
                if r.status_code != 200 or data.get("errors"):
                    raise RuntimeError(str(data.get("errors"))[:150] or f"HTTP {r.status_code}")
                tl = ((data.get("data") or {}).get("user") or {}).get("result") or {}
                tl = (tl.get("timeline") or {}).get("timeline") or tl.get("timeline") or {}
                cursor = None
                for ins in tl.get("instructions") or []:
                    for e in ins.get("entries") or []:
                        content = e.get("content") or {}
                        if content.get("entryType") == "TimelineTimelineCursor":
                            if content.get("cursorType") == "Bottom":
                                cursor = content.get("value")
                            continue
                        res = (content.get("userResults") or {}).get("result") or {}
                        core, legacy = res.get("core") or {}, res.get("legacy") or {}
                        sn = legacy.get("screen_name") or core.get("screen_name")
                        _add(sn, legacy.get("name") or core.get("name"),
                             (legacy.get("profile_image_url_https") or "").replace("_normal", "_bigger"))
                if not cursor:
                    break
            if users:
                logging.info("关注列表（GraphQL）：获取 %d 个博主", len(users))
                return users
        except Exception as e:
            logging.warning("GraphQL Following 失败，改走 1.1 接口: %s", e)
            users, seen = [], set()

    # 路径 2：1.1 friends/list.json 兜底
    cursor = -1
    for _ in range(max_pages):
        r = s.get(
            "https://api.x.com/1.1/friends/list.json",
            headers=_api_headers(bearer, ct0),
            params={"user_id": uid, "count": 200, "cursor": cursor,
                    "skip_status": "true", "include_user_entities": "false"},
            timeout=25,
        )
        if r.status_code != 200:
            raise RuntimeError(f"读取关注列表失败 HTTP {r.status_code}: {r.text[:150]}")
        d = r.json()
        for u in d.get("users") or []:
            _add(u.get("screen_name"), u.get("name"),
                 (u.get("profile_image_url_https") or "").replace("_normal", "_bigger"))
        cursor = int(d.get("next_cursor", 0) or 0)
        if not cursor:
            break
    if not users:
        raise RuntimeError("关注列表为空或接口未返回数据（可能被限流，稍后再试）")
    logging.info("关注列表（1.1）：获取 %d 个博主", len(users))
    return users

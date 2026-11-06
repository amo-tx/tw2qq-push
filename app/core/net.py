# -*- coding: utf-8 -*-
"""全局网络代理支持

原理：把代理写入进程环境变量（HTTP_PROXY/HTTPS_PROXY），requests 默认
trust_env=True 会自动读取，因此 x_source / qq_bot / feed / media / qq_login
等所有模块的请求无需改动即可走代理；NO_PROXY 保证 NapCat(127.0.0.1) 不被代理。

兼容 ChromeGo 等 Clash/Xray 类工具的本地混合代理端口：
  clash.meta = 7890，Xray/hysteria/sing-box/naive 等 = 1080
"""
import logging
import os
import socket
import time

# 常见本地代理端口（Clash 系 / V2Ray 系 / ChromeGo 各内核）
COMMON_PORTS = [7890, 7897, 1080, 2080, 3080, 4080, 10808, 10809, 33210]
# 本地 NapCat / 控制台，以及腾讯官方 API、WorkBuddy 云数据面一律直连：
# 这些服务国内可直连，且必须在 ChromeGo 内核启动之前/期间就可用，否则冷启动会
# 因代理端口尚未监听而 Connection refused（QQ WS / token / 云数据库）。
# 只有 x.com 等被墙站点才走 HTTP_PROXY。
NO_PROXY_HOSTS = (
    "127.0.0.1,localhost,"
    ".qq.com,.sgroup.qq.com,.bots.qq.com,.q.qq.com,"
    ".workbuddy.host,.workbuddy.cn,.tencentbuddy.com,.myqcloud.com"
)


def apply_proxy(proxy_url: str):
    """设置/清除全局代理（proxy_url 为空字符串表示直连）"""
    proxy_url = (proxy_url or "").strip()
    if proxy_url and not proxy_url.startswith(("http://", "socks5://", "socks4://")):
        proxy_url = "http://" + proxy_url  # 允许用户只填 127.0.0.1:7890
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        logging.info("全局代理已启用: %s", proxy_url)
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
    # 本地 NapCat / 控制台永不走代理
    os.environ["NO_PROXY"] = NO_PROXY_HOSTS
    os.environ["no_proxy"] = NO_PROXY_HOSTS
    return proxy_url


def current_proxy() -> str:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""


def _port_open(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_local_proxies() -> list:
    """探测本机在监听的常见代理端口，返回 ['127.0.0.1:7890', ...]"""
    return [f"127.0.0.1:{p}" for p in COMMON_PORTS if _port_open(p)]


def test_proxy(proxy_url: str, targets=None, timeout: int = 12) -> list:
    """通过指定代理（None=直连）访问测试目标，返回 [{target, ok, detail}]"""
    import requests

    proxies = (
        {"http": proxy_url, "https": proxy_url} if proxy_url else None
    )
    targets = targets or [
        ("x.com", "https://x.com/robots.txt"),
        ("GitHub API", "https://api.github.com/zen"),
    ]
    results = []
    for label, url in targets:
        t0 = time.time()
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            ms = int((time.time() - t0) * 1000)
            results.append({"target": label, "ok": r.status_code < 500,
                            "detail": f"HTTP {r.status_code} ({ms}ms)"})
        except Exception as e:
            results.append({"target": label, "ok": False,
                            "detail": f"{type(e).__name__}: {e}"[:120]})
    return results

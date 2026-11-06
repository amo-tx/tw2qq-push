# -*- coding: utf-8 -*-
"""媒体处理：视频/动图下载 + 转 GIF（ffmpeg，经 imageio-ffmpeg 自带二进制）"""
import logging
import os
import subprocess
from pathlib import Path

import requests

# QQ 官方机器人富媒体图片上限 5MB；留出 base64 与元数据余量，
# GIF 一律压到 4.5MB 以下再上传（以前 media.py 放到 6MB，上传必失败）。
MAX_GIF_BYTES = int(4.5 * 1024 * 1024)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def ffmpeg_exe() -> str:
    from core.platform import ffmpeg_exe as _resolve
    return _resolve()


def _ffmpeg_env() -> dict:
    """ffmpeg 读小写的 http_proxy；云端访问 X CDN 必须继承 WorkBuddy 代理。"""
    env = os.environ.copy()
    proxy = env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or ""
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env.setdefault("no_proxy", env.get("NO_PROXY", ""))
    return env


def download_media(url: str, dest: Path, timeout: int = 180) -> Path:
    """流式下载媒体文件（pbs.twimg.com / video.twimg.com 均为公开 CDN）。

    先写 .part 再原子替换，避免半截 mp4 被 ffmpeg 当成有效输入；
    返回 HTML/空内容时明确报错，便于日志判断是不是被代理拦截。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _UA, "Referer": "https://x.com/"}
    with requests.get(url, stream=True, timeout=timeout, headers=headers,
                      allow_redirects=True) as r:
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError(f"媒体地址返回 HTML（可能被代理/风控拦截）: {url[:100]}")
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
        if size == 0:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError(f"媒体下载为空: {url[:100]}")
        tmp.replace(dest)
    logging.info("媒体已下载: %s (%.1f MB, %s)", dest.name, size / 1048576, ct)
    return dest


def hls_url(url: str) -> bool:
    return ".m3u8" in str(url).lower()


def video_to_gif(video_path, gif_path: Path = None,
                 max_seconds: int = 10, width: int = 480, fps: int = 12,
                 max_bytes: int = MAX_GIF_BYTES) -> Path:
    """视频/动图转 GIF：调色板两段法保证色彩质量；超长自动截断前 N 秒。

    video_path 可以是本地 mp4，也可以是 HLS(.m3u8) URL（X 少数视频只给 HLS，
    此时 ffmpeg 直接读流，不落盘）。体积超限时逐级降分辨率/帧率/时长，
    保证结果能塞进 QQ 富媒体接口。
    """
    src = str(video_path)
    gif_path = Path(gif_path) if gif_path else Path(video_path).with_suffix(".gif")
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    import sys

    def _render(w: int, f: int, secs: float):
        vf = (
            f"fps={f},scale={w}:-2:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer"
        )
        cmd = [
            ffmpeg_exe(), "-y", "-loglevel", "error",
            "-i", src, "-t", str(secs), "-vf", vf, "-loop", "0",
            str(gif_path),
        ]
        r = subprocess.run(
            cmd, capture_output=True, timeout=600, env=_ffmpeg_env(),
            creationflags=0x08000000 if sys.platform == "win32" else 0)  # CREATE_NO_WINDOW
        if r.returncode != 0:
            raise RuntimeError(
                f"ffmpeg 转换失败: {r.stderr[-400:].decode('utf-8', 'ignore')}")

    attempts = [
        (width, fps, max_seconds),
        (360, 10, max_seconds),
        (280, 8, max(4, max_seconds // 2)),
        (220, 7, 4),
        (160, 5, 3),
    ]
    last = 0
    for i, (w, f, secs) in enumerate(attempts):
        _render(w, f, secs)
        size = gif_path.stat().st_size
        last = size
        if size <= max_bytes:
            if i > 0:
                logging.info("GIF 已压缩到 %.1fMB (%dpx/%dfps/%.0fs)",
                             size / 1048576, w, f, secs)
            return gif_path
    logging.warning("GIF 仍偏大 %.1fMB，QQ 可能拒收", last / 1048576)
    return gif_path

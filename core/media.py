# -*- coding: utf-8 -*-
"""媒体处理：视频下载 + 视频转 GIF（ffmpeg，经 imageio-ffmpeg 自带二进制）"""
import logging
import subprocess
from pathlib import Path

import requests


def ffmpeg_exe() -> str:
    from core.platform import ffmpeg_exe as _resolve
    return _resolve()


def download_media(url: str, dest: Path, timeout: int = 180) -> Path:
    """流式下载媒体文件（pbs.twimg.com / video.twimg.com 均为公开 CDN）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return dest


def video_to_gif(video_path: Path, gif_path: Path = None,
                 max_seconds: int = 10, width: int = 480, fps: int = 12,
                 max_bytes: int = 6 * 1024 * 1024) -> Path:
    """视频转 GIF：调色板两段法保证色彩质量；超长自动截断前 N 秒。

    大小超 max_bytes 时自动降级（降分辨率/帧率/时长）重转，
    QQ 消息里塞不下几十 MB 的 base64，这是之前 GIF 发不出来的根因。
    """
    gif_path = gif_path or Path(video_path).with_suffix(".gif")

    def _render(w: int, f: int, secs: float):
        vf = (
            f"fps={f},scale={w}:-2:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer"
        )
        cmd = [
            ffmpeg_exe(), "-y", "-i", str(video_path),
            "-t", str(secs), "-vf", vf, "-loop", "0", str(gif_path),
        ]
        import sys
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)  # CREATE_NO_WINDOW
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 转换失败: {r.stderr[-400:].decode('utf-8', 'ignore')}")

    attempts = [
        (width, fps, max_seconds),
        (360, 10, max_seconds),
        (280, 8, max(4, max_seconds // 2)),
        (220, 7, 4),
    ]
    for i, (w, f, secs) in enumerate(attempts):
        _render(w, f, secs)
        size = gif_path.stat().st_size
        if size <= max_bytes or i == len(attempts) - 1:
            if i > 0:
                logging.info("GIF 已压缩: %.1fMB -> %.1fMB (%dx%d/%dfps/%.0fs)",
                             gif_path.stat().st_size / 1048576, size / 1048576,
                             w, w, f, secs)
            return gif_path
    return gif_path

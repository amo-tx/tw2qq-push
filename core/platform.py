# -*- coding: utf-8 -*-
"""平台判断：Windows 桌面版 / Android(Termux) 移植版共用一套代码。

- Windows: 完整功能（内置 ChromeGo 内核、NapCat Shell 进程管理、PyInstaller 打包）
- Android(Termux): 纯 Python 部分 + NapCat 由 proot 容器自行管理（外部模式），
  代理走系统全局 VPN（Clash for Android 等），ffmpeg 用 Termux 的 pkg 版
"""
import os
import shutil
import sys

IS_WINDOWS = sys.platform == "win32"


def _is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "") or ""
    if "com.termux" in prefix:
        return True
    return os.path.isdir("/data/data/com.termux")


def _is_android_apk() -> bool:
    """APK 版：proot Debian 内由启动脚本导出 TW2QQ_ANDROID=1"""
    return os.environ.get("TW2QQ_ANDROID", "") == "1"


IS_ANDROID = (not IS_WINDOWS) and (_is_termux() or _is_android_apk())
IS_LINUX = (not IS_WINDOWS) and (not IS_ANDROID) and hasattr(os, "fork")
EXTERNAL_NAPCAT = IS_ANDROID or IS_LINUX  # NapCat 不由本程序拉起，仅探测 OneBot 端口


def ffmpeg_exe() -> str:
    """ffmpeg 可执行文件：优先 imageio-ffmpeg 自带二进制，再退到系统 PATH。

    云端 Linux 镜像通常没有系统 ffmpeg；旧实现只在 Windows 分支尝试
    imageio_ffmpeg，Linux 分支还调用了未 import 的 shutil.which，导致云端
    转 GIF 一律 NameError 失败（表现为「视频/GIF 拉不到」）。这里统一为
    「自带二进制 → PATH → 兜底名」，三个平台行为一致。
    """
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return exe
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    return found or "ffmpeg"

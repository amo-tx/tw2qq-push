# -*- coding: utf-8 -*-
"""平台判断：Windows 桌面版 / Android(Termux) 移植版共用一套代码。

- Windows: 完整功能（内置 ChromeGo 内核、NapCat Shell 进程管理、PyInstaller 打包）
- Android(Termux): 纯 Python 部分 + NapCat 由 proot 容器自行管理（外部模式），
  代理走系统全局 VPN（Clash for Android 等），ffmpeg 用 Termux 的 pkg 版
"""
import os
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
    """ffmpeg 可执行文件：Windows 用打包内置的 imageio_ffmpeg，其他平台用系统 PATH"""
    if IS_WINDOWS:
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"

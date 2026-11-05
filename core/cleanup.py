# -*- coding: utf-8 -*-
"""垃圾文件自动清理：
1. PyInstaller onefile 残留：%TEMP%/_MEI* 目录（每次运行释放 ~150MB 内核，
   进程被强杀时不会自删，会无限累积）——只删含本程序标记(chromego)且超过 1 天的
2. 媒体缓存：data/media 下已推送过的视频/GIF——按天数过期 + 总量上限（LRU）
"""
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

_MEI_MIN_AGE_SEC = 12 * 3600     # _MEI 目录至少存活 12 小时才清理（避免误删正在运行的实例）
_MEI_MARKER = "chromego"          # 本程序 onefile 释放目录的特有子目录


def cleanup_mei_temp(max_age_sec: int = _MEI_MIN_AGE_SEC) -> int:
    """清理 %TEMP% 下属于本程序的 _MEI* 残留目录，返回清理数量（仅 Windows PyInstaller 场景）"""
    from core.platform import IS_WINDOWS
    if not IS_WINDOWS:
        return 0  # 安卓/Linux 源码运行，无 _MEI 释放
    tmp = tempfile.gettempdir()
    now = time.time()
    n = 0
    try:
        entries = os.listdir(tmp)
    except Exception:
        return 0
    for name in entries:
        if not name.startswith("_MEI"):
            continue
        p = Path(tmp) / name
        try:
            if not p.is_dir():
                continue
            if not (p / _MEI_MARKER).exists():
                continue  # 不是本程序的（其他 PyInstaller 应用不能动）
            if now - p.stat().st_mtime < max_age_sec:
                continue  # 太新，可能正在使用
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                n += 1
        except Exception:
            continue
    if n:
        logging.info("已清理 %d 个 _MEI 临时残留目录", n)
    return n


def cleanup_media_cache(base: Path, days: int = 3, max_mb: int = 500) -> tuple:
    """清理 data/media 媒体缓存：按天数过期 + 总量上限（LRU 删最旧）。
    返回 (按时间删除数, 按容量删除数)"""
    media_dir = Path(base) / "data" / "media"
    if not media_dir.is_dir():
        return 0, 0
    now = time.time()
    expired = 0
    files = []
    for f in media_dir.iterdir():
        try:
            if not f.is_file():
                continue
            st = f.stat()
            files.append((st.st_mtime, st.st_size, f))
            if days > 0 and now - st.st_mtime > days * 86400:
                f.unlink(missing_ok=True)
                expired += 1
        except Exception:
            continue
    if expired:
        logging.info("媒体缓存：清理 %d 个超过 %d 天的文件", expired, days)
        files = [(mt, sz, f) for mt, sz, f in files if f.exists()]
    # 容量上限（LRU）
    by_cap = 0
    if max_mb > 0:
        total = sum(sz for _, sz, _ in files)
        limit = max_mb * 1024 * 1024
        if total > limit:
            files.sort(key=lambda x: x[0])  # 最旧在前
            for _, size, f in files:
                if total <= limit:
                    break
                try:
                    f.unlink(missing_ok=True)
                    total -= size
                    by_cap += 1
                except Exception:
                    break
            if by_cap:
                logging.info("媒体缓存：超 %dMB 上限，删除最旧 %d 个文件", max_mb, by_cap)
    return expired, by_cap


def run_all(base: Path, config: dict):
    """启动时执行全部清理（静默，异常不影响主流程）"""
    try:
        cleanup_mei_temp()
    except Exception as e:
        logging.warning("_MEI 清理失败: %s", e)
    try:
        cleanup_media_cache(
            base,
            days=int((config or {}).get("media_cache_days", 3)),
            max_mb=int((config or {}).get("media_cache_max_mb", 500)),
        )
    except Exception as e:
        logging.warning("媒体缓存清理失败: %s", e)

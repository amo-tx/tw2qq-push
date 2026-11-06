# -*- coding: utf-8 -*-
"""GitHub Actions one-shot 入口：抓 X → 推 QQ 官方机器人 → 状态写云数据库。

无状态设计：每次从云数据库拉去重记录，跑完即退出。凭据全部来自环境变量
（GitHub Secrets），注入后的配置只写进临时目录，绝不回写仓库。
"""
import io
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

REQUIRED_SECRETS = ("X_AUTH_TOKEN", "QQ_APP_ID", "QQ_APP_SECRET",
                    "CLOUD_PUBLISHABLE_KEY")

CLOUD_APP = os.environ.get("TW2QQ_CLOUD_APP",
                           "https://tw2qq-pusher.app.workbuddy.host")
SHARECODE = os.environ.get("TW2QQ_SHARECODE", "Z3YR5C5DTB")


def wake_cloud(trigger_check: bool = True) -> dict:
    """唤醒云端应用，必要时让它代跑本轮。

    WorkBuddy 免费云应用是「按需唤醒」的 sandbox：一旦长时间无外部 HTTP 请求，
    平台会回收进程，进程内的 self-ping / watchdog 也随之消失。因此**每一轮**
    GHA 运行都必须先无条件打一次 /api/health（trigger_check=False 时只唤醒），
    而不只是在自己抓 X 失败时才唤醒 —— 这是当前唯一完全独立于用户电脑和
    WorkBuddy 桌面客户端的外部心跳源。

    云端走代理，X 通道正常；trigger_check=True 时额外同步触发一轮抓取。
    任何异常只记录、不抛出。
    """
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "tw2qq-gha-watchdog/1.0"})
    q = {"sharecode": SHARECODE}
    out = {"ok": False, "health": None, "checked": None}
    for i in range(3):
        try:
            r = s.get(CLOUD_APP + "/api/health", params=q, timeout=45)
            out["health"] = r.status_code
            if r.status_code == 200:
                out["ok"] = True
                break
        except Exception as e:
            logging.warning("云端应用唤醒第 %d 次失败: %s", i + 1, e)
        time.sleep(5)
    if not out["ok"] or not trigger_check:
        return out
    # 立刻触发一轮抓取以缩短空窗；该接口同步执行，超时不影响判定
    try:
        r = s.post(CLOUD_APP + "/api/check", params=q, timeout=150)
        out["checked"] = r.status_code
        if r.status_code == 200:
            logging.info("云端代跑一轮: %s", str(r.json())[:200])
    except Exception as e:
        logging.warning("云端代跑触发失败（云端轮询线程仍会自行抓取）: %s", e)
    return out


def build_runtime_config():
    cfg = yaml.safe_load(io.open(REPO / "config.yaml", encoding="utf-8").read())
    missing = [k for k in REQUIRED_SECRETS if not os.environ.get(k)]
    if missing:
        raise RuntimeError("缺少 GitHub Secrets: " + ", ".join(missing))
    cfg["x"]["auth_token"] = os.environ["X_AUTH_TOKEN"]
    cfg["qq"]["official"]["app_id"] = os.environ["QQ_APP_ID"]
    cfg["qq"]["official"]["client_secret"] = os.environ["QQ_APP_SECRET"]
    cfg["qq"]["official"]["collect_groups"] = False  # one-shot 不需要 WS
    cfg["cloud"]["publishable_key"] = os.environ["CLOUD_PUBLISHABLE_KEY"]
    # 本地联调可用该变量临时挂代理；GHA runner 在境外直连，不设置
    if os.environ.get("TW2QQ_PROXY"):
        cfg["network"]["proxy"] = os.environ["TW2QQ_PROXY"]

    # 每次运行独立临时目录：data/media、logs、注入后的 config 都放这里
    run_base = Path(tempfile.mkdtemp(prefix="tw2qq-"))
    (run_base / "data" / "media").mkdir(parents=True, exist_ok=True)
    (run_base / "logs").mkdir(exist_ok=True)
    io.open(run_base / "config.yaml", "w", encoding="utf-8").write(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    return cfg, run_base


def main():
    # 独立于 WorkBuddy 客户端的外部心跳：无论本轮抓 X 是否成功，都先把
    # 云端 sandbox 唤回在线。冷启动可能需要几十秒，故放在最前面并行预热。
    import threading
    threading.Thread(target=lambda: wake_cloud(trigger_check=False),
                     daemon=True, name="cloud-wake").start()

    cfg, run_base = build_runtime_config()
    from core.monitor import Monitor
    from core.cloudstore import CloudStore

    dry = os.environ.get("TW2QQ_DRY_RUN", "0") == "1"
    store = CloudStore(cfg["cloud"]["endpoint"],
                       cfg["cloud"]["publishable_key"], dry_run=dry)
    monitor = Monitor(run_base)   # 构造时用本地空库，随后整体替换为云存储
    monitor.store = store

    if dry:
        would_push = []

        def fake_send_post(groups, text, images=None, video_paths=None,
                           at_all=False):
            would_push.append({"groups": list(groups),
                               "text": (text or "")[:70],
                               "images": len(images or []),
                               "videos": len(video_paths or [])})
            return list(groups), []

        monitor.bot.send_post = fake_send_post

    run_id = os.environ.get("GITHUB_RUN_ID", "local")

    def release_cloud():
        """本轮失败时清掉 GHA 心跳，避免云端让位却无人推送。"""
        try:
            store.clear_heartbeat()
            logging.info("已清除 GHA 心跳，云端应用将立即接管轮询")
        except Exception as e:
            logging.warning("清除 GHA 心跳失败: %s", e)

    try:
        # 必须先确认 QQ 通道可用再写心跳：凭据失效时不能把云端也拖停
        ok, info = monitor.bot.get_status()
        logging.info("QQ 官方通道: %s / %s", ok, info)
        if not ok:
            raise RuntimeError("QQ 官方通道不可用: " + str(info))

        # 写心跳：云端应用据此在本轮执行期间让位，避免双份推送。
        # 注意：GHA 是 one-shot，跑完必须立刻释放（见 finally），否则云端
        # 会因为「15 分钟内 GHA 在线」持续让位，而 GHA 早已退出 → 推送空窗。
        try:
            store.write_heartbeat("gha-" + str(run_id),
                                  {"pushed_total": store.total_seen()})
            logging.info("GHA 心跳已写入（云端应用本轮让位）")
        except Exception as e:
            logging.warning("GHA 心跳写入失败（不影响本轮推送）: %s", e)

        sent, errors = monitor.check_once()
        logging.info("本轮完成：推送 %d 条，源错误 %d，云端写入 %d 次，去重总数 %d",
                     sent, errors, store.writes, store.total_seen())
        if dry:
            logging.info("DRY_RUN 本将推送: %s", would_push)

        # 全部博主都抓失败：CI runner 的共享出口 IP 常被 X 限流。
        # 此时先释放心跳让云端恢复轮询，再唤醒云端代跑，避免「两头都停」。
        # 只有云端也不可达时才判本轮失败。dry-run 不判失败（本机常无代理）
        if errors >= len(cfg["twitter"]["feeds"]) and not dry:
            release_cloud()
            cloud = wake_cloud()
            if cloud["ok"]:
                logging.warning(
                    "本轮 GHA 直连 X 全部失败（出口 IP 限流），已交回云端应用推送；"
                    "health=%s check=%s", cloud["health"], cloud["checked"])
                return 0
            raise RuntimeError("全部博主抓取失败，且云端应用不可达（health=%s）"
                               % cloud["health"])
        return 0
    except Exception:
        release_cloud()
        raise
    finally:
        # 无论成功失败，one-shot 结束后都把主推送权交还云端轮询。
        # 云端 watchdog 每 30s 巡检，会立即重启 monitor，空窗从 15 分钟降到 30 秒。
        if not dry:
            try:
                store.clear_heartbeat()
                logging.info("已释放 GHA 心跳，云端应用恢复常驻轮询")
            except Exception as e:
                logging.warning("释放 GHA 心跳失败: %s", e)


if __name__ == "__main__":
    sys.exit(main())

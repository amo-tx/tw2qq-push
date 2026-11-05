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
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

REQUIRED_SECRETS = ("X_AUTH_TOKEN", "QQ_APP_ID", "QQ_APP_SECRET",
                    "CLOUD_PUBLISHABLE_KEY")


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

    # 先写心跳：云端应用据此自动让位，避免这一轮双份推送
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    try:
        store.write_heartbeat("gha-" + str(run_id),
                              {"pushed_total": store.total_seen()})
        logging.info("GHA 心跳已写入（云端应用将自动让位）")
    except Exception as e:
        logging.warning("GHA 心跳写入失败（不影响本轮推送）: %s", e)

    ok, info = monitor.bot.get_status()
    logging.info("QQ 官方通道: %s / %s", ok, info)
    if not ok:
        raise RuntimeError("QQ 官方通道不可用: " + str(info))

    sent, errors = monitor.check_once()
    logging.info("本轮完成：推送 %d 条，源错误 %d，云端写入 %d 次，去重总数 %d",
                 sent, errors, store.writes, store.total_seen())
    if dry:
        logging.info("DRY_RUN 本将推送: %s", would_push)

    # 全部博主都抓失败才判失败；单源异常不阻断。dry-run 不判失败（本机常无代理）
    if errors >= len(cfg["twitter"]["feeds"]) and not dry:
        raise RuntimeError("全部博主抓取失败，请检查网络或 X 凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())

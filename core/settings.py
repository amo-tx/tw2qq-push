# -*- coding: utf-8 -*-
"""配置与运行时状态的唯一读写入口。

旧实现里同一份配置被四处各读各的：
  * webui 里 _disk_config / _save_config_key 用 monitor.config_path
  * webui 里 _relay_cfg 曾误用相对 cwd 的 Path("relay.json")
  * relay.py 用 BASE / "relay.json"
  * clouddb 另有云库副本
路径不一致导致「前端绑定订阅成功、转发器重启后订阅丢失」。这里把路径与
读写规则固定下来，并保留多副本自愈（sandbox 重置任一目录都能找回）。

另外明确区分两类文件：
  * 配置文件（config.yaml / relay.json）——随部署包发布
  * 运行时状态（data/seen.db 等）——**不得**放进部署包，否则每次发布都会把
    云端累积的去重记录回滚成打包时的旧快照，正是刷屏的根因之一。
"""
import json
import logging
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent

CONFIG_PATH = BASE / "config.yaml"
RELAY_CFG_PATH = BASE / "relay.json"
# 订阅属于运行时状态：只写在这里、且不随部署包发布。重新发布时部署工具是
# 覆盖式上传（云端多出来的文件不会被删），因此这里的用户订阅能一直保留。
RELAY_STATE_PATH = BASE / "data" / "relay_targets.json"
RELAY_BACKUP = RELAY_STATE_PATH      # 兼容旧名字
# 旧版把整份 relay.json 当「备份」写进同名文件，内容可能是几个月前的空订阅。
# 加版本标记区分：只有带标记的状态文件才算用户当前的订阅，旧文件一律迁移。
STATE_SCHEMA = 2
RELAY_LEGACY = Path("relay.json")     # 老版本误用的 cwd 相对路径，仅作兼容

# 运行时状态（部署包中应排除；云端 sandbox 内持久保留）
RUNTIME_FILES = ("data/seen.db", "data/seen.db-wal", "data/seen.db-shm",
                 "data/push_state.json", "data/relay_targets.json",
                 "data/relay.db", "data/relay_source.json",
                 "data/keepalive.log")


def deep_merge(dst: dict, patch: dict):
    """递归合并：patch 里的嵌套 dict 只更新对应子键，不整体替换（否则丢兄弟键）。"""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def read_yaml(path=None) -> dict:
    path = Path(path or CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def write_yaml(cfg: dict, path=None):
    path = Path(path or CONFIG_PATH)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def patch_yaml(patch: dict, path=None, fallback: dict = None) -> dict:
    """定点更新配置键：从磁盘重读最新内容再打补丁，避免覆盖手动编辑过的键。"""
    cfg = read_yaml(path) or dict(fallback or {})
    deep_merge(cfg, patch)
    write_yaml(cfg, path)
    return cfg


# ---------- relay.json（静态配置） + data/relay_targets.json（运行时订阅） ----------
def _read_json(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_relay_cfg(rj: dict):
    """静态配置写 relay.json（随包发布），订阅另写运行时状态文件（不随包发布）。"""
    text = json.dumps(rj, ensure_ascii=False, indent=2)
    RELAY_CFG_PATH.write_text(text, encoding="utf-8")
    try:
        RELAY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RELAY_STATE_PATH.write_text(json.dumps(
            {"schema": STATE_SCHEMA,
             "target_groups": rj.get("target_groups") or [],
             "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.warning("[relay] 订阅状态写入失败（重启后可能回退）: %s", e)
    # 老版本误用的 cwd 相对路径，保留一份兼容副本
    try:
        if RELAY_LEGACY.resolve() != RELAY_CFG_PATH.resolve():
            RELAY_LEGACY.write_text(text, encoding="utf-8")
    except Exception as e:
        logging.debug("转发配置兼容副本写入失败: %s", e)


def read_relay_cfg() -> dict:
    """静态配置来自 relay.json；target_groups 以运行时订阅状态为权威。

    重新部署会用包里的 relay.json 覆盖云端文件，如果订阅只写在那里，
    每次发布都要手工重新绑定；状态文件不在包里，覆盖上传不会删掉它。
    """
    cfg = _read_json(RELAY_CFG_PATH)
    state = _read_json(RELAY_STATE_PATH)
    if state.get("schema") == STATE_SCHEMA and "target_groups" in state:
        cfg["target_groups"] = state["target_groups"]
        return cfg
    # 旧版无标记的备份只当迁移素材，不能覆盖包里的订阅
    if not cfg.get("target_groups"):
        old = state.get("target_groups") or _read_json(RELAY_LEGACY).get("target_groups")
        if old:
            cfg["target_groups"] = old
    return cfg


def _has_feeds(targets) -> bool:
    return any(isinstance(t, dict) and (t.get("feeds") or []) for t in (targets or []))


def ensure_relay_state() -> dict:
    """启动时把订阅收敛成一份「带版本标记」的运行时状态，并同步所有可能路径。

    重新部署会覆盖 relay.json，而旧版遗留的 data/relay_targets.json 没有版本
    标记、内容可能还是几个月前的空订阅。这里做一次性迁移：
        带标记状态 > 包内 relay.json > config.yaml 的 relay.target_groups
                   > 旧备份 / cwd 相对路径
    结果同时写回 relay.json 和状态文件，转发器无论读哪个文件都拿到同一份订阅。
    之后每次部署状态文件都会保留，不再需要手工热重载。
    """
    static = _read_json(RELAY_CFG_PATH)
    state = _read_json(RELAY_STATE_PATH)
    if state.get("schema") == STATE_SCHEMA and "target_groups" in state:
        targets = state["target_groups"]
    else:
        targets = static.get("target_groups") or []
        if not _has_feeds(targets):
            targets = ((read_yaml().get("relay") or {}).get("target_groups")
                       or targets)
        if not _has_feeds(targets):
            targets = (state.get("target_groups")
                       or _read_json(RELAY_LEGACY).get("target_groups")
                       or targets)
    rj = dict(static)
    if targets:
        rj["target_groups"] = targets
        write_relay_cfg(rj)
        logging.info("[relay] 订阅状态已播种/迁移: %s", targets)
    return read_relay_cfg()


def normalize_targets(rj: dict) -> list:
    """target_groups 归一化成 [{"id": int, "feeds": [小写 handle,...]}]，兼容旧纯群号写法。"""
    out = []
    for x in (rj.get("target_groups") or []):
        if isinstance(x, dict):
            try:
                gid = int(x.get("id") or 0)
            except (TypeError, ValueError):
                continue
            feeds = [str(f).strip().lstrip("@").lower()
                     for f in (x.get("feeds") or []) if str(f).strip()]
        else:
            try:
                gid = int(x)
            except (TypeError, ValueError):
                continue
            feeds = []
        if gid > 0 and all(t["id"] != gid for t in out):
            out.append({"id": gid, "feeds": feeds})
    return out


def packaged_runtime_files(base=None) -> list:
    """列出误被打进**本地待发布目录**的运行时状态文件（部署前应移走）。

    只有同工作区存在 tw2qq-cloud/ 发布目录时才属于「打包前检查」；
    云端运行时没有这个同级目录，此时 data/seen.db 是进程自己维护的
    正常运行库，不是打包内容，返回空列表以免误报。
    """
    base = Path(base or BASE)
    sibling = base.parent / "tw2qq-cloud"
    # 云端可能就叫 tw2qq-cloud/，此时 sibling 就是运行目录本身，不能再当
    # 「待发布目录」检查，否则进程自己维护的 seen.db 会被误报成打包污染。
    if sibling.resolve() == base.resolve() or not (sibling / "config.yaml").exists():
        return []
    return [str(rel) for rel in RUNTIME_FILES if (sibling / rel).exists()]

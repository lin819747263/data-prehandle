"""服务端状态落盘：工作区命令日志与最后一次会话。

这里落的**从来不是明细数据**。明细要么在数据集目录里的原始文件中，要么能由
「载入来源 + 命令日志重放」复原。之所以要落这份日志：撤销/重做的历史一旦搬到服务端，
只有它跨进程存在，才谈得上「刷新还在、后端重启也还在」——浏览器内存里的快照栈做不到，
localStorage 更做不到（它存不下也不该存下整帧历史）。

    TSS_STATE_DIR（默认 <cwd>/.tss-state）
      workspaces/<wsId>.json   单个工作区：来源、命令日志（含重放私有上下文）、游标
      session.json             最后一次会话：UI 层状态 + 它引用了哪些工作区

与数据集目录（`dataset_store`，只放表格）分开的理由：这两个文件不是数据资产，
混进 dataset/ 会被「最近打开的数据集」列成可打开的表格。两者共用同一套文件名守卫。
"""
from __future__ import annotations
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
LOG_VERSION = 1
MAX_LOG_BYTES = 32 * 1024 * 1024      # 单个日志上限：命令规格不该撑到几十 MB
SESSION_KEY = "session.json"


def state_dir() -> Path:
    override = os.environ.get("TSS_STATE_DIR")
    root = Path(override).expanduser().resolve() if override else Path.cwd() / ".tss-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspaces_dir() -> Path:
    d = state_dir() / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_atomic(path: Path, payload: str) -> None:
    """先写临时文件再 replace：进程在中途被杀掉也不会留下半份 JSON。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _ws_path(ws_id: str) -> Path:
    if not WORKSPACE_ID_RE.match(str(ws_id or "")):
        raise ValueError(f"工作区 ID 不合法：{ws_id!r}")
    path = (workspaces_dir() / f"{ws_id}.json").resolve()
    if not path.is_relative_to(workspaces_dir().resolve()):
        raise ValueError("工作区日志路径越界")
    return path


def save_workspace_log(ws_id: str, doc: dict) -> None:
    payload = json.dumps(doc, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) > MAX_LOG_BYTES:
        raise ValueError(f"命令日志已达 {MAX_LOG_BYTES // 1024 // 1024} MiB 上限，请删除多余工作区")
    _write_atomic(_ws_path(ws_id), payload)


def load_workspace_log(ws_id: str) -> dict | None:
    path = _ws_path(ws_id)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 日志坏了不能装作没发生：留在原地并改名备份，调用方按「没有历史」处理
        bad = path.with_suffix(f".broken-{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
        path.rename(bad)
        raise ValueError(f"工作区 {ws_id} 的日志已损坏，原文件备份为 {bad.name}")
    return doc if isinstance(doc, dict) and doc.get("v") == LOG_VERSION else None


def delete_workspace_log(ws_id: str) -> bool:
    path = _ws_path(ws_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_workspace_logs() -> list[dict]:
    """磁盘上有哪些工作区历史（含当前进程没装载的那些）。"""
    out = []
    for path in sorted(workspaces_dir().glob("*.json")):
        if not WORKSPACE_ID_RE.match(path.stem):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(doc, dict) or doc.get("v") != LOG_VERSION:
            continue
        out.append({
            "wsId": doc.get("wsId") or path.stem,
            "name": (doc.get("meta") or {}).get("name") or "workspace",
            "rowCount": (doc.get("meta") or {}).get("rowCount"),
            "colCount": (doc.get("meta") or {}).get("colCount"),
            "version": int(doc.get("cursor") or 0),
            "opsTotal": len(doc.get("ops") or []),
            "source": doc.get("source") or {},
            "updatedAt": doc.get("updatedAt") or "",
            "loaded": False,
        })
    return out


def save_session(doc: dict) -> dict:
    payload = {"v": LOG_VERSION, "savedAt": _stamp(), **doc}
    _write_atomic(state_dir() / SESSION_KEY, json.dumps(payload, ensure_ascii=False, default=str))
    return payload


def load_session() -> dict | None:
    path = state_dir() / SESSION_KEY
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    return doc if isinstance(doc, dict) and doc.get("v") == LOG_VERSION else None


def clear_session() -> bool:
    path = state_dir() / SESSION_KEY
    if not path.exists():
        return False
    path.unlink()
    return True

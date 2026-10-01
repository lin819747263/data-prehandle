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


def is_workspace_id(ws_id: object) -> bool:
    """这个字符串能不能当 wsId：路由层要先问它，才能把「请求写错了」和「工作区不存在」分开。

    没有它就只能一律按「查不到」处理，非法 ID 也就跟着吃了 404（或更糟：进到磁盘那一步
    由 _ws_path 抛 ValueError 变成 500）。
    """
    return bool(WORKSPACE_ID_RE.match(str(ws_id or "")))


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
    if not is_workspace_id(ws_id):
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


def _log_summary(path) -> dict | None:
    """读一份命令日志，只取出列表页要的那几个字段；读不出可用摘要就回 None。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != LOG_VERSION:
        return None
    return {
        "wsId": doc.get("wsId") or path.stem,
        "name": (doc.get("meta") or {}).get("name") or "workspace",
        "rowCount": (doc.get("meta") or {}).get("rowCount"),
        "colCount": (doc.get("meta") or {}).get("colCount"),
        "version": int(doc.get("cursor") or 0),
        "opsTotal": len(doc.get("ops") or []),
        "source": doc.get("source") or {},
        "updatedAt": doc.get("updatedAt") or "",
        "loaded": False,
    }


# 每份日志的摘要缓存：文件名 → (mtime_ns, 字节数, 摘要 | None)。
# 工作区列表每刷新一次都要看磁盘上全部历史，而它们绝大多数一个字都没变
# （本机 111 份实测 16.3 ms/次，缓存全命中约 0.2 ms）。写日志只走 save_workspace_log 的
# 原子 replace，那必然刷新 mtime（纳秒），所以 mtime+size 相同就认为摘要也相同；
# 列表页这一份允许比真相差一次刷新，代价换掉每次请求重跑整目录的 JSON 解析。
# None 表示这份文件读不出摘要（坏 JSON / 版本不符），不必再解析第二遍。
_LOG_SUMMARY: dict[str, tuple[int, int, dict | None]] = {}


def list_workspace_logs() -> list[dict]:
    """磁盘上有哪些工作区历史（含当前进程没装载的那些）。

    条目是缓存里那份，调用方只读：改它就会污染列表页下一次的数据。
    """
    out = []
    seen: set[str] = set()
    for path in sorted(workspaces_dir().glob("*.json")):
        if not WORKSPACE_ID_RE.match(path.stem):
            continue
        seen.add(path.name)
        try:
            info = path.stat()
            token = (info.st_mtime_ns, info.st_size)
        except OSError:
            continue
        cached = _LOG_SUMMARY.get(path.name)
        if cached is not None and cached[:2] == token:
            if cached[2] is not None:
                out.append(cached[2])
            continue
        summary = _log_summary(path)
        _LOG_SUMMARY[path.name] = (token[0], token[1], summary)
        if summary is not None:
            out.append(summary)
    # 删掉的工作区要把条目一起摘掉，否则同名文件重建时可能顶着旧摘要
    for gone in _LOG_SUMMARY.keys() - seen:
        del _LOG_SUMMARY[gone]
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

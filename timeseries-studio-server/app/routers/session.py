"""会话（session）路由：上次打开了什么、停在哪一步，由服务端记住。

搬这一层的理由不是"localStorage 存不下"，而是它记的东西本身就说不清还剩多少是真的：
浏览器存下的会话快照里写着"工作区 X 在第 7 版"，可 X 早可能被淘汰、服务端也可能重启过。
现在会话只存 UI 层状态 + 它引用了哪些 wsId，`GET /api/session` 当场按命令日志把每个
工作区重建/核对一遍，回给你**此刻真实存活**的版本号与可撤销/可重做状态——
横幅上那句"上次会话"于是永远是有依据的陈述，而不是一句承诺。

明细与历史都不在这里：明细在数据集目录的原始文件里，历史在各工作区的命令日志里。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..schemas import (MAX_SESSION_ACTION_LOG, MAX_SESSION_BYTES, MAX_SESSION_INLINE_ARRAY,
                       SessionRequest)
from ..services import state_store, workspace as ws_store

router = APIRouter(prefix="/api/session", tags=["session"])


def _long_arrays(node, path: str, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _long_arrays(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(node, list):
        if len(node) > MAX_SESSION_INLINE_ARRAY:
            out.append(f"{path}（{len(node)} 个元素）")
        for i, v in enumerate(node[:8]):
            _long_arrays(v, f"{path}[{i}]", out)


def _validate(payload: SessionRequest) -> None:
    records = payload.actionLog or []
    if len(records) > MAX_SESSION_ACTION_LOG:
        raise HTTPException(status_code=400,
                            detail=f"操作记录 {len(records)} 条，超过会话上限 {MAX_SESSION_ACTION_LOG} 条")
    heavy: list[str] = []
    for i, rec in enumerate(records):
        _long_arrays(rec, f"actionLog[{i}]", heavy)
    if heavy:
        raise HTTPException(
            status_code=400,
            detail="会话里不允许携带整列数据：" + "、".join(heavy[:4]) +
                   f"（审计记录只该存规格与数字，明细留在服务端工作区）")
    size = len(json.dumps(payload.model_dump(), ensure_ascii=False).encode("utf-8"))
    if size > MAX_SESSION_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"会话内容 {size // 1024} KiB，超过 {MAX_SESSION_BYTES // 1024 // 1024} MiB 上限")


def _probe(ws_id: str, want_version: int | None) -> dict:
    """按日志把工作区核对/重建一遍，回它此刻的真实状态。"""
    out = {"wsId": ws_id, "alive": False, "name": None, "version": None,
           "rowCount": None, "canUndo": False, "canRedo": False, "reason": None}
    if not ws_id:
        out["reason"] = "会话没有记录工作区 ID"
        return out
    try:
        ws = ws_store.get(ws_id)
    except KeyError:
        out["reason"] = "没有这个工作区的命令日志，请回第一步重新载入数据"
        return out
    except ValueError as exc:
        out["reason"] = str(exc)
        return out
    m = ws.meta_view()
    out.update({"alive": True, "name": m["name"], "version": m["version"],
                "rowCount": m["rowCount"], "canUndo": m["canUndo"], "canRedo": m["canRedo"],
                "undoLabel": m["undoLabel"], "redoLabel": m["redoLabel"],
                "opsTotal": m["opsTotal"], "logError": m.get("logError")})
    if want_version is not None and want_version != m["version"]:
        out["versionDrift"] = {"sessionSays": want_version, "serverSays": m["version"]}
    return out


@router.get("")
def get_session() -> dict:
    doc = state_store.load_session()
    if not doc:
        return {"exists": False}
    workspaces = [{**w, **_probe(w.get("wsId") or "", w.get("version"))}
                  for w in (doc.get("workspaces") or [])]
    return {"exists": True, "savedAt": doc.get("savedAt"), "meta": doc.get("meta"),
            "snap": {k: v for k, v in doc.items()
                     if k not in ("v", "savedAt", "meta", "workspaces")},
            "workspaces": workspaces,
            "stateDir": str(state_store.state_dir())}


@router.put("")
def put_session(payload: SessionRequest) -> dict:
    _validate(payload)
    dump = payload.model_dump()
    names = {}
    for w in dump.get("workspaces") or []:
        try:
            names[w["wsId"]] = ws_store.get(w["wsId"]).meta.get("name")
        except (KeyError, ValueError):
            names[w["wsId"]] = None
    dump["workspaces"] = [{**w, "name": names.get(w.get("wsId"))} for w in dump.get("workspaces") or []]
    doc = state_store.save_session(dump)
    return {"saved": True, "savedAt": doc["savedAt"],
            "meta": doc.get("meta"), "bytes": len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))}


@router.delete("")
def delete_session() -> dict:
    return {"cleared": state_store.clear_session()}

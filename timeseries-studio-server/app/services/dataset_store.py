"""数据集目录（dataset/）读写：为"最近打开的数据集"提供真实文件来源。

目录默认为后端进程的 <cwd>/dataset，可用环境变量 TSS_DATASET_DIR 覆盖。
不存在时自动创建。上传导入的文件会落盘到该目录，因此下次打开即出现在最近列表。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

# 与前端 accept 保持一致的表格格式白名单
ALLOWED_EXTS = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".parquet", ".feather", ".ft"}

_FORMAT_BY_EXT = {
    ".csv": "csv", ".tsv": "tsv", ".txt": "txt",
    ".xlsx": "xlsx", ".xls": "xls",
    ".parquet": "parquet", ".feather": "feather", ".ft": "feather",
}


def dataset_dir() -> Path:
    """解析并确保数据集目录存在。"""
    raw = os.environ.get("TSS_DATASET_DIR", "").strip()
    path = Path(raw).expanduser() if raw else Path.cwd() / "dataset"
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def size_text(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def safe_name(filename: str) -> str:
    """校验并归一化文件名，拒绝任何越出目录的路径写法。"""
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        raise ValueError("文件名为空")
    if name.startswith("."):
        raise ValueError("不接受隐藏文件")
    if ".." in name or "/" in name:
        raise ValueError("文件名不得包含路径分隔符")
    if not re.fullmatch(r"[^\x00-\x1f<>:\"|?*]+", name):
        raise ValueError("文件名含非法字符")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的文件类型 {ext or '(无扩展名)'}，仅支持 {'/'.join(sorted(ALLOWED_EXTS))}")
    return name


def resolve_in_dir(filename: str) -> Path:
    """把文件名解析为目录内的真实路径，并二次确认未越界。"""
    root = dataset_dir()
    target = (root / safe_name(filename)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("非法的文件路径")
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{filename}")
    return target


def list_datasets() -> dict:
    """列举目录内的可解析表格文件，按修改时间倒序。"""
    root = dataset_dir()
    items = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ALLOWED_EXTS:
            continue
        st = p.stat()
        items.append({
            "filename": p.name,
            "name": p.stem,
            "ext": ext.lstrip("."),
            "format": _FORMAT_BY_EXT[ext],
            "size": st.st_size,
            "sizeText": size_text(st.st_size),
            "modifiedAt": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "mtime": int(st.st_mtime),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"dir": str(root), "count": len(items), "items": items}


def read_bytes(filename: str) -> tuple[bytes, str]:
    """读取目录内已存在的文件，返回 (字节, 真实文件名)。"""
    path = resolve_in_dir(filename)
    return path.read_bytes(), path.name


def save_bytes(filename: str, content: bytes) -> str:
    """把上传的文件写入目录，重名时追加时间戳后缀，返回最终文件名。"""
    name = safe_name(filename)
    root = dataset_dir()
    target = root / name
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        name = f"{stem}__{datetime.now():%Y%m%d_%H%M%S}{ext}"
        target = root / name
    target.write_bytes(content)
    return name

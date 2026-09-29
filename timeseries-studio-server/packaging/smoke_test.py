"""打包产物冒烟测试：真的把 exe 拉起来，打几个走完整依赖链的接口，再整棵进程树收掉。

挑这几条接口不是随手选的，每条都对应一个 PyInstaller 最容易漏的东西：
  /api/health                      服务起得来 + 数据集/状态目录确实落到了指定位置
  /api/ws/preset                   pandas / numpy 的 cython 扩展在不在
  /api/ws/{id}/anomaly-detect      sklearn IsolationForest（函数内延迟导入，静态分析看不见）
  /api/ws/{id}/export?format=*     openpyxl 与 pyarrow 这两套二进制扩展
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

IS_WIN = os.name == "nt"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, payload: dict | None = None, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read()
            ctype = res.headers.get("Content-Type", "")
            return (json.loads(body) if "json" in ctype else body), res.status
    except urllib.error.HTTPError as exc:
        # 422/400 的 detail 才是线索，光抛 HTTPError 等于没测
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"{method} {url} → HTTP {exc.code}：{detail}") from exc


def kill_tree(pid: int) -> None:
    if IS_WIN:
        subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"], capture_output=True)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)


class Checker:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.rows.append((name, bool(ok), detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}：{detail}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包版后端冒烟测试")
    parser.add_argument("--exe", required=True, help="timeseries-backend.exe 路径")
    parser.add_argument("--boot-timeout", type=float, default=180.0, help="one-file 解包慢，探活最长等多久")
    args = parser.parse_args()

    exe = Path(args.exe).resolve()
    if not exe.exists():
        print(f"找不到产物：{exe}")
        return 2
    size_mb = exe.stat().st_size / 1024 / 1024

    work = Path(tempfile.mkdtemp(prefix="tss-smoke-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [str(exe), "--host", "127.0.0.1", "--port", str(port),
           "--dataset-dir", str(work / "dataset"), "--state-dir", str(work / ".tss-state")]

    print(f"产物：{exe}")
    print(f"体积：{size_mb:.1f} MB")
    print(f"启动：{' '.join(cmd)}")

    started = time.time()
    proc = subprocess.Popen(cmd, cwd=str(exe.parent), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    c = Checker()
    health = None
    try:
        body = None
        while time.time() - started < args.boot_timeout:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print(f"进程提前退出 code={proc.returncode}\n{out[-4000:]}")
                return 3
            try:
                body, status = http("GET", f"{base}/api/health", timeout=3)
                if status == 200:
                    health = body
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        boot_s = time.time() - started
        if health is None:
            print(f"FAIL 探活超时（{args.boot_timeout:.0f}s）")
            return 3

        c.check("探活", health.get("status") == "ok",
                f"v{health.get('version')} · 能力 {len(health.get('capabilities') or [])} 项 · {boot_s:.1f}s")
        c.check("数据集目录", str(Path(health["datasetDir"]).resolve()) == str((work / "dataset").resolve()),
                health["datasetDir"])
        c.check("状态目录", str(Path(health["stateDir"]).resolve()) == str((work / ".tss-state").resolve()),
                health["stateDir"])
        codecs = health.get("exportCodecs") or {}
        c.check("导出编码可用", all(v is None for v in codecs.values()) and len(codecs) >= 4,
                " ".join(f"{k}={'可用' if v is None else v}" for k, v in codecs.items()))

        t = time.time()
        preset, status = http("POST", f"{base}/api/ws/preset", {"key": "pv", "seed": 7})
        meta = preset["meta"]
        c.check("pandas/numpy 建区", status == 200 and meta["rowCount"] > 0,
                f"{meta['rowCount']} 行 × {len(meta['columns'])} 列 · {time.time() - t:.2f}s")
        ws_id = meta["wsId"]

        t = time.time()
        det, status = http("POST", f"{base}/api/ws/{ws_id}/anomaly-detect",
                           {"algo": "iforest_sklearn", "nEstimators": 100, "contamination": "auto", "randomState": 42})
        d = det.get("detection") or {}
        total = (d.get("summary") or {}).get("totalAnomalies", -1)
        c.check("sklearn 孤立森林", status == 200 and total >= 0 and d.get("engine") == "sklearn.IsolationForest",
                f"引擎 {d.get('engine')} · 命中 {total} 条 · {time.time() - t:.2f}s")

        for fmt in ("csv", "xlsx", "parquet", "feather"):
            t = time.time()
            raw, status = http("GET", f"{base}/api/ws/{ws_id}/export?format={fmt}", timeout=120)
            n = len(raw) if isinstance(raw, bytes) else 0
            c.check(f"导出 {fmt}", status == 200 and n > 0, f"{n / 1024:.1f} KB · {time.time() - t:.2f}s")

        # cols 留空 = 服务端自己挑全部数值列，省得在这边重算一遍"哪些列是数值"
        stats, status = http("GET", f"{base}/api/ws/{ws_id}/stats")
        c.check("统计矩阵", status == 200, f"{len(stats.get('rows') or [])} 列指标 · {stats.get('rowCount')} 行")

        http("DELETE", f"{base}/api/ws/{ws_id}")
    finally:
        kill_tree(proc.pid)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in c.rows if not r[1]]
    print(f"\n结果：{len(c.rows) - len(failed)}/{len(c.rows)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

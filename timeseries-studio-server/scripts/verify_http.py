"""第①期 HTTP 端验收：用真实 HTTP 请求证明"明细留在服务端"，并与 pandas 原始读取对拍。

前置：uvicorn app.main:app --port 8000（本脚本不自启后端）。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_http.py
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]


def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None,
         content_type: str | None = None) -> tuple[int, bytes]:
    url = BASE + path
    data = None
    ct = content_type
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        ct = "application/json"
    elif raw is not None:
        data = raw
    req = urllib.request.Request(url, data=data, method=method)
    if ct:
        req.add_header("Content-Type", ct)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = call(method, path, body)
    text = payload.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"_raw": text[:400]}
    if status >= 400:
        raise SystemExit(f"请求失败 {method} {path} → {status} {text[:400]}")
    return parsed


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----tss" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode("utf-8"))
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def show(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def ok(label: str, cond: bool, detail: object = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}{(' · ' + str(detail)) if detail != '' else ''}")
    return cond


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: object = "") -> None:
        if not ok(name, cond, detail):
            failures.append(name)

    health = api("GET", "/api/health")
    print("\n== /api/health ==")
    show("version", health["status"])
    show("能力数", len(health["capabilities"]))
    show("上限", health["limits"])

    # ---------- 1. machine.csv：与原始 pandas 读取对拍 ----------
    print("\n== 1. 上传 machine.csv：与 pd.read_csv 原始结果对拍 ==")
    csv_bytes = (ROOT / "dataset" / "machine.csv").read_bytes()
    body, ctype = multipart("file", "machine.csv", csv_bytes)
    status, payload = call("POST", "/api/ws", raw=body, content_type=ctype)
    created = json.loads(payload.decode("utf-8"))
    check("上传返回 200", status == 200, f"HTTP {status}")
    meta = created["meta"]
    ws = meta["wsId"]
    show("工作区", ws)
    show("表头", [c["key"] for c in meta["columns"]])
    show("行列", f"{meta['rowCount']} × {meta['colCount']}")
    show("频率", meta["freqLabel"])
    show("格式投票", meta["timeDetect"])

    ref = pd.read_csv(ROOT / "dataset" / "machine.csv", encoding="utf-8-sig")
    ref_ts = pd.to_datetime(ref["timestamp"])
    check("行数与 pd.read_csv 一致", meta["rowCount"] == len(ref), f"{meta['rowCount']} vs {len(ref)}")
    check("列数与 pd.read_csv 一致", meta["colCount"] == ref.shape[1], f"{meta['colCount']} vs {ref.shape[1]}")

    ov = api("GET", f"/api/ws/{ws}/overview")
    show("总览", {k: ov[k] for k in ["cellCount", "missingDenominator", "missingCells", "missingRate",
                                     "duplicateRows", "unparsedTimes", "memoryBytes"] if k in ov})
    ref_missing = int(ref.isna().sum().sum()) + int(ref["timestamp"].isna().sum())
    check("缺失单元格数与 pandas 一致", ov["missingCells"] == ref_missing,
          f"{ov['missingCells']} vs {ref_missing}")
    ref_dup = int(ref.duplicated().sum())
    check("重复行数与 pandas 一致", ov["duplicateRows"] == ref_dup, f"{ov['duplicateRows']} vs {ref_dup}")
    show("时间范围", ov["timeRange"])
    check("时间范围两端与 pandas 一致",
          [ov["timeRange"]["start"], ov["timeRange"]["end"]]
          == [ref_ts.min().strftime("%Y-%m-%d %H:%M:%S"), ref_ts.max().strftime("%Y-%m-%d %H:%M:%S")],
          f"{ref_ts.min()} ~ {ref_ts.max()}")

    # ---------- 2. 内置示例：命令链 + 版本号 ----------
    print("\n== 2. pv 示例：重命名 → 单位换算 → 派生列 → 时间格式 → 重采样 ==")
    preset = api("POST", "/api/ws/preset", {"key": "pv", "seed": 7})
    w2 = preset["meta"]["wsId"]
    show("初始", f"v{preset['meta']['version']} · {preset['meta']['rowCount']} 行")

    r1 = api("POST", f"/api/ws/{w2}/op/rename-column", {"key": "active_power", "label": "有功功率"})
    power_key = r1["newKey"]
    show("重命名", f"v{r1['meta']['version']} · {r1['summary']} · 新 key '{power_key}'")
    check("中文列名按前端同规则折叠成 key", power_key == "____", power_key)
    r2 = api("POST", f"/api/ws/{w2}/op/convert-unit",
             {"key": "irradiance", "factor": 0.001, "offset": 0.0, "newUnit": "kW/m²"})
    show("单位换算", f"v{r2['meta']['version']} · {r2['summary']}")
    r3 = api("POST", f"/api/ws/{w2}/op/derived-column",
             {"name": "出力利用率", "terms": [{"col": power_key, "op": ""}, {"col": "irradiance", "op": "/"}]})
    show("派生列", f"v{r3['meta']['version']} · {r3['summary']}")
    new_cols = [(c["key"], c["label"]) for c in r3["meta"]["columns"]]
    derived_key = r3["meta"]["derivedCols"][0]["key"]
    show("派生列 key", f"'{derived_key}' · 列元数据 {new_cols}")
    check("派生列进入列元数据", any(k == derived_key for k, _ in new_cols), derived_key)
    d_idx = r3["page"]["columns"].index(derived_key)
    i_idx = r3["page"]["columns"].index("irradiance")
    p_idx = r3["page"]["columns"].index(power_key)
    probe = next((i for i, row in enumerate(r3["page"]["rows"])
                  if row[i_idx] and row[p_idx] is not None), None)
    row1 = r3["page"]["rows"][probe]
    sample_derived = row1[d_idx]
    expect_derived = round(row1[p_idx] / row1[i_idx], 4)
    show("派生列抽检", f"第 {probe} 行 {row1[p_idx]} / {row1[i_idx]} = {expect_derived}（保留 4 位，与前端 toFixed(4) 同规则）· 服务端 {sample_derived}")
    check("派生列数值 = 同页两列之商(4 位)", abs(sample_derived - expect_derived) < 1e-9,
          f"偏差 {abs(sample_derived - expect_derived):.2e}")
    r4 = api("POST", f"/api/ws/{w2}/op/time-format", {"format": "YYYY/MM/DD HH:mm"})
    show("时间格式", f"v{r4['meta']['version']} · {r4['meta']['timeFormat']} · 首页首行 {r4['page']['rows'][0][0]}")
    check("渲染斜杠真实存在", str(r4["page"]["rows"][0][0]).count("/") == 2, r4["page"]["rows"][0][0])

    preview = api("GET", f"/api/ws/{w2}/resample-preview?targetMinutes=60")
    show("重采样预演", {k: preview[k] for k in ["currentRows", "projectedRows", "filledBuckets",
                                                "emptyBuckets", "compression"]})
    r5 = api("POST", f"/api/ws/{w2}/op/resample", {"targetMinutes": 60, "method": "mean"})
    show("重采样", f"v{r5['meta']['version']} · {r5['summary']}")
    check("重采样后行数 = 预演投影", r5["meta"]["rowCount"] == preview["projectedRows"],
          f"{r5['meta']['rowCount']} vs {preview['projectedRows']}")
    check("重采样后频率标签更新", r5["meta"]["freqMinutes"] == 60, r5["meta"]["freqLabel"])

    after_unit = api("POST", f"/api/ws/{w2}/op/convert-unit",
                     {"key": power_key, "factor": 0.001, "offset": 0.0, "newUnit": "MW"})
    bp_idx = after_unit["page"]["columns"].index(power_key)
    restored = api("POST", f"/api/ws/{w2}/restore", {"version": 5})
    # 选一行"确实被换算改过"的行做撤销对拍，避免 0 × 0.001 = 0 的假通过
    changed = [i for i, (a, b) in enumerate(zip(after_unit["page"]["rows"], restored["page"]["rows"]))
               if a[bp_idx] != b[bp_idx]]
    check("换算确实改变了页内数值", len(changed) > 0, f"改变行数 {len(changed)}")
    i = changed[0] if changed else 3
    back = restored["page"]["rows"][i]
    row_before = api("GET", f"/api/ws/{w2}/rows?offset={i}&limit=1")["page"]["rows"][0]
    check("回到 v5 后该行逐格一致", back == row_before,
          f"第 {i} 行 v5={back[bp_idx]} · 撤销前={after_unit['page']['rows'][i][bp_idx]}")
    show("撤销后", f"v{restored['meta']['version']} · 命令数 {len(restored['meta']['ops'])}")

    st, pl = call("POST", f"/api/ws/{w2}/op/delete-column", {"key": "timestamp"})
    check("删除时间列被拒绝", st == 400, pl.decode("utf-8")[:90])

    # ---------- 3. 大表：响应体积与行数无关 ----------
    print("\n== 3. 大表 big40.csv（11000 行 × 40 列 = 440000 单元格）==")
    big_path = ROOT / "dataset" / "big40.csv"
    if not big_path.exists():
        raise SystemExit("请先运行 scripts/make_big_csv.py 生成大表")
    big_bytes = big_path.read_bytes()
    show("文件", f"{len(big_bytes)/1024/1024:.2f} MiB")

    body, ctype = multipart("file", "big40.csv", big_bytes)
    t0 = time.perf_counter()
    status, payload = call("POST", "/api/ws", raw=body, content_type=ctype)
    t_create = time.perf_counter() - t0
    check("大表上传返回 200", status == 200, f"HTTP {status}")
    big = json.loads(payload.decode("utf-8"))
    show("建区耗时", f"{t_create*1000:.0f} ms · 响应 {len(payload)/1024:.1f} KiB")
    w3 = big["meta"]["wsId"]
    show("元数据", f"{big['meta']['rowCount']} × {big['meta']['colCount']} · 单元格 {big['meta']['cellCount']}")
    show("服务端内存", f"{big['meta']['memoryBytes']/1024/1024:.2f} MiB")

    sizes = {}
    lat = {}
    for limit in (50, 500):
        t0 = time.perf_counter()
        st, pl = call("GET", f"/api/ws/{w3}/rows?offset=0&limit={limit}")
        lat[limit] = (time.perf_counter() - t0) * 1000
        sizes[limit] = len(pl)
        page = json.loads(pl.decode("utf-8"))["page"]
        show(f"limit={limit}", f"{len(page['rows'])} 行 · {len(pl)/1024:.1f} KiB · {lat[limit]:.0f} ms")
    check("整表从未出现在响应里", sizes[500] < len(big_bytes) / 20,
          f"最大响应 {sizes[500]/1024:.1f} KiB vs 原始表 {len(big_bytes)/1024/1024:.2f} MiB")

    t0 = time.perf_counter()
    st, pl = call("GET", f"/api/ws/{w3}/overview")
    big_ov = json.loads(pl.decode("utf-8"))
    show("总览耗时", f"{(time.perf_counter()-t0)*1000:.0f} ms")
    show("总览", {k: big_ov[k] for k in ["cellCount", "missingCells", "missingRate", "duplicateRows", "memoryBytes"]})
    refbig = pd.read_csv(big_path, encoding="utf-8")
    check("大表缺失数与 pandas 一致", big_ov["missingCells"] == int(refbig.isna().sum().sum()),
          f"{big_ov['missingCells']} vs {int(refbig.isna().sum().sum())}")

    t0 = time.perf_counter()
    rs = api("POST", f"/api/ws/{w3}/op/resample", {"targetMinutes": 60, "method": "mean"})
    show("大表重采样", f"{rs['summary']} · {(time.perf_counter()-t0)*1000:.0f} ms · 响应 {len(json.dumps(rs))/1024:.1f} KiB")
    check("大表重采样行数", rs["meta"]["rowCount"] == 5500, rs["meta"]["rowCount"])

    # ---------- 4. 错误路径 ----------
    print("\n== 4. 错误路径 ==")
    st, pl = call("GET", "/api/ws/deadbeef0000")
    check("未知工作区 404", st == 404, pl.decode("utf-8")[:80])
    st, pl = call("POST", f"/api/ws/{w3}/op/rename-column", {"key": "不存在", "label": "X"})
    check("坏列名 400", st == 400, pl.decode("utf-8")[:80])
    st, pl = call("GET", f"/api/ws/{w3}/rows?limit=99999")
    check("超限分页被 schema 拒绝", st == 422, f"HTTP {st}")

    api("DELETE", f"/api/ws/{w2}")
    api("DELETE", f"/api/ws/{w3}")
    api("DELETE", f"/api/ws/{ws}")
    lst = api("GET", "/api/ws")
    alive = [w["wsId"] for w in lst["items"]]
    show("清理后活跃工作区", f"{lst['count']} 个 · {alive}")
    check("本次测试建立的工作区已全部关闭", not ({w2, w3, ws} & set(alive)), f"剩余 {alive}")

    print("\n== 结果 ==")
    if failures:
        print(f"失败 {len(failures)} 项: {failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

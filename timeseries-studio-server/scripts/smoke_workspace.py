"""workspace 服务的直接冒烟：不走 HTTP，逐条命令验证真实数字。

运行前设 PYTHONIOENCODING=utf-8：
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m scripts.smoke_workspace
"""
from __future__ import annotations
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from app.services import workspace as W  # noqa: E402


def show(label, value):
    print(f"  {label}: {value}")


def main():
    big = Path(__file__).resolve().parents[1] / "dataset" / "machine.csv"
    print("== 预设数据集 pv ==")
    ws = W.create_preset("pv", seed=7)
    m = ws.meta_view()
    show("meta", {k: m[k] for k in ("name", "rowCount", "colCount", "timeCol", "freqMinutes", "freqLabel", "timeFormat", "version")})
    ov = ws.overview()
    show("overview", {k: ov[k] for k in ("missingCells", "missingDenominator", "missingRate", "duplicateRows", "duplicateRate", "unparsedTimes", "timeRange", "cellCount", "memoryBytes")})
    page = ws.rows(0, 3)
    show("rows[0:3]", page["rows"])
    show("columnTypes", [(c["key"], c["type"]) for c in m["columns"]])

    print("\n== 列管理命令 ==")
    r = ws.apply({"kind": "rename_column", "params": {"key": "wind_speed", "label": "瞬时风速"}})
    show("rename", {"newKey": r["newKey"], "cols": [c["key"] for c in r["meta"]["columns"]], "version": r["version"]})
    r = ws.apply({"kind": "convert_unit", "params": {"key": "temperature", "factor": 9 / 5, "offset": 32, "newUnit": "°F"}})
    show("unit °C→°F", {"changed": r["changed"], "label": r["label"], "sample": ws.rows(0, 2)["rows"]})
    r = ws.apply({"kind": "derived_column", "params": {"name": "出力比", "terms": [
        {"col": "active_power", "op": ""}, {"col": "irradiance", "op": "/"}, {"col": "temperature", "op": "+"}]}})
    show("derived", {"key": r["key"], "formula": r["formula"], "validRows": r["validRows"], "nullRows": r["nullRows"]})
    show("derived sample", ws.rows(0, 2)["rows"])
    r = ws.apply({"kind": "set_time_format", "params": {"format": "YYYY/MM/DD HH:mm"}})
    show("time format", {"changed": r["changed"], "format": r["format"], "first": ws.rows(0, 1)["rows"][0]})

    print("\n== 版本重放（撤销） ==")
    v3 = ws.meta_view()["version"]
    target = ws.rows(1200, 2)["rows"]
    snapshot_cols = [c["key"] for c in ws.meta_view()["columns"]]
    ws.apply({"kind": "set_time_format", "params": {"format": "YYYY/MM/DD HH:mm"}})
    ws.apply({"kind": "delete_column", "params": {"key": "weather_type"}})
    show("v5 列", [c["key"] for c in ws.meta_view()["columns"]])
    ws.restore(v3)
    back = ws.meta_view()
    show("restore to", {"version": back["version"], "cols": [c["key"] for c in back["columns"]]})
    show("timeFormat 复位", back["timeFormat"])
    show("行窗口逐格回到 v3", ws.rows(1200, 2)["rows"] == target)
    show("列顺序与 v3 一致", [c["key"] for c in back["columns"]] == snapshot_cols)
    ws.apply({"kind": "delete_column", "params": {"key": "weather_type"}})
    show("重放后再删除列", [c["key"] for c in ws.meta_view()["columns"]])
    try:
        ws.apply({"kind": "delete_column", "params": {"key": "timestamp"}})
    except ValueError as e:
        show("删时间列被拒", str(e))

    print("\n== 重采样 ==")
    pv = W.create_preset("pv", seed=11)
    show("resample 60min 预测", W.resample_preview(pv, 60))
    r = pv.apply({"kind": "resample", "params": {"targetMinutes": 60, "method": "mean"}})
    show("resample", {k: r[k] for k in ("oldCount", "newCount", "filledBuckets", "emptyBuckets", "unparsedDropped", "summary")})
    show("重采样后 meta", {k: pv.meta_view()[k] for k in ("rowCount", "freqMinutes", "freqLabel")})
    show("前 3 行", pv.rows(0, 3)["rows"])
    r2 = pv.apply({"kind": "resample", "params": {"targetMinutes": 1440, "method": "sum"}})
    show("再降到 1440min", {k: r2[k] for k in ("oldCount", "newCount", "emptyBuckets")})
    show("sum 后首行", pv.rows(0, 2)["rows"])
    r3 = pv.apply({"kind": "resample", "params": {"targetMinutes": 60, "method": "interpolate"}})
    show("interpolate", {"newCount": r3["newCount"], "first": pv.rows(0, 2)["rows"]})

    print("\n== 时间格式渲染（对照前端 convertSingleTime 的期望值） ==")
    ts = pd.Series(pd.to_datetime(["2024-06-01 07:05:03", "2024-12-31 00:00:00", None]))
    expect = {
        "YYYY-MM-DD HH:mm:ss": ["2024-06-01 07:05:03", "2024-12-31 00:00:00", None],
        "YYYY-MM-DD HH:mm": ["2024-06-01 07:05", "2024-12-31 00:00", None],
        "YYYY-MM-DD": ["2024-06-01", "2024-12-31", None],
        "YYYY/MM/DD HH:mm": ["2024/06/01 07:05", "2024/12/31 00:00", None],
        "YYYY/MM/DD": ["2024/06/01", "2024/12/31", None],
        "YYYY-MM-DDTHH:mm:ss": ["2024-06-01T07:05:03", "2024-12-31T00:00:00", None],
        "YYYYMMDDHHmmss": ["20240601070503", "20241231000000", None],
        "YYYYMMDD": ["20240601", "20241231", None],
    }
    for f, want in expect.items():
        got = W.render_times(ts, f)
        show(f"{f}", {"结果": got, "与前端一致": got == want})
    epoch = W.render_times(ts, "epoch_s")
    show("epoch_s", {"结果": epoch, "对照 datetime.timestamp": [datetime(2024, 6, 1, 7, 5, 3).timestamp(), datetime(2024, 12, 31, 0, 0, 0).timestamp()]})
    back_ms = W.parse_time_column(pd.Series([str(v) for v in W.render_times(ts, "epoch_ms")]), "epoch_ms")
    show("epoch_ms 往返一致", [str(a)[:19] == str(b)[:19] for a, b in zip(back_ms, ts.dropna())])
    show("自定义格式", W.render_times(ts, "YYYY年MM月DD日 HH:mm"))

    print("\n== 真实导入文件 ==")
    if big.exists():
        content = big.read_bytes()
        wi = W.create_from_bytes(content, big.name)
        mi = wi.meta_view()
        show("meta", {k: mi[k] for k in ("name", "rowCount", "colCount", "timeCol", "freqMinutes", "timeFormat")})
        show("timeDetect", mi["timeDetect"])
        show("overview", {k: wi.overview()[k] for k in ("missingRate", "duplicateRate", "cellCount", "memoryBytes", "unparsedTimes")})
        show("首行前两列", wi.rows(0, 1)["rows"][0][:3])
        df_raw = pd.read_csv(io_bytes(content), encoding="utf-8-sig")
        show("pandas 直读行数/列数", (df_raw.shape[0], df_raw.shape[1]))
        show("pandas 直读总缺失", int(df_raw.isna().sum().sum()))
        show("后端总缺失(同口径)", int(wi.base_df.isna().sum().sum()))
        print("  列类型分布:", json.dumps({t: sum(1 for c in mi["columns"] if c["type"] == t) for t in ("float", "category", "datetime")}))
    else:
        show("跳过", f"{big} 不存在")


def io_bytes(content):
    import io as _io
    return _io.BytesIO(content)


if __name__ == "__main__":
    main()

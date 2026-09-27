"""生成大表压测样本：11000 行 × 40 列（1 个时间列 + 39 个数值列），落在 dataset/big40.csv。

用于第①期验收：证明明细真的留在服务端 —— 浏览器端任何一次响应的字节数都与行数无关。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 11000
COLS = int(sys.argv[2]) if len(sys.argv) > 2 else 40
OUT = Path(__file__).resolve().parents[1] / "dataset" / f"big{COLS}.csv"


def build() -> pd.DataFrame:
    rng = np.random.default_rng(20260924)
    ts = pd.date_range("2024-01-01 00:00:00", periods=ROWS, freq="30min")
    frame = {"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S")}
    for i in range(1, COLS):
        base = rng.normal(50.0 + i, 6.0, ROWS)
        values = np.round(base + 3.0 * np.sin(np.arange(ROWS) / (12 + i)), 3)
        # 每列埋 0.3% 缺失，保证缺失率统计非零
        miss = rng.choice(ROWS, size=max(1, ROWS // 300), replace=False)
        values[miss] = np.nan
        frame[f"传感器{i:02d}"] = values
    return pd.DataFrame(frame)


if __name__ == "__main__":
    df = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8")
    print(f"写出 {OUT}")
    print(f"形状 {df.shape[0]} 行 × {df.shape[1]} 列 · 单元格 {df.shape[0]*df.shape[1]}")
    print(f"缺失 {int(df.isna().sum().sum())} · 文件 {OUT.stat().st_size/1024/1024:.2f} MiB")

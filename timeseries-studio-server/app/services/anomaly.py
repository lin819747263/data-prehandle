"""异常检测：scikit-learn 完整孤立森林（IsolationForest）。

前端浏览器版是近似实现，此处用真实 sklearn 模型逐列拟合，
返回异常点索引、原始分数，以及由正常点分位数推出的上下界，
供前端复用既有的散点标注与「截断修复」链路。
"""
from __future__ import annotations
import math
from typing import Any

import numpy as np
from sklearn.ensemble import IsolationForest


def _clean(values: list[Any]) -> tuple[np.ndarray, list[int]]:
    """把一列转成 float 数组，返回 (数组, 有效值对应的原始行索引)。缺失值剔除。"""
    arr: list[float] = []
    idxs: list[int] = []
    for i, v in enumerate(values):
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        arr.append(f)
        idxs.append(i)
    return np.asarray(arr, dtype=float), idxs


def detect_iforest(payload: dict) -> dict:
    columns = payload["columns"]
    n_estimators = int(payload.get("n_estimators", 200))
    contamination = payload.get("contamination", "auto")
    if isinstance(contamination, str) and contamination.lower() != "auto":
        try:
            contamination = float(contamination)
        except ValueError:
            contamination = "auto"
    elif contamination == "auto":
        contamination = "auto"
    else:
        contamination = float(contamination)
    max_samples = payload.get("max_samples")
    random_state = int(payload.get("random_state", 42))
    lower_q = float(payload.get("normal_lower_q", 0.005))
    upper_q = float(payload.get("normal_upper_q", 0.995))

    per_column: dict[str, dict] = {}
    total_anomalies = 0
    total_points = 0

    for col in columns:
        key = col["key"]
        values = col["values"]
        arr, valid_idx = _clean(values)
        n = arr.size
        total_points += n
        if n < 3:
            per_column[key] = {
                "anomalyIndices": [], "scores": [None] * len(values),
                "count": 0, "validCount": n, "lower": None, "upper": None,
            }
            continue

        model = IsolationForest(
            n_estimators=n_estimators,
            contamination=contamination,
            max_samples=max_samples if max_samples and max_samples <= n else "auto",
            random_state=random_state,
            bootstrap=False,
        )
        X = arr.reshape(-1, 1)
        pred = model.fit_predict(X)                 # -1 = 异常, 1 = 正常
        scores = model.score_samples(X)             # 越小越异常

        anomaly_positions = np.where(pred == -1)[0]
        anomaly_indices = [int(valid_idx[p]) for p in anomaly_positions]
        total_anomalies += len(anomaly_indices)

        normal_vals = arr[pred == 1]
        if normal_vals.size > 0:
            lower = float(np.quantile(normal_vals, lower_q))
            upper = float(np.quantile(normal_vals, upper_q))
        else:
            lower = float(arr.min())
            upper = float(arr.max())

        # 把分数按原始行索引回填（缺失/无效行为 None）
        full_scores: list[Any] = [None] * len(values)
        for pos, orig in enumerate(valid_idx):
            full_scores[orig] = round(float(scores[pos]), 6)

        per_column[key] = {
            "anomalyIndices": anomaly_indices,
            "scores": full_scores,
            "count": len(anomaly_indices),
            "validCount": int(n),
            "lower": round(lower, 4),
            "upper": round(upper, 4),
        }

    overall_rate = round(total_anomalies / total_points * 100, 2) if total_points else 0.0
    return {
        "engine": "sklearn.IsolationForest",
        "params": {
            "n_estimators": n_estimators,
            "contamination": contamination,
            "max_samples": max_samples or "auto",
            "random_state": random_state,
        },
        "perColumn": per_column,
        "summary": {
            "totalAnomalies": total_anomalies,
            "totalPoints": total_points,
            "overallRate": overall_rate,
        },
    }

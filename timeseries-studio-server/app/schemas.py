"""Pydantic 请求/响应模型（无状态：前端始终随请求携带数据）。"""
from __future__ import annotations
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


class ExportRequest(BaseModel):
    format: str = Field(..., pattern="^(parquet|feather|csv|xlsx)$")
    columns: list[str]
    rows: list[dict[str, Any]]
    filename: Optional[str] = None


class IForestColumn(BaseModel):
    key: str
    values: list[Optional[float]]


class IForestRequest(BaseModel):
    columns: list[IForestColumn]
    n_estimators: int = Field(200, ge=1, le=2000)
    contamination: Union[float, Literal["auto"]] = Field("auto", description="'auto' 或 (0, 0.5] 的浮点数")
    max_samples: Optional[int] = Field(None, ge=2)
    random_state: int = Field(42, ge=0)
    normal_lower_q: float = Field(0.005, gt=0, lt=0.5)
    normal_upper_q: float = Field(0.995, gt=0.5, lt=1)

    @field_validator("contamination")
    @classmethod
    def _check_contamination(cls, v):
        if isinstance(v, float) and not (0 < v <= 0.5):
            raise ValueError("contamination 需为 'auto' 或 (0, 0.5] 的浮点数")
        return v

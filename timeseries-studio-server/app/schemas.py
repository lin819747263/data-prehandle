"""Pydantic 请求模型。

工作区类请求（/api/ws/...）只带命令与参数：明细行始终留在服务端工作区，
浏览器既不整表上传、也不整表下载。
"""
from __future__ import annotations
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 会话的几条硬上限放在这里，/api/session 的守卫与 /api/health 的 limits 共用同一份数字
MAX_SESSION_WORKSPACES = 8
MAX_SESSION_ACTION_LOG = 2000
MAX_SESSION_BYTES = 2 * 1024 * 1024
# 审计记录里出现这么长的数组，就说明有人把整列数据塞进了会话（第③期之前的老毛病）
MAX_SESSION_INLINE_ARRAY = 200
# 上传入口（主表 / 侧表）共用这一份大小上限，两处报的是同一句话里的同一个数字
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)


class ExportRequest(BaseModel):
    format: str = Field(..., pattern="^(parquet|feather|csv|xlsx)$")
    columns: list[str]
    rows: list[dict[str, Any]]
    filename: Optional[str] = None


class SaveAsRequest(BaseModel):
    """另存为数据集：把服务端工作区当前帧写进数据集目录。

    文件名留空则由后端用「工作区名 + 版本号」起名；重名时 dataset_store 追加时间戳，
    绝不覆盖已有文件。
    """
    filename: Optional[str] = Field(None, description="不含扩展名的文件名，留空用工作区名 + 版本号")


# ---------------- 第四步：清洗与异常 ----------------

class ImputeTarget(BaseModel):
    key: str
    startIdx: int = Field(..., ge=0)
    endIdx: int = Field(..., ge=0)
    algo: Literal["linear", "ffill", "spline", "zero"] = "linear"


class ImputeRequest(BaseModel):
    targets: list[ImputeTarget] = Field(default_factory=list)
    dedupe: Optional[Literal["mean", "first", "last"]] = None
    # all=true 时由服务端自己扫描缺失段（缺失段超过 /quality 上限的大表只能走这条）
    all: bool = Field(False, description="true=服务端扫描全表逐段填补，忽略 targets")
    keys: Optional[list[str]] = Field(None, description="all=true 时限定列，缺省为全部数值列")
    defaultAlgo: Literal["linear", "ffill", "spline", "zero"] = "linear"
    algos: dict[str, Literal["linear", "ffill", "spline", "zero"]] = Field(
        default_factory=dict, description="all=true 时的按列算法覆盖")


class AnomalyDetectRequest(BaseModel):
    algo: Literal["3sigma", "iqr", "iforest", "iforest_sklearn", "expr"]
    expr: Optional[str] = Field(None, description="algo=expr 时的判定表达式")
    nEstimators: int = Field(200, ge=1, le=2000)
    contamination: Union[float, Literal["auto"]] = Field("auto", description="'auto' 或 (0, 0.5] 的浮点数")
    randomState: int = Field(42, ge=0)
    normalLowerQ: float = Field(0.005, gt=0, lt=0.5)
    normalUpperQ: float = Field(0.995, gt=0.5, lt=1)

    @field_validator("contamination")
    @classmethod
    def _check_contamination(cls, v):
        if isinstance(v, float) and not (0 < v <= 0.5):
            raise ValueError("contamination 需为 'auto' 或 (0, 0.5] 的浮点数")
        return v


class AnomalyRepairRequest(BaseModel):
    repair: Literal["clip", "nan_impute", "mask_only"]


class MaskGenerateRequest(BaseModel):
    maskName: str = Field(..., min_length=1)
    startIdx: int = Field(..., ge=0)
    endIdx: int = Field(..., ge=0)


class MaskDeleteRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1)


# ---------------- 第五步：特征构建 ----------------

class FeatureTimeRequest(BaseModel):
    dims: list[str] = Field(..., min_length=1, description="hour/day/month/weekday/is_weekend/holiday")
    cyclical: bool = Field(False, description="兼容拆分前的旧命令：true = 对全部已勾选的周期维度做正余弦编码")
    cycDims: list[str] = Field(default_factory=list,
                               description="本次改用正余弦编码的周期维度（hour/weekday/month）")
    keepCycOriginal: bool = Field(True, description="被编码的维度是否同时保留数值列；false 表示只留 sin/cos")
    # 节假日表不在这里传：feature_time 一律读工作区当前生效的那份（GET /holidays 同一份），
    # 界面上看到的日期、真算出来的列、导出脚本复现的集合因此只会是一个。


class FeatureLagRequest(BaseModel):
    cols: list[str] = Field(..., min_length=1)
    lags: list[int] = Field(default_factory=list)
    windows: list[int] = Field(default_factory=list)
    stats: list[str] = Field(default_factory=list, description="mean/std/max/min/median")
    expanding: bool = False
    ewm: bool = False
    ewmSpan: int = Field(12, ge=2, le=500)
    group: Optional[Literal["lag", "window"]] = Field(
        None, description="lag 只生成滞后 / window 只生成滑动窗口；缺省两类一起（拆分前的旧命令）")


class FeatureDiffRequest(BaseModel):
    cols: list[str] = Field(..., min_length=1)
    d1: bool = True
    d2: bool = False
    seasonal: bool = False
    period: int = Field(24, ge=1, le=5000)
    fftDominant: bool = False
    fftEntropy: bool = False
    fftPowerRatio: bool = False
    group: Optional[Literal["diff", "fft"]] = Field(
        None, description="diff 只生成差分 / fft 只生成频域；缺省两类一起（拆分前的旧命令）")


class FeatureCatRequest(BaseModel):
    cols: list[str] = Field(..., min_length=1)
    method: Literal["onehot", "ordinal", "target"] = "onehot"
    targetColumn: Optional[str] = Field(None, description="目标均值编码的参照数值列，缺省由服务端挑主列")


class SplitApplyRequest(BaseModel):
    """切分落成真实列：比例与界面滑杆同一档（50~85），验证/测试平分剩余。"""
    ratio: int = Field(70, ge=50, le=85)
    key: Optional[str] = Field(None, description="划分列名，缺省 dataset_split")
    label: Optional[str] = Field(None, description="划分列显示名，缺省 数据集划分")


class HolidaysRequest(BaseModel):
    """节假日表配置：整份替换当前工作区的生效日期集合（空列表 = 不认任何节假日）。"""
    days: list[str] = Field(..., max_length=200)
    source: Optional[str] = Field(None, description="界面标注来源用，如 preset-2024 / custom")


# ---------------- 工作区 ----------------

class PresetRequest(BaseModel):
    key: str = Field(..., pattern="^(pv|load)$")
    seed: Optional[int] = Field(None, ge=0, description="同 seed 必然得到同一张表")


class DatasetWorkspaceRequest(BaseModel):
    filename: str


class RenameColumnRequest(BaseModel):
    key: str
    label: str = Field(..., min_length=1)
    newKey: Optional[str] = Field(None, description="显式指定新列键；与 key 相同表示只改显示名")


class DeleteColumnRequest(BaseModel):
    key: str


class ConvertUnitRequest(BaseModel):
    key: str
    factor: float
    offset: float = 0.0
    newUnit: str = Field(..., min_length=1)


class DerivedTerm(BaseModel):
    col: str
    op: str = ""


class DerivedColumnRequest(BaseModel):
    name: str = Field(..., min_length=1)
    terms: list[DerivedTerm] = Field(..., min_length=2)


class ExoPresetRequest(BaseModel):
    presetKey: str = Field(..., min_length=1)
    key: Optional[str] = None
    label: Optional[str] = None
    seed: Optional[int] = Field(None, ge=0)


class ExoFormulaRequest(BaseModel):
    key: str = Field(..., min_length=1)
    expr: str = Field(..., min_length=1)
    label: Optional[str] = None


class ExoTarget(BaseModel):
    """侧表一列 → 主表一列：from 用侧表原始表头，key 用主表列名。"""
    from_: str = Field(..., alias="from")
    key: str = Field(..., min_length=1)
    label: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class ExoFileRequest(BaseModel):
    """提交侧表挂列。文件在 /api/exo/inspect 那一步就已经落盘，这里只引用名字 + 内容指纹，
    几十 MB 的侧表不必上传第二遍；sha 会写进命令日志，重放时对不上就报错。
    """
    filename: str = Field(..., min_length=1)
    sha: str = Field(..., min_length=1)
    sideTimeCol: str = Field(..., min_length=1)
    mode: Literal["left", "nearest"] = "left"
    toleranceMinutes: Optional[int] = Field(None, ge=1, le=1440)
    targets: list[ExoTarget] = Field(default_factory=list)


class TimeFormatRequest(BaseModel):
    format: str = Field(..., min_length=1)
    customFormat: Optional[str] = None


class ResampleRequest(BaseModel):
    targetMinutes: int = Field(..., ge=1, le=1440)
    method: Literal["mean", "sum", "first", "interpolate"] = "mean"


class RestoreRequest(BaseModel):
    version: int = Field(..., ge=0)


# ---------------- 第五步：会话落服务端 ----------------

class SessionWorkspace(BaseModel):
    """会话引用的一个工作区槽位。明细、页窗口、历史都不在这里，
    服务端按 wsId 现读命令日志就能重建，版本号只当"上次离开时我以为它是几"的线索。"""
    key: str = Field(..., min_length=1)
    wsId: str = Field(..., min_length=1)
    version: Optional[int] = Field(None, ge=0)


class SessionRequest(BaseModel):
    """上一次会话停在哪儿：UI 层状态 + 它引用了哪些工作区。

    服务端只**读** step / currentKey / workspaces 这几项（它们要参与核对），
    其余字段（审计记录、特征勾选、掩码配置……）是原样存取的前端状态。
    所以 extra 允许：前端加一个 UI 字段不该 require 后端改动，
    但体积与"不许携带整列数据"由 /api/session 的守卫把关。
    """
    model_config = ConfigDict(extra="allow")

    step: int = Field(..., ge=1, le=5)
    currentKey: str = ""
    splitRatio: float = Field(0.8, ge=0.0, le=1.0)
    workspaces: list[SessionWorkspace] = Field(default_factory=list, max_length=MAX_SESSION_WORKSPACES)
    actionLog: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    derivedCols: list[dict[str, Any]] = Field(default_factory=list)
    masks: list[dict[str, Any]] = Field(default_factory=list)
    imputeSegAlgos: dict[str, Any] = Field(default_factory=dict)
    # 其余纯 UI 状态（数据集名称/格式、页窗口位置、第五步的勾选态……）：服务端不参与理解，
    # 只在 GET 时原样回吐。外生变量列与特征列不在这里——它们是工作区的真实列，
    # 由命令日志重放出来，会话记一份反而会和服务端的帧对不上。
    ui: dict[str, Any] = Field(default_factory=dict)

"""Request/response contracts.  Kept explicit so Swagger is self-describing."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import domain

MAX_TEXT = 200


class MeasurementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measurement_no: str = Field(
        ..., min_length=1, max_length=MAX_TEXT, description="测量号（批内唯一）"
    )
    revision: int = Field(
        ..., ge=1, description="递增修订号；同一测量点每更正一次必须 +1"
    )
    value_mg_l: float = Field(..., ge=0, description="非负氨氮读数（mg/L）")

    @field_validator("measurement_no")
    @classmethod
    def _no_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("measurement_no 不能为空")
        return value

    @field_validator("value_mg_l")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value_mg_l 必须是有限数值")
        return value


class SampleInput(MeasurementInput):
    sample_no: int = Field(..., ge=1, le=100, description="样品序号 1～100")


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(None, max_length=MAX_TEXT, description="批次标签")
    samples: list[SampleInput] = Field(
        ..., min_length=1, max_length=domain.MAX_SAMPLES, description="1～100 个样品"
    )
    blank: MeasurementInput | None = Field(
        None, description="空白测量；未提供时批次可暂存但不可发布"
    )
    spike: MeasurementInput | None = Field(
        None, description="加标空白测量（已知加入 10 mg/L）；未提供时不可发布"
    )

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _unique_points(self) -> BatchCreate:
        sample_nos = [sample.sample_no for sample in self.samples]
        if len(set(sample_nos)) != len(sample_nos):
            raise ValueError("samples 中存在重复的 sample_no")

        measurement_nos = [sample.measurement_no for sample in self.samples]
        if self.blank is not None:
            measurement_nos.append(self.blank.measurement_no)
        if self.spike is not None:
            measurement_nos.append(self.spike.measurement_no)
        if len(set(measurement_nos)) != len(measurement_nos):
            raise ValueError("measurement_no 必须在一个批次内唯一")

        first_revisions = [sample.revision for sample in self.samples]
        for extra in (self.blank, self.spike):
            if extra is not None:
                first_revisions.append(extra.revision)
        if any(revision != 1 for revision in first_revisions):
            raise ValueError("创建批次时每个测量点的首次记录必须是 revision=1")
        return self


class MeasurementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sample", "blank", "spike"]
    sample_no: int | None = Field(
        None, ge=1, le=100, description="kind=sample 时必填，其余必须为空"
    )
    measurement_no: str = Field(..., min_length=1, max_length=MAX_TEXT)
    revision: int = Field(..., ge=1)
    value_mg_l: float = Field(..., ge=0)

    @field_validator("measurement_no")
    @classmethod
    def _no_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("measurement_no 不能为空")
        return value

    @field_validator("value_mg_l")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value_mg_l 必须是有限数值")
        return value

    @model_validator(mode="after")
    def _kind_matches_sample_no(self) -> MeasurementCreate:
        if self.kind == "sample" and self.sample_no is None:
            raise ValueError("kind=sample 时必须提供 sample_no")
        if self.kind != "sample" and self.sample_no is not None:
            raise ValueError(f"kind={self.kind} 时不得提供 sample_no")
        return self


class MeasurementView(BaseModel):
    kind: str
    sample_no: int | None
    measurement_no: str
    revision: int
    value_mg_l: float
    created_at: str


class MeasurementRecordResponse(BaseModel):
    batch_id: int
    duplicate: bool = Field(..., description="同版同值重送时为 true，不产生新记录")
    expired_publication_id: int | None = Field(
        None, description="本次新增/更正导致过期的原当前报告"
    )
    current_publication_id: int | None = Field(
        None, description="批次当前有效报告；新增/更正后通常为 null"
    )
    measurement: MeasurementView


class QCBlank(BaseModel):
    measurement_no: str
    revision: int
    value_mg_l: float
    limit_mg_l: float
    passes: bool


class QCSpike(BaseModel):
    measurement_no: str
    revision: int
    value_mg_l: float
    added_mg_l: float
    blank_corrected_mg_l: float | None
    recovery_percent: float | None
    min_percent: float
    max_percent: float
    passes: bool | None


class QCReason(BaseModel):
    code: str
    message: str


class QCEvaluation(BaseModel):
    complete: bool
    passes: bool
    sample_count: int
    blank: QCBlank | None
    spike: QCSpike | None
    reasons: list[QCReason]


class PublicationSummary(BaseModel):
    publication_id: int
    batch_id: int
    batch_label: str | None
    status: Literal["current", "expired"]
    published_at: str
    expired_at: str | None
    sample_count: int
    blank_value_mg_l: float
    spike_value_mg_l: float
    recovery_percent: float


class ReportSample(BaseModel):
    sample_no: int
    measurement_no: str
    source_revision: int
    source_value_mg_l: float
    blank_value_mg_l: float
    concentration_mg_l: float | None = Field(
        None, description="扣空白后浓度；样品低于空白时为 null"
    )
    reason: str | None = Field(None, description="留空原因；正常时为 null")
    note: str | None


class PublicationReport(BaseModel):
    publication_id: int
    batch_id: int
    batch_label: str | None
    status: Literal["current", "expired"]
    published_at: str
    expired_at: str | None
    sample_count: int
    qc: QCEvaluation
    samples: list[ReportSample]


class PublishResponse(BaseModel):
    already_published: bool = Field(
        ..., description="当前修订集合已发布且一致时为 true，不新建报告"
    )
    expired_publication_id: int | None
    publication: PublicationReport


class BatchSummary(BaseModel):
    batch_id: int
    batch_label: str | None
    created_at: str
    sample_count: int
    measurement_count: int
    qc: QCEvaluation
    current_publication: PublicationSummary | None
    expired_publication_count: int


class BatchSampleView(MeasurementView):
    sample_no: int


class BatchDetail(BatchSummary):
    samples: list[BatchSampleView]
    blank: MeasurementView | None
    spike: MeasurementView | None


class ErrorResponse(BaseModel):
    detail: dict[str, Any]

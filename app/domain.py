"""Quality-control arithmetic for ammonia-nitrogen batches.

The rules are intentionally pure and Decimal-based so that the acceptance
limits (0.2 mg/L blank, 80-120 % recovery) are evaluated exactly at their
boundaries rather than through binary floating-point noise.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BLANK_LIMIT_MG_L = Decimal("0.2")
SPIKE_ADDED_MG_L = Decimal("10")
RECOVERY_MIN_PERCENT = Decimal("80")
RECOVERY_MAX_PERCENT = Decimal("120")
MAX_SAMPLES = 100
MIN_SAMPLES = 1

SIX_PLACES = Decimal("0.000001")


def dec(value: float | int | str) -> Decimal:
    """Convert a stored float to a Decimal using its shortest repr."""

    return Decimal(str(value))


def quantize(value: Decimal) -> float:
    """Quantise a computed concentration/recovery for storage and display."""

    return float(value.quantize(SIX_PLACES, rounding=ROUND_HALF_UP))


def format_value(value: Decimal | float) -> str:
    text = format(dec(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def evaluate_qc(
    blank: dict[str, Any] | None,
    spike: dict[str, Any] | None,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a JSON-ready QC evaluation for the current measurements."""

    reasons: list[dict[str, str]] = []

    blank_payload: dict[str, Any] | None = None
    blank_value: Decimal | None = None
    blank_passes: bool | None = None

    if blank is None:
        reasons.append(
            {
                "code": "QC_BLANK_MISSING",
                "message": "缺少空白测量，无法计算扣空白结果。",
            }
        )
    else:
        blank_value = dec(blank["value_mg_l"])
        blank_passes = blank_value <= BLANK_LIMIT_MG_L
        blank_payload = {
            "measurement_no": blank["measurement_no"],
            "revision": blank["revision"],
            "value_mg_l": blank["value_mg_l"],
            "limit_mg_l": float(BLANK_LIMIT_MG_L),
            "passes": blank_passes,
        }
        if not blank_passes:
            reasons.append(
                {
                    "code": "QC_BLANK_ABOVE_LIMIT",
                    "message": (
                        f"空白读数 {format_value(blank_value)} mg/L 超过 "
                        f"{format_value(BLANK_LIMIT_MG_L)} mg/L 限值。"
                    ),
                }
            )

    spike_payload: dict[str, Any] | None = None
    if spike is None:
        reasons.append(
            {
                "code": "QC_SPIKE_MISSING",
                "message": "缺少加标空白测量，无法计算回收率。",
            }
        )
    else:
        spike_value = dec(spike["value_mg_l"])
        corrected: Decimal | None = None
        recovery: Decimal | None = None
        spike_passes: bool | None = None
        if blank_value is not None:
            corrected = spike_value - blank_value
            # Judge the same quantised value that is stored and reported, so a
            # displayed "120.0 %" can never be paired with passes=false.
            recovery = (corrected / SPIKE_ADDED_MG_L * Decimal("100")).quantize(
                SIX_PLACES, rounding=ROUND_HALF_UP
            )
            spike_passes = RECOVERY_MIN_PERCENT <= recovery <= RECOVERY_MAX_PERCENT
        spike_payload = {
            "measurement_no": spike["measurement_no"],
            "revision": spike["revision"],
            "value_mg_l": spike["value_mg_l"],
            "added_mg_l": float(SPIKE_ADDED_MG_L),
            "blank_corrected_mg_l": quantize(corrected)
            if corrected is not None
            else None,
            "recovery_percent": quantize(recovery) if recovery is not None else None,
            "min_percent": float(RECOVERY_MIN_PERCENT),
            "max_percent": float(RECOVERY_MAX_PERCENT),
            "passes": spike_passes,
        }
        if recovery is not None and not spike_passes:
            reasons.append(
                {
                    "code": "QC_RECOVERY_OUT_OF_RANGE",
                    "message": (
                        f"扣空白后的加标回收率 {format_value(quantize(recovery))}% "
                        f"不在 {format_value(RECOVERY_MIN_PERCENT)}%～"
                        f"{format_value(RECOVERY_MAX_PERCENT)}% 范围内。"
                    ),
                }
            )

    sample_count = len(samples)
    if not MIN_SAMPLES <= sample_count <= MAX_SAMPLES:
        reasons.append(
            {
                "code": "QC_SAMPLE_COUNT_INVALID",
                "message": (
                    f"样品数量为 {sample_count}，必须在 {MIN_SAMPLES}～{MAX_SAMPLES} 之间。"
                ),
            }
        )

    complete = (
        blank is not None
        and spike is not None
        and MIN_SAMPLES <= sample_count <= MAX_SAMPLES
    )
    passes = complete and not reasons

    return {
        "complete": complete,
        "sample_count": sample_count,
        "blank": blank_payload,
        "spike": spike_payload,
        "passes": passes,
        "reasons": reasons,
    }

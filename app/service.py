"""Batch, measurement, QC and publication service layer.

Every state-changing function takes an already-open connection and performs no
transaction control of its own: callers wrap the whole operation in
``app.db.transaction``.  That way a measurement write and the expiry of the
affected publication commit or roll back together, and a publication write
either stores the complete report or stores nothing.

Concurrency model: every write begins with ``BEGIN IMMEDIATE`` (SQLite's
database-wide write lock).  A publication therefore reads exactly the same
revision set that a concurrent correction sees, and the two orders are:

* correction commits first -> publication cites the corrected revision;
* publication commits first -> the correction immediately expires it.

There is no interleaving in which a current report cites an old revision.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app import domain
from app.db import transaction
from app.errors import ApiError

SAMPLE_LIMIT_MESSAGE = (
    f"每批样品数必须在 {domain.MIN_SAMPLES}～{domain.MAX_SAMPLES} 之间。"
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _fetch_batch(conn: Any, batch_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ApiError(
            404, "BATCH_NOT_FOUND", f"批次 {batch_id} 不存在。", batch_id=batch_id
        )
    return dict(row)


def _measurement_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "sample_no": row["sample_no"],
        "measurement_no": row["measurement_no"],
        "revision": row["revision"],
        "value_mg_l": row["value_mg_l"],
        "created_at": row["created_at"],
    }


def _point_key(row_or_kind: Any, sample_no: Any = None) -> tuple[str, int | None]:
    if isinstance(row_or_kind, dict):
        return (row_or_kind["kind"], row_or_kind["sample_no"])
    return (row_or_kind, sample_no)


def _insert_measurement(
    conn: Any,
    *,
    batch_id: int,
    kind: str,
    sample_no: int | None,
    measurement_no: str,
    revision: int,
    value_mg_l: float,
    created_at: str,
) -> dict[str, Any]:
    """Insert one measurement revision.  Isolated for failure-injection tests."""

    cursor = conn.execute(
        """
        INSERT INTO measurements
            (batch_id, measurement_no, kind, sample_no, revision, value_mg_l, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, measurement_no, kind, sample_no, revision, value_mg_l, created_at),
    )
    row = conn.execute(
        """
        SELECT * FROM measurements
        WHERE batch_id = ? AND measurement_no = ? AND revision = ?
        """,
        (batch_id, measurement_no, revision),
    ).fetchone()
    assert row is not None, "measurement insert did not persist"
    assert cursor.lastrowid is not None
    return dict(row)


def _insert_publication_items(
    conn: Any, publication_id: int, items: list[dict[str, Any]]
) -> None:
    """Insert report line items.  Isolated for failure-injection tests."""

    for item in items:
        conn.execute(
            """
            INSERT INTO publication_items
                (publication_id, sample_no, measurement_no, source_revision,
                 source_value_mg_l, concentration_mg_l, reason, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                item["sample_no"],
                item["measurement_no"],
                item["source_revision"],
                item["source_value_mg_l"],
                item["concentration_mg_l"],
                item["reason"],
                item["note"],
            ),
        )


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def all_measurements(conn: Any, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM measurements
        WHERE batch_id = ?
        ORDER BY kind, sample_no IS NULL, sample_no, measurement_no, revision
        """,
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def current_measurements(conn: Any, batch_id: int) -> dict[str, Any]:
    """Current (highest-revision) measurement for each point."""

    by_point: dict[tuple[str, int | None], dict[str, Any]] = {}
    for row in all_measurements(conn, batch_id):
        key = _point_key(row)
        existing = by_point.get(key)
        if existing is None or row["revision"] > existing["revision"]:
            by_point[key] = row

    samples = [row for key, row in by_point.items() if key[0] == "sample"]
    samples.sort(key=lambda row: row["sample_no"])
    return {
        "samples": samples,
        "blank": next(
            (row for key, row in by_point.items() if key[0] == "blank"), None
        ),
        "spike": next(
            (row for key, row in by_point.items() if key[0] == "spike"), None
        ),
    }


def qc_for_batch(conn: Any, batch_id: int) -> dict[str, Any]:
    _fetch_batch(conn, batch_id)
    measurements = current_measurements(conn, batch_id)
    return domain.evaluate_qc(
        measurements["blank"], measurements["spike"], measurements["samples"]
    )


def _current_publication_row(conn: Any, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM publications
        WHERE batch_id = ? AND status = 'current'
        """,
        (batch_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _publication_row(conn: Any, publication_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM publications WHERE id = ?", (publication_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "PUBLICATION_NOT_FOUND",
            f"发布报告 {publication_id} 不存在。",
            publication_id=publication_id,
        )
    return dict(row)


def _publication_items(conn: Any, publication_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM publication_items
        WHERE publication_id = ?
        ORDER BY sample_no
        """,
        (publication_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _expire_current_publication(conn: Any, batch_id: int) -> int | None:
    rows = conn.execute(
        """
        SELECT id FROM publications
        WHERE batch_id = ? AND status = 'current'
        """,
        (batch_id,),
    ).fetchall()
    if not rows:
        return None
    expired_at = utc_now()
    for row in rows:
        conn.execute(
            """
            UPDATE publications
            SET status = 'expired', expired_at = ?
            WHERE id = ?
            """,
            (expired_at, row["id"]),
        )
    return int(rows[0]["id"])


def _publication_summary(conn: Any, publication: dict[str, Any]) -> dict[str, Any]:
    batch = _fetch_batch(conn, publication["batch_id"])
    return {
        "publication_id": publication["id"],
        "batch_id": publication["batch_id"],
        "batch_label": batch["label"],
        "status": publication["status"],
        "published_at": publication["published_at"],
        "expired_at": publication["expired_at"],
        "sample_count": publication["sample_count"],
        "blank_value_mg_l": publication["blank_value_mg_l"],
        "spike_value_mg_l": publication["spike_value_mg_l"],
        "recovery_percent": publication["recovery_percent"],
    }


def publication_report(conn: Any, publication_id: int) -> dict[str, Any]:
    publication = _publication_row(conn, publication_id)
    items = _publication_items(conn, publication_id)
    batch = _fetch_batch(conn, publication["batch_id"])

    reasons: list[dict[str, str]] = []
    if publication["blank_value_mg_l"] > float(domain.BLANK_LIMIT_MG_L):
        reasons.append(
            {
                "code": "QC_BLANK_ABOVE_LIMIT",
                "message": "空白超过限值。",
            }
        )
    if not (
        domain.RECOVERY_MIN_PERCENT
        <= domain.dec(publication["recovery_percent"])
        <= domain.RECOVERY_MAX_PERCENT
    ):
        reasons.append(
            {
                "code": "QC_RECOVERY_OUT_OF_RANGE",
                "message": "回收率超出范围。",
            }
        )

    return {
        "publication_id": publication["id"],
        "batch_id": publication["batch_id"],
        "batch_label": batch["label"],
        "status": publication["status"],
        "published_at": publication["published_at"],
        "expired_at": publication["expired_at"],
        "sample_count": publication["sample_count"],
        "qc": {
            "complete": True,
            "passes": not reasons,
            "sample_count": publication["sample_count"],
            "blank": {
                "measurement_no": publication["blank_measurement_no"],
                "revision": publication["blank_revision"],
                "value_mg_l": publication["blank_value_mg_l"],
                "limit_mg_l": float(domain.BLANK_LIMIT_MG_L),
                "passes": publication["blank_value_mg_l"]
                <= float(domain.BLANK_LIMIT_MG_L),
            },
            "spike": {
                "measurement_no": publication["spike_measurement_no"],
                "revision": publication["spike_revision"],
                "value_mg_l": publication["spike_value_mg_l"],
                "added_mg_l": publication["spike_added_mg_l"],
                "blank_corrected_mg_l": publication["corrected_spike_mg_l"],
                "recovery_percent": publication["recovery_percent"],
                "min_percent": publication["recovery_min_percent"],
                "max_percent": publication["recovery_max_percent"],
                "passes": publication["recovery_percent"] is not None
                and (
                    publication["recovery_min_percent"]
                    <= publication["recovery_percent"]
                    <= publication["recovery_max_percent"]
                ),
            },
            "reasons": reasons,
        },
        "samples": [
            {
                "sample_no": item["sample_no"],
                "measurement_no": item["measurement_no"],
                "source_revision": item["source_revision"],
                "source_value_mg_l": item["source_value_mg_l"],
                "blank_value_mg_l": publication["blank_value_mg_l"],
                "concentration_mg_l": item["concentration_mg_l"],
                "reason": item["reason"],
                "note": item["note"],
            }
            for item in items
        ],
    }


def batch_summary(conn: Any, batch_id: int) -> dict[str, Any]:
    batch = _fetch_batch(conn, batch_id)
    measurements = current_measurements(conn, batch_id)
    qc = domain.evaluate_qc(
        measurements["blank"], measurements["spike"], measurements["samples"]
    )
    current_publication = _current_publication_row(conn, batch_id)
    historical_count = conn.execute(
        """
        SELECT COUNT(*) FROM publications
        WHERE batch_id = ? AND status = 'expired'
        """,
        (batch_id,),
    ).fetchone()[0]
    return {
        "batch_id": batch["id"],
        "batch_label": batch["label"],
        "created_at": batch["created_at"],
        "sample_count": len(measurements["samples"]),
        "measurement_count": len(all_measurements(conn, batch_id)),
        "qc": qc,
        "current_publication": _publication_summary(conn, current_publication)
        if current_publication is not None
        else None,
        "expired_publication_count": historical_count,
    }


def batch_detail(conn: Any, batch_id: int) -> dict[str, Any]:
    summary = batch_summary(conn, batch_id)
    measurements = current_measurements(conn, batch_id)
    summary.update(
        {
            "samples": [
                {
                    "sample_no": row["sample_no"],
                    **_measurement_view(row),
                }
                for row in measurements["samples"]
            ],
            "blank": _measurement_view(measurements["blank"])
            if measurements["blank"] is not None
            else None,
            "spike": _measurement_view(measurements["spike"])
            if measurements["spike"] is not None
            else None,
        }
    )
    return summary


def list_batches(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM batches ORDER BY id").fetchall()
    return [batch_summary(conn, row["id"]) for row in rows]


def list_publications(conn: Any, batch_id: int) -> list[dict[str, Any]]:
    _fetch_batch(conn, batch_id)
    rows = conn.execute(
        """
        SELECT * FROM publications
        WHERE batch_id = ?
        ORDER BY id DESC
        """,
        (batch_id,),
    ).fetchall()
    return [_publication_summary(conn, dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_batch(
    conn: Any,
    *,
    label: str | None,
    samples: Iterable[dict[str, Any]],
    blank: dict[str, Any] | None,
    spike: dict[str, Any] | None,
) -> dict[str, Any]:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO batches (created_at, label) VALUES (?, ?)", (now, label)
    )
    assert cursor.lastrowid is not None
    batch_id = int(cursor.lastrowid)

    for sample in samples:
        _apply_measurement(
            conn,
            batch_id,
            kind="sample",
            sample_no=sample["sample_no"],
            measurement_no=sample["measurement_no"],
            revision=sample["revision"],
            value_mg_l=sample["value_mg_l"],
        )
    if blank is not None:
        _apply_measurement(
            conn,
            batch_id,
            kind="blank",
            sample_no=None,
            measurement_no=blank["measurement_no"],
            revision=blank["revision"],
            value_mg_l=blank["value_mg_l"],
        )
    if spike is not None:
        _apply_measurement(
            conn,
            batch_id,
            kind="spike",
            sample_no=None,
            measurement_no=spike["measurement_no"],
            revision=spike["revision"],
            value_mg_l=spike["value_mg_l"],
        )
    return batch_detail(conn, batch_id)


def _apply_measurement(
    conn: Any,
    batch_id: int,
    *,
    kind: str,
    sample_no: int | None,
    measurement_no: str,
    revision: int,
    value_mg_l: float,
) -> dict[str, Any]:
    """Insert a new measurement or a new revision, enforcing revision rules.

    Returns ``duplicate`` (same revision, same value), ``expired_publication_id``
    and the stored row.  Raises ``ApiError`` on conflicts.
    """

    _fetch_batch(conn, batch_id)

    if kind == "sample":
        if sample_no is None:
            raise ApiError(
                422, "SAMPLE_NO_REQUIRED", "kind=sample 时必须提供 sample_no。"
            )
        if not domain.MIN_SAMPLES <= sample_no <= domain.MAX_SAMPLES:
            raise ApiError(
                422,
                "SAMPLE_NO_OUT_OF_RANGE",
                f"sample_no 必须在 {domain.MIN_SAMPLES}～{domain.MAX_SAMPLES} 之间。",
                sample_no=sample_no,
            )
    else:
        if sample_no is not None:
            raise ApiError(
                422,
                "SAMPLE_NO_NOT_ALLOWED",
                f"kind={kind} 时不得提供 sample_no。",
                kind=kind,
            )

    point = (kind, sample_no)
    existing = conn.execute(
        """
        SELECT * FROM measurements
        WHERE batch_id = ? AND measurement_no = ?
        """,
        (batch_id, measurement_no),
    ).fetchall()

    if existing:
        rows = [dict(row) for row in existing]
        first = rows[0]
        if _point_key(first) != point:
            raise ApiError(
                409,
                "MEASUREMENT_NO_CONFLICT",
                (
                    f"测量号 {measurement_no} 已属于批次内另一测量点"
                    f"（{first['kind']}, sample_no={first['sample_no']}），"
                    "不能改用于新的测量点。"
                ),
                measurement_no=measurement_no,
            )
        current = max(rows, key=lambda row: row["revision"])
        if revision == current["revision"]:
            if float(value_mg_l) == float(current["value_mg_l"]):
                return {
                    "duplicate": True,
                    "expired_publication_id": None,
                    "measurement": _measurement_view(current),
                    "current_publication_id": _current_publication_id(conn, batch_id),
                }
            raise ApiError(
                409,
                "REVISION_VALUE_CONFLICT",
                (
                    f"测量号 {measurement_no} 的修订 {revision} 已存在且读数为 "
                    f"{domain.format_value(current['value_mg_l'])} mg/L，"
                    f"本次读数为 {domain.format_value(value_mg_l)} mg/L；"
                    "同一修订号必须携带相同读数，如需更正请递增修订号。"
                ),
                measurement_no=measurement_no,
                revision=revision,
                current_revision=current["revision"],
                current_value_mg_l=current["value_mg_l"],
            )
        if revision < current["revision"]:
            raise ApiError(
                409,
                "REVISION_STALE",
                (
                    f"测量号 {measurement_no} 的当前修订为 {current['revision']}，"
                    f"提交的修订 {revision} 已过期，旧版拒收。"
                ),
                measurement_no=measurement_no,
                revision=revision,
                current_revision=current["revision"],
            )
        if revision != current["revision"] + 1:
            raise ApiError(
                409,
                "REVISION_GAP",
                (
                    f"测量号 {measurement_no} 的当前修订为 {current['revision']}，"
                    f"下一次更正必须提交修订 {current['revision'] + 1}，"
                    f"而不是 {revision}；修订号须逐次递增。"
                ),
                measurement_no=measurement_no,
                revision=revision,
                expected_revision=current["revision"] + 1,
            )
    else:
        if revision != 1:
            raise ApiError(
                409,
                "REVISION_GAP",
                (
                    f"测量号 {measurement_no} 是首次记录，必须从修订 1 开始，"
                    f"而不是 {revision}。"
                ),
                measurement_no=measurement_no,
                revision=revision,
                expected_revision=1,
            )
        point_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM measurements
                WHERE batch_id = ? AND kind = ?
                  AND ((? IS NULL AND sample_no IS NULL) OR sample_no = ?)
                """,
                (batch_id, kind, sample_no, sample_no),
            ).fetchall()
        ]
        if point_rows:
            occupant = point_rows[0]
            raise ApiError(
                409,
                "POINT_ALREADY_RECORDED",
                (
                    f"测量点（{kind}, sample_no={sample_no}）已由测量号 "
                    f"{occupant['measurement_no']} 记录；更正请沿用原测量号并递增修订号。"
                ),
                measurement_no=measurement_no,
                current_measurement_no=occupant["measurement_no"],
                revision=occupant["revision"],
            )
        if kind == "sample":
            sample_count = conn.execute(
                """
                SELECT COUNT(DISTINCT sample_no) FROM measurements
                WHERE batch_id = ? AND kind = 'sample'
                """,
                (batch_id,),
            ).fetchone()[0]
            if int(sample_count) >= domain.MAX_SAMPLES:
                raise ApiError(
                    422,
                    "SAMPLE_LIMIT_EXCEEDED",
                    SAMPLE_LIMIT_MESSAGE,
                    max_samples=domain.MAX_SAMPLES,
                )

    created_at = utc_now()
    row = _insert_measurement(
        conn,
        batch_id=batch_id,
        kind=kind,
        sample_no=sample_no,
        measurement_no=measurement_no,
        revision=revision,
        value_mg_l=float(value_mg_l),
        created_at=created_at,
    )
    expired_id = _expire_current_publication(conn, batch_id)
    return {
        "duplicate": False,
        "expired_publication_id": expired_id,
        "measurement": _measurement_view(row),
        "current_publication_id": _current_publication_id(conn, batch_id),
    }


def _current_publication_id(conn: Any, batch_id: int) -> int | None:
    row = _current_publication_row(conn, batch_id)
    return int(row["id"]) if row is not None else None


def record_measurement(
    conn: Any,
    batch_id: int,
    *,
    kind: str,
    sample_no: int | None,
    measurement_no: str,
    revision: int,
    value_mg_l: float,
) -> dict[str, Any]:
    result = _apply_measurement(
        conn,
        batch_id,
        kind=kind,
        sample_no=sample_no,
        measurement_no=measurement_no,
        revision=revision,
        value_mg_l=value_mg_l,
    )
    result["batch_id"] = batch_id
    return result


def _publication_matches_current(
    conn: Any, publication: dict[str, Any], measurements: dict[str, Any]
) -> bool:
    """Defensive consistency check before reusing a current publication."""

    blank = measurements["blank"]
    spike = measurements["spike"]
    if blank is None or spike is None:
        return False
    if (
        publication["blank_measurement_no"] != blank["measurement_no"]
        or publication["blank_revision"] != blank["revision"]
        or publication["spike_measurement_no"] != spike["measurement_no"]
        or publication["spike_revision"] != spike["revision"]
    ):
        return False

    items = _publication_items(conn, publication["id"])
    expected = measurements["samples"]
    if len(items) != len(expected):
        return False
    by_sample = {item["sample_no"]: item for item in items}
    for sample in expected:
        item = by_sample.get(sample["sample_no"])
        if item is None:
            return False
        if (
            item["measurement_no"] != sample["measurement_no"]
            or item["source_revision"] != sample["revision"]
            or float(item["source_value_mg_l"]) != float(sample["value_mg_l"])
        ):
            return False
    return True


MAX_PUBLISH_ATTEMPTS = 5


def _retire_stale_publication(conn: Any, batch_id: int) -> int | None:
    """Expire a current report that no longer matches the live revisions.

    Runs in its own committed transaction.  Retiring an invalid report is a
    decision that must stick even if the subsequent publish attempt is
    rejected for failing QC.
    """

    with transaction(conn):
        existing = _current_publication_row(conn, batch_id)
        if existing is None:
            return None
        measurements = current_measurements(conn, batch_id)
        if _publication_matches_current(conn, existing, measurements):
            return None
        return _expire_current_publication(conn, batch_id)


def _publish_once(conn: Any, batch_id: int) -> dict[str, Any] | str:
    """One publish attempt in a single write transaction.

    Returns ``"retry"`` if a concurrent writer made the current report stale
    after the retire step.  QC rejection raises ``ApiError`` and rolls back
    only this attempt, never the retirement that preceded it.
    """

    with transaction(conn):
        existing = _current_publication_row(conn, batch_id)
        measurements = current_measurements(conn, batch_id)
        if existing is not None:
            if _publication_matches_current(conn, existing, measurements):
                return {
                    "already_published": True,
                    "publication": publication_report(conn, int(existing["id"])),
                }
            return "retry"

        qc = domain.evaluate_qc(
            measurements["blank"], measurements["spike"], measurements["samples"]
        )
        if not qc["complete"]:
            raise ApiError(
                409,
                "QC_NOT_COMPLETE",
                "批次质控测量不完整，不能发布。",
                qc=qc,
            )
        if not qc["passes"]:
            raise ApiError(
                422,
                "QC_FAILED",
                "空白或加标回收率不合格，禁止发布。",
                qc=qc,
            )

        blank = measurements["blank"]
        spike = measurements["spike"]
        assert blank is not None and spike is not None

        blank_value = domain.dec(blank["value_mg_l"])
        spike_value = domain.dec(spike["value_mg_l"])
        corrected_spike = spike_value - blank_value
        recovery = corrected_spike / domain.SPIKE_ADDED_MG_L * Decimal("100")
        published_at = utc_now()

        cursor = conn.execute(
            """
            INSERT INTO publications
                (batch_id, published_at, status, blank_measurement_no, blank_revision,
                 blank_value_mg_l, spike_measurement_no, spike_revision,
                 spike_value_mg_l, spike_added_mg_l, corrected_spike_mg_l,
                 recovery_percent, recovery_min_percent, recovery_max_percent,
                 sample_count)
            VALUES (?, ?, 'current', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                published_at,
                blank["measurement_no"],
                blank["revision"],
                blank["value_mg_l"],
                spike["measurement_no"],
                spike["revision"],
                spike["value_mg_l"],
                float(domain.SPIKE_ADDED_MG_L),
                domain.quantize(corrected_spike),
                domain.quantize(recovery),
                float(domain.RECOVERY_MIN_PERCENT),
                float(domain.RECOVERY_MAX_PERCENT),
                len(measurements["samples"]),
            ),
        )
        assert cursor.lastrowid is not None
        publication_id = int(cursor.lastrowid)

        items: list[dict[str, Any]] = []
        for sample in measurements["samples"]:
            sample_value = domain.dec(sample["value_mg_l"])
            corrected = sample_value - blank_value
            if corrected < 0:
                item = {
                    "sample_no": sample["sample_no"],
                    "measurement_no": sample["measurement_no"],
                    "source_revision": sample["revision"],
                    "source_value_mg_l": sample["value_mg_l"],
                    "concentration_mg_l": None,
                    "reason": "SAMPLE_BELOW_BLANK",
                    "note": (
                        f"样品读数 {domain.format_value(sample_value)} mg/L 低于空白 "
                        f"{domain.format_value(blank_value)} mg/L，扣空白结果为负，"
                        "浓度留空。"
                    ),
                }
            else:
                item = {
                    "sample_no": sample["sample_no"],
                    "measurement_no": sample["measurement_no"],
                    "source_revision": sample["revision"],
                    "source_value_mg_l": sample["value_mg_l"],
                    "concentration_mg_l": domain.quantize(corrected),
                    "reason": None,
                    "note": None,
                }
            items.append(item)

        _insert_publication_items(conn, publication_id, items)

        # Build the response inside the same transaction from the rows just
        # written, so the caller never sees a report the database does not have.
        return {
            "already_published": False,
            "publication": publication_report(conn, publication_id),
        }


def publish_batch(conn: Any, batch_id: int) -> dict[str, Any]:
    """Publish a batch under the QC rules.

    Retiring an invalid current report commits on its own; the replacement
    report is then inserted atomically with all of its line items.  If a
    concurrent correction lands between the two, the attempt retries against
    the newer revisions instead of citing them from a half-built report.
    """

    _fetch_batch(conn, batch_id)

    superseded_id: int | None = None
    for _ in range(MAX_PUBLISH_ATTEMPTS):
        retired = _retire_stale_publication(conn, batch_id)
        if retired is not None:
            superseded_id = retired
        outcome = _publish_once(conn, batch_id)
        if outcome != "retry":
            outcome["expired_publication_id"] = superseded_id
            return outcome

    raise ApiError(
        409,
        "PUBLISH_CONTENTION",
        "并发更正持续发生，发布未能在限定次数内基于稳定修订完成，请重试。",
    )


def measurement_history(conn: Any, batch_id: int) -> list[dict[str, Any]]:
    _fetch_batch(conn, batch_id)
    return [_measurement_view(row) for row in all_measurements(conn, batch_id)]

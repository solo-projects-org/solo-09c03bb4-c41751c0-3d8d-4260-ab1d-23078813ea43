"""Interleaving and failure-atomicity guarantees.

These tests exercise the invariants from the outside:

1. A current report always cites exactly the live revisions, never an older one.
2. A report is either complete or absent; no half-written report is visible.
3. Retiring a stale report survives a later QC rejection.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.db import connect, transaction
from tests.helpers import create_batch, post_measurement, publish, sample


def _assert_report_complete(client: TestClient, publication_id: int) -> None:
    """A published report is always whole and internally coherent."""

    report = client.get(f"/publications/{publication_id}").json()
    assert report["status"] in ("current", "expired")
    assert report["sample_count"] == len(report["samples"])
    for item in report["samples"]:
        if item["concentration_mg_l"] is None:
            assert item["reason"] == "SAMPLE_BELOW_BLANK"
            assert item["source_value_mg_l"] < report["qc"]["blank"]["value_mg_l"]
        else:
            assert item["reason"] is None
            assert item["concentration_mg_l"] == pytest.approx(
                item["source_value_mg_l"] - report["qc"]["blank"]["value_mg_l"]
            )
    # Every line item cites a revision that could not be newer than the
    # publication itself; the source revision is recorded, not inferred.
    for item in report["samples"]:
        assert item["source_revision"] >= 1


def _assert_current_matches_live(client: TestClient, batch_id: int) -> None:
    """Quiescent check: a current report cites exactly the live revisions."""

    detail = client.get(f"/batches/{batch_id}").json()
    current = detail["current_publication"]
    if current is None:
        return
    report = client.get(f"/publications/{current['publication_id']}").json()
    assert report["status"] == "current"

    live = {row["sample_no"]: row for row in detail["samples"]}
    assert len(report["samples"]) == len(live)
    for item in report["samples"]:
        source = live[item["sample_no"]]
        assert item["measurement_no"] == source["measurement_no"]
        assert item["source_revision"] == source["revision"]
        assert item["source_value_mg_l"] == pytest.approx(source["value_mg_l"])


def test_concurrent_correction_and_publish_never_cites_stale_revision(
    client: TestClient,
) -> None:
    batch = create_batch(
        client,
        samples=[sample(no, 1.0 + no, mno=f"S{no}") for no in range(1, 6)],
        blank=0.05,
        spike=10.55,
    )
    batch_id = batch["batch_id"]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def corrector() -> None:
        try:
            barrier.wait()
            for revision in range(2, 8):
                response = post_measurement(
                    client,
                    batch_id,
                    sample(1, 5.0 + revision, revision=revision, mno="S1"),
                )
                assert response.status_code in (200, 201), response.text
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def publisher() -> None:
        try:
            barrier.wait()
            for _ in range(6):
                response = publish(client, batch_id)
                assert response.status_code in (200, 201, 409, 422), response.text
                if response.status_code in (200, 201):
                    publication_id = response.json()["publication"]["publication_id"]
                    _assert_report_complete(client, publication_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=corrector), threading.Thread(target=publisher)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors

    # Writers have stopped: the current report, if any, must cite the live
    # revisions exactly, and every historical report must still be whole.
    _assert_current_matches_live(client, batch_id)
    for summary in client.get(f"/batches/{batch_id}/publications").json():
        _assert_report_complete(client, summary["publication_id"])

    # At most one current report exists per batch.
    statuses = [
        item["status"]
        for item in client.get(f"/batches/{batch_id}/publications").json()
    ]
    assert statuses.count("current") <= 1


def test_stale_current_report_is_retired_even_if_republish_fails_qc(
    client: TestClient,
) -> None:
    """Simulates a legacy/corrupt 'current' row that cites an old revision.

    The retirement must commit independently: after a QC-failing publish the
    stale report must not be resurrected as current.
    """

    batch = create_batch(client, blank=0.05, spike=10.55)
    batch_id = batch["batch_id"]
    published = publish(client, batch_id)
    assert published.status_code == 201
    publication_id = published.json()["publication"]["publication_id"]

    # Bypass the API to create the impossible state: a correction that did not
    # expire the report (as if an older buggy writer had committed it), plus a
    # QC-failing blank revision.
    conn = connect(client.app.state.db_path)
    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO measurements
                    (batch_id, measurement_no, kind, sample_no, revision,
                     value_mg_l, created_at)
                VALUES (?, 'S1', 'sample', 1, 2, 9.9, '2026-01-01T00:00:00Z')
                """,
                (batch_id,),
            )
            conn.execute(
                """
                INSERT INTO measurements
                    (batch_id, measurement_no, kind, sample_no, revision,
                     value_mg_l, created_at)
                VALUES (?, 'BLK', 'blank', NULL, 2, 0.9, '2026-01-01T00:00:00Z')
                """,
                (batch_id,),
            )
    finally:
        conn.close()

    # Sanity: the API sees a current report that no longer matches live rows.
    assert client.get(f"/batches/{batch_id}").json()["current_publication"] is not None

    rejected = publish(client, batch_id)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "QC_FAILED"

    old = client.get(f"/publications/{publication_id}").json()
    assert old["status"] == "expired"
    assert old["expired_at"] is not None
    assert client.get(f"/batches/{batch_id}").json()["current_publication"] is None


def test_history_endpoint_exposes_all_revisions(client: TestClient) -> None:
    batch = create_batch(client, samples=[sample(1, 2.0, mno="A")])
    batch_id = batch["batch_id"]
    post_measurement(client, batch_id, sample(1, 2.5, revision=2, mno="A"))
    post_measurement(client, batch_id, sample(1, 3.0, revision=3, mno="A"))

    history = client.get(f"/batches/{batch_id}/measurements").json()
    revisions = [m["revision"] for m in history if m["measurement_no"] == "A"]
    assert revisions == [1, 2, 3]

    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["samples"][0]["revision"] == 3
    # 3 revisions of sample A plus the batch's blank and spike.
    assert detail["measurement_count"] == 5

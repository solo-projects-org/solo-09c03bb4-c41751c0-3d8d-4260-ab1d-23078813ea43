"""Publication lifecycle: expiry on any change, immutable history, no partials."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import service
from app.db import connect, transaction
from tests.helpers import create_batch, post_measurement, publish, sample


def publish_ok(client: TestClient, batch_id: int) -> dict:
    response = publish(client, batch_id)
    assert response.status_code == 201, response.text
    return response.json()["publication"]


def test_any_new_measurement_expires_current_report(client: TestClient) -> None:
    batch = create_batch(client, samples=[sample(1, 2.5)], blank=0.05, spike=10.55)
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)
    assert first["status"] == "current"

    correction = post_measurement(client, batch_id, sample(1, 3.5, revision=2))
    assert correction.status_code == 201
    assert correction.json()["expired_publication_id"] == first["publication_id"]
    assert correction.json()["current_publication_id"] is None

    # The old report is retained, but clearly marked expired.
    old = client.get(f"/publications/{first['publication_id']}").json()
    assert old["status"] == "expired"
    assert old["expired_at"] is not None
    assert old["samples"][0]["source_revision"] == 1
    assert old["samples"][0]["concentration_mg_l"] == pytest.approx(2.45)

    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["current_publication"] is None
    assert detail["expired_publication_count"] == 1
    assert detail["samples"][0]["revision"] == 2

    # Re-publication creates a new report citing the corrected revision.
    second = publish_ok(client, batch_id)
    assert second["publication_id"] != first["publication_id"]
    assert second["samples"][0]["source_revision"] == 2
    assert second["samples"][0]["concentration_mg_l"] == pytest.approx(3.45)

    history = client.get(f"/batches/{batch_id}/publications").json()
    assert [item["status"] for item in history] == ["current", "expired"]


@pytest.mark.parametrize("kind", ["blank", "spike"])
def test_blank_or_spike_correction_expires_report(
    client: TestClient, kind: str
) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)

    payload = {
        "measurement_no": "BLK" if kind == "blank" else "SPK",
        "revision": 2,
        "value_mg_l": 0.1 if kind == "blank" else 10.1,
    }
    response = post_measurement(client, batch_id, payload, kind=kind)
    assert response.status_code == 201, response.text
    assert response.json()["expired_publication_id"] == first["publication_id"]
    assert (
        client.get(f"/publications/{first['publication_id']}").json()["status"]
        == "expired"
    )


def test_qc_failing_correction_expires_but_cannot_republish(client: TestClient) -> None:
    batch = create_batch(client, blank=0.05, spike=10.5)
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)

    bad_blank = post_measurement(
        client,
        batch_id,
        {"measurement_no": "BLK", "revision": 2, "value_mg_l": 0.9},
        kind="blank",
    )
    assert bad_blank.status_code == 201
    assert (
        client.get(f"/publications/{first['publication_id']}").json()["status"]
        == "expired"
    )

    blocked = publish(client, batch_id)
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "QC_FAILED"
    assert client.get(f"/batches/{batch_id}").json()["current_publication"] is None


def test_republish_without_changes_is_idempotent(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)

    again = publish(client, batch_id)
    assert again.status_code == 200
    body = again.json()
    assert body["already_published"] is True
    assert body["publication"]["publication_id"] == first["publication_id"]
    assert (
        client.get(f"/batches/{batch_id}").json()["current_publication"][
            "publication_id"
        ]
        == first["publication_id"]
    )


def test_expired_report_is_never_mutated_by_later_changes(client: TestClient) -> None:
    batch = create_batch(client, samples=[sample(1, 2.5), sample(2, 0.5)])
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)
    snapshot = first["samples"]

    for revision in (2, 3):
        post_measurement(client, batch_id, sample(1, 2.5 + revision, revision=revision))
    publish_ok(client, batch_id)

    historical = client.get(f"/publications/{first['publication_id']}").json()
    assert historical["status"] == "expired"
    assert [
        {k: item[k] for k in ("sample_no", "source_revision", "concentration_mg_l")}
        for item in historical["samples"]
    ] == [
        {k: item[k] for k in ("sample_no", "source_revision", "concentration_mg_l")}
        for item in snapshot
    ]


def test_failed_measurement_write_leaves_no_trace(client: TestClient) -> None:
    """If writing the measurement row fails, publication expiry must roll back."""

    batch = create_batch(client)
    batch_id = batch["batch_id"]
    first = publish_ok(client, batch_id)

    def boom(*args, **kwargs):
        raise RuntimeError("injected write failure")

    original = service._insert_measurement
    service._insert_measurement = boom
    try:
        with pytest.raises(RuntimeError):
            with TestClient(client.app, raise_server_exceptions=True) as raw:
                raw.post(
                    f"/batches/{batch_id}/measurements",
                    json={
                        "kind": "sample",
                        "sample_no": 2,
                        "measurement_no": "S2",
                        "revision": 2,
                        "value_mg_l": 0.9,
                    },
                )
    finally:
        service._insert_measurement = original

    conn = connect(client.app.state.db_path)
    try:
        revisions = conn.execute(
            "SELECT revision FROM measurements WHERE batch_id = ? AND measurement_no = 'S2'",
            (batch_id,),
        ).fetchall()
        assert [row["revision"] for row in revisions] == [1]
        status = conn.execute(
            "SELECT status FROM publications WHERE id = ?", (first["publication_id"],)
        ).fetchone()["status"]
        assert status == "current"
    finally:
        conn.close()

    # The report still matches the live revisions, so it remains servable.
    assert (
        client.get(f"/publications/{first['publication_id']}").json()["status"]
        == "current"
    )


def test_failed_publication_write_leaves_no_partial_report(client: TestClient) -> None:
    batch = create_batch(client, samples=[sample(1, 2.5), sample(2, 0.5)])
    batch_id = batch["batch_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("injected item failure")

    original = service._insert_publication_items
    service._insert_publication_items = boom
    try:
        with pytest.raises(RuntimeError):
            with TestClient(client.app, raise_server_exceptions=True) as raw:
                raw.post(f"/batches/{batch_id}/publications")
    finally:
        service._insert_publication_items = original

    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["current_publication"] is None
    assert detail["expired_publication_count"] == 0
    publications = client.get(f"/batches/{batch_id}/publications").json()
    assert publications == []

    # The batch is still publishable afterwards; nothing was left half-written.
    report = publish_ok(client, batch_id)
    assert len(report["samples"]) == 2


def test_failure_after_some_items_written_rolls_back_everything(
    client: TestClient,
) -> None:
    """Fail on the *second* line item: the first must not survive either."""

    batch = create_batch(
        client, samples=[sample(no, 1.0 + no, mno=f"S{no}") for no in range(1, 5)]
    )
    batch_id = batch["batch_id"]

    original = service._insert_publication_items
    written: list[int] = []

    def partial_then_boom(conn, publication_id, items):
        # Write the first item for real, then fail on the next one.
        original(conn, publication_id, items[:1])
        written.append(items[0]["sample_no"])
        raise RuntimeError("injected failure after first item")

    service._insert_publication_items = partial_then_boom
    try:
        with pytest.raises(RuntimeError):
            with TestClient(client.app, raise_server_exceptions=True) as raw:
                raw.post(f"/batches/{batch_id}/publications")
    finally:
        service._insert_publication_items = original

    assert written, "the harness must actually have written a partial item"

    conn = connect(client.app.state.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM publication_items").fetchone()[0] == 0
    finally:
        conn.close()
    assert client.get(f"/batches/{batch_id}/publications").json() == []

    report = publish_ok(client, batch_id)
    assert len(report["samples"]) == 4


def test_publication_uses_single_consistent_revision_snapshot(
    client: TestClient,
) -> None:
    """Interleaved correction between QC check and report build is impossible.

    A write transaction holds the database write lock from ``BEGIN IMMEDIATE``
    until commit, so a second connection cannot commit a correction halfway
    through publishing.  Simulate the worst case by attempting a correction
    from another connection while the publish transaction is open.
    """

    batch = create_batch(client, samples=[sample(1, 2.5)], blank=0.05, spike=10.55)
    batch_id = batch["batch_id"]

    writer = connect(client.app.state.db_path)
    try:
        with transaction(writer):
            # Publisher's transaction is open; a correction must be blocked.
            contender = sqlite3.connect(
                client.app.state.db_path, timeout=0.2, isolation_level=None
            )
            try:
                with pytest.raises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
                    contender.execute(
                        """
                        UPDATE measurements SET value_mg_l = 9.9
                        WHERE batch_id = ? AND measurement_no = 'S1'
                        """,
                        (batch_id,),
                    )
            finally:
                contender.close()
    finally:
        writer.close()

"""Revision semantics: idempotent resend, conflicting values, stale revisions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import create_batch, post_measurement, publish, sample


def test_identical_resend_is_not_duplicated(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(client, batch_id, sample(2, 0.5, revision=1))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duplicate"] is True
    assert body["measurement"]["revision"] == 1

    history = client.get(f"/batches/{batch_id}/measurements").json()
    assert len([m for m in history if m["sample_no"] == 2]) == 1
    assert client.get(f"/batches/{batch_id}").json()["measurement_count"] == 4


def test_identical_resend_after_publication_keeps_report_current(
    client: TestClient,
) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]
    published = publish(client, batch_id)
    assert published.status_code == 201
    publication_id = published.json()["publication"]["publication_id"]

    replay = post_measurement(client, batch_id, sample(2, 0.5, revision=1))
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["current_publication_id"] == publication_id

    current = client.get(f"/publications/{publication_id}").json()
    assert current["status"] == "current"


def test_duplicate_blank_or_spike_resend_does_not_expire(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]
    publication_id = publish(client, batch_id).json()["publication"]["publication_id"]

    for kind, payload in (
        ("blank", {"measurement_no": "BLK", "revision": 1, "value_mg_l": 0.05}),
        ("spike", {"measurement_no": "SPK", "revision": 1, "value_mg_l": 10.5}),
    ):
        response = post_measurement(client, batch_id, payload, kind=kind)
        assert response.status_code == 200, response.text
        assert response.json()["duplicate"] is True
        assert response.json()["current_publication_id"] == publication_id
        assert (
            client.get(f"/publications/{publication_id}").json()["status"] == "current"
        )


def test_same_revision_different_value_is_rejected(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(client, batch_id, sample(2, 0.7, revision=1))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "REVISION_VALUE_CONFLICT"
    assert detail["current_revision"] == 1
    assert detail["current_value_mg_l"] == 0.5
    # Nothing was written.
    history = client.get(f"/batches/{batch_id}/measurements").json()
    assert len([m for m in history if m["sample_no"] == 2]) == 1


def test_older_revision_is_rejected(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    correction = post_measurement(client, batch_id, sample(2, 0.6, revision=2))
    assert correction.status_code == 201

    stale = post_measurement(client, batch_id, sample(2, 0.5, revision=1))
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_STALE"
    # Even a resend of a superseded identical value is still a stale revision.
    stale_same_value = post_measurement(client, batch_id, sample(2, 0.5, revision=1))
    assert stale_same_value.status_code == 409
    assert stale_same_value.json()["detail"]["code"] == "REVISION_STALE"


def test_revision_gap_is_rejected(client: TestClient) -> None:
    """A skip is rejected: revisions advance by one per point."""

    batch = create_batch(client)
    batch_id = batch["batch_id"]
    skipped = post_measurement(client, batch_id, sample(2, 0.8, revision=3))
    assert skipped.status_code == 409
    assert skipped.json()["detail"]["code"] == "REVISION_GAP"

    next_one = post_measurement(client, batch_id, sample(2, 0.8, revision=2))
    assert next_one.status_code == 201


def test_first_record_of_a_point_must_start_at_revision_one(
    client: TestClient,
) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(client, batch_id, sample(3, 1.2, revision=2, mno="S3"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_GAP"
    assert response.json()["detail"]["expected_revision"] == 1

    first = post_measurement(client, batch_id, sample(3, 1.2, revision=1, mno="S3"))
    assert first.status_code == 201


def test_measurement_number_cannot_move_to_another_point(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(client, batch_id, sample(1, 1.0, revision=1, mno="S2"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEASUREMENT_NO_CONFLICT"


def test_second_measurement_number_for_same_point_rejected(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(
        client, batch_id, sample(2, 0.9, revision=1, mno="S2-B")
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "POINT_ALREADY_RECORDED"


def test_kind_and_sample_no_must_agree(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    missing = client.post(
        f"/batches/{batch_id}/measurements",
        json={"kind": "sample", "measurement_no": "X", "revision": 1, "value_mg_l": 1},
    )
    assert missing.status_code == 422

    extra = client.post(
        f"/batches/{batch_id}/measurements",
        json={
            "kind": "blank",
            "sample_no": 3,
            "measurement_no": "X",
            "revision": 1,
            "value_mg_l": 1,
        },
    )
    assert extra.status_code == 422


def test_full_batch_still_accepts_corrections(client: TestClient) -> None:
    samples = [sample(no, 1.0, mno=f"M{no}") for no in range(1, 101)]
    batch = create_batch(client, samples=samples)

    response = post_measurement(
        client, batch["batch_id"], sample(1, 1.0, revision=2, mno="M1")
    )
    # Hitting the 100-sample cap must not block correcting existing points.
    assert response.status_code == 201
    assert client.get(f"/batches/{batch['batch_id']}").json()["sample_count"] == 100

    over_cap = client.post(
        "/batches",
        json={"samples": [sample(no, 1.0, mno=f"N{no}") for no in range(1, 102)]},
    )
    assert over_cap.status_code == 422


def test_blank_unique_per_batch(client: TestClient) -> None:
    batch = create_batch(client)
    batch_id = batch["batch_id"]

    response = post_measurement(
        client,
        batch_id,
        {"measurement_no": "BLK2", "revision": 1, "value_mg_l": 0.1},
        kind="blank",
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "POINT_ALREADY_RECORDED"


def test_unknown_batch_returns_404(client: TestClient) -> None:
    assert client.get("/batches/999").status_code == 404
    assert client.get("/batches/999/qc").status_code == 404
    assert post_measurement(client, 999, sample(1, 1.0)).status_code == 404
    assert publish(client, 999).status_code == 404

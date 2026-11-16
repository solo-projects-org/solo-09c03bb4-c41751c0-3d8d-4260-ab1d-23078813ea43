"""QC acceptance rules and report arithmetic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import create_batch, publish, sample


@pytest.mark.parametrize("blank_value", [0.0, 0.1, 0.2])
def test_blank_at_or_below_limit_publishes(
    client: TestClient, blank_value: float
) -> None:
    batch = create_batch(client, blank=blank_value, spike=10.0 + blank_value)
    assert batch["qc"]["blank"]["passes"] is True

    response = publish(client, batch["batch_id"])
    assert response.status_code == 201, response.text
    assert response.json()["publication"]["qc"]["passes"] is True


def test_blank_just_above_limit_blocks_publication(client: TestClient) -> None:
    batch = create_batch(client, blank=0.2000001, spike=10.2)
    response = publish(client, batch["batch_id"])

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "QC_FAILED"
    codes = {reason["code"] for reason in detail["qc"]["reasons"]}
    assert "QC_BLANK_ABOVE_LIMIT" in codes
    assert (
        client.get(f"/batches/{batch['batch_id']}").json()["current_publication"]
        is None
    )


@pytest.mark.parametrize(
    "blank_value,spike_value,expected_recovery",
    [
        (0.05, 8.05, 80.0),
        (0.0, 8.0, 80.0),
        (0.05, 12.05, 120.0),
        (0.0, 12.0, 120.0),
    ],
)
def test_recovery_boundaries_are_inclusive(
    client: TestClient,
    blank_value: float,
    spike_value: float,
    expected_recovery: float,
) -> None:
    batch = create_batch(client, blank=blank_value, spike=spike_value)
    response = publish(client, batch["batch_id"])
    assert response.status_code == 201, response.text
    qc = response.json()["publication"]["qc"]["spike"]
    assert qc["passes"] is True
    assert qc["recovery_percent"] == pytest.approx(expected_recovery)


def test_recovery_at_rounded_boundary_is_published(client: TestClient) -> None:
    """79.9999999 % rounds to 80.000000 %, and the decision follows the display."""

    batch = create_batch(client, blank=0.05, spike=8.049999999)
    qc = client.get(f"/batches/{batch['batch_id']}/qc").json()
    assert qc["spike"]["recovery_percent"] == pytest.approx(80.0, abs=1e-6)
    assert qc["spike"]["passes"] is True

    response = publish(client, batch["batch_id"])
    assert response.status_code == 201, response.text
    assert response.json()["publication"]["qc"]["spike"]["passes"] is True


@pytest.mark.parametrize(
    "blank_value,spike_value",
    [
        (0.05, 8.049),  # 79.99 %
        (0.0, 7.999),  # 79.99 %
        (0.05, 12.051),  # 120.01 %
        (0.0, 12.001),  # 120.01 %
    ],
)
def test_recovery_just_outside_range_blocks_publication(
    client: TestClient, blank_value: float, spike_value: float
) -> None:
    batch = create_batch(client, blank=blank_value, spike=spike_value)
    response = publish(client, batch["batch_id"])
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "QC_FAILED"
    assert {r["code"] for r in detail["qc"]["reasons"]} == {"QC_RECOVERY_OUT_OF_RANGE"}
    assert detail["qc"]["spike"]["recovery_percent"] == pytest.approx(
        (spike_value - blank_value) / 10 * 100, abs=5e-7
    )


def test_qc_endpoint_reflects_blank_correction_of_spike(client: TestClient) -> None:
    batch = create_batch(client, blank=0.4, spike=10.4)
    qc = client.get(f"/batches/{batch['batch_id']}/qc").json()
    assert qc["blank"]["passes"] is False
    assert qc["spike"]["blank_corrected_mg_l"] == pytest.approx(10.0)
    assert qc["spike"]["recovery_percent"] == pytest.approx(100.0)
    assert qc["passes"] is False


def test_publication_deducts_blank_and_records_source_revision(
    client: TestClient,
) -> None:
    batch = create_batch(
        client,
        samples=[sample(1, 2.5), sample(2, 0.02), sample(3, 0.05)],
        blank=0.05,
        spike=10.55,
    )
    response = publish(client, batch["batch_id"])
    assert response.status_code == 201, response.text
    publication = response.json()["publication"]

    by_sample = {item["sample_no"]: item for item in publication["samples"]}
    assert by_sample[1]["concentration_mg_l"] == pytest.approx(2.45)
    assert by_sample[1]["source_revision"] == 1
    assert by_sample[1]["reason"] is None

    # Strictly below the blank: left empty with a documented reason.
    assert by_sample[2]["concentration_mg_l"] is None
    assert by_sample[2]["reason"] == "SAMPLE_BELOW_BLANK"
    assert by_sample[2]["note"]
    # Equal to the blank: mathematically zero, so it is reported, not blanked.
    assert by_sample[3]["concentration_mg_l"] == 0.0
    assert by_sample[3]["reason"] is None


def test_missing_blank_or_spike_cannot_publish(client: TestClient) -> None:
    batch = create_batch(client, blank=None, spike=10.5)
    response = publish(client, batch["batch_id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "QC_NOT_COMPLETE"
    codes = {r["code"] for r in response.json()["detail"]["qc"]["reasons"]}
    assert "QC_BLANK_MISSING" in codes


def test_batch_requires_between_one_and_hundred_samples(client: TestClient) -> None:
    payload = {"label": "empty", "samples": []}
    assert client.post("/batches", json=payload).status_code == 422

    too_many = [sample(no, 1.0, mno=f"M{no}") for no in range(1, 102)]
    response = client.post("/batches", json={"label": "big", "samples": too_many})
    assert response.status_code == 422


def test_duplicate_sample_numbers_rejected(client: TestClient) -> None:
    response = client.post(
        "/batches",
        json={"samples": [sample(1, 1.0), sample(1, 2.0, mno="OTHER")]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_full_batch_of_100_publishes_with_complete_report(client: TestClient) -> None:
    samples = [sample(no, 1.0 + no, mno=f"S{no}") for no in range(1, 101)]
    batch = create_batch(client, samples=samples, blank=0.2, spike=10.2)
    assert batch["qc"]["passes"] is True

    response = publish(client, batch["batch_id"])
    assert response.status_code == 201, response.text
    publication = response.json()["publication"]
    assert publication["sample_count"] == 100
    assert len(publication["samples"]) == 100
    assert publication["samples"][0]["concentration_mg_l"] == pytest.approx(1.8)
    assert publication["samples"][-1]["concentration_mg_l"] == pytest.approx(100.8)
    assert publication["qc"]["spike"]["recovery_percent"] == pytest.approx(100.0)


def test_negative_reading_rejected(client: TestClient) -> None:
    response = client.post("/batches", json={"samples": [sample(1, -0.01)]})
    assert response.status_code == 422


def test_duplicate_measurement_numbers_in_create_rejected(client: TestClient) -> None:
    duplicate_sample = client.post(
        "/batches",
        json={"samples": [sample(1, 1.0, mno="X"), sample(2, 2.0, mno="X")]},
    )
    assert duplicate_sample.status_code == 422

    blank_clashes_with_sample = client.post(
        "/batches",
        json={
            "samples": [sample(1, 1.0, mno="X")],
            "blank": {"measurement_no": "X", "revision": 1, "value_mg_l": 0.05},
        },
    )
    assert blank_clashes_with_sample.status_code == 422


def test_create_rejects_non_one_initial_revision(client: TestClient) -> None:
    response = client.post("/batches", json={"samples": [sample(1, 1.0, revision=2)]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"

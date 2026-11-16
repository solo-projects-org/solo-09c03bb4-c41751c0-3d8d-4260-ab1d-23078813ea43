"""Shared request builders for the API tests."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def sample(
    no: int, value: float, revision: int = 1, mno: str | None = None
) -> dict[str, Any]:
    return {
        "sample_no": no,
        "measurement_no": mno or f"S{no}",
        "revision": revision,
        "value_mg_l": value,
    }


def measurement(
    kind: str, value: float, revision: int = 1, mno: str | None = None
) -> dict[str, Any]:
    return {
        "measurement_no": mno or ("BLK" if kind == "blank" else "SPK"),
        "revision": revision,
        "value_mg_l": value,
    }


def create_batch(
    client: TestClient,
    *,
    samples: list[dict[str, Any]] | None = None,
    blank: float | None = 0.05,
    spike: float | None = 10.5,
    label: str = "batch",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": label,
        "samples": samples if samples is not None else [sample(1, 2.5), sample(2, 0.5)],
    }
    if blank is not None:
        payload["blank"] = measurement("blank", blank)
    if spike is not None:
        payload["spike"] = measurement("spike", spike)
    response = client.post("/batches", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def post_measurement(
    client: TestClient, batch_id: int, payload: dict[str, Any], kind: str = "sample"
):
    return client.post(
        f"/batches/{batch_id}/measurements", json={"kind": kind, **payload}
    )


def publish(client: TestClient, batch_id: int):
    return client.post(f"/batches/{batch_id}/publications")

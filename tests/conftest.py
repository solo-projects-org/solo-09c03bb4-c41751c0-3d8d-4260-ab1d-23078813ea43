from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def db_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ammonia-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def client(db_dir: Path) -> Iterator[TestClient]:
    app = create_app(os.path.join(db_dir, "test.db"))
    with TestClient(app) as test_client:
        yield test_client

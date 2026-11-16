"""FastAPI application for ammonia-nitrogen batch QC and publication."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import schemas, service
from app.config import resolve_db_path
from app.db import connect, init_db, transaction
from app.errors import ApiError

DESCRIPTION = """
同批氨氮测量录入、空白/加标质控与发布 API。

* 每批 **1～100** 个样品，外加 **1 条空白** 与 **1 条加标空白**（已知加入 10 mg/L）。
* 测量按 `(measurement_no, revision)` 幂等：同版同值重送不重复；同版异值或旧版拒收。
* 空白 > 0.2 mg/L，或扣空白后加标回收率不在 80%～120% 时 **禁止发布**。
* 发布逐样品记录扣空白浓度及所依据的测量修订号；样品低于空白时浓度留空并注明原因。
* 任何测量新增或更正都会立即使当前发布过期；历史报告仍可按 ID 查询。
* 更正与发布交错、或写入失败时，报告不会引用旧修订或留下半份结果。
"""


def create_app(db_path: str | None = None) -> FastAPI:
    resolved = resolve_db_path(db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(resolved)
        yield

    app = FastAPI(
        title="氨氮批次质控发布 API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.db_path = resolved

    # ------------------------------------------------------------------
    # Error handling: one stable JSON shape for every failure mode.
    # ------------------------------------------------------------------

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.to_detail()}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求数据未通过校验。",
                    "errors": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def _integrity_error(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "CONSTRAINT_VIOLATION",
                    "message": "数据库约束拒绝该写入。",
                }
            },
        )

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.db_path)
        try:
            # Deferred transaction: the first read pins one WAL snapshot, so a
            # multi-statement view (e.g. current publication + its items) can
            # never mix states from before and after a concurrent commit.
            with transaction(conn, immediate=False):
                yield conn
        finally:
            conn.close()

    def get_tx_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.db_path)
        try:
            with transaction(conn):
                yield conn
        finally:
            conn.close()

    def get_raw_conn(request: Request) -> Iterator[sqlite3.Connection]:
        """Connection for operations that manage their own transactions."""

        conn = connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @app.get("/healthz", tags=["system"], summary="健康检查")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/batches",
        response_model=schemas.BatchDetail,
        status_code=201,
        tags=["batches"],
        summary="录入批次（样品 1～100 条，可选空白与加标空白）",
        responses={
            409: {"model": schemas.ErrorResponse, "description": "测量号/修订冲突"},
            422: {"model": schemas.ErrorResponse, "description": "校验失败"},
        },
    )
    def create_batch(
        payload: schemas.BatchCreate, conn: sqlite3.Connection = Depends(get_tx_conn)
    ) -> dict[str, Any]:
        return service.create_batch(
            conn,
            label=payload.label,
            samples=[sample.model_dump() for sample in payload.samples],
            blank=payload.blank.model_dump() if payload.blank else None,
            spike=payload.spike.model_dump() if payload.spike else None,
        )

    @app.get(
        "/batches",
        response_model=list[schemas.BatchSummary],
        tags=["batches"],
        summary="列出批次及当前质控/发布状态",
    )
    def list_batches(
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        return service.list_batches(conn)

    @app.get(
        "/batches/{batch_id}",
        response_model=schemas.BatchDetail,
        tags=["batches"],
        summary="批次详情（当前修订、质控、当前报告）",
        responses={404: {"model": schemas.ErrorResponse}},
    )
    def get_batch(
        batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        return service.batch_detail(conn, batch_id)

    @app.get(
        "/batches/{batch_id}/qc",
        response_model=schemas.QCEvaluation,
        tags=["qc"],
        summary="按当前修订计算质控结论（不写库）",
        responses={404: {"model": schemas.ErrorResponse}},
    )
    def get_qc(
        batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        return service.qc_for_batch(conn, batch_id)

    @app.get(
        "/batches/{batch_id}/measurements",
        response_model=list[schemas.MeasurementView],
        tags=["measurements"],
        summary="全部测量修订历史（含已被取代版本）",
        responses={404: {"model": schemas.ErrorResponse}},
    )
    def get_measurements(
        batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict[str, Any]]:
        return service.measurement_history(conn, batch_id)

    @app.post(
        "/batches/{batch_id}/measurements",
        response_model=schemas.MeasurementRecordResponse,
        status_code=201,
        tags=["measurements"],
        summary="新增测量或更正（同版同值幂等；任何有效写入使当前发布过期）",
        responses={
            200: {
                "model": schemas.MeasurementRecordResponse,
                "description": "同版同值重送，未新建记录",
            },
            409: {
                "model": schemas.ErrorResponse,
                "description": "同版异值、旧版或测量点冲突",
            },
            422: {"model": schemas.ErrorResponse},
        },
    )
    def post_measurement(
        batch_id: int,
        payload: schemas.MeasurementCreate,
        response: Response,
        conn: sqlite3.Connection = Depends(get_tx_conn),
    ) -> dict[str, Any]:
        result = service.record_measurement(
            conn,
            batch_id,
            kind=payload.kind,
            sample_no=payload.sample_no,
            measurement_no=payload.measurement_no,
            revision=payload.revision,
            value_mg_l=payload.value_mg_l,
        )
        response.status_code = 200 if result["duplicate"] else 201
        return result

    @app.post(
        "/batches/{batch_id}/publications",
        response_model=schemas.PublishResponse,
        status_code=201,
        tags=["publications"],
        summary="发布合格批次（QC 不合格或当前修订已发布时分别拒绝/幂等）",
        responses={
            200: {
                "model": schemas.PublishResponse,
                "description": "当前修订已发布，返回现有报告",
            },
            409: {"model": schemas.ErrorResponse, "description": "质控不完整"},
            422: {"model": schemas.ErrorResponse, "description": "质控不合格"},
        },
    )
    def post_publication(
        batch_id: int,
        response: Response,
        conn: sqlite3.Connection = Depends(get_raw_conn),
    ) -> dict[str, Any]:
        result = service.publish_batch(conn, batch_id)
        response.status_code = 200 if result["already_published"] else 201
        return result

    @app.get(
        "/batches/{batch_id}/publications",
        response_model=list[schemas.PublicationSummary],
        tags=["publications"],
        summary="批次发布历史（当前与已过期）",
        responses={404: {"model": schemas.ErrorResponse}},
    )
    def get_publications(
        batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict[str, Any]]:
        return service.list_publications(conn, batch_id)

    @app.get(
        "/publications/{publication_id}",
        response_model=schemas.PublicationReport,
        tags=["publications"],
        summary="按 ID 查询报告（过期历史报告仍可查询）",
        responses={404: {"model": schemas.ErrorResponse}},
    )
    def get_publication(
        publication_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        return service.publication_report(conn, publication_id)

    return app


app = create_app(os.getenv("AMMONIA_DB_PATH"))

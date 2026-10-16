import logging
import re
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.errors import IdempotencyConflictError, StorageError
from app.logging_config import configure_logging, request_id_context
from app.schemas import IncidentIn, TriageOut
from app.storage import (
    create_incident_record,
    fetch_recent_incidents,
    get_incident,
    init_storage,
    ping_db,
    ping_redis,
)
from app.triage import find_duplicate, predict_severity, suggest_runbook

logger = logging.getLogger(__name__)
app = FastAPI(title="A.I. Incident Triage Copilot", version="0.1.0")


@app.on_event("startup")
def startup_event() -> None:
    configure_logging()
    if get_settings().skip_startup_init:
        return
    init_storage()


@app.middleware("http")
async def request_logging(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied_request_id
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied_request_id)
        else str(uuid.uuid4())
    )
    context_token = request_id_context.set(request_id)
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception(
            "Unhandled request error",
            extra={
                "event": "request.failed",
                "request_id": request_id,
                "error_type": type(exc).__name__,
            },
        )
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred",
                    "details": None,
                },
                "request_id": request_id,
            },
        )
    finally:
        request_id_context.reset(context_token)

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Request completed",
        extra={
            "event": "request.completed",
            "request_id": request_id,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": jsonable_encoder(
                    [
                        {
                            key: error[key]
                            for key in ("loc", "msg", "type")
                            if key in error
                        }
                        for error in exc.errors()
                    ]
                ),
            },
            "request_id": request_id_context.get(),
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = {
        status.HTTP_400_BAD_REQUEST: "invalid_request",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_409_CONFLICT: "conflict",
        status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
    }.get(exc.status_code, "http_error")
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request could not be completed"
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": detail if isinstance(detail, dict) else None,
            },
            "request_id": request_id_context.get(),
        },
    )


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    logger.error(
        "Storage request failed",
        extra={"event": "storage.failed", "error_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {
                "code": "storage_unavailable",
                "message": "The incident storage service is temporarily unavailable",
                "details": None,
            },
            "request_id": request_id_context.get(),
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    db_ok = ping_db()
    redis_ok = ping_redis()
    if db_ok and redis_ok:
        return {"status": "ready", "database": "ok", "redis": "ok"}

    detail = {
        "status": "not_ready",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@app.post("/incidents/triage", response_model=TriageOut)
def triage_incident(
    payload: IncidentIn,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> TriageOut:
    if idempotency_key and not re.fullmatch(r"[A-Za-z0-9._:-]{1,255}", idempotency_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key contains unsupported characters",
        )

    logger.info("Incident accepted", extra={"event": "incident.accepted"})
    recent = fetch_recent_incidents(limit=50)
    predicted_severity = predict_severity(payload.title, payload.description)
    duplicate_of = find_duplicate(payload.title, payload.description, recent)
    runbook = suggest_runbook(payload.title, payload.description, payload.service)

    try:
        record = create_incident_record(
            title=payload.title,
            description=payload.description,
            service=payload.service,
            source=payload.source,
            occurred_at=payload.timestamp,
            predicted_severity=predicted_severity,
            duplicate_of=duplicate_of,
            runbook_suggestion=runbook,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request",
        ) from exc

    logger.info(
        "Incident triaged",
        extra={"event": "incident.succeeded", "incident_id": record["incident_id"]},
    )
    return TriageOut(
        incident_id=record["incident_id"],
        predicted_severity=record["predicted_severity"],
        duplicate_of=record["duplicate_of"],
        runbook_suggestion=record["runbook_suggestion"],
        status=record["status"],
    )


@app.get("/incidents/{incident_id}")
def read_incident(incident_id: str) -> dict:
    record = get_incident(incident_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return record

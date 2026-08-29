"""CRE Public Notice Signals API.

GET /v1/events, GET /v1/events/{id}, GET /v1/sources
Honors X-RapidAPI-Proxy-Secret when RAPIDAPI_PROXY_SECRET is set,
and X-Api-Key when API_KEY is set.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from bot_engine import connect, run_collector_loop

log = logging.getLogger("cre_signals.api")

PUBLIC_PATHS = frozenset(
    {"/", "/health", "/docs", "/redoc", "/openapi.json"}
)


def proxy_secret() -> str:
    return os.getenv("RAPIDAPI_PROXY_SECRET", "").strip()


def api_key() -> str:
    return os.getenv("API_KEY", "").strip()


def _header_matches(received: str | None, expected: str) -> bool:
    if not expected or received is None:
        return False
    return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))


async def check_rapidapi_proxy(
    request: Request,
    x_rapidapi_proxy_secret: str | None = Header(default=None, alias="X-RapidAPI-Proxy-Secret"),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    expected_proxy = proxy_secret()
    expected_api_key = api_key()
    if not expected_proxy and not expected_api_key:
        return
    if request.url.path in PUBLIC_PATHS:
        return
    proxy_ok = _header_matches(x_rapidapi_proxy_secret, expected_proxy)
    api_key_ok = _header_matches(x_api_key, expected_api_key)
    if proxy_ok or api_key_ok:
        return
    raise HTTPException(status_code=403, detail="Invalid or missing origin credentials")


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in row.items()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    task: asyncio.Task | None = None
    enabled = os.getenv("COLLECTOR_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
    if enabled:
        task = asyncio.create_task(run_collector_loop(stop))
        log.info("collector background loop started")
    yield
    stop.set()
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="CRE Public Notice Signals",
    version="1.0.0",
    description=(
        "Public-record facts for commercial real estate distress: "
        "business bankruptcies (CM/ECF RSS petitions, SEC 8-K Item 1.03) "
        "and NYC commercial occupancy executions. "
        "Each event is facts + source_url + collected_at. "
        "Does not include PACER documents, SSNs, newspaper body HTML, "
        "or license-gated FL/TX/GA press-portal notice text."
    ),
    lifespan=lifespan,
    dependencies=[Depends(check_rapidapi_proxy)],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "CRE Public Notice Signals",
        "openapi": "/openapi.json",
        "docs": "/docs",
        "events": "/v1/events",
        "sources": "/v1/sources",
    }


@app.get("/v1/sources")
def list_sources() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, slug, name, operator, source_kind, jurisdiction_state,
                   jurisdiction_local, homepage_url, listing_url, access_model,
                   license_status, robots_notes, collection_status, created_at
            FROM public.sources
            ORDER BY slug
            """
        ).fetchall()
    return {"sources": [serialize_row(r) for r in rows]}


@app.get("/v1/events")
def list_events(
    signal_family: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    jurisdiction_state: str | None = Query(default=None, min_length=2, max_length=2),
    occupancy_class: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if signal_family:
        clauses.append("signal_family = %(signal_family)s")
        params["signal_family"] = signal_family
    if event_type:
        clauses.append("event_type = %(event_type)s")
        params["event_type"] = event_type
    if jurisdiction_state:
        clauses.append("jurisdiction_state = %(jurisdiction_state)s")
        params["jurisdiction_state"] = jurisdiction_state.upper()
    if occupancy_class:
        clauses.append("occupancy_class = %(occupancy_class)s")
        params["occupancy_class"] = occupancy_class
    where = " AND ".join(clauses)
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM public.events WHERE {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT id, source_id, source_event_key, source_url, signal_family,
                   event_type, occurred_on, published_on, collected_at,
                   jurisdiction_state, jurisdiction_local, court_or_office,
                   docket_or_notice_no, bankruptcy_chapter, occupancy_class,
                   primary_party_name, primary_party_kind, counterparty_name,
                   property_street, property_city, property_county,
                   property_state, property_postal_code, is_business, confidence
            FROM public.events
            WHERE {where}
            ORDER BY collected_at DESC, occurred_on DESC NULLS LAST
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return {"total": int(total), "limit": limit, "offset": offset, "events": [serialize_row(r) for r in rows]}


@app.get("/v1/events/{event_id}")
def get_event(event_id: UUID) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, source_id, source_event_key, source_url, signal_family,
                   event_type, occurred_on, published_on, collected_at,
                   jurisdiction_state, jurisdiction_local, court_or_office,
                   docket_or_notice_no, bankruptcy_chapter, occupancy_class,
                   primary_party_name, primary_party_kind, counterparty_name,
                   property_street, property_city, property_county,
                   property_state, property_postal_code, is_business, confidence
            FROM public.events
            WHERE id = %(id)s
            """,
            {"id": event_id},
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return serialize_row(row)


@app.exception_handler(RuntimeError)
async def missing_config(_request: Request, exc: RuntimeError) -> JSONResponse:
    log.error("runtime error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})

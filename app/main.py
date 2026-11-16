"""
FastAPI application.

    uvicorn app.main:app --reload

Interactive documentation is available at http://localhost:8000/docs.
"""
from datetime import date
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import net_outflows, queries
from app.db import get_conn

app = FastAPI(
    title="Itus client transactions",
    description="Cleaned PMS/AIF transaction data: distributor flows, client history, "
                "data quality.",
    version="0.1.0",
)


@app.exception_handler(psycopg.OperationalError)
async def db_unavailable(request: Request, exc: psycopg.OperationalError):
    return JSONResponse(status_code=503,
                        content={"detail": "Database unavailable. Is Postgres running and "
                                           "DATABASE_URL set?"})


@app.get("/health")
def health(conn=Depends(get_conn)):
    conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/distributors/summary")
def distributor_summary(
    start: date | None = Query(None, description="Defaults to first date in the data"),
    end: date | None = Query(None, description="Defaults to last date in the data"),
    conn=Depends(get_conn),
):
    if start and end and start > end:
        raise HTTPException(400, "start must be on or before end")
    return queries.distributor_summary(conn, start, end)


@app.get("/clients/{client_id}/transactions")
def client_transactions(client_id: str, conn=Depends(get_conn)):
    result = queries.client_history(conn, client_id.strip().upper())
    if result is None:
        raise HTTPException(404, f"No client with id {client_id}")
    return result


@app.get("/data-quality/issues")
def data_quality_issues(
    action: Literal["fixed", "excluded", "flagged"] | None = None,
    issue_type: str | None = None,
    conn=Depends(get_conn),
):
    return queries.data_quality_report(conn, action, issue_type)


@app.get("/reports/net-outflows")
def net_outflows_report(
    month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                              description="YYYY-MM. Defaults to the latest month in the data"),
    format: Literal["json", "text"] = "json",
    conn=Depends(get_conn),
):
    report = net_outflows.build_report(conn, month)
    if format == "text":
        return PlainTextResponse(net_outflows.render_text(report))
    return report

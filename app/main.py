"""FastAPI export service: referential-subset exporter for PostgreSQL.

Endpoints:
  GET  /healthz      liveness probe
  GET  /api/tables   describe the exportable source schema
  POST /api/export   export a referentially-complete subset as a SQL package
  GET  /docs         interactive API documentation (Swagger UI)
"""

from __future__ import annotations

import os
from functools import partial

import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from exporter import (
    ALLOWED_TYPES,
    MAX_ROWS,
    MAX_TABLES,
    ExportError,
    compute_closure,
    load_schema,
    render_sql,
    select_rows_by_keys,
    validate_schema,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://exporter:exporter@localhost:5432/source"
)

app = FastAPI(
    title="pg-subset-exporter",
    version="1.0.0",
    description=(
        "Export a referentially-complete row subset from a PostgreSQL source "
        "(schema `public`, up to 6 plain tables) as a self-contained SQL package. "
        "Schema metadata and all rows are read inside one `REPEATABLE READ READ ONLY` "
        "transaction, so the export is a single committed snapshot and never blocks "
        "source writes. If the closure exceeds 1000 rows, a reference cannot be "
        "resolved, or a read fails, the whole export fails and no partial package "
        "is produced."
    ),
)


class ExportRequest(BaseModel):
    table: str = Field(
        ..., examples=["users"], description="seed table in schema public"
    )
    pks: list[int] = Field(
        ...,
        min_length=1,
        max_length=MAX_ROWS,
        examples=[[1, 2]],
        description="primary-key values of the seed rows",
    )


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/healthz")
def healthz() -> dict:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/tables")
def list_tables() -> dict:
    """Describe the exportable source schema (validated against the supported subset)."""
    try:
        with _connect() as conn:
            conn.read_only = True
            schema = load_schema(conn)
            validate_schema(schema)
    except ExportError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return {
        "schema": "public",
        "limits": {"max_tables": MAX_TABLES, "max_rows": MAX_ROWS},
        "tables": [
            {
                "name": t.name,
                "primary_key": t.pk.columns[0],
                "columns": [
                    {
                        "name": c.name,
                        "type": ALLOWED_TYPES[c.type_name],
                        "not_null": c.not_null,
                    }
                    for c in t.columns
                ],
                "unique_constraints": [
                    {"name": u.name, "columns": list(u.columns)} for u in t.uniques
                ],
                "foreign_keys": [
                    {
                        "name": f.name,
                        "columns": list(f.columns),
                        "references": {"table": f.ref_table, "columns": list(f.ref_columns)},
                    }
                    for f in t.fkeys
                ],
            }
            for t in sorted(schema.tables.values(), key=lambda t: t.name)
        ],
    }


@app.post("/api/export")
def export_subset(req: ExportRequest) -> Response:
    """Export the referential closure of the given seed rows as a SQL download."""
    try:
        with _connect() as conn:
            # One snapshot for schema metadata and every row; MVCC keeps
            # writers unblocked. Set before the first statement starts the txn.
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            schema = load_schema(conn)
            validate_schema(schema)
            fetched = compute_closure(
                schema, req.table, req.pks, partial(select_rows_by_keys, conn)
            )
            if conn.info.server_version >= 130000:
                snap_sql = "SELECT now(), current_setting('transaction_isolation'), pg_current_snapshot()::text"
            else:
                snap_sql = "SELECT now(), current_setting('transaction_isolation'), NULL"
            with conn.cursor() as cur:
                cur.execute(snap_sql)
                snapshot_ts, isolation, tx_snapshot = cur.fetchone()
            sql_text = render_sql(
                schema, fetched, req.table, req.pks, snapshot_ts, isolation, tx_snapshot
            )
    except ExportError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except psycopg.Error as exc:
        raise HTTPException(500, f"source read failed: {type(exc).__name__}") from exc

    filename = f"subset_{req.table}_{snapshot_ts:%Y%m%dT%H%M%SZ}.sql"
    return Response(
        content=sql_text.encode("utf-8"),
        media_type="application/sql",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

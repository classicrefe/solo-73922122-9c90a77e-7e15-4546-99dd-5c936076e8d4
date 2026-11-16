"""Referential-closure extraction against a consistent PostgreSQL snapshot.

Every function here is executed inside ONE transaction opened by the caller
with `ISOLATION LEVEL REPEATABLE READ READ ONLY`, so schema metadata and all
exported rows come from the same committed snapshot, while writers on the
source database are never blocked (MVCC).

Supported source subset (validated before any row is read):
  * schema `public`, at most MAX_TABLES plain tables;
  * columns: integer (int2/int4/int8) or text, no defaults, not generated,
    not identity;
  * constraints: single-column integer primary keys, unique constraints,
    foreign keys (nullable, composite, self-referencing, circular).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from psycopg import sql
from psycopg.rows import dict_row

MAX_TABLES = 6
MAX_ROWS = 1000

INT_TYPES = {"int2": "smallint", "int4": "integer", "int8": "bigint"}
TEXT_TYPES = {"text": "text"}
ALLOWED_TYPES = {**INT_TYPES, **TEXT_TYPES}

_FK_ACTIONS = {"a": None, "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


class ExportError(Exception):
    """A deterministic, client-visible export failure.

    Always raised before any byte of the package is emitted, so a failed
    export never produces a partial SQL package.
    """

    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SchemaViolation(ExportError):
    """The source schema is outside the supported subset."""


# --------------------------------------------------------------------------
# Schema model
# --------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    type_name: str  # pg_type.typname, e.g. "int4" / "text"
    not_null: bool
    has_default: bool = False
    generated: bool = False
    identity: bool = False


@dataclass
class PrimaryKey:
    name: str
    columns: tuple[str, ...]


@dataclass
class UniqueConstraint:
    name: str
    columns: tuple[str, ...]
    nulls_not_distinct: bool = False


@dataclass
class ForeignKey:
    name: str
    table: str  # constrained (child) table
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    on_update: str | None = None
    on_delete: str | None = None
    match_full: bool = False
    deferrable: bool = False
    initially_deferred: bool = False


@dataclass
class Table:
    oid: int
    name: str
    columns: list[Column]
    pk: PrimaryKey | None = None
    uniques: list[UniqueConstraint] = field(default_factory=list)
    fkeys: list[ForeignKey] = field(default_factory=list)

    @property
    def pk_column(self) -> str:
        assert self.pk is not None and len(self.pk.columns) == 1
        return self.pk.columns[0]


@dataclass
class Schema:
    tables: dict[str, Table]


# --------------------------------------------------------------------------
# Schema introspection (runs inside the snapshot transaction)
# --------------------------------------------------------------------------

_TABLES_SQL = """
SELECT c.oid, c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT a.attrelid, a.attnum, a.attname, t.typname, a.attnotnull,
       a.attgenerated, a.attidentity,
       (d.adbin IS NOT NULL) AS has_default
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""


def _constraints_sql(has_nulls_not_distinct: bool) -> str:
    nulls_nd = "con.connullsnotdistinct" if has_nulls_not_distinct else "false"
    return f"""
SELECT con.conrelid, con.conname, con.contype,
       con.conkey::int2[] AS conkey,
       con.confrelid, con.confkey::int2[] AS confkey,
       con.confupdtype, con.confdeltype, con.confmatchtype,
       con.condeferrable, con.condeferred,
       {nulls_nd} AS nulls_not_distinct
FROM pg_constraint con
WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'u', 'f')
ORDER BY con.conrelid, con.conname
"""


def load_schema(conn) -> Schema:
    """Read table/column/constraint metadata for schema `public`."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_TABLES_SQL)
        tables = cur.fetchall()
        if not tables:
            raise SchemaViolation("schema 'public' contains no plain tables")
        oids = [t["oid"] for t in tables]
        cur.execute(_COLUMNS_SQL, (oids,))
        columns = cur.fetchall()
        # Detect catalog support explicitly: connullsnotdistinct exists on
        # PG15+, but some builds report a version without shipping the column.
        cur.execute(
            "SELECT 1 FROM pg_attribute"
            " WHERE attrelid = 'pg_constraint'::regclass"
            "   AND attname = 'connullsnotdistinct'"
        )
        has_nulls_nd = cur.fetchone() is not None
        cur.execute(_constraints_sql(has_nulls_nd), (oids,))
        constraints = cur.fetchall()

    name_of = {t["oid"]: t["relname"] for t in tables}
    attname = {(c["attrelid"], c["attnum"]): c["attname"] for c in columns}

    result: dict[str, Table] = {}
    for t in tables:
        cols = [
            Column(
                name=c["attname"],
                type_name=c["typname"],
                not_null=c["attnotnull"],
                has_default=c["has_default"],
                generated=bool(c["attgenerated"]),
                identity=bool(c["attidentity"]),
            )
            for c in columns
            if c["attrelid"] == t["oid"]
        ]
        result[t["relname"]] = Table(oid=t["oid"], name=t["relname"], columns=cols)

    for c in constraints:
        tname = name_of[c["conrelid"]]
        table = result[tname]
        key_cols = tuple(attname[(c["conrelid"], int(n))] for n in c["conkey"])
        if c["contype"] == "p":
            table.pk = PrimaryKey(name=c["conname"], columns=key_cols)
        elif c["contype"] == "u":
            table.uniques.append(
                UniqueConstraint(name=c["conname"], columns=key_cols,
                                 nulls_not_distinct=c["nulls_not_distinct"])
            )
        else:  # foreign key
            if c["confrelid"] not in name_of:
                raise SchemaViolation(
                    f"foreign key {c['conname']} on table {tname} references a table "
                    f"outside the plain tables of schema 'public'"
                )
            ref_cols = tuple(attname[(c["confrelid"], int(n))] for n in c["confkey"])
            table.fkeys.append(
                ForeignKey(
                    name=c["conname"],
                    table=tname,
                    columns=key_cols,
                    ref_table=name_of[c["confrelid"]],
                    ref_columns=ref_cols,
                    on_update=_FK_ACTIONS[c["confupdtype"]],
                    on_delete=_FK_ACTIONS[c["confdeltype"]],
                    match_full=(c["confmatchtype"] == "f"),
                    deferrable=c["condeferrable"],
                    initially_deferred=c["condeferred"],
                )
            )
    return Schema(tables=result)


def validate_schema(schema: Schema) -> None:
    """Reject sources outside the supported subset (fail the whole export)."""
    n = len(schema.tables)
    if n > MAX_TABLES:
        raise SchemaViolation(
            f"schema 'public' has {n} plain tables; at most {MAX_TABLES} are supported"
        )
    for t in schema.tables.values():
        if not t.columns:
            raise SchemaViolation(f"table {t.name}: no columns found")
        for c in t.columns:
            if c.type_name not in ALLOWED_TYPES:
                raise SchemaViolation(
                    f"table {t.name}: column {c.name} has unsupported type "
                    f"'{c.type_name}' (only integer and text columns are supported)"
                )
            if c.has_default:
                raise SchemaViolation(f"table {t.name}: column {c.name} has a default expression")
            if c.generated:
                raise SchemaViolation(f"table {t.name}: column {c.name} is a generated column")
            if c.identity:
                raise SchemaViolation(f"table {t.name}: column {c.name} is an identity column")
        if t.pk is None:
            raise SchemaViolation(f"table {t.name}: no primary key")
        if len(t.pk.columns) != 1:
            raise SchemaViolation(
                f"table {t.name}: primary key must be a single column, got {list(t.pk.columns)}"
            )
        pk_col = next(c for c in t.columns if c.name == t.pk.columns[0])
        if pk_col.type_name not in INT_TYPES:
            raise SchemaViolation(
                f"table {t.name}: primary key column {pk_col.name} must be an integer type"
            )


# --------------------------------------------------------------------------
# Referential closure
# --------------------------------------------------------------------------

# fetch_rows(table, cols, keys) -> full rows whose `cols` tuple is in `keys`
FetchRows = Callable[[Table, tuple[str, ...], list[tuple]], list[dict]]


def select_rows_by_keys(conn, table: Table, cols: tuple[str, ...], keys: list[tuple]) -> list[dict]:
    """Fetch full rows of `table` where (cols) matches one of `keys`."""
    if not keys:
        return []
    col_list = sql.SQL(", ").join(sql.Identifier(c.name) for c in table.columns)
    tbl = sql.Identifier("public", table.name)
    if len(cols) == 1:
        query = sql.SQL("SELECT {cols} FROM {tbl} WHERE {k} = ANY({vals})").format(
            cols=col_list, tbl=tbl, k=sql.Identifier(cols[0]), vals=sql.Placeholder()
        )
        params = [[k[0] for k in keys]]
    else:
        row_ph = sql.SQL(", ").join(
            sql.SQL("(") + sql.SQL(", ").join(sql.Placeholder() for _ in cols) + sql.SQL(")")
            for _ in keys
        )
        query = sql.SQL("SELECT {cols} FROM {tbl} WHERE ({kcols}) IN ({rows})").format(
            cols=col_list,
            tbl=tbl,
            kcols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            rows=row_ph,
        )
        params = [v for key in keys for v in key]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def compute_closure(
    schema: Schema,
    seed_table: str,
    seed_pks: Iterable[int],
    fetch_rows: FetchRows,
) -> dict[str, dict[int, dict]]:
    """Seed rows plus every row they reference, transitively (child -> parent).

    Rows that merely reference the seed set are unrelated and not copied.
    Nullable FK parts follow MATCH SIMPLE semantics: any NULL component means
    no referenced row is required. The whole export fails (ExportError) when a
    seed row is missing, a reference cannot be resolved, or the closure grows
    beyond MAX_ROWS.
    """
    if seed_table not in schema.tables:
        known = ", ".join(sorted(schema.tables))
        raise ExportError(f"unknown seed table '{seed_table}' (known tables: {known})", 404)

    fetched: dict[str, dict[int, dict]] = {name: {} for name in schema.tables}
    key_indexes: dict[tuple[str, tuple[str, ...]], dict[tuple, int]] = {}

    def add_row(tname: str, row: dict) -> bool:
        table = schema.tables[tname]
        pk = row[table.pk_column]
        if pk in fetched[tname]:
            return False
        fetched[tname][pk] = row
        for (t, cols), idx in key_indexes.items():
            if t == tname:
                idx[tuple(row[c] for c in cols)] = pk
        return True

    def key_index(tname: str, cols: tuple[str, ...]) -> dict[tuple, int]:
        key = (tname, cols)
        if key not in key_indexes:
            key_indexes[key] = {
                tuple(row[c] for c in cols): pk for pk, row in fetched[tname].items()
            }
        return key_indexes[key]

    def check_total() -> None:
        total = sum(len(rows) for rows in fetched.values())
        if total > MAX_ROWS:
            raise ExportError(
                f"referential closure exceeds {MAX_ROWS} rows; "
                f"refusing to export a partial package"
            )

    seed_keys = sorted({int(pk) for pk in seed_pks})
    if not seed_keys:
        raise ExportError("no seed primary keys given")
    table = schema.tables[seed_table]
    rows = fetch_rows(table, (table.pk_column,), [(pk,) for pk in seed_keys])
    missing = sorted(set(seed_keys) - {row[table.pk_column] for row in rows})
    if missing:
        raise ExportError(f"seed rows not found in table '{seed_table}': primary keys {missing}", 404)
    wave = []
    for row in rows:
        if add_row(seed_table, row):
            wave.append((seed_table, row[table.pk_column]))
    check_total()

    while wave:
        needed: dict[str, tuple[ForeignKey, set[tuple]]] = {}
        for tname, pk in wave:
            row = fetched[tname][pk]
            for fk in schema.tables[tname].fkeys:
                vals = tuple(row[c] for c in fk.columns)
                if any(v is None for v in vals):
                    continue  # MATCH SIMPLE: a NULL part satisfies the FK
                if vals in key_index(fk.ref_table, fk.ref_columns):
                    continue  # already fetched
                needed.setdefault(fk.name, (fk, set()))[1].add(vals)
        next_wave = []
        for fk, keyset in needed.values():
            keys = sorted(keyset)
            ref = schema.tables[fk.ref_table]
            rows = fetch_rows(ref, fk.ref_columns, keys)
            found = {tuple(row[c] for c in fk.ref_columns) for row in rows}
            unresolved = sorted(set(keys) - found)
            if unresolved:
                raise ExportError(
                    f"unresolvable reference via constraint {fk.name}: "
                    f"{fk.table}({', '.join(fk.columns)}) -> "
                    f"{fk.ref_table}({', '.join(fk.ref_columns)}), "
                    f"missing key(s) {unresolved[:5]}{' ...' if len(unresolved) > 5 else ''}"
                )
            for row in rows:
                if add_row(fk.ref_table, row):
                    next_wave.append((fk.ref_table, row[ref.pk_column]))
        check_total()
        wave = next_wave

    return fetched


# --------------------------------------------------------------------------
# SQL package rendering
# --------------------------------------------------------------------------

def qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def qname(table: str) -> str:
    return f'"public".{qi(table)}'


def _lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _create_table_ddl(t: Table) -> str:
    parts = []
    for c in t.columns:
        s = f"    {qi(c.name)} {ALLOWED_TYPES[c.type_name]}"
        if c.not_null:
            s += " NOT NULL"
        parts.append(s)
    parts.append(f"    CONSTRAINT {qi(t.pk.name)} PRIMARY KEY ({qi(t.pk.columns[0])})")
    for u in t.uniques:
        cols = ", ".join(qi(c) for c in u.columns)
        nnd = " NULLS NOT DISTINCT" if u.nulls_not_distinct else ""
        parts.append(f"    CONSTRAINT {qi(u.name)} UNIQUE{nnd} ({cols})")
    return f"CREATE TABLE {qname(t.name)} (\n" + ",\n".join(parts) + "\n);"


def _insert_sql(t: Table, rows: dict[int, dict]) -> str:
    cols = ", ".join(qi(c.name) for c in t.columns)
    vals = [
        "    (" + ", ".join(_lit(row[c.name]) for c in t.columns) + ")"
        for _, row in sorted(rows.items())
    ]
    return f"INSERT INTO {qname(t.name)} ({cols}) VALUES\n" + ",\n".join(vals) + ";"


def _add_fk_sql(fk: ForeignKey) -> str:
    cols = ", ".join(qi(c) for c in fk.columns)
    rcols = ", ".join(qi(c) for c in fk.ref_columns)
    s = (
        f"ALTER TABLE {qname(fk.table)} ADD CONSTRAINT {qi(fk.name)} "
        f"FOREIGN KEY ({cols}) REFERENCES {qname(fk.ref_table)} ({rcols})"
    )
    if fk.match_full:
        s += " MATCH FULL"
    if fk.on_update:
        s += f" ON UPDATE {fk.on_update}"
    if fk.on_delete:
        s += f" ON DELETE {fk.on_delete}"
    if fk.deferrable:
        s += " DEFERRABLE"
        if fk.initially_deferred:
            s += " INITIALLY DEFERRED"
    return s + ";"


def render_sql(
    schema: Schema,
    fetched: dict[str, dict[int, dict]],
    seed_table: str,
    seed_pks: Iterable[int],
    snapshot_ts: datetime | None = None,
    isolation: str = "repeatable read",
    tx_snapshot: str | None = None,
) -> str:
    """Render a self-contained SQL package restorable into an empty database.

    Tables are created without foreign keys, all rows are inserted, and the
    foreign keys are added (and thereby validated) last, so circular and
    self references restore cleanly as long as the closure is complete.
    """
    total = sum(len(rows) for rows in fetched.values())
    per_table = ", ".join(
        f"{name}:{len(fetched[name])}" for name in sorted(fetched) if fetched[name]
    )
    seeds = ", ".join(str(pk) for pk in sorted({int(p) for p in seed_pks}))

    out: list[str] = [
        "-- ================================================================",
        "-- PostgreSQL referential subset export",
        f"-- seed: {qname(seed_table)} primary key(s): {seeds}",
        f"-- snapshot time (transaction start): "
        f"{snapshot_ts.isoformat() if snapshot_ts else 'n/a'}",
        f"-- snapshot isolation: {isolation}, read only"
        + (f", pg snapshot: {tx_snapshot}" if tx_snapshot else ""),
        f"-- tables: {len(schema.tables)}",
        f"-- rows: {total}",
        f"-- rows per table: {per_table}",
        "-- restore into an empty database:",
        "--   psql -v ON_ERROR_STOP=1 -d <empty_db> -f <this file>",
        "-- ================================================================",
        "",
        "SET client_encoding = 'UTF8';",
        "SET standard_conforming_strings = on;",
        "",
        "BEGIN;",
        "",
    ]
    for name in sorted(schema.tables):
        out.append(_create_table_ddl(schema.tables[name]))
        out.append("")
    for name in sorted(schema.tables):
        if fetched.get(name):
            out.append(_insert_sql(schema.tables[name], fetched[name]))
            out.append("")
    for name in sorted(schema.tables):
        for fk in sorted(schema.tables[name].fkeys, key=lambda f: f.name):
            out.append(_add_fk_sql(fk))
    out += ["", "COMMIT;", ""]
    return "\n".join(out)

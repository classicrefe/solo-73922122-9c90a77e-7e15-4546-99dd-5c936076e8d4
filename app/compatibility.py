"""Target-schema compatibility check for the migration workflow.

Before exporting a referential subset, ``POST /api/compatibility/check``
answers: *can the target database host the current ``public`` schema?*

The caller submits a candidate **target schema** as JSON with the same
structure that ``GET /api/tables`` returns (its response can be edited and
posted back).  The service reads the source schema inside the same kind of
read-only snapshot transaction used for exports and compares tables, column
types/nullability, primary keys, unique constraints and foreign keys.

Severity model
--------------
blocking
    The target cannot hold the source structure or closure data:

    * ``missing_table`` / ``missing_column`` -- rows have nowhere to go;
    * ``type_narrowed`` -- integer type narrowed (``bigint`` -> ``integer``
      -> ``smallint``); ``type_incompatible`` -- ``text`` -> integer, or
      integer -> ``text`` on a column that participates in foreign keys
      (PostgreSQL cannot declare FK constraints across integer/text);
    * ``nullability_narrowed`` -- source allows NULL, target says NOT NULL;
    * ``missing_primary_key`` / ``primary_key_mismatch``;
    * ``missing_unique_constraint`` -- when a foreign key references those
      columns (the FK becomes undeclarable in the target).

    Constraint impact is propagated along the source foreign-key dependency
    graph: every blocking issue carries ``dependency_paths`` naming the
    affected foreign-key edges, e.g.
    ``users(team_id) -> teams(id) [users_team_fk]``.

warning
    Differences that do not prevent loading but weaken guarantees or may
    reject data: unreferenced unique constraints missing, foreign keys
    missing in the target, integer -> text on columns outside the FK graph,
    and constraints the target adds on top of the source (extra NOT NULL
    columns, extra unique constraints, extra foreign keys).

The target is ``compatible`` iff no blocking issue is found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from exporter import ALLOWED_TYPES, ExportError, ForeignKey, Schema

_INT_RANK = {"smallint": 1, "integer": 2, "bigint": 3}


# --------------------------------------------------------------------------
# Request model: same JSON structure as the GET /api/tables response
# (extra top-level keys such as "schema"/"limits" are ignored, so a
# /api/tables response can be edited and posted back as-is).
# --------------------------------------------------------------------------


class TargetColumn(BaseModel):
    name: str = Field(..., min_length=1)
    type: Literal["smallint", "integer", "bigint", "text"]
    not_null: bool = False


class TargetUniqueConstraint(BaseModel):
    name: str = Field(..., min_length=1)
    columns: list[str] = Field(..., min_length=1)


class TargetForeignKeyReference(BaseModel):
    table: str = Field(..., min_length=1)
    columns: list[str] = Field(..., min_length=1)


class TargetForeignKey(BaseModel):
    name: str = Field(..., min_length=1)
    columns: list[str] = Field(..., min_length=1)
    references: TargetForeignKeyReference


class TargetTable(BaseModel):
    name: str = Field(..., min_length=1)
    primary_key: str | None = None
    columns: list[TargetColumn] = Field(..., min_length=1)
    unique_constraints: list[TargetUniqueConstraint] = []
    foreign_keys: list[TargetForeignKey] = []


class CompatibilityRequest(BaseModel):
    """Candidate target schema, in the GET /api/tables response shape."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tables": [
                        {
                            "name": "teams",
                            "primary_key": "id",
                            "columns": [
                                {"name": "id", "type": "integer", "not_null": True},
                                {"name": "name", "type": "text", "not_null": True},
                                {"name": "lead_id", "type": "integer", "not_null": False},
                            ],
                            "unique_constraints": [
                                {"name": "teams_name_key", "columns": ["name"]}
                            ],
                            "foreign_keys": [
                                {
                                    "name": "teams_lead_fk",
                                    "columns": ["lead_id"],
                                    "references": {"table": "users", "columns": ["id"]},
                                }
                            ],
                        },
                        {
                            "name": "users",
                            "primary_key": "id",
                            "columns": [
                                {"name": "id", "type": "integer", "not_null": True},
                                {"name": "name", "type": "text", "not_null": True},
                                {"name": "team_id", "type": "integer", "not_null": False},
                            ],
                            "unique_constraints": [],
                            "foreign_keys": [
                                {
                                    "name": "users_team_fk",
                                    "columns": ["team_id"],
                                    "references": {"table": "teams", "columns": ["id"]},
                                }
                            ],
                        },
                    ]
                }
            ]
        }
    )

    tables: list[TargetTable] = Field(..., min_length=1)


@dataclass
class TargetSchema:
    tables: dict[str, TargetTable]


def parse_target(req: CompatibilityRequest) -> TargetSchema:
    """Validate the submitted target schema semantically (format errors -> 422).

    Shape errors (missing fields, unknown column types, ...) are rejected
    earlier by pydantic.  Here we reject everything that makes a table
    description self-inconsistent: duplicate names, primary-key / unique /
    foreign-key columns that are not columns of their own table, and
    foreign keys whose column count does not match the referenced list.

    Foreign keys may reference tables or columns that are absent from the
    submitted document: that is not a format error but exactly the kind of
    "the target lacks this" situation the comparison reports (e.g. dropping
    the `teams` entry is how a caller expresses "the target has no teams").
    """
    tables: dict[str, TargetTable] = {}
    for t in req.tables:
        if t.name in tables:
            raise ExportError(f"duplicate table '{t.name}' in target schema", 422)
        tables[t.name] = t

    for t in req.tables:
        colnames = [c.name for c in t.columns]
        dups = sorted({n for n in colnames if colnames.count(n) > 1})
        if dups:
            raise ExportError(f"table '{t.name}': duplicate column(s) {dups}", 422)
        cols = set(colnames)
        if t.primary_key is not None and t.primary_key not in cols:
            raise ExportError(
                f"table '{t.name}': primary key column '{t.primary_key}' "
                f"is not a column of the table",
                422,
            )
        for uq in t.unique_constraints:
            unknown = [c for c in uq.columns if c not in cols]
            if unknown:
                raise ExportError(
                    f"table '{t.name}': unique constraint '{uq.name}' "
                    f"references unknown column(s) {unknown}",
                    422,
                )
        for fk in t.foreign_keys:
            unknown = [c for c in fk.columns if c not in cols]
            if unknown:
                raise ExportError(
                    f"table '{t.name}': foreign key '{fk.name}' "
                    f"uses unknown column(s) {unknown}",
                    422,
                )
            if len(fk.columns) != len(fk.references.columns):
                raise ExportError(
                    f"table '{t.name}': foreign key '{fk.name}' has "
                    f"{len(fk.columns)} column(s) but references "
                    f"{len(fk.references.columns)} column(s)",
                    422,
                )
    return TargetSchema(tables=tables)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass
class Issue:
    severity: str  # "blocking" | "warning"
    category: str
    table: str
    reason: str
    column: str | None = None
    constraint: str | None = None
    dependency_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "table": self.table,
            "column": self.column,
            "constraint": self.constraint,
            "reason": self.reason,
            "dependency_paths": self.dependency_paths,
        }


def _edge(fk: ForeignKey) -> str:
    """Render one FK dependency edge: child(col) -> parent(col) [constraint]."""
    left = f"{fk.table}({', '.join(fk.columns)})"
    right = f"{fk.ref_table}({', '.join(fk.ref_columns)})"
    return f"{left} -> {right} [{fk.name}]"


def compare_schema(source: Schema, target: TargetSchema) -> list[Issue]:
    """Compare the live source schema against the candidate target schema.

    `source` must already have passed validate_schema(); the result is sorted
    with blocking issues first, then warnings.
    """
    issues: list[Issue] = []

    # FK dependency index over the SOURCE schema (child -> parent edges).
    incoming: dict[str, list[ForeignKey]] = {name: [] for name in source.tables}
    outgoing: dict[str, list[ForeignKey]] = {name: [] for name in source.tables}
    for t in source.tables.values():
        for fk in t.fkeys:
            outgoing[t.name].append(fk)
            incoming[fk.ref_table].append(fk)

    def paths_for_table(name: str) -> list[str]:
        return [_edge(fk) for fk in outgoing[name]] + [
            _edge(fk) for fk in incoming[name]
        ]

    def paths_for_column(table: str, column: str) -> list[str]:
        edges = [fk for fk in outgoing[table] if column in fk.columns]
        edges += [fk for fk in incoming[table] if column in fk.ref_columns]
        return [_edge(fk) for fk in edges]

    for name in sorted(source.tables):
        st = source.tables[name]
        tt = target.tables.get(name)
        if tt is None:
            issues.append(
                Issue(
                    "blocking",
                    "missing_table",
                    name,
                    reason=f"target schema has no table '{name}'; its rows and "
                    f"constraints cannot be loaded",
                    dependency_paths=paths_for_table(name),
                )
            )
            continue

        tcols = {c.name: c for c in tt.columns}
        src_pk_col = st.pk.columns[0]  # validate_schema: single-column pk

        # --- columns: presence, type, nullability ---
        for sc in st.columns:
            tc = tcols.get(sc.name)
            if tc is None:
                extra = " (primary-key column)" if sc.name == src_pk_col else ""
                issues.append(
                    Issue(
                        "blocking",
                        "missing_column",
                        name,
                        column=sc.name,
                        reason=f"target table '{name}' lacks column "
                        f"'{sc.name}'{extra}",
                        dependency_paths=paths_for_column(name, sc.name),
                    )
                )
                continue
            src_type = ALLOWED_TYPES[sc.type_name]
            tgt_type = tc.type
            if src_type != tgt_type:
                paths = paths_for_column(name, sc.name)
                if src_type == "text":
                    issues.append(
                        Issue(
                            "blocking",
                            "type_incompatible",
                            name,
                            column=sc.name,
                            reason=f"column '{sc.name}': source type text cannot "
                            f"be loaded into target type {tgt_type}",
                            dependency_paths=paths,
                        )
                    )
                elif tgt_type == "text":
                    if paths:
                        issues.append(
                            Issue(
                                "blocking",
                                "type_incompatible",
                                name,
                                column=sc.name,
                                reason=f"column '{sc.name}': source type {src_type} "
                                f"becomes text in target; foreign keys cannot be "
                                f"declared across integer/text types",
                                dependency_paths=paths,
                            )
                        )
                    else:
                        issues.append(
                            Issue(
                                "warning",
                                "type_changed",
                                name,
                                column=sc.name,
                                reason=f"column '{sc.name}': source type {src_type} "
                                f"becomes text in target; values fit but the "
                                f"type changes",
                            )
                        )
                elif _INT_RANK[tgt_type] < _INT_RANK[src_type]:
                    issues.append(
                        Issue(
                            "blocking",
                            "type_narrowed",
                            name,
                            column=sc.name,
                            reason=f"column '{sc.name}': target type {tgt_type} "
                            f"cannot hold all source {src_type} values",
                            dependency_paths=paths,
                        )
                    )
                # wider integer target type: safe, no issue
            if not sc.not_null and tc.not_null:
                issues.append(
                    Issue(
                        "blocking",
                        "nullability_narrowed",
                        name,
                        column=sc.name,
                        reason=f"column '{sc.name}': source allows NULL but the "
                        f"target defines NOT NULL",
                        dependency_paths=paths_for_column(name, sc.name),
                    )
                )

        # --- primary key (skipped when the pk column itself is missing:
        # the missing_column issue above already covers it) ---
        if src_pk_col in tcols:
            ref_paths = [
                _edge(fk) for fk in incoming[name] if src_pk_col in fk.ref_columns
            ]
            if tt.primary_key is None:
                issues.append(
                    Issue(
                        "blocking",
                        "missing_primary_key",
                        name,
                        constraint=st.pk.name,
                        reason=f"target table '{name}' has no primary key; the "
                        f"source requires PRIMARY KEY ({src_pk_col})",
                        dependency_paths=ref_paths,
                    )
                )
            elif tt.primary_key != src_pk_col:
                issues.append(
                    Issue(
                        "blocking",
                        "primary_key_mismatch",
                        name,
                        constraint=st.pk.name,
                        reason=f"target table '{name}' primary key is "
                        f"({tt.primary_key}); the source requires ({src_pk_col})",
                        dependency_paths=ref_paths,
                    )
                )

        # --- unique constraints (matched by column set, not by name) ---
        tgt_uq_sets = [frozenset(u.columns) for u in tt.unique_constraints]
        for uq in st.uniques:
            if any(c not in tcols for c in uq.columns):
                continue  # covered by missing_column above
            if frozenset(uq.columns) in tgt_uq_sets:
                continue
            refs = [
                fk
                for fk in incoming[name]
                if frozenset(fk.ref_columns) == frozenset(uq.columns)
            ]
            if refs:
                issues.append(
                    Issue(
                        "blocking",
                        "missing_unique_constraint",
                        name,
                        constraint=uq.name,
                        reason=f"target table '{name}' lacks UNIQUE "
                        f"({', '.join(uq.columns)}) required by foreign key(s) "
                        f"{', '.join(fk.name for fk in refs)}",
                        dependency_paths=[_edge(fk) for fk in refs],
                    )
                )
            else:
                issues.append(
                    Issue(
                        "warning",
                        "missing_unique_constraint",
                        name,
                        constraint=uq.name,
                        reason=f"target table '{name}' lacks UNIQUE "
                        f"({', '.join(uq.columns)}); no foreign key depends on "
                        f"it, but the uniqueness guarantee is lost",
                    )
                )

        # --- foreign keys (matched by columns + referenced target) ---
        for fk in st.fkeys:
            if any(c not in tcols for c in fk.columns):
                continue  # covered by missing_column on this table
            rt = target.tables.get(fk.ref_table)
            if rt is None:
                continue  # covered by missing_table on the referenced table
            rcols = {c.name for c in rt.columns}
            if any(c not in rcols for c in fk.ref_columns):
                continue  # covered by missing_column on the referenced table
            match = any(
                list(tf.columns) == list(fk.columns)
                and tf.references.table == fk.ref_table
                and list(tf.references.columns) == list(fk.ref_columns)
                for tf in tt.foreign_keys
            )
            if not match:
                issues.append(
                    Issue(
                        "warning",
                        "missing_foreign_key",
                        name,
                        constraint=fk.name,
                        reason=f"target table '{name}' lacks foreign key "
                        f"({', '.join(fk.columns)}) -> {fk.ref_table}"
                        f"({', '.join(fk.ref_columns)}); data loads but the "
                        f"referential guarantee is not enforced",
                        dependency_paths=[_edge(fk)],
                    )
                )

        # --- constraints the target adds on top of the source ---
        src_uq_sets = [frozenset(u.columns) for u in st.uniques]
        src_uq_sets.append(frozenset(st.pk.columns))
        for uq in tt.unique_constraints:
            if frozenset(uq.columns) not in src_uq_sets:
                issues.append(
                    Issue(
                        "warning",
                        "extra_unique_constraint",
                        name,
                        constraint=uq.name,
                        reason=f"target table '{name}' defines UNIQUE "
                        f"({', '.join(uq.columns)}) absent from the source; "
                        f"it may reject valid source rows",
                    )
                )
        for tfk in tt.foreign_keys:
            match = any(
                list(tfk.columns) == list(sf.columns)
                and tfk.references.table == sf.ref_table
                and list(tfk.references.columns) == list(sf.ref_columns)
                for sf in st.fkeys
            )
            if not match:
                issues.append(
                    Issue(
                        "warning",
                        "extra_foreign_key",
                        name,
                        constraint=tfk.name,
                        reason=f"target table '{name}' defines foreign key "
                        f"({', '.join(tfk.columns)}) -> {tfk.references.table}"
                        f"({', '.join(tfk.references.columns)}) absent from the "
                        f"source; it may reject valid source rows",
                    )
                )
        scols = {c.name for c in st.columns}
        for tc in tt.columns:
            if tc.name not in scols and tc.not_null:
                issues.append(
                    Issue(
                        "warning",
                        "extra_not_null_column",
                        name,
                        column=tc.name,
                        reason=f"target column '{tc.name}' is NOT NULL and "
                        f"absent from the source; exported INSERTs do not "
                        f"provide values for it",
                    )
                )

    order = {"blocking": 0, "warning": 1}
    issues.sort(
        key=lambda i: (
            order[i.severity],
            i.table,
            i.category,
            i.column or "",
            i.constraint or "",
        )
    )
    return issues

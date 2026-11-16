"""Unit tests for the exporter core (no database required)."""

import unittest

from exporter import (
    MAX_ROWS,
    Column,
    ExportError,
    ForeignKey,
    PrimaryKey,
    Schema,
    SchemaViolation,
    Table,
    UniqueConstraint,
    compute_closure,
    render_sql,
    validate_schema,
)


def col(name, type_name="int4", not_null=False, **kw):
    return Column(name=name, type_name=type_name, not_null=not_null, **kw)


def sample_schema():
    """users <-> teams (circular), tasks self-ref, attachments composite FK."""
    users = Table(
        1,
        "users",
        [col("id", "int4", True), col("name", "text", True), col("team_id")],
        pk=PrimaryKey("users_pkey", ("id",)),
        uniques=[UniqueConstraint("users_name_key", ("name",))],
        fkeys=[
            ForeignKey("users_team_fk", "users", ("team_id",), "teams", ("id",))
        ],
    )
    teams = Table(
        2,
        "teams",
        [col("id", "int4", True), col("name", "text", True), col("lead_id")],
        pk=PrimaryKey("teams_pkey", ("id",)),
        fkeys=[
            ForeignKey("teams_lead_fk", "teams", ("lead_id",), "users", ("id",))
        ],
    )
    tasks = Table(
        3,
        "tasks",
        [col("id", "int4", True), col("title", "text", True), col("parent_id")],
        pk=PrimaryKey("tasks_pkey", ("id",)),
        fkeys=[
            ForeignKey("tasks_parent_fk", "tasks", ("parent_id",), "tasks", ("id",))
        ],
    )
    links = Table(
        4,
        "task_links",
        [col("id", "int4", True), col("task_id", "int4", True), col("label", "text", True)],
        pk=PrimaryKey("task_links_pkey", ("id",)),
        uniques=[UniqueConstraint("task_links_task_label_key", ("task_id", "label"))],
        fkeys=[
            ForeignKey("task_links_task_fk", "task_links", ("task_id",), "tasks", ("id",))
        ],
    )
    attachments = Table(
        5,
        "attachments",
        [
            col("id", "int4", True),
            col("file_name", "text", True),
            col("task_id"),
            col("label"),
        ],
        pk=PrimaryKey("attachments_pkey", ("id",)),
        fkeys=[
            ForeignKey(
                "attachments_task_fk",
                "attachments",
                ("task_id", "label"),
                "task_links",
                ("task_id", "label"),
            )
        ],
    )
    return Schema(tables={t.name: t for t in [users, teams, tasks, links, attachments]})


class FakeDB:
    """fetch_rows stand-in: serves rows from an in-memory dataset."""

    def __init__(self, data):
        self.data = data  # {table_name: [row, ...]}

    def fetch(self, table, cols, keys):
        wanted = set(keys)
        return [
            dict(row)
            for row in self.data.get(table.name, [])
            if tuple(row[c] for c in cols) in wanted
        ]


def sample_db():
    return FakeDB(
        {
            "users": [
                {"id": 1, "name": "alice", "team_id": 1},
                {"id": 2, "name": "bob", "team_id": 1},
                {"id": 5, "name": "erin", "team_id": None},
            ],
            "teams": [
                {"id": 1, "name": "core", "lead_id": 2},
            ],
            "tasks": [
                {"id": 100, "title": "design", "parent_id": None},
                {"id": 101, "title": "build", "parent_id": 100},
                {"id": 102, "title": "test", "parent_id": 101},
            ],
            "task_links": [
                {"id": 1000, "task_id": 100, "label": "spec"},
            ],
            "attachments": [
                {"id": 5000, "file_name": "spec.pdf", "task_id": 100, "label": "spec"},
                {"id": 5001, "file_name": "notes.txt", "task_id": 100, "label": None},
            ],
        }
    )


class ValidateTests(unittest.TestCase):
    def test_valid_schema_passes(self):
        validate_schema(sample_schema())

    def test_too_many_tables(self):
        schema = sample_schema()
        for i in range(6, 9):
            schema.tables[f"extra{i}"] = Table(
                100 + i, f"extra{i}", [col("id", "int4", True)],
                pk=PrimaryKey(f"extra{i}_pkey", ("id",)),
            )
        with self.assertRaises(SchemaViolation):
            validate_schema(schema)

    def test_unsupported_column_type(self):
        schema = sample_schema()
        schema.tables["users"].columns.append(col("active", "bool"))
        with self.assertRaises(SchemaViolation):
            validate_schema(schema)

    def test_column_default_rejected(self):
        schema = sample_schema()
        schema.tables["users"].columns[1].has_default = True
        with self.assertRaises(SchemaViolation):
            validate_schema(schema)

    def test_composite_pk_rejected(self):
        schema = sample_schema()
        schema.tables["users"].pk = PrimaryKey("users_pkey", ("id", "name"))
        with self.assertRaises(SchemaViolation):
            validate_schema(schema)

    def test_text_pk_rejected(self):
        schema = sample_schema()
        schema.tables["users"].pk = PrimaryKey("users_pkey", ("name",))
        with self.assertRaises(SchemaViolation):
            validate_schema(schema)


class ClosureTests(unittest.TestCase):
    def test_circular_reference_terminates(self):
        fetched = compute_closure(sample_schema(), "users", [1], sample_db().fetch)
        self.assertEqual(sorted(fetched["users"]), [1, 2])
        self.assertEqual(sorted(fetched["teams"]), [1])
        self.assertEqual(fetched["tasks"], {})

    def test_unrelated_rows_not_copied(self):
        fetched = compute_closure(sample_schema(), "users", [1], sample_db().fetch)
        names = {r["name"] for r in fetched["users"].values()}
        self.assertNotIn("erin", names)

    def test_self_reference_chain(self):
        fetched = compute_closure(sample_schema(), "tasks", [102], sample_db().fetch)
        self.assertEqual(sorted(fetched["tasks"]), [100, 101, 102])

    def test_nullable_composite_fk(self):
        # 5000 resolves (100,'spec'); 5001 has NULL label -> nothing to resolve.
        fetched = compute_closure(
            sample_schema(), "attachments", [5000, 5001], sample_db().fetch
        )
        self.assertEqual(sorted(fetched["attachments"]), [5000, 5001])
        self.assertEqual(sorted(fetched["task_links"]), [1000])
        self.assertEqual(sorted(fetched["tasks"]), [100])

    def test_dangling_reference_fails(self):
        db = sample_db()
        db.data["tasks"] = [{"id": 200, "title": "orphan", "parent_id": 999}]
        with self.assertRaisesRegex(ExportError, "unresolvable reference"):
            compute_closure(sample_schema(), "tasks", [200], db.fetch)

    def test_missing_seed_fails_404(self):
        with self.assertRaises(ExportError) as ctx:
            compute_closure(sample_schema(), "users", [999], sample_db().fetch)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_seed_table_fails_404(self):
        with self.assertRaises(ExportError) as ctx:
            compute_closure(sample_schema(), "nope", [1], sample_db().fetch)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_row_limit_fails(self):
        db = FakeDB(
            {"tasks": [
                {"id": i, "title": f"t{i}", "parent_id": i - 1 if i > 1 else None}
                for i in range(1, MAX_ROWS + 2)
            ]}
        )
        with self.assertRaisesRegex(ExportError, "exceeds"):
            compute_closure(sample_schema(), "tasks", [MAX_ROWS + 1], db.fetch)


class RenderTests(unittest.TestCase):
    def render(self):
        schema = sample_schema()
        fetched = compute_closure(
            schema, "attachments", [5000, 5001], sample_db().fetch
        )
        return render_sql(schema, fetched, "attachments", [5000, 5001])

    def test_null_and_quote_escaping(self):
        schema = sample_schema()
        db = sample_db()
        db.data["users"][0]["name"] = "o'hara"
        db.data["users"][0]["team_id"] = None
        fetched = compute_closure(schema, "users", [1], db.fetch)
        sql_text = render_sql(schema, fetched, "users", [1])
        self.assertIn("'o''hara'", sql_text)
        self.assertIn("NULL", sql_text)

    def test_ddl_before_data_before_fks(self):
        sql_text = self.render()
        self.assertLess(sql_text.index("CREATE TABLE"), sql_text.index("INSERT INTO"))
        self.assertLess(sql_text.index("INSERT INTO"), sql_text.index("ADD CONSTRAINT"))

    def test_circular_fks_added_after_data(self):
        sql_text = self.render()
        self.assertIn('ADD CONSTRAINT "users_team_fk"', sql_text)
        self.assertIn('ADD CONSTRAINT "teams_lead_fk"', sql_text)
        self.assertIn('ADD CONSTRAINT "tasks_parent_fk"', sql_text)
        self.assertIn(
            'FOREIGN KEY ("task_id", "label") REFERENCES "public"."task_links" ("task_id", "label")',
            sql_text,
        )

    def test_unique_constraints_preserved(self):
        sql_text = self.render()
        self.assertIn('CONSTRAINT "users_name_key" UNIQUE ("name")', sql_text)
        self.assertIn(
            'CONSTRAINT "task_links_task_label_key" UNIQUE ("task_id", "label")', sql_text
        )

    def test_wrapped_in_transaction(self):
        sql_text = self.render()
        self.assertIn("\nBEGIN;", sql_text)
        self.assertTrue(sql_text.rstrip().endswith("COMMIT;"))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the target-schema compatibility check (no database required)."""

import unittest

from compatibility import compare_schema, parse_target, CompatibilityRequest
from exporter import ALLOWED_TYPES, ExportError

from test_exporter import sample_schema


def schema_to_target(schema):
    """Render an exporter Schema as a /api/tables-shaped target payload."""
    tables = []
    for t in sorted(schema.tables.values(), key=lambda t: t.name):
        tables.append(
            {
                "name": t.name,
                "primary_key": t.pk.columns[0] if t.pk else None,
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
                        "references": {
                            "table": f.ref_table,
                            "columns": list(f.ref_columns),
                        },
                    }
                    for f in t.fkeys
                ],
            }
        )
    return {"tables": tables}


def check(schema, target_dict):
    """Parse + compare; returns the issue list."""
    target = parse_target(CompatibilityRequest(**target_dict))
    return compare_schema(schema, target)


def table(target_dict, name):
    return next(t for t in target_dict["tables"] if t["name"] == name)


def column(target_dict, tbl, name):
    return next(c for c in table(target_dict, tbl)["columns"] if c["name"] == name)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.schema = sample_schema()
        self.target = schema_to_target(self.schema)

    def issues(self, target=None):
        return check(self.schema, target if target is not None else self.target)

    def test_identical_schema_has_no_issues(self):
        self.assertEqual(self.issues(), [])

    def test_missing_table_blocks_with_dependency_paths(self):
        self.target["tables"] = [
            t for t in self.target["tables"] if t["name"] != "teams"
        ]
        issues = self.issues()
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.severity, "blocking")
        self.assertEqual(issue.category, "missing_table")
        self.assertEqual(issue.table, "teams")
        # both circular edges touching teams are reported
        self.assertIn(
            "users(team_id) -> teams(id) [users_team_fk]", issue.dependency_paths
        )
        self.assertIn(
            "teams(lead_id) -> users(id) [teams_lead_fk]", issue.dependency_paths
        )

    def test_missing_plain_column_blocks_without_paths(self):
        table(self.target, "tasks")["columns"] = [
            c for c in table(self.target, "tasks")["columns"] if c["name"] != "title"
        ]
        (issue,) = self.issues()
        self.assertEqual((issue.severity, issue.category), ("blocking", "missing_column"))
        self.assertEqual(issue.column, "title")
        self.assertEqual(issue.dependency_paths, [])

    def test_missing_pk_column_reported_once_as_missing_column(self):
        table(self.target, "teams")["columns"] = [
            c for c in table(self.target, "teams")["columns"] if c["name"] != "id"
        ]
        table(self.target, "teams")["primary_key"] = None
        issues = self.issues()
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.category, "missing_column")
        self.assertIn("primary-key", issue.reason)
        self.assertIn(
            "users(team_id) -> teams(id) [users_team_fk]", issue.dependency_paths
        )

    def test_type_narrowing_blocks(self):
        column(self.target, "users", "id")["type"] = "smallint"
        (issue,) = self.issues()
        self.assertEqual((issue.severity, issue.category), ("blocking", "type_narrowed"))
        self.assertEqual(issue.column, "id")

    def test_type_widening_is_safe(self):
        column(self.target, "users", "id")["type"] = "bigint"
        self.assertEqual(self.issues(), [])

    def test_text_to_int_blocks(self):
        column(self.target, "users", "name")["type"] = "integer"
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("blocking", "type_incompatible")
        )

    def test_int_to_text_on_fk_column_blocks(self):
        # users.id is referenced by teams_lead_fk: FKs cannot span int/text.
        column(self.target, "users", "id")["type"] = "text"
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("blocking", "type_incompatible")
        )
        self.assertIn(
            "teams(lead_id) -> users(id) [teams_lead_fk]", issue.dependency_paths
        )

    def test_int_to_text_on_plain_column_warns(self):
        from exporter import Column

        schema = sample_schema()
        schema.tables["tasks"].columns.append(Column("priority", "int4", False))
        target = schema_to_target(schema)
        column(target, "tasks", "priority")["type"] = "text"
        (issue,) = check(schema, target)
        self.assertEqual((issue.severity, issue.category), ("warning", "type_changed"))

    def test_nullability_narrowing_blocks(self):
        column(self.target, "users", "team_id")["not_null"] = True
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("blocking", "nullability_narrowed")
        )

    def test_nullability_widening_is_safe(self):
        column(self.target, "users", "name")["not_null"] = False
        self.assertEqual(self.issues(), [])

    def test_missing_primary_key_blocks_with_paths(self):
        table(self.target, "teams")["primary_key"] = None
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("blocking", "missing_primary_key")
        )
        self.assertEqual(issue.constraint, "teams_pkey")
        self.assertIn(
            "users(team_id) -> teams(id) [users_team_fk]", issue.dependency_paths
        )

    def test_primary_key_mismatch_blocks(self):
        table(self.target, "teams")["primary_key"] = "name"
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("blocking", "primary_key_mismatch")
        )

    def test_missing_referenced_unique_blocks_with_path(self):
        table(self.target, "task_links")["unique_constraints"] = []
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category),
            ("blocking", "missing_unique_constraint"),
        )
        self.assertEqual(issue.constraint, "task_links_task_label_key")
        self.assertIn(
            "attachments(task_id, label) -> task_links(task_id, label) "
            "[attachments_task_fk]",
            issue.dependency_paths,
        )

    def test_missing_unreferenced_unique_warns(self):
        table(self.target, "users")["unique_constraints"] = []
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category),
            ("warning", "missing_unique_constraint"),
        )
        self.assertEqual(issue.dependency_paths, [])

    def test_missing_foreign_key_warns(self):
        table(self.target, "users")["foreign_keys"] = []
        (issue,) = self.issues()
        self.assertEqual(
            (issue.severity, issue.category), ("warning", "missing_foreign_key")
        )
        self.assertEqual(issue.constraint, "users_team_fk")
        self.assertEqual(
            issue.dependency_paths, ["users(team_id) -> teams(id) [users_team_fk]"]
        )

    def test_missing_fk_not_reported_when_ref_table_missing(self):
        # the missing_table issue already lists users_team_fk as affected
        self.target["tables"] = [
            t for t in self.target["tables"] if t["name"] != "teams"
        ]
        categories = [i.category for i in self.issues()]
        self.assertEqual(categories, ["missing_table"])

    def test_extra_constraints_and_not_null_column_warn(self):
        table(self.target, "teams")["unique_constraints"].append(
            {"name": "teams_name_key", "columns": ["name"]}
        )
        table(self.target, "tasks")["foreign_keys"].append(
            {
                "name": "tasks_title_fk",
                "columns": ["title"],
                "references": {"table": "users", "columns": ["name"]},
            }
        )
        table(self.target, "users")["columns"].append(
            {"name": "tenant", "type": "text", "not_null": True}
        )
        categories = sorted(i.category for i in self.issues())
        self.assertEqual(
            categories,
            ["extra_foreign_key", "extra_not_null_column", "extra_unique_constraint"],
        )
        self.assertTrue(all(i.severity == "warning" for i in self.issues()))

    def test_blocking_sorts_before_warnings(self):
        table(self.target, "users")["unique_constraints"] = []  # warning
        self.target["tables"] = [
            t for t in self.target["tables"] if t["name"] != "teams"
        ]  # blocking
        issues = self.issues()
        self.assertEqual([i.severity for i in issues], ["blocking", "warning"])


class ParseTargetTests(unittest.TestCase):
    def base(self):
        return schema_to_target(sample_schema())

    def parse(self, doc):
        return parse_target(CompatibilityRequest(**doc))

    def assert_422(self, doc, needle):
        with self.assertRaises(ExportError) as ctx:
            self.parse(doc)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn(needle, ctx.exception.detail)

    def test_valid_target_parses(self):
        target = self.parse(self.base())
        self.assertEqual(sorted(target.tables), sorted(sample_schema().tables))

    def test_extra_top_level_keys_ignored(self):
        doc = self.base()
        doc["schema"] = "public"
        doc["limits"] = {"max_tables": 6, "max_rows": 1000}
        self.parse(doc)  # a /api/tables response can be posted back as-is

    def test_duplicate_table_rejected(self):
        doc = self.base()
        doc["tables"].append(doc["tables"][0])
        self.assert_422(doc, "duplicate table")

    def test_duplicate_column_rejected(self):
        doc = self.base()
        users = table(doc, "users")
        users["columns"].append(users["columns"][0])
        self.assert_422(doc, "duplicate column")

    def test_primary_key_must_be_a_column(self):
        doc = self.base()
        table(doc, "users")["primary_key"] = "nope"
        self.assert_422(doc, "primary key column 'nope'")

    def test_unique_constraint_unknown_column(self):
        doc = self.base()
        table(doc, "users")["unique_constraints"].append(
            {"name": "bad_key", "columns": ["nope"]}
        )
        self.assert_422(doc, "unknown column")

    def test_fk_unknown_child_column(self):
        doc = self.base()
        table(doc, "users")["foreign_keys"][0]["columns"] = ["nope"]
        self.assert_422(doc, "unknown column")

    def test_fk_arity_mismatch(self):
        doc = self.base()
        table(doc, "users")["foreign_keys"][0]["references"]["columns"] = ["id", "name"]
        self.assert_422(doc, "column(s) but references")

    def test_fk_referencing_absent_table_is_a_finding_not_a_format_error(self):
        # dropping the teams entry is how a caller says "target has no teams";
        # the dangling target FK is allowed and comparison reports the absence
        doc = self.base()
        doc["tables"] = [t for t in doc["tables"] if t["name"] != "teams"]
        target = self.parse(doc)  # must not raise
        issues = compare_schema(sample_schema(), target)
        self.assertEqual([i.category for i in issues], ["missing_table"])

    def test_fk_referencing_absent_column_is_a_finding_not_a_format_error(self):
        doc = self.base()
        teams = table(doc, "teams")
        teams["columns"] = [c for c in teams["columns"] if c["name"] != "id"]
        teams["primary_key"] = None
        target = self.parse(doc)  # users_team_fk -> teams(id) dangles: allowed
        issues = compare_schema(sample_schema(), target)
        self.assertEqual([i.category for i in issues], ["missing_column"])


if __name__ == "__main__":
    unittest.main()

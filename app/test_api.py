"""API-level tests for POST /api/compatibility/check (database access stubbed)."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from test_compatibility import sample_schema, schema_to_target

SNAP = {
    "time": "2026-11-16T00:00:00+00:00",
    "isolation": "repeatable read, read only",
    "tx_snapshot": "10:12:",
}


class CompatibilityApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.schema = sample_schema()

    def post(self, payload):
        with patch.object(
            main, "_snapshot_schema", return_value=(self.schema, SNAP)
        ):
            return self.client.post("/api/compatibility/check", json=payload)

    def test_identical_target_is_compatible(self):
        resp = self.post(schema_to_target(self.schema))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["compatible"])
        self.assertEqual(body["summary"], {"blocking": 0, "warning": 0})
        self.assertEqual(body["issues"], [])
        self.assertEqual(body["schema"], "public")
        self.assertEqual(body["source_tables"], len(self.schema.tables))
        self.assertEqual(body["snapshot"]["isolation"], "repeatable read, read only")

    def test_broken_target_reports_blocking_with_dependency_paths(self):
        target = schema_to_target(self.schema)
        target["tables"] = [t for t in target["tables"] if t["name"] != "teams"]
        for t in target["tables"]:
            if t["name"] == "task_links":
                t["unique_constraints"] = []
            if t["name"] == "attachments":
                t["foreign_keys"] = []
        resp = self.post(target)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["compatible"])
        self.assertEqual(body["summary"], {"blocking": 2, "warning": 1})
        by_cat = {i["category"]: i for i in body["issues"]}
        self.assertEqual(
            set(by_cat),
            {"missing_table", "missing_unique_constraint", "missing_foreign_key"},
        )
        self.assertEqual(by_cat["missing_table"]["severity"], "blocking")
        self.assertTrue(
            any(
                "users_team_fk" in p
                for p in by_cat["missing_table"]["dependency_paths"]
            )
        )
        self.assertEqual(by_cat["missing_unique_constraint"]["severity"], "blocking")
        self.assertTrue(
            any(
                "attachments_task_fk" in p
                for p in by_cat["missing_unique_constraint"]["dependency_paths"]
            )
        )
        self.assertEqual(by_cat["missing_foreign_key"]["severity"], "warning")

    def test_shape_error_422_before_any_db_access(self):
        with patch.object(
            main, "_snapshot_schema", side_effect=AssertionError("db touched")
        ):
            resp = self.client.post(
                "/api/compatibility/check", json={"tables": [{"name": "x"}]}
            )
        self.assertEqual(resp.status_code, 422)

    def test_unknown_column_type_422(self):
        target = schema_to_target(self.schema)
        target["tables"][0]["columns"][0]["type"] = "jsonb"
        self.assertEqual(self.post(target).status_code, 422)

    def test_semantic_error_422(self):
        target = schema_to_target(self.schema)
        target["tables"].append(target["tables"][0])  # duplicate table
        resp = self.post(target)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate table", resp.json()["detail"])

    def test_empty_body_422(self):
        self.assertEqual(self.post({}).status_code, 422)

    def test_existing_routes_preserved(self):
        paths = {r.path for r in main.app.routes}
        for p in (
            "/healthz",
            "/api/tables",
            "/api/export",
            "/api/compatibility/check",
            "/docs",
        ):
            self.assertIn(p, paths)

    def test_docs_available(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/api/compatibility/check", spec["paths"])
        self.assertIn("/api/export", spec["paths"])


if __name__ == "__main__":
    unittest.main()

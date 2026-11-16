#!/usr/bin/env bash
# End-to-end demo for pg-subset-exporter:
#   1. circular reference export (users <-> teams)
#   2. nullable composite FK export (attachments -> task_links)
#   3. self-reference chain export (tasks)
#   4. restore of the SQL package into a fresh empty database (all FKs valid)
#   5. concurrent writes during exports (source never blocked)
#   6. failure semantics: missing seed, unknown table, >1000-row closure
#
# Defaults target `docker compose up`; override with env vars for a local run:
#   APP_URL=http://127.0.0.1:8000 PSQL=/path/to/psql DB_PORT=55432 \
#   DB_PASSWORD=exporter WRITER_LOG=/tmp/writer.log ./scripts/demo.sh
set -euo pipefail

APP_URL=${APP_URL:-http://localhost:8000}
DB_USER=${DB_USER:-exporter}
DB_PASSWORD=${DB_PASSWORD:-exporter}
DB_NAME=${DB_NAME:-source}
DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}
COMPOSE=${COMPOSE:-docker compose}
ARTIFACTS=${ARTIFACTS:-artifacts}
RESTORE_DB=${RESTORE_DB:-restore_check}  # scratch database used for the restore check
PSQL=${PSQL:-}            # local psql binary; default: docker compose exec db psql
WRITER_LOG=${WRITER_LOG:-} # writer log file; default: docker compose logs writer

mkdir -p "$ARTIFACTS"
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '   ok: %s\n' "$*"; }
die()  { printf '   FAIL: %s\n' "$*" >&2; exit 1; }

psql_db() { # psql_db <database> [psql-args...]
  local db=$1; shift
  if [ -n "$PSQL" ]; then
    PGPASSWORD="$DB_PASSWORD" "$PSQL" -v ON_ERROR_STOP=1 -X \
      -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$db" "$@"
  else
    $COMPOSE exec -T db psql -v ON_ERROR_STOP=1 -X -U "$DB_USER" -d "$db" "$@"
  fi
}

writer_commits() {
  if [ -n "$WRITER_LOG" ]; then
    grep -c 'committed' "$WRITER_LOG" 2>/dev/null || true
  else
    $COMPOSE logs --no-color writer 2>/dev/null | grep -c 'committed' || true
  fi
}

post_export() { # post_export <table> <pks-json> <outfile> -> prints http code
  curl -sS -o "$3" -w '%{http_code}' \
    -X POST "$APP_URL/api/export" \
    -H 'Content-Type: application/json' \
    -d "{\"table\": \"$1\", \"pks\": $2}"
}

step "0. wait for the API"
for i in $(seq 1 60); do
  curl -sf "$APP_URL/healthz" >/dev/null 2>&1 && break
  [ "$i" = 60 ] && die "API did not become healthy at $APP_URL"
  sleep 2
done
curl -sf "$APP_URL/docs" >/dev/null || die "/docs not reachable"
ok "API healthy, /docs available at $APP_URL/docs"

step "1. schema introspection (GET /api/tables)"
tables=$(curl -sf "$APP_URL/api/tables")
echo "$tables" | grep -q '"users"' || die "users table missing from /api/tables"
n_tables=$(echo "$tables" | grep -o '"name"' | wc -l)
ok "schema public exposes tables: $(echo "$tables" | tr -d '\n' | grep -o '"name": *"[a-z_]*"' | head -8 | tr '\n' ' ')"

step "2. circular reference export (seed users.id=1, users <-> teams)"
code=$(post_export users '[1]' "$ARTIFACTS/subset_users.sql")
[ "$code" = 200 ] || die "export failed with HTTP $code: $(cat "$ARTIFACTS/subset_users.sql")"
grep -q 'CREATE TABLE "public"."users"' "$ARTIFACTS/subset_users.sql" || die "users DDL missing"
grep -q 'CREATE TABLE "public"."teams"' "$ARTIFACTS/subset_users.sql" || die "teams DDL missing"
grep -q 'ADD CONSTRAINT "users_team_fk"' "$ARTIFACTS/subset_users.sql" || die "users->teams FK missing"
grep -q 'ADD CONSTRAINT "teams_lead_fk"' "$ARTIFACTS/subset_users.sql" || die "teams->users FK missing"
grep -q 'alice' "$ARTIFACTS/subset_users.sql" || die "seed row alice missing"
grep -q 'bob' "$ARTIFACTS/subset_users.sql" || die "referenced lead bob missing"
grep -q 'repeatable read' "$ARTIFACTS/subset_users.sql" || die "snapshot isolation marker missing"
! grep -q 'erin' "$ARTIFACTS/subset_users.sql" || die "unrelated row erin leaked into export"
ok "closure = users{1,2} + teams{1}; cycle terminates; unrelated rows excluded"

step "3. nullable composite FK export (seed attachments 5000,5001)"
code=$(post_export attachments '[5000, 5001]' "$ARTIFACTS/subset_attachments.sql")
[ "$code" = 200 ] || die "export failed with HTTP $code"
grep -q 'FOREIGN KEY ("task_id", "label") REFERENCES "public"."task_links" ("task_id", "label")' \
  "$ARTIFACTS/subset_attachments.sql" || die "composite FK DDL missing"
grep -q "'spec.pdf'" "$ARTIFACTS/subset_attachments.sql" || die "attachment 5000 missing"
grep -q "'notes.txt'" "$ARTIFACTS/subset_attachments.sql" || die "attachment 5001 missing"
grep -q '(1000, 100, '"'"'spec'"'"')' "$ARTIFACTS/subset_attachments.sql" || die "referenced task_links row missing"
grep -q 'design' "$ARTIFACTS/subset_attachments.sql" || die "referenced task missing"
ok "composite FK resolved for (100,'spec'); NULL label kept as NULL without resolution"

step "4. self-reference chain export (seed tasks.id=102)"
code=$(post_export tasks '[102]' "$ARTIFACTS/subset_tasks.sql")
[ "$code" = 200 ] || die "export failed with HTTP $code"
for title in design build "te''st"; do
  grep -q "$title" "$ARTIFACTS/subset_tasks.sql" || die "task chain row '$title' missing"
done
grep -q 'ADD CONSTRAINT "tasks_parent_fk"' "$ARTIFACTS/subset_tasks.sql" || die "self FK missing"
ok "parent chain 102 -> 101 -> 100 exported"

step "5. restore the package into a fresh empty database"
psql_db postgres -c "DROP DATABASE IF EXISTS $RESTORE_DB;" >/dev/null
psql_db postgres -c "CREATE DATABASE $RESTORE_DB;" >/dev/null
psql_db "$RESTORE_DB" < "$ARTIFACTS/subset_tasks.sql" >/dev/null \
  || die "restore failed (DDL/data/FK validation)"
invalid=$(psql_db "$RESTORE_DB" -tA -c \
  "SELECT count(*) FROM pg_constraint c
   JOIN pg_class r ON r.oid = c.conrelid
   JOIN pg_namespace n ON n.oid = r.relnamespace
   WHERE n.nspname = 'public' AND c.contype = 'f' AND NOT c.convalidated;")
[ "$invalid" = "0" ] || die "$invalid foreign keys invalid after restore"
restored=$(psql_db "$RESTORE_DB" -tA -c \
  "SELECT (SELECT count(*) FROM attachments) + (SELECT count(*) FROM projects)
        + (SELECT count(*) FROM task_links) + (SELECT count(*) FROM tasks)
        + (SELECT count(*) FROM teams) + (SELECT count(*) FROM users);")
expected=$(grep -m1 '^-- rows: ' "$ARTIFACTS/subset_tasks.sql" | awk '{print $3}')
[ "$restored" = "$expected" ] || die "row count mismatch: restored=$restored expected=$expected"
ok "restored into empty DB: $restored rows, all foreign keys valid"

step "6. concurrent writes during export (writer service is active)"
before=$(writer_commits)
for i in $(seq 1 30); do
  code=$(post_export tasks '[102]' "$ARTIFACTS/concurrent.sql")
  [ "$code" = 200 ] || die "export #$i failed with HTTP $code while writer was active"
done
after=$(writer_commits)
delta=$((after - before))
echo "   writer commits while 30 exports ran: $delta (before=$before after=$after)"
[ "$delta" -gt 0 ] || die "writer made no progress during exports (blocked?)"
ok "30/30 exports succeeded from a consistent snapshot while writes committed"

step "7. failure semantics (no partial packages)"
code=$(post_export users '[999999]' "$ARTIFACTS/err_missing.json")
[ "$code" = 404 ] || die "missing seed: expected 404, got $code"
ok "missing seed row -> HTTP 404"

code=$(post_export nope '[1]' "$ARTIFACTS/err_table.json")
[ "$code" = 404 ] || die "unknown table: expected 404, got $code"
ok "unknown table -> HTTP 404"

code=$(post_export users '[]' "$ARTIFACTS/err_empty.json")
[ "$code" = 422 ] || die "empty pks: expected 422, got $code"
ok "empty seed list -> HTTP 422"

psql_db "$DB_NAME" -c \
  "INSERT INTO tasks (id, title, project_id, assignee_id, parent_id)
   SELECT 900000 + g, 'bulk-' || g, 10, NULL,
          CASE WHEN g = 1 THEN NULL ELSE 900000 + g - 1 END
   FROM generate_series(1, 1100) g;" >/dev/null
code=$(post_export tasks '[901100]' "$ARTIFACTS/err_toobig.json")
[ "$code" = 422 ] || die "oversized closure: expected 422, got $code"
grep -qi 'exceeds' "$ARTIFACTS/err_toobig.json" || die "no limit explanation in error"
! grep -q 'CREATE TABLE' "$ARTIFACTS/err_toobig.json" || die "partial SQL package leaked"
psql_db "$DB_NAME" -c "DELETE FROM tasks WHERE id BETWEEN 900001 AND 901100;" >/dev/null
ok "closure of 1100+ rows -> HTTP 422, error is JSON, no partial SQL package"

printf '\n\033[1mALL DEMO CHECKS PASSED\033[0m\n'
echo "artifacts written to $ARTIFACTS/"

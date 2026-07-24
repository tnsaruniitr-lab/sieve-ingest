#!/bin/bash
# Run the Phase-0 drills against a disposable scratch Postgres (zonky pg16 build,
# no docker needed). Usage: tests/run-local.sh [pytest args]
set -euo pipefail

PG=${PG_BIN:-$HOME/toolchain/pg16/bin}
DATA=/tmp/sieve-ingest-testpg
PORT=54333
export SIEVE_DB_URL="postgresql://postgres@localhost:$PORT/sieve_test"

# macOS and many CI images expose only `python3`. Prefer the repo's virtualenv
# when present so the launcher uses the same dependencies as local development,
# while still allowing an explicit PYTHON override in CI.
if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"
elif [[ -x .venv-local/bin/python ]]; then
    PY=.venv-local/bin/python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

cleanup() { "$PG/pg_ctl" -D "$DATA" stop -m immediate >/dev/null 2>&1 || true; }
trap cleanup EXIT

rm -rf "$DATA"
"$PG/initdb" -D "$DATA" -U postgres --auth=trust --no-locale -E UTF8 >/dev/null
"$PG/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp -c listen_addresses=localhost" \
    -l "$DATA/pg.log" -w start >/dev/null
"$PY" - <<'EOF'
import psycopg2
c = psycopg2.connect("postgresql://postgres@localhost:54333/postgres")
c.autocommit = True
c.cursor().execute("CREATE DATABASE sieve_test")
c.close()
EOF

"$PY" -m pytest tests/ -q "$@"

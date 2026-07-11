#!/bin/bash
# Run the Phase-0 drills against a disposable scratch Postgres (zonky pg16 build,
# no docker needed). Usage: tests/run-local.sh [pytest args]
set -euo pipefail

PG=${PG_BIN:-$HOME/toolchain/pg16/bin}
DATA=/tmp/sieve-ingest-testpg
PORT=54333
export SIEVE_DB_URL="postgresql://postgres@localhost:$PORT/sieve_test"

cleanup() { "$PG/pg_ctl" -D "$DATA" stop -m immediate >/dev/null 2>&1 || true; }
trap cleanup EXIT

rm -rf "$DATA"
"$PG/initdb" -D "$DATA" -U postgres --auth=trust --no-locale -E UTF8 >/dev/null
"$PG/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp -c listen_addresses=localhost" \
    -l "$DATA/pg.log" -w start >/dev/null
python - <<'EOF'
import psycopg2
c = psycopg2.connect("postgresql://postgres@localhost:54333/postgres")
c.autocommit = True
c.cursor().execute("CREATE DATABASE sieve_test")
c.close()
EOF

python -m pytest tests/ -q "$@"

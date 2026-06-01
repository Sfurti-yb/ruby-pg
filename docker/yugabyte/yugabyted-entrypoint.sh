#!/bin/bash
# Custom entrypoint for the yugabyte service.
# Builds --tserver_flags dynamically from environment variables, then execs yugabyted.
#
# Environment variables read:
#   YB_ENABLE_YSQL_CONN_MGR  1/true → add enable_ysql_conn_mgr=true
set -euo pipefail

# ---------------------------------------------------------------------------
# Base tserver flags – always applied
#
# logical_decoding_work_mem=64kB: keeps memory footprint low and matches
# what the Npgsql/C# stack uses (consistent baseline).
# ---------------------------------------------------------------------------
TSERVER_FLAGS="ysql_pg_conf_csv=logical_decoding_work_mem=64kB"

# ---------------------------------------------------------------------------
# Optional: YSQL connection manager
# ---------------------------------------------------------------------------
if [ "${YB_ENABLE_YSQL_CONN_MGR:-0}" != "0" ]; then
    TSERVER_FLAGS="${TSERVER_FLAGS},enable_ysql_conn_mgr=true"
    echo "[entrypoint] YSQL connection manager: ON"
else
    echo "[entrypoint] YSQL connection manager: OFF"
fi

echo "[entrypoint] --tserver_flags=${TSERVER_FLAGS}"
echo "[entrypoint] Starting yugabyted ..."
exec bin/yugabyted start --background=false "--tserver_flags=${TSERVER_FLAGS}"

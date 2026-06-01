#!/usr/bin/env python3
"""
ruby-pg / YugabyteDB Docker Test Runner
----------------------------------------
Runs YugabyteDB in one Compose service and the ruby-pg rspec suite in another.
Output streams to the terminal and is also written to a log file.

Edit RUNNER_DEFAULTS below to change permanent settings.
Any value can also be overridden by an environment variable (env wins over defaults).

Environment variables
---------------------
  RUBY_PG_DOCKER_DIR        Directory containing docker-compose.yml (default: this script's dir)
  RUBY_PG_COMPOSE_FILE      Full path to compose file (default: RUBY_PG_DOCKER_DIR/docker-compose.yml)
  COMPOSE_PROJECT_NAME      Docker Compose project name

  RUBY_PG_BRANCH            Branch/tag of yugabyte/ruby-pg to clone and test (default: master)
  RUBY_PG_TEST_OUTPUT_LOG   Full path for the log file (default: one stable file per mode:
                            ruby-pg-yugabyte-cm-off.log or ruby-pg-yugabyte-cm-on.log)

  RUBY_PG_SKIP_CLEANUP      1/true: skip initial  `compose down`
  RUBY_PG_SKIP_PULL         1/true: skip          `compose pull yugabyte`
  RUBY_PG_SKIP_BUILD        1/true: skip          `compose build ruby-pg-tests`
  RUBY_PG_SKIP_SETUP        1/true: skip pull + build (run stack/tests only)
  RUBY_PG_REMOVE_VOLUMES    1/true: add -v to compose down calls
  RUBY_PG_POST_DOWN         1/true: run compose down after tests (default); 0/false: leave stack up

  YB_WAIT_SEC               Seconds to sleep before first YSQL readiness check
  YB_VERIFY_RETRIES         Max YSQL readiness attempts (each waits 5 s)

  YB_ENABLE_YSQL_CONN_MGR   1/true: enable YSQL Connection Manager (cm-on); 0/false: off (cm-off)
                             Log files are automatically named with the cm-on / cm-off suffix.

  RUBY_PG_YB_HOST_YSQL_PORT  Host port → container 5433  (YSQL)
  RUBY_PG_YB_HOST_ADMIN_PORT Host port → container 9000  (admin)
  RUBY_PG_YB_HOST_UI_PORT    Host port → container 15433 (UI)
  RUBY_PG_YB_HOST_YCQL_PORT  Host port → container 9042  (YCQL)

Usage
-----
  # Connection manager OFF (default)
  python3 run_ruby_pg_yugabyte_tests.py

  # Connection manager ON
  YB_ENABLE_YSQL_CONN_MGR=1 python3 run_ruby_pg_yugabyte_tests.py

  Run from the ruby/docker directory, or set RUBY_PG_DOCKER_DIR to the directory
  that contains this script and docker-compose.yml.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# =============================================================================
# RUNNER_DEFAULTS
# Edit these values to change permanent defaults.
# Any key can be overridden by the corresponding environment variable above.
# =============================================================================
RUNNER_DEFAULTS: dict = {
    # Docker Compose
    "compose_file":  None,              # None → docker-compose.yml next to this script
    "project_name":  "ruby-pg-yb",

    # Ruby-pg branch to test
    "ruby_pg_branch": "master",

    # Log file
    "log_file": None,                   # None → docker/logs/ruby-pg-yugabyte-cm-{off|on}.log

    # Lifecycle
    "skip_cleanup":    False,           # skip initial compose down
    "skip_pull":       False,           # skip compose pull yugabyte
    "skip_build":      False,           # skip compose build ruby-pg-tests
    "skip_setup":      False,           # skip pull + build
    "remove_volumes":  False,           # pass -v to compose down
    "no_post_down":    False,           # set True to leave the stack up after tests

    # YugabyteDB connection manager (YB_ENABLE_YSQL_CONN_MGR)
    "enable_conn_mgr": 1,           # True → cm-on, False → cm-off

    # YugabyteDB readiness
    "yb_wait_sec":       20,            # initial sleep before readiness probes
    "yb_verify_retries": 60,            # max probe attempts (each sleeps 5 s on failure)

    # Published host ports (chosen not to clash with C# Npgsql stack on 45433/49000)
    "yb_host_ysql_port":  55433,
    "yb_host_admin_port": 59000,
    "yb_host_ui_port":    55434,
    "yb_host_ycql_port":  59042,
}

TOTAL_STEPS = 7


# =============================================================================
# Config helpers
# =============================================================================

def _flag(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _str(key: str, default: str | None) -> str | None:
    v = os.environ.get(key, "").strip()
    return v if v else default


# =============================================================================
# Config dataclass
# =============================================================================

@dataclass
class RunnerConfig:
    script_dir:         Path
    docker_dir:         Path
    compose_file:       Path
    project_name:       str
    ruby_pg_branch:     str
    log_file:           Path | None
    skip_cleanup:       bool
    skip_pull:          bool
    skip_build:         bool
    skip_setup:         bool
    remove_volumes:     bool
    post_down:          bool
    enable_conn_mgr:    bool
    yb_wait_sec:        int
    yb_verify_retries:  int
    yb_host_ysql_port:  int
    yb_host_admin_port: int
    yb_host_ui_port:    int
    yb_host_ycql_port:  int


def load_config(script_dir: Path) -> RunnerConfig:
    d = RUNNER_DEFAULTS

    docker_dir = Path(os.environ.get("RUBY_PG_DOCKER_DIR", str(script_dir))).resolve()

    cf_raw = _str("RUBY_PG_COMPOSE_FILE", None)
    if cf_raw:
        compose_file = Path(cf_raw).expanduser().resolve()
    elif d.get("compose_file"):
        compose_file = Path(str(d["compose_file"])).expanduser().resolve()
    else:
        compose_file = (docker_dir / "docker-compose.yml").resolve()

    log_env = _str("RUBY_PG_TEST_OUTPUT_LOG", None)
    if log_env:
        log_file: Path | None = Path(log_env).expanduser().resolve()
    elif d.get("log_file"):
        log_file = Path(str(d["log_file"])).expanduser().resolve()
    else:
        log_file = None

    branch = _str("RUBY_PG_BRANCH", str(d["ruby_pg_branch"])) or "master"

    return RunnerConfig(
        script_dir         = script_dir,
        docker_dir         = docker_dir,
        compose_file       = compose_file,
        project_name       = os.environ.get("COMPOSE_PROJECT_NAME", d["project_name"]),
        ruby_pg_branch     = branch,
        log_file           = log_file,
        skip_cleanup       = _flag("RUBY_PG_SKIP_CLEANUP",      bool(d["skip_cleanup"])),
        skip_pull          = _flag("RUBY_PG_SKIP_PULL",         bool(d["skip_pull"])),
        skip_build         = _flag("RUBY_PG_SKIP_BUILD",        bool(d["skip_build"])),
        skip_setup         = _flag("RUBY_PG_SKIP_SETUP",        bool(d["skip_setup"])),
        remove_volumes     = _flag("RUBY_PG_REMOVE_VOLUMES",    bool(d["remove_volumes"])),
        post_down          = _flag("RUBY_PG_POST_DOWN",         not bool(d["no_post_down"])),
        enable_conn_mgr    = _flag("YB_ENABLE_YSQL_CONN_MGR",  bool(d["enable_conn_mgr"])),
        yb_wait_sec        = _int("YB_WAIT_SEC",                    int(d["yb_wait_sec"])),
        yb_verify_retries  = _int("YB_VERIFY_RETRIES",              int(d["yb_verify_retries"])),
        yb_host_ysql_port  = _int("RUBY_PG_YB_HOST_YSQL_PORT",     int(d["yb_host_ysql_port"])),
        yb_host_admin_port = _int("RUBY_PG_YB_HOST_ADMIN_PORT",     int(d["yb_host_admin_port"])),
        yb_host_ui_port    = _int("RUBY_PG_YB_HOST_UI_PORT",        int(d["yb_host_ui_port"])),
        yb_host_ycql_port  = _int("RUBY_PG_YB_HOST_YCQL_PORT",      int(d["yb_host_ycql_port"])),
    )


# =============================================================================
# I/O helpers
# =============================================================================

def tee(log_fp, msg: str) -> None:
    sys.stdout.write(msg)
    sys.stdout.flush()
    log_fp.write(msg)
    log_fp.flush()


def tee_run(
    cmd: list[str],
    *,
    cwd: Path,
    log_fp,
    env: dict[str, str] | None = None,
) -> int:
    """Run cmd, streaming stdout+stderr to terminal and log file. Returns exit code."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_fp.write(line)
            log_fp.flush()
    finally:
        proc.stdout.close()
    return proc.wait()


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# =============================================================================
# Failure / skip summary
# =============================================================================

def _parse_rspec_summary(content: str) -> tuple[list[str], list[str]]:
    """
    Parse an rspec --format documentation output.
    Returns (failed_examples, pending_examples) as lists of strings.
    """
    failures: list[str] = []
    pending:  list[str] = []

    # Match lines like:   "  1) SomeClass#method does something"
    in_failures = False
    in_pending  = False

    for line in content.splitlines():
        if re.match(r"^Failures:$", line.strip()):
            in_failures = True
            in_pending  = False
            continue
        if re.match(r"^Pending:$", line.strip()):
            in_failures = False
            in_pending  = True
            continue
        if re.match(r"^\d+ example", line.strip()):
            in_failures = False
            in_pending  = False

        m = re.match(r"^\s+\d+\)\s+(.+)", line)
        if m:
            if in_failures:
                failures.append(m.group(1).strip())
            elif in_pending:
                pending.append(m.group(1).strip())

    return failures, pending


def emit_summary(log_path: Path, *, stream_fp=None) -> None:
    if stream_fp is not None:
        stream_fp.flush()
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""

    failures, pending = _parse_rspec_summary(content)
    W = 72
    lines: list[str] = [
        "",
        "=" * W,
        f"Full test log: {log_path}",
        "=" * W,
        "",
    ]

    lines.append("FAILURE SUMMARY")
    lines.append("-" * W)
    if failures:
        for i, f in enumerate(failures, 1):
            lines.append(f"  {i}. {f}")
        lines += ["", "-" * W, f"Total failures: {len(failures)} — see log for full output."]
    else:
        lines += ["  (none)", "", "-" * W, "Total failures: 0"]

    lines += ["", "PENDING SUMMARY", "-" * W]
    if pending:
        for i, p in enumerate(pending, 1):
            lines.append(f"  {i}. {p}")
        lines += ["", "-" * W, f"Total pending: {len(pending)}"]
    else:
        lines.append("  (none)")
        lines += ["", "-" * W, "Total pending: 0"]

    lines.append("")

    # Find the rspec totals line (e.g. "150 examples, 3 failures, 12 pending")
    for line in content.splitlines():
        if re.match(r"^\d+ example", line.strip()):
            lines += ["", "RSPEC TOTALS", "-" * W, f"  {line.strip()}", ""]
            break

    block = "\n".join(lines) + "\n"
    sys.stdout.write(block)
    sys.stdout.flush()
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        pass


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    cfg = load_config(Path(__file__).resolve().parent)

    if not cfg.compose_file.is_file():
        print(f"error: compose file not found: {cfg.compose_file}", file=sys.stderr)
        return 1

    busy = [
        (name, port)
        for name, port in [
            ("YSQL",  cfg.yb_host_ysql_port),
            ("Admin", cfg.yb_host_admin_port),
            ("UI",    cfg.yb_host_ui_port),
            ("YCQL",  cfg.yb_host_ycql_port),
        ]
        if not is_port_free(port)
    ]
    if busy:
        for name, port in busy:
            print(f"error: {name} host port {port} is already in use", file=sys.stderr)
        print(
            "Set RUBY_PG_YB_HOST_*_PORT env vars to choose different ports.",
            file=sys.stderr,
        )
        return 2

    # ── Connection-manager label ──────────────────────────────────────────────
    cm_label = "cm-on" if cfg.enable_conn_mgr else "cm-off"

    # ── Log path ─────────────────────────────────────────────────────────────
    logs_dir = cfg.docker_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if cfg.log_file is not None:
        log_path = cfg.log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_path = logs_dir / f"ruby-pg-yugabyte-{cm_label}.log"

    # ── Compose env ──────────────────────────────────────────────────────────
    compose_env = os.environ.copy()
    compose_env["COMPOSE_PROJECT_NAME"]    = cfg.project_name
    compose_env["YB_PUBLISH_YSQL"]         = str(cfg.yb_host_ysql_port)
    compose_env["YB_PUBLISH_9000"]         = str(cfg.yb_host_admin_port)
    compose_env["YB_PUBLISH_UI"]           = str(cfg.yb_host_ui_port)
    compose_env["YB_PUBLISH_YCQL"]         = str(cfg.yb_host_ycql_port)
    compose_env["RUBY_PG_BRANCH"]          = cfg.ruby_pg_branch
    compose_env["YB_ENABLE_YSQL_CONN_MGR"] = "1" if cfg.enable_conn_mgr else "0"

    compose = ["docker", "compose", "-f", str(cfg.compose_file)]

    # ── Banner ────────────────────────────────────────────────────────────────
    print("=== ruby-pg / YugabyteDB Docker Test Runner ===")
    print(f"  docker dir:   {cfg.docker_dir}")
    print(f"  compose file: {cfg.compose_file}")
    print(f"  project:      {cfg.project_name}")
    print(f"  branch:       {cfg.ruby_pg_branch}")
    print(f"  conn manager: {cm_label}")
    print(
        f"  ports:        ysql={cfg.yb_host_ysql_port}  admin={cfg.yb_host_admin_port}"
        f"  ui={cfg.yb_host_ui_port}  ycql={cfg.yb_host_ycql_port}"
    )
    print(f"  log file:     {log_path}")
    print()

    exit_code = 0

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(
            f"=== ruby-pg / YugabyteDB test run ===\n"
            f"UTC time:     {datetime.now(timezone.utc).isoformat()}\n"
            f"compose file: {cfg.compose_file}\n"
            f"project:      {cfg.project_name}\n"
            f"branch:       {cfg.ruby_pg_branch}\n"
            f"conn manager: {cm_label}\n"
            f"ports:        ysql={cfg.yb_host_ysql_port}  admin={cfg.yb_host_admin_port}"
            f"  ui={cfg.yb_host_ui_port}  ycql={cfg.yb_host_ycql_port}\n"
            f"log file:     {log_path}\n"
            f"===\n\n"
        )

        def run_step(step: int, label: str, cmd: list[str]) -> int:
            tee(log_fp, f"\n[{step}/{TOTAL_STEPS}] {label}\n$ {' '.join(cmd)}\n\n")
            code = tee_run(cmd, cwd=cfg.docker_dir, log_fp=log_fp, env=compose_env)
            tee(log_fp, f"\n----- finished (exit {code}) -----\n")
            return code

        # ── Step 1: Cleanup ──────────────────────────────────────────────────
        if not cfg.skip_cleanup:
            cmd = compose + ["down", "--remove-orphans"] + (["-v"] if cfg.remove_volumes else [])
            if run_step(1, "Cleanup (compose down)", cmd) != 0:
                emit_summary(log_path, stream_fp=log_fp)
                return 1
        else:
            tee(log_fp, f"\n[1/{TOTAL_STEPS}] Cleanup skipped (RUBY_PG_SKIP_CLEANUP)\n\n")

        # ── Steps 2–3: Pull + build ──────────────────────────────────────────
        if not cfg.skip_setup:
            if not cfg.skip_pull:
                if run_step(2, "Pull yugabyte image", compose + ["pull", "yugabyte"]) != 0:
                    emit_summary(log_path, stream_fp=log_fp)
                    return 1
            else:
                tee(log_fp, f"\n[2/{TOTAL_STEPS}] Pull skipped (RUBY_PG_SKIP_PULL)\n\n")

            if not cfg.skip_build:
                build_cmd = compose + ["build", "ruby-pg-tests"]
                if cfg.ruby_pg_branch != "master":
                    build_cmd += ["--build-arg", f"RUBY_PG_BRANCH={cfg.ruby_pg_branch}"]
                if run_step(3, "Build ruby-pg-tests image", build_cmd) != 0:
                    emit_summary(log_path, stream_fp=log_fp)
                    return 1
            else:
                tee(log_fp, f"\n[3/{TOTAL_STEPS}] Build skipped (RUBY_PG_SKIP_BUILD)\n\n")
        else:
            tee(log_fp, f"\n[2-3/{TOTAL_STEPS}] Setup skipped (RUBY_PG_SKIP_SETUP)\n\n")

        # ── Step 4: Start YugabyteDB ─────────────────────────────────────────
        if run_step(4, "Start yugabyte (compose up -d)", compose + ["up", "-d", "yugabyte"]) != 0:
            run_step(5, "Diagnostics (compose logs)", compose + ["logs", "--no-color"])
            emit_summary(log_path, stream_fp=log_fp)
            return 1

        # ── Step 5: YSQL readiness ───────────────────────────────────────────
        tee(log_fp, f"\n[5/{TOTAL_STEPS}] Waiting {cfg.yb_wait_sec}s before readiness checks\n")
        time.sleep(max(0, cfg.yb_wait_sec))

        ysql_probe = compose + [
            "exec", "-T", "yugabyte",
            "/home/yugabyte/bin/ysqlsh",
            "-h", "yugabyte", "-p", "5433", "-U", "yugabyte", "-c", "SELECT 1", "-q",
        ]
        ready = False
        for attempt in range(1, cfg.yb_verify_retries + 1):
            if run_step(
                5,
                f"YSQL readiness {attempt}/{cfg.yb_verify_retries}",
                ysql_probe,
            ) == 0:
                ready = True
                break
            if attempt < cfg.yb_verify_retries:
                tee(log_fp, "YSQL not ready yet; retrying in 5s...\n")
                time.sleep(5)

        if not ready:
            run_step(5, "Diagnostics (compose logs)", compose + ["logs", "--no-color"])
            emit_summary(log_path, stream_fp=log_fp)
            return 1

        # ── Step 6: Run tests ────────────────────────────────────────────────
        test_cmd = compose + ["run", "--rm", "ruby-pg-tests"]
        exit_code = run_step(6, "Run rspec test suite", test_cmd)

        # ── Step 6 (diagnostics on failure) ─────────────────────────────────
        if exit_code != 0:
            run_step(6, "Diagnostics (compose logs)", compose + ["logs", "--no-color"])

        # ── Step 7: Post-run cleanup ─────────────────────────────────────────
        if cfg.post_down:
            cmd = compose + ["down", "--remove-orphans"] + (["-v"] if cfg.remove_volumes else [])
            run_step(7, "Post-run cleanup (compose down)", cmd)
        else:
            tee(log_fp, f"\n[7/{TOTAL_STEPS}] Post-run cleanup skipped (RUBY_PG_POST_DOWN=0)\n\n")

        tee(log_fp, f"\n=== finished: rspec exit code {exit_code} | log: {log_path} ===\n")

    emit_summary(log_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

#!/bin/bash
# Local debug loop for the sales-performance-tracking task.
#
# Mirrors how the harness treats /workspace/ as ephemeral: wipes it, restages
# this task's data, runs the oracle, then runs the verifier.
#
# One-time setup (per machine):
#   sudo mkdir -p /workspace && sudo chown $USER /workspace
#   python3 -m venv venv
#   ./venv/bin/pip install \
#     pandas==2.3.3 numpy==2.4.2 \
#     pytest==9.0.2 pytest-json-ctrf==0.3.5
#
# See environment/Dockerfile for the canonical pin list.

set -uo pipefail
TASK_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$TASK_DIR"

if ! [ -w /workspace ]; then
    echo "ERROR: /workspace is not writable." >&2
    echo "  One-time setup: sudo mkdir -p /workspace && sudo chown \$USER /workspace" >&2
    exit 1
fi

if ! [ -x ./venv/bin/python ]; then
    echo "ERROR: ./venv/bin/python not found. Set up the venv first:" >&2
    echo "  python3 -m venv venv" >&2
    echo "  ./venv/bin/pip install pandas==2.3.3 numpy==2.4.2 pytest==9.0.2 pytest-json-ctrf==0.3.5" >&2
    exit 1
fi

echo "[local_run] wiping /workspace/ ..."
find /workspace -mindepth 1 -delete

echo "[local_run] staging data: environment/data/ -> /workspace/data/"
cp -R "$TASK_DIR/environment/data" /workspace/data

ORACLE_EXIT=0
if [ -f "$TASK_DIR/solution/solve.py" ]; then
    echo "[local_run] running oracle: solution/solve.py"
    ./venv/bin/python solution/solve.py
    ORACLE_EXIT=$?
    if [ "$ORACLE_EXIT" -ne 0 ]; then
        echo "[local_run] WARNING: oracle exited $ORACLE_EXIT -- running tests anyway" >&2
    fi
else
    echo "[local_run] no solution/solve.py yet -- skipping oracle"
fi

TESTS_EXIT=0
if [ -f "$TASK_DIR/tests/test_output.py" ]; then
    echo "[local_run] running tests: tests/test_output.py"
    ./venv/bin/python -m pytest tests/test_output.py -v
    TESTS_EXIT=$?
else
    echo "[local_run] no tests/test_output.py yet -- skipping tests"
fi

if [ "$ORACLE_EXIT" -ne 0 ]; then
    exit "$ORACLE_EXIT"
fi
exit "$TESTS_EXIT"

#!/bin/bash
# TerminalBench verifier entrypoint.
# Runs the pytest suite against the agent's /workspace/ artifacts and writes a
# binary reward (0 or 1) to $VERIFIER_DIR/reward.txt. Tasks are pass/fail.

EXIT_CODE=0
VERIFIER_DIR="/logs/verifier"

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile."
    exit 1
fi

mkdir -p "$VERIFIER_DIR"

echo "STEP 1: Running unit tests"
pytest --ctrf "$VERIFIER_DIR/ctrf.json" /tests/test_output.py -rA -v 2>/dev/null \
    || pytest /tests/test_output.py -rA -v
PYTEST_EXIT=$?
if [ "$PYTEST_EXIT" -eq 5 ]; then
    echo "No unit tests found"
elif [ "$PYTEST_EXIT" -ne 0 ]; then
    EXIT_CODE=1
fi

if [ "$EXIT_CODE" -ne 0 ]; then
    echo 0 > "$VERIFIER_DIR/reward.txt"
else
    echo 1 > "$VERIFIER_DIR/reward.txt"
fi

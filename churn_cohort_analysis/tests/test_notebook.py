"""Verifier tests for churn-cohort-analysis.

Ground-truth values are pinned constants computed once from the immutable
task data shipped in this repository. The verifier deliberately does NOT
re-read the CSVs from `/workspace/data/`, because that directory is
writable by the agent. Reading from it would let an agent overwrite the
inputs with a trivial dataset, run the spec on the tampered data, and
pass the tests even though the agent never solved the original task.

The constants below were computed from the canonical
`environment/data/*.csv` shipped with this task.
"""
import json
from pathlib import Path

import pytest

VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")

EXPECTED = {
    "cohort_size": 14,
    "churned_users": 5,
    "churn_rate": 0.3571,
    "inactivity_threshold": 18.0,
    "high_risk_users": ["U08", "U11", "U12"],
}


@pytest.fixture(scope="module")
def notebook_variables():
    assert VARIABLES_PATH.exists(), "notebook_variables.json not found"
    with open(VARIABLES_PATH) as f:
        return json.load(f)


def test_variables_exist(notebook_variables):
    for key in EXPECTED:
        assert key in notebook_variables, f"missing top-level variable: {key}"


def test_cohort_size(notebook_variables):
    assert notebook_variables["cohort_size"] == EXPECTED["cohort_size"]


def test_churned_users(notebook_variables):
    assert notebook_variables["churned_users"] == EXPECTED["churned_users"]


def test_churn_rate(notebook_variables):
    assert (
        abs(notebook_variables["churn_rate"] - EXPECTED["churn_rate"]) < 0.0001
    )


def test_inactivity_threshold(notebook_variables):
    assert (
        abs(
            notebook_variables["inactivity_threshold"]
            - EXPECTED["inactivity_threshold"]
        )
        < 0.01
    )


def test_high_risk_users(notebook_variables):
    assert notebook_variables["high_risk_users"] == EXPECTED["high_risk_users"]

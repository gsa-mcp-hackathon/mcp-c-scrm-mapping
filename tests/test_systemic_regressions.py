"""REG-01..REG-12 systemic regression tests (offline, deterministic).

Each test reproduces a historical bug class with synthetic fixtures. The
same checks run live inside `run_cscrm_audit_suite` via
`systemic_checks.run_all_systemic_checks`.
"""

import pytest

from usa_spending_mcp_server.systemic_checks import (
    check_matrix_invariants,
    run_all_systemic_checks,
)


@pytest.mark.parametrize("reg_id", [f"REG-{i:02d}" for i in range(1, 13)])
def test_systemic_regression(reg_id):
    results = run_all_systemic_checks()
    assert reg_id in results, f"{reg_id} missing from systemic check registry"
    entry = results[reg_id]
    assert entry["status"] == "PASS", f"{reg_id} ({entry['title']}): {entry['detail']}"


def test_reg09_live_matrix_shape():
    """REG-09 invariant helper accepts the v5 matrix schema."""
    rows = [{"agency": "Department of Justice", "tier1a_in_window": 3758671.05,
             "tier1b_in_window": 0.0, "tier2_provisional": 0.0,
             "in_window_award_count": 6, "active_award_count": 4,
             "completed_award_count": 2}]
    assert check_matrix_invariants(rows) == []


def test_no_standalone_without_orthogonal_evidence():
    """Tautology guard: a term alone never confers precision without signals."""
    from usa_spending_mcp_server.tools.cscrm_triage import _score_sample
    rows = [{"Award ID": "X1", "Recipient Name": "RANDOM PARTS INC",
             "Description": "9000 SERIES WIDGET, PART 9000-41A",
             "NAICS Code": "332999", "PSC Code": "5340"}]
    scored = _score_sample("9000", rows)
    assert scored["precision"] == 0.0

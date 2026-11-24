"""C-SCRM pipeline stages (Model A bounded resumable execution)."""

from usaspending_cscrm_mcp.pipeline import (  # noqa: F401
    stage0_triage,
    stage1_search,
    stage2_hydrate,
    stage3_idv,
    stage4_adjudicate,
    stage5_subawards,
    stage6_matrix,
)

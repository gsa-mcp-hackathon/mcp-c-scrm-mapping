# Architecture — as-built vs §3/§5/§6 (2026-11-01)

## As-built
- Package `usa_spending_mcp_server` (`server.py` stdio, `server_http.py` HTTP+`/health`, `auth.py`
  Login.gov OIDC/PKCE). Client: shared `httpx.AsyncClient`, token-bucket 800/300s + burst 20,
  max 8 concurrent, Retry-After honor, 60s cooldown on 429/503/504, jitter, `transient` flag
  preserved through wrapping; slash guard before dispatch; no redirect following.
- Pipeline (7 stages, verified): triage+manifest → dual-vector search → hydration (unfiltered,
  GID-attributed sums, ghost segregation w/ reason enums) → IDV traversal (Gates A/B + single-awardee
  homogeneity) → guarded adjudication (inflection guards, boilerplate masking, taxonomy veto,
  homograph drop, TIER_1C sibling) → segregated subawards (adjudicated sweep) → cohort matrix + cert.
- 10 live tools: `triage_and_deconflict_target`, `discover_agencies_by_target`,
  `get_award_cscrm_details`, `search_awards_exhaustive`, `traverse_idv_hierarchy`,
  `resolve_recipient_tree`, `fetch_award_full_context`, `adjudicate_lexical_matches`,
  `search_subawards_exhaustive`, `run_cscrm_audit_suite`.
- Gates AT-00..AT-10 + REG-SYSTEMIC battery + 5 live canaries; phased client-side assembly mirrors
  server logic (direct-hydration fallback for Apps egress throttle).

## Gap table (§-required → as-built)
| Requirement | Status |
|---|---|
| Package `usaspending_cscrm_mcp`, `pipeline/stageN`, models/evidence/execution/certification/settings modules | MISSING — different layout (functionally equivalent logic elsewhere) |
| Exact 10 tool names (§5) | MISMATCH — 10 tools live, different names; renaming breaks the Hermes client config |
| `Decimal` accounting | MISSING — `round(float,2)` throughout; unfiltered sums verified but binary-float |
| `/ready`, allowed-hosts/origins config | MISSING (`/health` only) |
| `AUTH_MODE` logingov/databricks_passthrough/dual | MISSING — boolean `AUTH_ENABLED` only; prod runs `false` |
| Signed certificates + independent verifier, `canaries.json` | MISSING — certs are unsigned JSON dicts; canaries live in runner scripts |
| Execution model A/B (continuation or durable jobs) | MISSING — phased client-side re-calls; no server continuation tokens |
| `databricks.yml`, secret bindings, resources | MISSING — deploy via `workspace import-dir` + `apps deploy`, no Bundle |
| docs/ (this set) | ADDED this phase |
| Bounded tool output/pages/records | PARTIAL — page caps exist; no byte budget |
| `gates.py` (new, variance) | AT-00..AT-10 + 5 live canaries live here — the spec file list has no audit module; gates are required functionality with no other home. `canaries.json` ships the verified C-CURE ground truth as documented, replaceable config. |
| `deploy/v2-app/app.yaml` | v2 app deploy config (new separate app; `databricks_passthrough`; secrets bound at deploy time, never in file). |
| `unit/integration/protocol/live` test split | PARTIAL — flat `tests/` + `evals/`; protocol covered via live tools/list+call, no stdio test |

## Decisions required before Phase 1 (see feasibility doc §Prerequisites)
Package/tool rename (breaking), auth mode, execution model, deployment authorization.

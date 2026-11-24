# Phase 0 — Feasibility & Authentication (2026-11-01)

## Environment (verified this date)
- Repo: `C:/Users/User/projects/cscrm-usa-spending-mcp`, git clean at `6c480e4`
  ("INV2 adjudication bar…"), no docs/, no databricks.yml, no canaries.json.
- Package `usa-spending-cscrm-mcp-server` 0.1.0 (`usa_spending_mcp_server`), hatchling wheel,
  `requires-python >=3.11`. Local interpreter 3.14.7; lockfile compiled for 3.11 x86_64 Linux.
- Pinned: fastmcp 4.1.3, httpx 0.28.1, pydantic 2.x, uvicorn, starlette. `ruff` + `pytest` green (87/2).
- Databricks CLI v1.21.0. Host `https://dbc-aae0ba16-8727.cloud.databricks.com`.
- App `cscrm-usa-spending-mcp` RUNNING (MEDIUM) at
  `https://cscrm-usa-spending-mcp-7474654188777746.aws.databricksapps.com`
  (app update_time 2026-11-01T00:40:50Z — redeploy occurred outside agent sessions; owner unconfirmed).
- Live tools/list returns 10 tools (names differ from §5 — see architecture.md).
- `app.yaml` sets `AUTH_ENABLED=false`; startup binds 0.0.0.0 on `DATABRICKS_APP_PORT`/`PORT`/8000;
  `/health` exists (no `/ready`).

## Authentication modes (§4 assessment)
- `databricks_passthrough`: feasible. Databricks ingress fronts the app; no caller-supplied
  identity headers are trusted today (none read). Header inventory still to be documented from
  ingress behavior (which component sets/strips what, and whether the app is reachable outside
  the trusted boundary).
- `logingov`: code present (`auth.py`, OIDCProxy + PKCE) but **blocked** — no
  `LOGINGOV_CLIENT_ID`, no `JWT_SIGNING_KEY`, no registered redirect URI. Requires user
  onboarding at partners.login.gov. Cannot be made operational by implementation alone.
- `dual`: blocked on `logingov`.
- Current production posture is "ingress-only, application auth off" — not one of the three
  permitted modes. No bypass mode may be retained; mode selection is a user decision (below).

## Prerequisites / blockers
1. Login.gov credentials + redirect registration (user action) — blocks logingov/dual.
2. Auth-mode selection (logingov | databricks_passthrough | dual).
3. Package + tool-name decision (§5 mandates `usaspending_cscrm_mcp` and ten exact tool names;
   current names differ and the Hermes client config binds to current names — renaming is breaking).
4. Deployment authorization for the rebuilt artifact (target app: same vs new; credentials/CLI
   profile do not constitute authorization).
5. Execution-model selection (bounded resumable vs durable jobs) — affects certification claims.
6. Verified canary identifiers per §12 (existing v5 canaries cover EOP/GAO/FCC/HHS/JCI/collision;
   Arctic Wolf and any new mandated cases need identifiers or discovery criteria).

## Decisions recorded (user, 2026-11-01)
1. Rename package + all 10 tool names to the spec (breaking; Hermes client config updated alongside).
2. Auth: `databricks_passthrough` now; `logingov` later (code structured so Login.gov slots in).
3. Execution model A (bounded resumable with continuation tokens).
4. Deployment authorized to a NEW separate app (name TBD at deploy time).

## Docs in this set
- `api_contracts.md` — USAspending capability matrix (all operations verified by live probe).
- `architecture.md` — as-built vs §3/§5/§6 gap table.
- `methodology.md` — analytical policies preserved from prior verification.
- `deployment.md` — as-built deployment, readiness gaps, recovery.

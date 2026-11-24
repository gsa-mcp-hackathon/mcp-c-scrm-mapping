# Deployment — as-built, readiness gaps, recovery (2026-11-01)

## As-built (verified)
- Workflow: `databricks workspace import-dir . <ws-path> --overwrite` + `apps deploy <name> --no-wait`.
- `app.yaml` command: `PYTHONPATH=/app/python/source_code/src python -m usa_spending_mcp_server.server_http`
  with `DATABRICKS_APP_PORT=8000`; env `AUTH_ENABLED=false`. No secret bindings. No `databricks.yml`.
- Deps install via `requirements.txt` (uv-compiled, py3.11 linux x86_64; includes `pywin32`? — pinned
  for Apps build; verify exclusion if rebuilds fail on win32-only wheels).
- App RUNNING (MEDIUM), `/health` live, 10/10 tools on `/mcp`, smoke calls green (Oct 27–Nov 1).
- Auth to CLI/API: user-seeded SSO (`databricks auth login` in user terminal; `auth token -p` reuse,
  ~1h TTL). No PAT (org-disabled). No service principal yet.

## Readiness gaps (§7/§15/§16)
- `databricks bundle validate` never run (no bundle); validation ≠ readiness anyway.
- Secrets existence/access: `CSCRM_SIGNING_KEY` bound as an Apps secret env
  reference (`valueFrom: cscrm-signing-key` in `app.yaml`); value lives in a
  Databricks secret scope, never in repo — required before logingov/dual.
- Ingress identity headers undocumented (needed for databricks_passthrough mode).
- External acceptance per §16 not performed under this spec (prior smoke tests used the same
  checks against the current tool names).
- Operational storage: none selected (blocks execution-model B and any durable-run claims).

## Recovery
- Inspect: `databricks apps get <name>`, `apps logs` (agent channel may return empty; use API probes).
- Restore last-known-good: re-`import-dir` the last green commit + `apps deploy`; current head
  `6c480e4` is green (ruff+pytest 87/2, live PASS Oct 27).
- Disable: `databricks apps stop <name>` (do not delete the app, scopes, or run artifacts as recovery).
- Key rotation: replace the secret value in the scope below, redeploy, verify a
  fresh certificate reports SIGNED/VALID. Old signatures keep verifying against
  the `key_id` pubkey carried in their own envelope. No dual-key overlap yet;
  add a rotation procedure with Phase 5 dual-signing if overlap is required.

## Signing key provisioning (CSCRM_SIGNING_KEY)

- Generate once (never commit, log, or bake into images):
  `python -c "import secrets; print(secrets.token_hex(32))"`.
- Store the value in Databricks (secret scope; create one if needed):
  `databricks secrets create-scope <scope>` then
  `databricks secrets put-secret <scope> CSCRM_SIGNING_KEY --string-value "<hex>"`.
- Bind it to the app: add a secret resource on the Databricks App with
  resource key `cscrm-signing-key` pointing at `<scope>/CSCRM_SIGNING_KEY`
  (Apps UI → app → Environment/Resources, or at `apps deploy` time). The
  manifests reference it by key only (`valueFrom: cscrm-signing-key`); the
  value is injected as an env var at runtime and read solely by
  `signing_key_from_env()` at signing time.
- Live manifest: repo-root `app.yaml` is what Databricks Apps deploys (whole
  repo is imported; verified live config matches root: v1 server,
  `AUTH_ENABLED=false`). `deploy/databricks/app.yaml` (v1 spare) and
  `deploy/v2-app/app.yaml` (v2 server) carry the same reference for their targets.
- No-key behavior is fail-closed by design: unbound key → certificate
  `UNSIGNED`, verification `INVALID` ("not independently verifiable").
  Present-but-invalid keys (placeholder, non-hex, <32 bytes) raise a loud
  `CSCRM_SIGNING_KEY` error at signing time instead of signing weakly.
- Run state across redeploys: client-side checkpoints only; redeploys invalidate MCP sessions
  (re-initialize; never reuse SIDs).

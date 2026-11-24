#!/bin/sh
# Deploy the v2 C-SCRM MCP server to the v2 Databricks App.
# IMPORTANT: import-dir syncs the repo root app.yaml (v1 command), which would
# clobber the v2 app's entrypoint — so this script ALWAYS overlays
# deploy/v2-app/app.yaml afterwards. Never deploy the v2 app with a bare
# import-dir.
set -eu
PROFILE="${DATABRICKS_PROFILE:-goldstein.menachem@pbgc.gov}"
V2PATH="/Workspace/Users/goldstein.menachem@pbgc.gov/cscrm-usa-spending-mcp-v2"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Native Windows tools (databricks CLI) cannot resolve MSYS /c/... paths:
# convert to a C:/...-style path when running under git-bash/MSYS.
case "$REPO_ROOT" in
  /c/*|/d/*|/e/*) REPO_ROOT="$(cygpath -w "$REPO_ROOT" 2>/dev/null || echo "$REPO_ROOT")" ;;
esac
# databricks CLI also dislikes backslashes here; normalize to forward slashes.
REPO_ROOT="$(echo "$REPO_ROOT" | sed 's|\\|/|g')"

databricks workspace import-dir "$REPO_ROOT" "$V2PATH" --overwrite --profile "$PROFILE" >/dev/null
echo "import-dir done"

# NOTE: do not use $TMPDIR here — under git-bash it is MSYS-style (/tmp),
# which native Windows python cannot open. Keep the temp file in the repo
# (native path) and remove it afterwards.
TMP_JSON="$REPO_ROOT/.v2_app_yaml.json"
python - "$REPO_ROOT/deploy/v2-app/app.yaml" "$V2PATH/app.yaml" "$PROFILE" "$TMP_JSON" <<'EOF'
import base64, json, subprocess, sys
local, remote, profile, tmp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
raw = open(local, "rb").read()
body = {"path": remote, "format": "AUTO",
        "content": base64.b64encode(raw).decode(), "overwrite": True}
with open(tmp, "w") as f:
    json.dump(body, f)
r = subprocess.run(["databricks", "api", "post", "/api/2.0/workspace/import",
                    "--profile", profile, "--json", "@" + tmp],
                   capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip()[-200:])
EOF
rm -f "$TMP_JSON"
echo "app.yaml overlay done"

verify="$(databricks workspace export "$V2PATH/app.yaml" --profile "$PROFILE" 2>/dev/null \
  | grep -c usaspending_cscrm_mcp.server_http || true)"
if [ "$verify" != "1" ]; then
  echo "FATAL: v2 app.yaml overlay not in place — aborting before deploy" >&2
  exit 1
fi
echo "overlay verified"

databricks apps deploy cscrm-usa-spending-mcp-v2 --profile "$PROFILE"

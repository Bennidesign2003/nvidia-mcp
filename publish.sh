#!/bin/bash
set -e

# ============================================================
# nvidia-mcp Publish & Release Script (Gitea)
#
# Bumps the version markers in server.py (patch by default, or minor/major
# via $1), renames the "## Unreleased" section in CHANGELOG.md to the
# new version, commits + pushes the bump, computes server.py SHA256,
# writes update.json, and creates the Gitea release with both files
# as assets.
#
# Requires:
#   GITEA_TOKEN env var with write:repository scope
#   (Gitea: Settings -> Applications -> Generate New Token)
#
# Usage:
#   export GITEA_TOKEN=xxxxxxxxxxxxxxxx
#   ./publish.sh           # patch bump (1.0.0 -> 1.0.1)
#   ./publish.sh patch     # same
#   ./publish.sh minor     # 1.0.1 -> 1.1.0
#   ./publish.sh major     # 1.1.0 -> 2.0.0
#   ./publish.sh 2.5.0     # explicit version
# ============================================================

GITEA_URL="https://arminio.flowmora.net"
GITEA_OWNER="benjamin"
GITEA_REPO="nvidiamcp"
REPO="${GITEA_OWNER}/${GITEA_REPO}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_FILE="${SCRIPT_DIR}/server.py"
CHANGELOG_FILE="${SCRIPT_DIR}/CHANGELOG.md"
UPDATE_JSON="${SCRIPT_DIR}/update.json"

BUMP="${1:-patch}"

# --- preflight ---
if [[ -z "${GITEA_TOKEN}" ]]; then
    echo "ERROR: GITEA_TOKEN env var not set." >&2
    echo "       Create one at ${GITEA_URL}/user/settings/applications" >&2
    echo "       export GITEA_TOKEN=xxxx" >&2
    exit 1
fi
if ! command -v curl &>/dev/null; then
    echo "ERROR: curl not found." >&2
    exit 1
fi
if [[ ! -f "${SERVER_FILE}" ]]; then
    echo "ERROR: server.py missing" >&2
    exit 1
fi
if [[ -n "$(git -C "${SCRIPT_DIR}" status --porcelain | grep -vE '^.. (CHANGELOG\.md|server\.py|update\.json)$')" ]]; then
    echo "ERROR: uncommitted changes (other than CHANGELOG/server.py/update.json). Commit or stash first." >&2
    git -C "${SCRIPT_DIR}" status --short
    exit 1
fi

# --- preflight: API auth check ---
AUTH_CHECK="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: token ${GITEA_TOKEN}" \
    "${GITEA_URL}/api/v1/repos/${REPO}")"
if [[ "${AUTH_CHECK}" != "200" ]]; then
    echo "ERROR: Gitea API auth failed (HTTP ${AUTH_CHECK}). Check GITEA_TOKEN and that ${REPO} exists." >&2
    exit 1
fi

# --- preflight: compile + smoke-test before tagging anything ---
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi
if [[ -n "${PYTHON_BIN}" ]]; then
    echo "  preflight: py_compile..."
    "${PYTHON_BIN}" -m py_compile "${SERVER_FILE}" || { echo "ERROR: server.py has syntax errors"; exit 1; }
    if [[ -f "${SCRIPT_DIR}/tests/test_smoke.py" ]]; then
        echo "  preflight: smoke tests..."
        "${PYTHON_BIN}" "${SCRIPT_DIR}/tests/test_smoke.py" || { echo "ERROR: smoke tests failed"; exit 1; }
    fi
else
    echo "  WARN: no python found, skipping preflight checks"
fi

# --- resolve new version (read from line 1 of server.py: # __mcp_version__ = "X.Y.Z") ---
CURRENT="$(head -1 "${SERVER_FILE}" | sed -n 's/^# __mcp_version__ = "\(.*\)"$/\1/p')"
if [[ -z "${CURRENT}" ]]; then
    echo "ERROR: server.py line 1 must be '# __mcp_version__ = \"X.Y.Z\"'" >&2
    head -1 "${SERVER_FILE}" >&2
    exit 1
fi

IFS='.' read -r MAJ MIN PAT <<< "${CURRENT}"

case "${BUMP}" in
    patch) NEW_VERSION="${MAJ}.${MIN}.$((PAT + 1))" ;;
    minor) NEW_VERSION="${MAJ}.$((MIN + 1)).0" ;;
    major) NEW_VERSION="$((MAJ + 1)).0.0" ;;
    *)
        if [[ "${BUMP}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            NEW_VERSION="${BUMP}"
        else
            echo "ERROR: invalid argument '${BUMP}' (expected: patch|minor|major|X.Y.Z)" >&2
            exit 1
        fi
        ;;
esac

TAG="v${NEW_VERSION}"
TODAY="$(date -u +%Y-%m-%d)"
PUBLISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "=========================================="
echo "  nvidia-mcp Publish ${CURRENT} -> ${NEW_VERSION}"
echo "  Target: ${GITEA_URL}/${REPO}"
echo "=========================================="

# --- update server.py: line 1 marker + __version__ constant ---
sed -i.bak "1s|^# __mcp_version__ = \"${CURRENT}\"$|# __mcp_version__ = \"${NEW_VERSION}\"|" "${SERVER_FILE}"
sed -i.bak "s/^__version__ = \"${CURRENT}\"$/__version__ = \"${NEW_VERSION}\"/" "${SERVER_FILE}"
rm -f "${SERVER_FILE}.bak"
echo "  server.py: __mcp_version__ + __version__ = \"${NEW_VERSION}\""

# --- update CHANGELOG.md: rename Unreleased -> v1.0.1 (TODAY), add fresh Unreleased ---
python3 - "${CHANGELOG_FILE}" "${NEW_VERSION}" "${TODAY}" <<'PY'
import sys, re, pathlib

path = pathlib.Path(sys.argv[1])
new_version = sys.argv[2]
today = sys.argv[3]

text = path.read_text()
target = f"## v{new_version} ({today})"

# Replace the first "## Unreleased" with the new versioned heading.
new_text, count = re.subn(r"^## Unreleased\s*$", target, text, count=1, flags=re.MULTILINE)
if count == 0:
    raise SystemExit("ERROR: no '## Unreleased' section found in CHANGELOG.md")

# Insert a fresh empty Unreleased section above the new heading.
new_text = new_text.replace(
    target,
    f"## Unreleased\n\n<!-- Add bullets for the next release here. -->\n\n{target}",
    1,
)

path.write_text(new_text)
print(f"  CHANGELOG.md: Unreleased -> v{new_version} ({today})")
PY

# --- commit + push the bump ---
git -C "${SCRIPT_DIR}" add "${SERVER_FILE}" "${CHANGELOG_FILE}"
git -C "${SCRIPT_DIR}" commit -m "release: v${NEW_VERSION}"
git -C "${SCRIPT_DIR}" push origin HEAD

# --- compute SHA256 ---
if command -v shasum &>/dev/null; then
    SHA256="$(shasum -a 256 "${SERVER_FILE}" | awk '{print $1}')"
else
    SHA256="$(sha256sum "${SERVER_FILE}" | awk '{print $1}')"
fi
DOWNLOAD_URL="${GITEA_URL}/${REPO}/releases/download/${TAG}/server.py"

# --- extract this version's changelog ---
CHANGELOG_BODY="$(awk -v target="## v${NEW_VERSION}" '
    $0 ~ "^"target { found=1; next }
    found && /^## / { exit }
    found { print }
' "${CHANGELOG_FILE}")"
[[ -z "${CHANGELOG_BODY// }" ]] && CHANGELOG_BODY="Release ${TAG}"

# --- write update.json ---
python3 - <<EOF > "${UPDATE_JSON}"
import json
data = {
    "version": "${NEW_VERSION}",
    "tag": "${TAG}",
    "download_url": "${DOWNLOAD_URL}",
    "sha256": "${SHA256}",
    "published_at": "${PUBLISHED_AT}",
    "changelog": """${CHANGELOG_BODY}""".strip(),
}
print(json.dumps(data, indent=2))
EOF

echo "  sha256:      ${SHA256}"
echo "  update.json: ${UPDATE_JSON}"
echo ""
cat "${UPDATE_JSON}"
echo ""

# --- create / update release via Gitea API ---
API="${GITEA_URL}/api/v1/repos/${REPO}"
AUTH_HEADER="Authorization: token ${GITEA_TOKEN}"

# Look up existing release by tag
EXISTING="$(curl -sS -H "${AUTH_HEADER}" "${API}/releases/tags/${TAG}")"
RELEASE_ID="$(echo "${EXISTING}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get("id","") if isinstance(d,dict) else "")
except Exception:
    print("")
')"

if [[ -n "${RELEASE_ID}" ]]; then
    echo "Release ${TAG} exists (id=${RELEASE_ID}), removing existing assets..."
    # Delete each existing asset, then re-upload
    ASSET_IDS="$(echo "${EXISTING}" | python3 -c 'import json,sys
d=json.load(sys.stdin)
for a in d.get("assets",[]) or []:
    print(a["id"])
')"
    for AID in ${ASSET_IDS}; do
        curl -sS -X DELETE -H "${AUTH_HEADER}" \
            "${API}/releases/${RELEASE_ID}/assets/${AID}" >/dev/null
    done
else
    echo "Creating release ${TAG}..."
    CREATE_PAYLOAD="$(python3 -c 'import json,sys
print(json.dumps({
    "tag_name": sys.argv[1],
    "name": sys.argv[2],
    "body": sys.argv[3],
    "draft": False,
    "prerelease": False,
}))' "${TAG}" "nvidia-mcp ${NEW_VERSION}" "${CHANGELOG_BODY}")"
    CREATE_RESP="$(curl -sS -X POST \
        -H "${AUTH_HEADER}" \
        -H "Content-Type: application/json" \
        -d "${CREATE_PAYLOAD}" \
        "${API}/releases")"
    RELEASE_ID="$(echo "${CREATE_RESP}" | python3 -c 'import json,sys
d=json.load(sys.stdin)
if "id" not in d:
    print("ERROR creating release:", d, file=sys.stderr); sys.exit(1)
print(d["id"])
')"
    if [[ -z "${RELEASE_ID}" ]]; then
        echo "ERROR: could not create release. Response above." >&2
        exit 1
    fi
fi

# Upload assets
echo "Uploading assets to release ${RELEASE_ID}..."
for ASSET in "${SERVER_FILE}" "${UPDATE_JSON}"; do
    NAME="$(basename "${ASSET}")"
    curl -sS -X POST \
        -H "${AUTH_HEADER}" \
        -F "attachment=@${ASSET}" \
        "${API}/releases/${RELEASE_ID}/assets?name=${NAME}" >/dev/null
    echo "  uploaded: ${NAME}"
done

echo ""
echo "=========================================="
echo "  DONE. ${GITEA_URL}/${REPO}/releases/tag/${TAG}"
echo "=========================================="

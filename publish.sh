#!/bin/bash
set -e

# ============================================================
# nvidia-mcp Publish & Release Script
#
# Bumps the version markers in server.py (patch by default, or minor/major
# via $1), renames the "## Unreleased" section in CHANGELOG.md to the
# new version, commits + pushes the bump, computes server.py SHA256,
# writes update.json, and creates the GitHub release with both files
# as assets.
#
# Usage:
#   ./publish.sh           # patch bump (1.0.0 -> 1.0.1)
#   ./publish.sh patch     # same
#   ./publish.sh minor     # 1.0.1 -> 1.1.0
#   ./publish.sh major     # 1.1.0 -> 2.0.0
#   ./publish.sh 2.5.0     # explicit version
# ============================================================

REPO="Bennidesign2003/nvidia-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_FILE="${SCRIPT_DIR}/server.py"
CHANGELOG_FILE="${SCRIPT_DIR}/CHANGELOG.md"
UPDATE_JSON="${SCRIPT_DIR}/update.json"

BUMP="${1:-patch}"

# --- preflight ---
if ! command -v gh &>/dev/null; then
    echo "ERROR: GitHub CLI (gh) not found. brew install gh" >&2
    exit 1
fi
if ! gh auth status &>/dev/null; then
    echo "ERROR: not logged into GitHub. gh auth login" >&2
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
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${TAG}/server.py"

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

# --- create / update release ---
if gh release view "${TAG}" --repo "${REPO}" &>/dev/null; then
    echo "Release ${TAG} exists, replacing assets..."
    gh release upload "${TAG}" "${SERVER_FILE}" "${UPDATE_JSON}" --repo "${REPO}" --clobber
else
    echo "Creating release ${TAG}..."
    gh release create "${TAG}" \
        "${SERVER_FILE}" "${UPDATE_JSON}" \
        --repo "${REPO}" \
        --title "nvidia-mcp ${NEW_VERSION}" \
        --notes "${CHANGELOG_BODY}"
fi

echo ""
echo "=========================================="
echo "  DONE. https://github.com/${REPO}/releases/tag/${TAG}"
echo "=========================================="

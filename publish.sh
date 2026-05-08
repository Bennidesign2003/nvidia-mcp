#!/bin/bash
set -e

# ============================================================
# nvidia-mcp Publish & Release Script
#
# Reads version from updater.py, computes server.py SHA256,
# writes update.json, creates GitHub release, uploads both
# server.py + update.json as release assets.
# ============================================================

REPO="Bennidesign2003/nvidia-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

VERSION="$(grep -m1 '^__version__' "${SCRIPT_DIR}/updater.py" | sed 's/.*"\(.*\)".*/\1/')"
TAG="v${VERSION}"
SERVER_FILE="${SCRIPT_DIR}/server.py"
UPDATE_JSON="${SCRIPT_DIR}/update.json"

if [[ -z "${VERSION}" ]]; then
    echo "ERROR: could not read __version__ from updater.py" >&2
    exit 1
fi

echo "=========================================="
echo "  nvidia-mcp Publish ${TAG}"
echo "=========================================="

if ! command -v gh &>/dev/null; then
    echo "ERROR: GitHub CLI (gh) not found. brew install gh" >&2
    exit 1
fi

if ! gh auth status &>/dev/null; then
    echo "ERROR: not logged into GitHub. gh auth login" >&2
    exit 1
fi

if [[ ! -f "${SERVER_FILE}" ]]; then
    echo "ERROR: server.py not found at ${SERVER_FILE}" >&2
    exit 1
fi

# SHA256 of server.py (works on macOS + Linux)
if command -v shasum &>/dev/null; then
    SHA256="$(shasum -a 256 "${SERVER_FILE}" | awk '{print $1}')"
else
    SHA256="$(sha256sum "${SERVER_FILE}" | awk '{print $1}')"
fi

DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${TAG}/server.py"
PUBLISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Optional: read changelog from CHANGELOG.md (## v1.0.0 section), else fallback
CHANGELOG=""
if [[ -f "${SCRIPT_DIR}/CHANGELOG.md" ]]; then
    CHANGELOG="$(awk -v tag="## ${TAG}" '
        $0 == tag { found=1; next }
        found && /^## / { exit }
        found { print }
    ' "${SCRIPT_DIR}/CHANGELOG.md" | sed '/./,$!d' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"
fi

if [[ -z "${CHANGELOG}" ]]; then
    CHANGELOG="Release ${TAG}"
fi

# Build update.json
python3 - <<EOF > "${UPDATE_JSON}"
import json, sys
data = {
    "version": "${VERSION}",
    "tag": "${TAG}",
    "download_url": "${DOWNLOAD_URL}",
    "sha256": "${SHA256}",
    "published_at": "${PUBLISHED_AT}",
    "changelog": """${CHANGELOG}""".strip(),
}
print(json.dumps(data, indent=2))
EOF

echo "  version:     ${VERSION}"
echo "  sha256:      ${SHA256}"
echo "  update.json: ${UPDATE_JSON}"
echo ""
cat "${UPDATE_JSON}"
echo ""

# Create or update release
if gh release view "${TAG}" --repo "${REPO}" &>/dev/null; then
    echo "Release ${TAG} exists; replacing assets..."
    gh release upload "${TAG}" "${SERVER_FILE}" "${UPDATE_JSON}" --repo "${REPO}" --clobber
else
    echo "Creating release ${TAG}..."
    gh release create "${TAG}" \
        "${SERVER_FILE}" "${UPDATE_JSON}" \
        --repo "${REPO}" \
        --title "nvidia-mcp ${VERSION}" \
        --notes "${CHANGELOG}"
fi

echo ""
echo "=========================================="
echo "  DONE. Release: https://github.com/${REPO}/releases/tag/${TAG}"
echo "=========================================="

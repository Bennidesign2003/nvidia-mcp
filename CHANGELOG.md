# Changelog

## Unreleased

<!-- Add bullets for the next release here. publish.sh will rename this section to the new version on release. -->

## v1.0.0

- Initial public release.
- Self-update mechanism via GitHub Releases (`updater.py`):
  - Background check on startup, optional auto-apply
  - SHA256 verification against the release's `update.json`
  - Atomic file replacement; prior `server.py` kept as `server.py.bak`
  - Disable with `NVIDIA_MCP_NO_AUTO_UPDATE=1`
- New MCP tools: `check_for_updates`, `apply_update`, `get_mcp_version`.

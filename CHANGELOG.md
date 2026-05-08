# Changelog

## Unreleased

<!-- Add bullets for the next release here. -->

## v1.0.1 (2026-05-08)
- Renamed self-update MCP tools to be unambiguous so the LLM stops confusing them with NVIDIA driver / Windows updates:
  - `check_for_updates` → `check_nvidia_mcp_server_update`
  - `apply_update` → `install_nvidia_mcp_server_update`
  - `get_mcp_version` → `get_nvidia_mcp_server_version`
- Tool docstrings now explicitly list trigger phrases (DE + EN) and call out which tools NOT to use them in place of.

## v1.0.0

- Initial public release.
- Self-update mechanism via GitHub Releases (`updater.py`):
  - Background check on startup, optional auto-apply
  - SHA256 verification against the release's `update.json`
  - Atomic file replacement; prior `server.py` kept as `server.py.bak`
  - Disable with `NVIDIA_MCP_NO_AUTO_UPDATE=1`
- New MCP tools: `check_for_updates`, `apply_update`, `get_mcp_version`.

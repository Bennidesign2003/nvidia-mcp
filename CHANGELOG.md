# Changelog

## Unreleased

- **Architecture: GameCopilot is now the primary updater.** The host application
  checks `Bennidesign2003/nvidia-mcp/releases/latest` on startup and replaces the
  AppData copy of `server.py` if a newer version (with valid SHA256) is published.
  `server.py` no longer runs its own background updater on import.
- **Version jump 1.0.1 → 3.7.0** to sync with GameCopilot's existing embedded
  version series (3.6.5). Required so GameCopilot's version comparison treats
  the GitHub-released `server.py` as newer than the embedded fallback and keeps it.
- `server.py` is now single-file: `updater.py` is gone and the (lightweight)
  update logic lives inline. Allows GameCopilot's existing single-file
  extraction-and-version-check mechanism (`McpClientService.ExtractBundledServer`)
  to keep working unchanged.
- New first line of `server.py`: `# __mcp_version__ = "3.7.0"` — read by both
  GameCopilot and `publish.sh` as the canonical version marker.
- `publish.sh` now bumps the marker in `server.py` instead of `updater.py`.
- The MCP tools (`check_nvidia_mcp_server_update`, `install_nvidia_mcp_server_update`,
  `get_nvidia_mcp_server_version`) still work standalone for users running
  `python server.py` outside of GameCopilot.

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

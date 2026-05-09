# Changelog

## Unreleased

<!-- Add bullets for the next release here. -->

## v3.8.1 (2026-05-09)
### Update tools — labeled responses
- Every update-related tool now returns `kind` (`"mcp_server"` |
  `"nvidia_driver"`), `component` (human label), `status`, and a
  pre-formatted `message` so GameCopilot's notification UI / the LLM can
  always say WHICH thing has the update — never a generic "Update verfügbar".
  Examples:
  - `nvidia-mcp Server: Update verfügbar (3.8.0 → 3.8.1)`
  - `NVIDIA-Treiber: aktuell (572.16)`
- New `check_all_updates` tool: one call, aggregates nvidia-mcp + NVIDIA
  driver into a single labeled list. Useful when the user asks the generic
  "sind Updates verfügbar?" without naming a component.
- `check_and_install_driver` returns `kind: "nvidia_driver"` and a
  pre-formatted German `message`.
- `check_nvidia_mcp_server_update` / `install_nvidia_mcp_server_update` /
  `get_nvidia_mcp_server_version` returns include `kind: "mcp_server"` and a
  pre-formatted `message`.
- Docstrings instruct the LLM to report the `message` verbatim and always
  name the component, so a separate driver / Windows / GameCopilot-app
  update can never be confused with an MCP-server update.

## v3.8.0 (2026-05-09)
### Security
- **PowerShell command-injection fixed in 9 tools.** `manage_processes`,
  `manage_services`, `network_diagnostics`, `manage_startup_programs`,
  `manage_firewall`, `disk_analysis`, `manage_installed_software`,
  `manage_users` (incl. password), `manage_scheduled_tasks` no longer build
  PowerShell strings via f-string interpolation. Untrusted values are now
  bound through env vars (`$env:NVMCP_ARG0`) — a value like `'; rm -rf /` is
  just a string and cannot break out of the command.
- **`browser_click` / `browser_type` JS-injection fixed.** Selector and text
  arguments are JSON-encoded and passed via `Function.apply` instead of being
  string-interpolated into JS source.
- **`run_shell_command` is now opt-in.** Set `NVIDIA_MCP_ALLOW_SHELL=1` to
  enable. Every invocation is logged to `server.log`. Default-deny because a
  prompt-injection through any web page the LLM reads can pivot into RCE.
- **Browser CDP no longer kills the user's main Chrome.** A separate
  user-data-dir (`%LOCALAPPDATA%\GameCopilot\chrome-cdp`) is used by default.
  Set `NVIDIA_MCP_CDP_USE_MAIN_PROFILE=1` to opt back into the legacy
  kill+relaunch behavior.

### Robustness
- `pynvml` is now lazy-imported. Server starts on systems without an NVIDIA
  driver (CI, dev, non-NV hardware); GPU tools return a structured error.
- `get_gpu_status` now catches NVML init failures.
- Update check has a 5-minute cache to avoid hammering the GitHub API on
  repeated LLM checks.
- Updater always cleans up its `.new` temp files via `finally`, plus removes
  any stale ones from earlier aborted runs before downloading.
- Logging falls back to `%TEMP%\nvidia-mcp.log` when `server.log` next to
  `server.py` is on a read-only mount.
- Best-effort version-drift check at startup: warns on stderr if line 1's
  `# __mcp_version__` doesn't match `__version__`.

### MCP API surface
- **5 new resources** for read-only views (no tool call needed):
  `nvidia-mcp://version`, `nvidia-mcp://changelog`, `nvidia-mcp://gpu-status`,
  `nvidia-mcp://msfs-usercfg`, `nvidia-mcp://server-log`.
- **3 new prompts** (canned playbooks): `optimize_msfs_for_vr`,
  `diagnose_msfs_issue`, `check_for_server_updates`.

### Dev / release
- Pinned dependency versions in `requirements.txt`.
- New `tests/test_smoke.py` — smoke-test that imports server.py, verifies tool
  / resource / prompt counts, and checks security defaults.
- New `.github/workflows/ci.yml` — runs py_compile + smoke tests on every push.
- `publish.sh` now runs py_compile + smoke tests as a preflight before
  tagging a release. Refuses to release a server.py with syntax errors.

### Deferred to a future release
- Splitting `server.py` (~9700 lines) into a `nvidia_mcp/` package with a
  build step that bundles back to a single file for GameCopilot's extractor.
  Postponed until a proper test suite exists to catch regressions.

## v3.7.0 (2026-05-08)
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

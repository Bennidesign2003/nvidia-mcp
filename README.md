# nvidia-mcp

MCP server (`FastMCP` name: `nvidia-gpu`) used by [GameCopilot](https://github.com/Bennidesign2003/GodotRenderingAI) to read and tune Windows / NVIDIA / MSFS / OpenXR / Pimax / ReShade settings on the user's PC. Speaks the [Model Context Protocol](https://modelcontextprotocol.io/) so any MCP-aware LLM client (Claude Desktop, Continue, GameCopilot, etc.) can call its tools.

## What it does

49 tools across these areas:

- **Hardware** — `get_gpu_status`, `get_system_info`, NVIDIA driver check + install
- **MSFS 2024** — read / write `UserCfg.opt`, fix shader cache, repair install
- **OpenXR & Pimax** — analyze + set OpenXR runtime, Pimax client / runtime configs
- **ReShade** — list presets, toggle effects, set per-effect uniforms
- **Windows** — user account management, firewall rules, process control, file ops
- **Browser automation** — open URLs, query DOM, fill forms (for AI-driven setup flows)

## Install

```bash
python -m venv .venv
. .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate     # Windows PowerShell
pip install -r requirements.txt
```

## Run

```bash
python server.py
```

The server speaks MCP over stdio. Wire it into your MCP client of choice. GameCopilot launches it automatically — no manual step needed there.

## Auto-update

On every start the server checks `https://github.com/Bennidesign2003/nvidia-mcp/releases/latest`. If a newer `server.py` is found, it is downloaded, SHA256-verified against the release's `update.json`, and atomically replaces the local file. The previous `server.py` is kept as `server.py.bak` for rollback. The new version becomes active on the next launch.

Disable: set environment variable `NVIDIA_MCP_NO_AUTO_UPDATE=1`.

The update logic is also exposed as MCP tools so the LLM client can drive it explicitly: `check_for_updates`, `apply_update`, `get_mcp_version`.

To publish a new release, bump `__version__` in `updater.py`, append a section to `CHANGELOG.md`, then run `./publish.sh` (requires `gh` CLI authenticated).

## Logs

Runtime logs land in `server.log` next to `server.py`.

## License

Internal / personal project. Not yet a public-license release.

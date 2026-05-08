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

### Releasing a new version

`publish.sh` auto-increments the version, renames the `## Unreleased` section in `CHANGELOG.md` to the new version, commits + pushes the bump, and creates the GitHub release with `server.py` + `update.json`.

```bash
# 1. Add bullets under "## Unreleased" in CHANGELOG.md
# 2. Commit your code changes (publish.sh refuses to run with a dirty tree)
git add -A && git commit -m "..."
git push

# 3. Pick a bump and publish:
./publish.sh           # patch (1.0.0 -> 1.0.1) — default
./publish.sh minor     # 1.0.1 -> 1.1.0
./publish.sh major     # 1.1.0 -> 2.0.0
./publish.sh 2.5.0     # explicit version
```

Existing users pick up the update on their next server start (background check + atomic apply).

## Logs

Runtime logs land in `server.log` next to `server.py`.

## License

Internal / personal project. Not yet a public-license release.

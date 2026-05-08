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

**When used via GameCopilot** (the typical case), the host app checks `Bennidesign2003/nvidia-mcp/releases/latest` on startup and replaces the AppData copy of `server.py` (`%APPDATA%/GameCopilot/mcp-server/server.py`) when a newer release is published, with SHA256 verification.

**When used standalone** (`python server.py`), the embedded MCP tools handle it:
- `check_nvidia_mcp_server_update` — read-only check against GitHub Releases
- `install_nvidia_mcp_server_update` — downloads + atomic replace, restart required
- `get_nvidia_mcp_server_version` — reports the running version

Each release ships `server.py` plus an `update.json` containing `{version, download_url, sha256, ...}`. The first line of `server.py` is `# __mcp_version__ = "X.Y.Z"` — that's the canonical version marker and is what GameCopilot's version-aware extraction reads.

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

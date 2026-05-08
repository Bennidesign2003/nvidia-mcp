"""
Auto-updater for nvidia-mcp.

Checks the GitHub Releases API for a newer version, downloads the new
``server.py``, verifies its SHA256 against the release's ``update.json``,
and atomically replaces the local file. The new version becomes active
on the next launch.

Disable with environment variable: ``NVIDIA_MCP_NO_AUTO_UPDATE=1``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

__version__ = "1.0.1"

GITHUB_REPO = "Bennidesign2003/nvidia-mcp"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SCRIPT_DIR = Path(__file__).parent.resolve()
SERVER_FILE = SCRIPT_DIR / "server.py"
BACKUP_FILE = SCRIPT_DIR / "server.py.bak"

logger = logging.getLogger("nvidia-mcp.updater")


def _parse_version(s: str) -> tuple[int, ...]:
    s = s.lstrip("v").strip()
    parts = s.split(".")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            break
    return tuple(out)


def _is_newer(remote: str, local: str = __version__) -> bool:
    r = _parse_version(remote)
    l = _parse_version(local)
    return bool(r) and r > l


def _fetch_latest_release() -> dict[str, Any] | None:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as c:
            r = c.get(RELEASE_API, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("update check failed: %s", e)
        return None


def _find_asset_url(release: dict[str, Any], name: str) -> str | None:
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_for_updates() -> dict[str, Any]:
    """Look up the latest release and report whether it is newer than the local version."""
    release = _fetch_latest_release()
    if not release:
        return {"status": "error", "message": "Could not reach GitHub Releases API"}

    remote = (release.get("tag_name") or "").lstrip("v")
    if not remote:
        return {"status": "error", "message": "Latest release has no tag_name"}

    if not _is_newer(remote):
        return {
            "status": "current",
            "current_version": __version__,
            "latest_version": remote,
        }

    return {
        "status": "update_available",
        "current_version": __version__,
        "latest_version": remote,
        "release_url": release.get("html_url"),
        "release_notes": release.get("body", "") or "",
    }


def apply_update() -> dict[str, Any]:
    """Download the latest ``server.py``, verify its SHA256, and replace the local file.

    The current ``server.py`` is moved to ``server.py.bak`` first so a botched
    update can be reverted by restoring that file.
    """
    release = _fetch_latest_release()
    if not release:
        return {"status": "error", "message": "Could not reach GitHub Releases API"}

    remote = (release.get("tag_name") or "").lstrip("v")
    if not _is_newer(remote):
        return {"status": "already_current", "version": __version__}

    server_url = _find_asset_url(release, "server.py")
    update_json_url = _find_asset_url(release, "update.json")
    if not server_url:
        return {"status": "error", "message": "Release is missing server.py asset"}

    expected_sha: str | None = None
    if update_json_url:
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as c:
                r = c.get(update_json_url)
                r.raise_for_status()
                expected_sha = r.json().get("sha256")
        except Exception as e:
            logger.warning("could not read update.json: %s", e)

    fd, tmp_path_str = tempfile.mkstemp(prefix="server-", suffix=".py.new", dir=SCRIPT_DIR)
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            with c.stream("GET", server_url) as r:
                r.raise_for_status()
                with tmp_path.open("wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)

        if expected_sha:
            actual = _sha256_of(tmp_path)
            if actual.lower() != expected_sha.lower():
                tmp_path.unlink(missing_ok=True)
                return {
                    "status": "error",
                    "message": f"SHA256 mismatch (expected {expected_sha}, got {actual})",
                }

        if SERVER_FILE.exists():
            shutil.copy2(SERVER_FILE, BACKUP_FILE)
        os.replace(tmp_path, SERVER_FILE)

        logger.info("auto-update: %s → %s (restart required)", __version__, remote)
        return {
            "status": "updated",
            "previous_version": __version__,
            "new_version": remote,
            "restart_required": True,
            "backup": str(BACKUP_FILE),
        }
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        logger.error("apply_update failed: %s", e)
        return {"status": "error", "message": str(e)}


def _background_worker() -> None:
    if os.environ.get("NVIDIA_MCP_NO_AUTO_UPDATE") == "1":
        logger.info("auto-update disabled via NVIDIA_MCP_NO_AUTO_UPDATE")
        return
    try:
        check = check_for_updates()
        logger.info("update check: %s", check)
        if check.get("status") == "update_available":
            result = apply_update()
            logger.info("auto-apply: %s", result)
    except Exception as e:
        logger.warning("background updater crashed: %s", e)


def start_background_update_check() -> None:
    """Spawn a daemon thread that checks for and applies updates without blocking startup."""
    threading.Thread(target=_background_worker, daemon=True, name="updater").start()

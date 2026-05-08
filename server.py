# __mcp_version__ = "3.7.0"
from __future__ import annotations

import datetime
import hashlib
import json as _json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Literal

import httpx

from mcp.server.fastmcp import FastMCP
import pynvml

# ---------------------------------------------------------------------------
# Self-updater (single-file, read-only check + write apply).
# When run inside GameCopilot, the host's UpdateService also keeps this file
# fresh on startup; this code is the standalone fallback so users running
# `python server.py` directly still benefit from auto-update.
# ---------------------------------------------------------------------------
__version__ = "3.7.0"
_GITHUB_REPO = "Bennidesign2003/nvidia-mcp"
_RELEASE_API = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_SCRIPT_DIR = Path(__file__).parent.resolve()
_SERVER_FILE = _SCRIPT_DIR / "server.py"
_BACKUP_FILE = _SCRIPT_DIR / "server.py.bak"


def _updater_parse_version(s: str) -> tuple[int, ...]:
    s = s.lstrip("v").strip()
    out: list[int] = []
    for p in s.split("."):
        try:
            out.append(int(p))
        except ValueError:
            break
    return tuple(out)


def _updater_is_newer(remote: str, local: str = __version__) -> bool:
    r = _updater_parse_version(remote)
    return bool(r) and r > _updater_parse_version(local)


def _updater_fetch_latest_release() -> dict[str, Any] | None:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as c:
            r = c.get(_RELEASE_API, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def _updater_find_asset_url(release: dict[str, Any], name: str) -> str | None:
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None


def _updater_sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _updater_check() -> dict[str, Any]:
    release = _updater_fetch_latest_release()
    if not release:
        return {"status": "error", "message": "Could not reach GitHub Releases API"}
    remote = (release.get("tag_name") or "").lstrip("v")
    if not remote:
        return {"status": "error", "message": "Latest release has no tag_name"}
    if not _updater_is_newer(remote):
        return {"status": "current", "current_version": __version__, "latest_version": remote}
    return {
        "status": "update_available",
        "current_version": __version__,
        "latest_version": remote,
        "release_url": release.get("html_url"),
        "release_notes": release.get("body", "") or "",
    }


def _updater_apply() -> dict[str, Any]:
    release = _updater_fetch_latest_release()
    if not release:
        return {"status": "error", "message": "Could not reach GitHub Releases API"}
    remote = (release.get("tag_name") or "").lstrip("v")
    if not _updater_is_newer(remote):
        return {"status": "already_current", "version": __version__}
    server_url = _updater_find_asset_url(release, "server.py")
    update_json_url = _updater_find_asset_url(release, "update.json")
    if not server_url:
        return {"status": "error", "message": "Release is missing server.py asset"}

    expected_sha: str | None = None
    if update_json_url:
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as c:
                r = c.get(update_json_url)
                r.raise_for_status()
                expected_sha = r.json().get("sha256")
        except Exception:
            pass

    fd, tmp_path_str = tempfile.mkstemp(prefix="server-", suffix=".py.new", dir=_SCRIPT_DIR)
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
            actual = _updater_sha256_of(tmp_path)
            if actual.lower() != expected_sha.lower():
                tmp_path.unlink(missing_ok=True)
                return {"status": "error", "message": f"SHA256 mismatch (expected {expected_sha}, got {actual})"}
        if _SERVER_FILE.exists():
            shutil.copy2(_SERVER_FILE, _BACKUP_FILE)
        os.replace(tmp_path, _SERVER_FILE)
        return {
            "status": "updated",
            "previous_version": __version__,
            "new_version": remote,
            "restart_required": True,
            "backup": str(_BACKUP_FILE),
        }
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return {"status": "error", "message": str(e)}


mcp = FastMCP("nvidia-gpu")

# ---------------------------------------------------------------------------
# Logging — file-based so users can debug "the AI says ok but I see nothing"
# ---------------------------------------------------------------------------

_LOG_FILE = Path(__file__).parent / "server.log"
logger = logging.getLogger("nvidia-mcp")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    try:
        _fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        _fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(_fh)
    except Exception:
        # If we can't open the log file (e.g. permission), fall back silently.
        pass

# ---------------------------------------------------------------------------
# UserCfg.opt helpers
# ---------------------------------------------------------------------------

# MSFS 2024 is the primary target. 2024 paths come first and are preferred.
_USERCFG_CANDIDATES_2024 = [
    # MSFS 2024 Steam (confirmed location)
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024" / "UserCfg.opt",
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024 (Steam)" / "UserCfg.opt",
    Path(os.environ.get("APPDATA", "")) / "MicrosoftFlightSimulator2024" / "UserCfg.opt",
    # MSFS 2024 MS Store
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "UserCfg.opt",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    / "Microsoft.FlightSimulator2024_8wekyb3d8bbwe" / "LocalCache" / "UserCfg.opt",
]

_USERCFG_CANDIDATES = _USERCFG_CANDIDATES_2024

# Default: MSFS 2024 path (primary target)
DEFAULT_USERCFG_PATH = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft Flight Simulator 2024"
    / "UserCfg.opt"
)


def _is_msfs_usercfg(path: Path) -> bool:
    """Check if a file looks like a real MSFS UserCfg.opt."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:2000]
        # Must contain at least one MSFS-specific section
        return any(s in text for s in ("{Graphics", "{GraphicsVR", "{Video", "{Sound"))
    except Exception:
        return False


def _find_all_usercfg() -> list[dict]:
    """Find ALL UserCfg.opt files on the system with metadata.

    Returns list of dicts with path, mtime, size, and key settings.
    Searches: known candidates, APPDATA/LOCALAPPDATA glob, PowerShell
    filesystem search as last resort.
    """
    import datetime as _dt
    seen: set[str] = set()
    results: list[dict] = []

    def _add(p: Path):
        rp = str(p.resolve())
        if rp in seen:
            return
        seen.add(rp)
        if not _is_msfs_usercfg(p):
            return
        try:
            stat = p.stat()
            mtime = _dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            size = stat.st_size

            # Read key settings for display
            text = p.read_text(encoding="utf-8", errors="ignore")
            entries = _parse_usercfg(text)
            settings: dict[str, str] = {}
            for section, line in entries:
                if section in ("Video", "Graphics", "GraphicsVR", "RayTracing"):
                    m = re.match(r"^\s*(\S+)\s+(\S+)", line)
                    if m:
                        key = f"{section}.{m.group(1)}"
                        if key in SETTING_DEFS:
                            defn = SETTING_DEFS[key]
                            raw = m.group(2)
                            display = defn.get("values", {}).get(raw, raw)
                            settings[defn.get("label", key)] = display

            results.append({
                "path": str(p),
                "modified": mtime,
                "size_kb": round(size / 1024, 1),
                "settings": settings,
            })
        except Exception as exc:
            results.append({"path": str(p), "error": str(exc)})

    # 1. Check known candidate paths
    for c in _USERCFG_CANDIDATES:
        if c.exists():
            _add(c)

    # 2. Glob APPDATA and LOCALAPPDATA
    for env_var in ("APPDATA", "LOCALAPPDATA"):
        root = Path(os.environ.get(env_var, ""))
        if not root.exists():
            continue
        try:
            for hit in root.glob("**/UserCfg.opt"):
                _add(hit)
        except Exception:
            pass

    # 3. Glob common Steam library folders
    for steam_root in [
        r"C:\Program Files (x86)\Steam\steamapps",
        r"D:\Steam\steamapps",
        r"D:\SteamLibrary\steamapps",
        r"E:\SteamLibrary\steamapps",
    ]:
        sr = Path(steam_root)
        if sr.exists():
            try:
                for hit in sr.glob("**/UserCfg.opt"):
                    _add(hit)
            except Exception:
                pass

    # 4. PowerShell search as last resort (broader, slower)
    if not results:
        try:
            ps_script = (
                "Get-ChildItem -Path $env:APPDATA,$env:LOCALAPPDATA,"
                "'C:\\Users' -Recurse -Filter 'UserCfg.opt' "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty FullName"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=30,
            )
            for line in r.stdout.strip().splitlines():
                p = Path(line.strip())
                if p.exists():
                    _add(p)
        except Exception:
            pass

    # Sort by modification time (newest first)
    results.sort(
        key=lambda x: x.get("modified", ""),
        reverse=True,
    )
    return results


def _find_usercfg(custom_path: str = "") -> Path | None:
    """Find the MSFS 2024 UserCfg.opt.

    Priority order:
      1. Custom path (if provided and exists)
      2. Known MSFS 2024 candidate paths (first match wins)
      3. Filesystem search for UserCfg.opt with '2024' in the path
    """
    if custom_path:
        p = Path(custom_path)
        if p.exists():
            return p
        return None

    # 1. Check MSFS 2024 candidates — first existing one wins
    for c in _USERCFG_CANDIDATES_2024:
        if c.exists() and _is_msfs_usercfg(c):
            return c

    # 2. Filesystem search as fallback
    all_configs = _find_all_usercfg()
    if all_configs:
        for cfg in all_configs:
            if "2024" in cfg["path"]:
                return Path(cfg["path"])
        return Path(all_configs[0]["path"])

    return None


# Community folder paths (MSFS 2024 only)
COMMUNITY_CANDIDATES = [
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft Flight Simulator 2024"
    / "Packages"
    / "Community",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache"
    / "Packages" / "Community",
]

SUPPORTED_ARCHIVES = {".zip", ".7z", ".rar"}

# ---------------------------------------------------------------------------
# Driver-update helpers
# ---------------------------------------------------------------------------

NVIDIA_DRIVER_API = (
    "https://gfwsl.geforce.com/services_toolkit/services/com"
    "/nvidia/services/AjaxDriverService.php"
)

# GPU model name fragment -> (psid, pfid) for NVIDIA's driver lookup API.
# psid 101 = GeForce Desktop.  Extend as needed.
GPU_DRIVER_LOOKUP: dict[str, tuple[int, int]] = {
    # RTX 50 Series
    "RTX 5090":          (101, 985),
    "RTX 5080":          (101, 986),
    "RTX 5070 Ti":       (101, 987),
    "RTX 5070":          (101, 988),
    "RTX 5060 Ti":       (101, 989),
    "RTX 5060":          (101, 990),
    # RTX 40 Series
    "RTX 4090":          (101, 933),
    "RTX 4080 SUPER":    (101, 966),
    "RTX 4080":          (101, 934),
    "RTX 4070 Ti SUPER": (101, 967),
    "RTX 4070 Ti":       (101, 935),
    "RTX 4070 SUPER":    (101, 968),
    "RTX 4070":          (101, 936),
    "RTX 4060 Ti":       (101, 937),
    "RTX 4060":          (101, 938),
    # RTX 30 Series
    "RTX 3090 Ti":       (101, 929),
    "RTX 3090":          (101, 876),
    "RTX 3080 Ti":       (101, 904),
    "RTX 3080":          (101, 877),
    "RTX 3070 Ti":       (101, 905),
    "RTX 3070":          (101, 878),
    "RTX 3060 Ti":       (101, 899),
    "RTX 3060":          (101, 892),
    "RTX 3050":          (101, 932),
    # RTX 20 Series
    "RTX 2080 Ti":       (101, 815),
    "RTX 2080 SUPER":    (101, 852),
    "RTX 2080":          (101, 816),
    "RTX 2070 SUPER":    (101, 853),
    "RTX 2070":          (101, 817),
    "RTX 2060 SUPER":    (101, 854),
    "RTX 2060":          (101, 818),
    # GTX 16 Series
    "GTX 1660 Ti":       (101, 820),
    "GTX 1660 SUPER":    (101, 855),
    "GTX 1660":          (101, 821),
    "GTX 1650 SUPER":    (101, 856),
    "GTX 1650":          (101, 822),
    # GTX 10 Series
    "GTX 1080 Ti":       (101, 790),
    "GTX 1080":          (101, 783),
    "GTX 1070 Ti":       (101, 805),
    "GTX 1070":          (101, 784),
    "GTX 1060":          (101, 785),
    "GTX 1050 Ti":       (101, 791),
    "GTX 1050":          (101, 792),
}

# Windows OS IDs for the NVIDIA API
_OS_IDS: dict[str, int] = {
    "win10_64": 57,
    "win11_64": 57,   # same driver channel
}


def _get_gpu_info() -> tuple[str, str]:
    """Return (driver_version, gpu_name) via NVML."""
    pynvml.nvmlInit()
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(driver, bytes):
            driver = driver.decode()
        if isinstance(name, bytes):
            name = name.decode()
        return driver, name
    finally:
        pynvml.nvmlShutdown()


def _match_gpu_model(gpu_name: str) -> tuple[int, int] | None:
    """Find the best matching (psid, pfid) for *gpu_name*.

    Matches longest key first so "RTX 4080 SUPER" wins over "RTX 4080".
    """
    upper = gpu_name.upper()
    # Sort by key length descending so more-specific names match first
    for model in sorted(GPU_DRIVER_LOOKUP, key=len, reverse=True):
        if model.upper() in upper:
            return GPU_DRIVER_LOOKUP[model]
    return None


def _lookup_pfid(gpu_name: str) -> tuple[int, int] | None:
    """Dynamically look up the (psid, pfid) for a GPU from NVIDIA's API.

    Queries the product-family list for each known product series (psid/psrid)
    and matches the GPU name.  Falls back to the static table.
    """
    # First try the static table
    static = _match_gpu_model(gpu_name)

    # Product-type IDs to try (Desktop GeForce series)
    # psid=101 (GeForce), ptid varies by generation, osID=57 (Win10/11 64-bit)
    series_ids = [
        ("101", "14"),  # RTX 50 Series
        ("101", "13"),  # RTX 40 Series
        ("101", "12"),  # RTX 30 Series
        ("101", "11"),  # RTX 20 Series / GTX 16
        ("101", "10"),  # GTX 10 Series
    ]

    upper = gpu_name.upper()
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        for psid, ptid in series_ids:
            try:
                resp = client.get(
                    NVIDIA_DRIVER_API,
                    params={
                        "func": "DriverManualLookup",
                        "psid": psid,
                        "ptid": ptid,
                        "osID": "57",
                        "languageCode": "1033",
                        "numberOfResults": "0",  # just need product list
                    },
                )
                # Parse the product families from the response
                for m in re.finditer(
                    r'<LookupValue\b[^>]*ParentID="(\d+)"[^>]*>.*?'
                    r"<Name>\s*([^<]+?)\s*</Name>",
                    resp.text,
                    re.DOTALL,
                ):
                    pfid_str, product_name = m.group(1), m.group(2)
                    if product_name.upper().strip() in upper or upper in product_name.upper().strip():
                        return (int(psid), int(pfid_str))
            except Exception:
                continue

    return static


def _query_latest_driver(psid: int, pfid: int) -> dict | None:
    """Query the NVIDIA driver API and return latest driver info or None."""
    params = {
        "func": "DriverManualLookup",
        "psid": str(psid),
        "pfid": str(pfid),
        "osID": "57",          # Windows 10/11 64-bit
        "languageCode": "1033", # English
        "isWHQL": "1",
        "dch": "1",            # DCH (modern Windows driver model)
        "sort1": "0",
        "numberOfResults": "1",
    }
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        resp = client.get(NVIDIA_DRIVER_API, params=params)
        resp.raise_for_status()

    # The API returns XML with driver details inside <LookupValue> elements.
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    lv = root.find(".//{*}LookupValue")
    if lv is None:
        lv = root.find(".//LookupValue")
    if lv is None:
        return None

    def _text(tag: str) -> str:
        el = lv.find(tag)
        if el is None:
            el = lv.find(f"{{*}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    version = _text("Version")
    download_url = _text("DownloadURL")
    name = _text("Name")
    release_date = _text("ReleaseDateTime")

    if not version:
        return None

    return {
        "version": version,
        "name": name,
        "download_url": download_url,
        "release_date": release_date,
    }


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert '560.94' to (560, 94) for comparison."""
    return tuple(int(x) for x in re.split(r"[.\-]", v) if x.isdigit())


# -- Presets ----------------------------------------------------------------
# Each preset maps  "SectionName.Key" -> value  (value is always a string).
# Sections in UserCfg.opt look like:  {SectionName   key value   ... }

# ---------------------------------------------------------------------------
# GPU-aware VR presets for MSFS 2024 + Pimax
# ---------------------------------------------------------------------------
# Pimax headsets render at very high resolutions (per eye ~2448x2448 or higher).
# Even top-tier GPUs cannot run Ultra settings at these resolutions.
# These presets are tuned for smooth VR in Pimax — prioritizing stable 45+ FPS
# with Smart Smoothing over raw quality.
#
# GPU classification:
#   - Flagship (RTX 5090, 4090): 24 GB VRAM, can push higher settings
#   - High-end (RTX 5080, 4080, 3090): 16 GB VRAM, needs careful tuning
#   - Mid-range (RTX 4070, 3080, 3070): 8-12 GB VRAM, must be conservative
#   - Entry (RTX 4060, 3060, below): 8 GB or less, minimum settings
# ---------------------------------------------------------------------------

# Known GPU performance tiers for VR (approximate raster perf relative to 4090=100)
_GPU_VR_TIER: dict[str, str] = {
    # RTX 50 Series (longer/more specific keys first to avoid substring collisions)
    "RTX 5090":         "flagship",
    "RTX 5080":         "high_end",
    "RTX 5070 Ti":      "mid_high",
    "RTX 5070":         "mid_range",
    "RTX 5060 Ti":      "mid_range",   # ~RTX 4070-class in VR
    "RTX 5060":         "entry",
    # RTX 40 Series
    "RTX 4090":         "flagship",
    "RTX 4080 SUPER":   "high_end",
    "RTX 4080":         "high_end",
    "RTX 4070 Ti SUPER":"mid_high",
    "RTX 4070 Ti":      "mid_high",
    "RTX 4070 SUPER":   "mid_range",
    "RTX 4070":         "mid_range",
    "RTX 4060 Ti":      "entry",
    "RTX 4060":         "entry",
    # RTX 30 Series
    "RTX 3090 Ti":      "high_end",
    "RTX 3090":         "high_end",
    "RTX 3080 Ti":      "mid_high",
    "RTX 3080 12GB":    "mid_high",    # 12 GB variant
    "RTX 3080":         "mid_high",
    "RTX 3070 Ti":      "mid_range",
    "RTX 3070":         "mid_range",
    "RTX 3060 Ti":      "entry",
    "RTX 3060":         "entry",
    # RTX 20 Series (still used in VR)
    "RTX 2080 Ti":      "mid_range",
    "RTX 2080 SUPER":   "entry",
    "RTX 2080":         "entry",
    "RTX 2070 SUPER":   "entry",
    "RTX 2070":         "entry",
    "RTX 2060 SUPER":   "entry",
    "RTX 2060":         "entry",
}


def _detect_gpu_tier() -> tuple[str, str, int]:
    """Detect GPU and return (gpu_name, tier, vram_mb).

    Tier is one of: flagship, high_end, mid_high, mid_range, entry, unknown.
    """
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()

        if isinstance(name, bytes):
            name = name.decode()
        vram_mb = round(mem.total / 1024 / 1024)

        # Match GPU name against known tiers
        name_upper = name.upper()
        tier = "unknown"
        for gpu_key, gpu_tier in _GPU_VR_TIER.items():
            if gpu_key.upper() in name_upper:
                tier = gpu_tier
                break

        # Fallback by VRAM if not matched
        if tier == "unknown":
            vram_gb = vram_mb / 1024
            if vram_gb >= 20:
                tier = "flagship"
            elif vram_gb >= 14:
                tier = "high_end"
            elif vram_gb >= 10:
                tier = "mid_high"
            elif vram_gb >= 8:
                tier = "mid_range"
            else:
                tier = "entry"

        return name, tier, vram_mb
    except Exception:
        return "Unknown", "unknown", 0


# GPU-tier-specific MSFS 2024 presets for Pimax VR
# Values tuned from real-world testing at Pimax-level resolutions
_VR_PRESETS_BY_TIER: dict[str, dict[str, str]] = {
    "flagship": {
        # RTX 5090 / 4090 — can push more but not Ultra in Pimax VR
        "Video.DLSS": "3",              # Balanced (not Quality — Pimax res is huge)
        "Video.DLSSG": "1",             # Frame Gen On
        "GraphicsVR.TerrainLoD": "2.000000",
        "GraphicsVR.ObjectsLoD": "2.000000",
        "GraphicsVR.CloudsQuality": "2",    # High
        "GraphicsVR.AnisotropicFilter": "4", # 16x
        "GraphicsVR.SSContact": "0",         # SSAO Off (too expensive in VR)
        "GraphicsVR.Reflections": "1",       # Low
        "GraphicsVR.TextureResolution": "3", # Ultra
        "GraphicsVR.MotionBlur": "0",
        "GraphicsVR.ShadowQuality": "2",     # High
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "high_end": {
        # RTX 5080 / 4080 / 3090 — good but must be conservative in Pimax VR
        "Video.DLSS": "4",              # Performance (MUST for Pimax resolution)
        "Video.DLSSG": "1",             # Frame Gen On
        "GraphicsVR.TerrainLoD": "1.500000",
        "GraphicsVR.ObjectsLoD": "1.500000",
        "GraphicsVR.CloudsQuality": "1",    # Medium
        "GraphicsVR.AnisotropicFilter": "3", # 8x
        "GraphicsVR.SSContact": "0",         # Off
        "GraphicsVR.Reflections": "0",       # Off
        "GraphicsVR.TextureResolution": "2", # High
        "GraphicsVR.MotionBlur": "0",
        "GraphicsVR.ShadowQuality": "1",     # Medium
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "mid_high": {
        # RTX 4070 Ti / 3080 — limited in Pimax VR
        "Video.DLSS": "4",              # Performance
        "Video.DLSSG": "1",
        "GraphicsVR.TerrainLoD": "1.000000",
        "GraphicsVR.ObjectsLoD": "1.000000",
        "GraphicsVR.CloudsQuality": "1",    # Medium
        "GraphicsVR.AnisotropicFilter": "2", # 4x
        "GraphicsVR.SSContact": "0",
        "GraphicsVR.Reflections": "0",
        "GraphicsVR.TextureResolution": "2", # High
        "GraphicsVR.MotionBlur": "0",
        "GraphicsVR.ShadowQuality": "1",     # Medium
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "mid_range": {
        # RTX 4070 / 3070 — tight in Pimax VR
        "Video.DLSS": "5",              # Ultra Performance
        "Video.DLSSG": "1",
        "GraphicsVR.TerrainLoD": "1.000000",
        "GraphicsVR.ObjectsLoD": "1.000000",
        "GraphicsVR.CloudsQuality": "0",    # Low
        "GraphicsVR.AnisotropicFilter": "1", # 2x
        "GraphicsVR.SSContact": "0",
        "GraphicsVR.Reflections": "0",
        "GraphicsVR.TextureResolution": "1", # Medium
        "GraphicsVR.MotionBlur": "0",
        "GraphicsVR.ShadowQuality": "0",     # Low
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "entry": {
        # RTX 4060 / 3060 — minimum for VR
        "Video.DLSS": "5",              # Ultra Performance
        "Video.DLSSG": "1",
        "GraphicsVR.TerrainLoD": "0.500000",
        "GraphicsVR.ObjectsLoD": "0.500000",
        "GraphicsVR.CloudsQuality": "0",    # Low
        "GraphicsVR.AnisotropicFilter": "1", # 2x
        "GraphicsVR.SSContact": "0",
        "GraphicsVR.Reflections": "0",
        "GraphicsVR.TextureResolution": "0", # Low
        "GraphicsVR.MotionBlur": "0",
        "GraphicsVR.ShadowQuality": "0",     # Low
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
}

# Desktop presets are more forgiving (single-panel, lower resolution)
_DESKTOP_PRESETS_BY_TIER: dict[str, dict[str, str]] = {
    "flagship": {
        "Video.DLSS": "1",              # DLAA
        "Video.DLSSG": "1",
        "Graphics.TerrainLoD": "3.000000",
        "Graphics.ObjectsLoD": "3.000000",
        "Graphics.CloudsQuality": "3",     # Ultra
        "Graphics.AnisotropicFilter": "4",  # 16x
        "Graphics.SSContact": "1",
        "Graphics.Reflections": "3",        # High
        "Graphics.TextureResolution": "3",  # Ultra
        "Graphics.ShadowQuality": "3",      # Ultra
        "RayTracing.Enabled": "1",
        "RayTracing.Reflections": "1",
    },
    "high_end": {
        "Video.DLSS": "2",              # Quality
        "Video.DLSSG": "1",
        "Graphics.TerrainLoD": "2.500000",
        "Graphics.ObjectsLoD": "2.500000",
        "Graphics.CloudsQuality": "2",     # High
        "Graphics.AnisotropicFilter": "4",
        "Graphics.SSContact": "1",
        "Graphics.Reflections": "2",        # Medium
        "Graphics.TextureResolution": "3",  # Ultra
        "Graphics.ShadowQuality": "2",      # High
        "RayTracing.Enabled": "1",
        "RayTracing.Reflections": "1",
    },
    "mid_high": {
        "Video.DLSS": "3",              # Balanced
        "Video.DLSSG": "1",
        "Graphics.TerrainLoD": "2.000000",
        "Graphics.ObjectsLoD": "2.000000",
        "Graphics.CloudsQuality": "2",
        "Graphics.AnisotropicFilter": "3",
        "Graphics.SSContact": "1",
        "Graphics.Reflections": "1",
        "Graphics.TextureResolution": "2",
        "Graphics.ShadowQuality": "2",
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "mid_range": {
        "Video.DLSS": "4",              # Performance
        "Video.DLSSG": "1",
        "Graphics.TerrainLoD": "1.500000",
        "Graphics.ObjectsLoD": "1.500000",
        "Graphics.CloudsQuality": "1",
        "Graphics.AnisotropicFilter": "2",
        "Graphics.SSContact": "0",
        "Graphics.Reflections": "0",
        "Graphics.TextureResolution": "2",
        "Graphics.ShadowQuality": "1",
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
    "entry": {
        "Video.DLSS": "4",
        "Video.DLSSG": "1",
        "Graphics.TerrainLoD": "1.000000",
        "Graphics.ObjectsLoD": "1.000000",
        "Graphics.CloudsQuality": "0",
        "Graphics.AnisotropicFilter": "1",
        "Graphics.SSContact": "0",
        "Graphics.Reflections": "0",
        "Graphics.TextureResolution": "1",
        "Graphics.ShadowQuality": "0",
        "RayTracing.Enabled": "0",
        "RayTracing.Reflections": "0",
    },
}

# Legacy alias — existing code references PRESETS["VR"] / PRESETS["Desktop"]
PRESETS: dict[str, dict[str, str]] = {
    "VR": _VR_PRESETS_BY_TIER["high_end"],
    "Desktop": _DESKTOP_PRESETS_BY_TIER["high_end"],
}


def _parse_usercfg(text: str) -> list[tuple[str | None, str]]:
    """Parse UserCfg.opt into a list of (section_name | None, raw_line) tuples.

    Section headers look like ``{SectionName`` and end with ``}``.
    We keep every line verbatim so we can reconstruct the file losslessly.
    """
    entries: list[tuple[str | None, str]] = []
    current_section: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("{") and not stripped.startswith("}"):
            current_section = stripped.lstrip("{").strip()
            entries.append((None, line))  # section header is not inside a section
        elif stripped == "}":
            entries.append((None, line))
            current_section = None
        else:
            entries.append((current_section, line))
    return entries


def _apply_overrides(
    entries: list[tuple[str | None, str]],
    overrides: dict[str, str],
) -> list[tuple[str | None, str]]:
    """Return a new entries list with the requested key-value overrides applied."""
    # Build a lookup:  section -> key -> new_value
    lookup: dict[str, dict[str, str]] = {}
    for compound_key, value in overrides.items():
        section, key = compound_key.split(".", 1)
        lookup.setdefault(section, {})[key] = value

    applied: set[str] = set()
    new_entries = []
    for section, line in entries:
        if section and section in lookup:
            # Try to match "  Key   Value" pattern
            m = re.match(r"^(\s*)(\S+)(\s+)(\S+)(.*)", line)
            if m:
                key = m.group(2)
                if key in lookup[section]:
                    indent, _, spacing, _, rest = m.groups()
                    new_line = f"{indent}{key}{spacing}{lookup[section][key]}{rest}"
                    if not new_line.endswith(("\n", "\r\n")) and line.endswith(("\n", "\r\n")):
                        new_line += "\n"
                    new_entries.append((section, new_line))
                    applied.add(f"{section}.{key}")
                    continue
        new_entries.append((section, line))

    not_applied = set(overrides.keys()) - applied
    return new_entries, not_applied


def _entries_to_text(entries: list[tuple[str | None, str]]) -> str:
    return "".join(line for _, line in entries)


@mcp.tool()
def get_gpu_status(gpu_index: int = 0) -> dict:
    """Read temperature, utilization and VRAM of an NVIDIA GPU.

    Args:
        gpu_index: Index of the GPU (default 0 for the first GPU).
    """
    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        name = pynvml.nvmlDeviceGetName(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

        return {
            "gpu_name": name,
            "temperature_c": temp,
            "gpu_utilization_percent": util.gpu,
            "memory_utilization_percent": util.memory,
            "vram_total_mb": round(mem.total / 1024 / 1024),
            "vram_used_mb": round(mem.used / 1024 / 1024),
            "vram_free_mb": round(mem.free / 1024 / 1024),
        }
    finally:
        pynvml.nvmlShutdown()


# ---------------------------------------------------------------------------
# MSFS graphics version history
# ---------------------------------------------------------------------------

# History is stored as a JSON file next to UserCfg.opt:
#   ...Microsoft Flight Simulator 2024/UserCfg_history.json
# Structure: [ { "timestamp": ..., "label": ..., "content": "full file" }, ... ]

def _history_path(cfg_path: Path) -> Path:
    return cfg_path.parent / "UserCfg_history.json"


def _load_history(cfg_path: Path) -> list[dict]:
    hp = _history_path(cfg_path)
    if hp.exists():
        return _json.loads(hp.read_text(encoding="utf-8"))
    return []


def _save_history(cfg_path: Path, history: list[dict]) -> None:
    hp = _history_path(cfg_path)
    hp.write_text(_json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def _backup_dir(cfg_path: Path) -> Path:
    """Return (and create) the backup folder next to UserCfg.opt."""
    d = cfg_path.parent / "UserCfg_backups"
    d.mkdir(exist_ok=True)
    return d


def _snapshot(cfg_path: Path, label: str) -> int:
    """Save the current UserCfg.opt as a new history entry AND a .opt file.

    Returns the version number.  Each snapshot produces:
      1. A JSON entry in UserCfg_history.json  (for restore_msfs_graphics)
      2. A timestamped copy in UserCfg_backups/ (for manual recovery)
    """
    content = cfg_path.read_text(encoding="utf-8")
    now = datetime.datetime.now()

    # --- JSON history ---
    history = _load_history(cfg_path)
    version = len(history) + 1
    history.append({
        "version": version,
        "timestamp": now.isoformat(timespec="seconds"),
        "label": label,
        "content": content,
    })
    _save_history(cfg_path, history)

    # --- File backup ---
    ts = now.strftime("%Y%m%d_%H%M%S")
    backup_file = _backup_dir(cfg_path) / f"UserCfg_v{version}_{ts}.opt"
    backup_file.write_text(content, encoding="utf-8")

    # Also keep a simple "latest" backup that's always the most recent state
    latest = _backup_dir(cfg_path) / "UserCfg_latest.opt"
    latest.write_text(content, encoding="utf-8")

    return version


# ---------------------------------------------------------------------------
# MSFS graphics analysis & recommendations
# ---------------------------------------------------------------------------

# Which settings to read from which section, with human-readable names & value maps
SETTING_DEFS: dict[str, dict] = {
    # ---- Video section ----
    "Video.DLSS": {
        "label": "DLSS Mode",
        "values": {"0": "Off", "1": "DLAA", "2": "Quality", "3": "Balanced",
                   "4": "Performance", "5": "Ultra Performance"},
    },
    "Video.DLSSG": {
        "label": "DLSS Frame Generation",
        "values": {"0": "Off", "1": "On"},
    },
    "Video.RenderScale": {
        "label": "Render Scale",
    },
    # ---- Graphics (Desktop) ----
    "Graphics.TerrainLoD": {"label": "Terrain LoD (Desktop)"},
    "Graphics.ObjectsLoD": {"label": "Objects LoD (Desktop)"},
    "Graphics.CloudsQuality": {
        "label": "Clouds Quality (Desktop)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "Graphics.AnisotropicFilter": {
        "label": "Anisotropic Filter (Desktop)",
        "values": {"1": "2x", "2": "4x", "3": "8x", "4": "16x"},
    },
    "Graphics.SSContact": {
        "label": "SSAO (Desktop)",
        "values": {"0": "Off", "1": "On"},
    },
    "Graphics.Reflections": {
        "label": "SSR Reflections (Desktop)",
        "values": {"0": "Off", "1": "Low", "2": "Medium", "3": "High", "4": "Ultra"},
    },
    "Graphics.TextureResolution": {
        "label": "Texture Resolution (Desktop)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "Graphics.MotionBlur": {
        "label": "Motion Blur (Desktop)",
        "values": {"0": "Off", "1": "On"},
    },
    "Graphics.DepthOfField": {
        "label": "Depth of Field (Desktop)",
        "values": {"0": "Off", "1": "On"},
    },
    "Graphics.Buildings": {
        "label": "Buildings Quality (Desktop)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "Graphics.Trees": {
        "label": "Trees Quality (Desktop)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "Graphics.GrassAndBushes": {
        "label": "Grass & Bushes (Desktop)",
        "values": {"0": "Off", "1": "Low", "2": "Medium", "3": "High", "4": "Ultra"},
    },
    "Graphics.ShadowQuality": {
        "label": "Shadow Quality (Desktop)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    # ---- GraphicsVR ----
    "GraphicsVR.TerrainLoD": {"label": "Terrain LoD (VR)"},
    "GraphicsVR.ObjectsLoD": {"label": "Objects LoD (VR)"},
    "GraphicsVR.CloudsQuality": {
        "label": "Clouds Quality (VR)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "GraphicsVR.AnisotropicFilter": {
        "label": "Anisotropic Filter (VR)",
        "values": {"1": "2x", "2": "4x", "3": "8x", "4": "16x"},
    },
    "GraphicsVR.SSContact": {
        "label": "SSAO (VR)",
        "values": {"0": "Off", "1": "On"},
    },
    "GraphicsVR.Reflections": {
        "label": "SSR Reflections (VR)",
        "values": {"0": "Off", "1": "Low", "2": "Medium", "3": "High", "4": "Ultra"},
    },
    "GraphicsVR.TextureResolution": {
        "label": "Texture Resolution (VR)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "GraphicsVR.MotionBlur": {
        "label": "Motion Blur (VR)",
        "values": {"0": "Off", "1": "On"},
    },
    "GraphicsVR.ShadowQuality": {
        "label": "Shadow Quality (VR)",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    # ---- RayTracing ----
    "RayTracing.Enabled": {
        "label": "Ray Tracing",
        "values": {"0": "Off", "1": "On"},
    },
    "RayTracing.Reflections": {
        "label": "RT Reflections",
        "values": {"0": "Off", "1": "On"},
    },
}

# GPU VRAM tiers → what the card can reasonably handle
# (vram_gb_min, tier_name, recommendations)
VRAM_TIERS = [
    (16, "high_end",  "Ultra-Texturen, hohe LoDs, RayTracing und DLSS Quality sind möglich."),
    (10, "mid_high",  "Hohe Texturen, mittlere-hohe LoDs. RayTracing nur mit DLSS Balanced/Performance."),
    (8,  "mid",       "Mittlere Texturen, LoD ~1.5. RayTracing besser aus, DLSS Performance empfohlen."),
    (6,  "low_mid",   "Mittlere Texturen, LoD ~1.0. RayTracing aus, DLSS Performance/Ultra Performance."),
    (0,  "low",       "Niedrige Texturen, minimale LoDs, DLSS Ultra Performance, alles Extras aus."),
]


# Sections we care about when reading all settings
_RELEVANT_SECTIONS = {"Graphics", "GraphicsVR", "Video", "RayTracing", "Misc"}


def _read_current_settings(cfg_path: Path) -> dict[str, str]:
    """Read ALL settings from graphics-relevant sections in UserCfg.opt.

    Returns Section.Key -> raw_value for every key in Graphics, GraphicsVR,
    Video, RayTracing, and Misc sections.
    """
    text = cfg_path.read_text(encoding="utf-8")
    entries = _parse_usercfg(text)
    result = {}
    for section, line in entries:
        if not section or section not in _RELEVANT_SECTIONS:
            continue
        m = re.match(r"^\s*(\S+)\s+(\S+)", line)
        if m:
            key, value = m.group(1), m.group(2)
            result[f"{section}.{key}"] = value
    return result


def _build_recommendations(
    current: dict[str, str], vram_mb: int, gpu_name: str
) -> list[dict]:
    """Generate optimization tips based on current settings and GPU."""
    vram_gb = vram_mb / 1024
    tier_name = "low"
    tier_desc = ""
    for min_gb, name, desc in VRAM_TIERS:
        if vram_gb >= min_gb:
            tier_name, tier_desc = name, desc
            break

    tips: list[dict] = []

    def _tip(key: str, suggestion: str, reason: str):
        tips.append({"setting": SETTING_DEFS[key]["label"], "key": key,
                      "current": current.get(key, "?"), "suggestion": suggestion,
                      "reason": reason})

    # --- DLSS ---
    dlss = current.get("Video.DLSS", "")
    if dlss == "" or dlss == "0":
        if vram_gb < 24:
            _tip("Video.DLSS", "2 (Quality) oder 3 (Balanced)",
                 "DLSS bringt massiv FPS ohne sichtbaren Qualitätsverlust.")
    elif dlss in ("4", "5") and tier_name == "high_end":
        _tip("Video.DLSS", "2 (Quality) oder 1 (DLAA)",
             "Deine GPU ist stark genug für DLSS Quality statt Performance.")

    dlssg = current.get("Video.DLSSG", "")
    if dlssg in ("", "0"):
        if "40" in gpu_name or "50" in gpu_name:
            _tip("Video.DLSSG", "1 (An)",
                 "DLSS Frame Generation verdoppelt die gefühlten FPS auf RTX 40/50.")

    # --- RayTracing ---
    rt = current.get("RayTracing.Enabled", "")
    if rt == "1" and tier_name in ("low", "low_mid", "mid"):
        _tip("RayTracing.Enabled", "0 (Aus)",
             f"RayTracing kostet viel Performance. Bei {vram_gb:.0f} GB VRAM besser deaktivieren.")
    elif rt == "0" and tier_name == "high_end":
        _tip("RayTracing.Enabled", "1 (An)",
             "Deine GPU kann RayTracing in Kombination mit DLSS gut handeln.")

    # --- Terrain/Objects LoD ---
    for prefix in ("Graphics", "GraphicsVR"):
        label = "Desktop" if prefix == "Graphics" else "VR"
        tlod_key = f"{prefix}.TerrainLoD"
        olod_key = f"{prefix}.ObjectsLoD"
        tlod_str = current.get(tlod_key, "")
        olod_str = current.get(olod_key, "")
        if not tlod_str:
            continue
        tlod = float(tlod_str)
        olod = float(olod_str) if olod_str else 2.0

        if tier_name == "high_end":
            if prefix == "Graphics" and tlod < 2.0:
                _tip(tlod_key, "2.5 – 4.0",
                     f"Deine GPU kann höhere Terrain LoD ({label}) problemlos handeln.")
            elif prefix == "GraphicsVR" and tlod < 1.0:
                _tip(tlod_key, "1.0 – 2.0",
                     "In VR kann die LoD etwas höher — deine GPU schafft das.")
        elif tier_name in ("mid", "low_mid"):
            if tlod > 2.0:
                _tip(tlod_key, "1.0 – 1.5",
                     f"Terrain LoD ({label}) über 2.0 frisst viel CPU/GPU.")
            if olod > 2.0:
                _tip(olod_key, "1.0 – 1.5",
                     f"Objects LoD ({label}) über 2.0 ist bei deinem VRAM zu hoch.")

    # --- VR specific ---
    vr_ssao = current.get("GraphicsVR.SSContact", "0")
    vr_refl = current.get("GraphicsVR.Reflections", "0")
    vr_mb = current.get("GraphicsVR.MotionBlur", "0")
    if "VR" in gpu_name.upper() or any(k.startswith("GraphicsVR") for k in current):
        if vr_ssao == "1":
            _tip("GraphicsVR.SSContact", "0 (Aus)",
                 "SSAO in VR kostet FPS und ist im Headset kaum sichtbar.")
        if vr_refl not in ("0", ""):
            _tip("GraphicsVR.Reflections", "0 (Aus)",
                 "SSR Reflections in VR sind performance-intensiv bei wenig Nutzen.")
        if vr_mb == "1":
            _tip("GraphicsVR.MotionBlur", "0 (Aus)",
                 "Motion Blur in VR verursacht Übelkeit — immer ausschalten.")

    # --- Desktop specific ---
    dof = current.get("Graphics.DepthOfField", "")
    if dof == "1" and tier_name not in ("high_end",):
        _tip("Graphics.DepthOfField", "0 (Aus)",
             "Depth of Field kostet FPS und wird von vielen Spielern als störend empfunden.")

    shadow = current.get("Graphics.ShadowQuality", "")
    if shadow == "3" and tier_name in ("mid", "low_mid", "low"):
        _tip("Graphics.ShadowQuality", "2 (High)",
             "Ultra-Schatten sind kaum von High zu unterscheiden, kosten aber deutlich mehr.")
    elif shadow in ("0", "1") and tier_name == "high_end":
        _tip("Graphics.ShadowQuality", "2 (High) oder 3 (Ultra)",
             "Deine GPU schafft höhere Schattenqualität problemlos.")

    clouds = current.get("Graphics.CloudsQuality", "")
    if clouds == "3" and tier_name in ("low_mid", "low"):
        _tip("Graphics.CloudsQuality", "2 (High)",
             "Ultra-Wolken sind sehr GPU-intensiv. High reicht optisch fast immer.")
    elif clouds in ("0", "1") and tier_name in ("high_end", "mid_high"):
        _tip("Graphics.CloudsQuality", "2 (High) oder 3 (Ultra)",
             "Deine GPU kann höhere Wolkenqualität — großer visueller Gewinn.")

    # Texture Resolution
    tex = current.get("Graphics.TextureResolution", "")
    if tex in ("0", "1") and tier_name in ("high_end", "mid_high"):
        _tip("Graphics.TextureResolution", "2 (High) oder 3 (Ultra)",
             "Bei 16 GB VRAM solltest du hohe Texturen nutzen — kein FPS-Verlust.")
    tex_vr = current.get("GraphicsVR.TextureResolution", "")
    if tex_vr in ("0", "1") and tier_name in ("high_end", "mid_high"):
        _tip("GraphicsVR.TextureResolution", "2 (High)",
             "VR-Texturen auf High kosten kaum Performance bei deinem VRAM.")

    # Anisotropic Filter
    af = current.get("Graphics.AnisotropicFilter", "")
    if af in ("1", "2") and tier_name in ("high_end", "mid_high"):
        _tip("Graphics.AnisotropicFilter", "4 (16x)",
             "Anisotroper Filter auf 16x kostet quasi keine Performance, sieht aber besser aus.")
    af_vr = current.get("GraphicsVR.AnisotropicFilter", "")
    if af_vr in ("1", "2") and tier_name in ("high_end", "mid_high"):
        _tip("GraphicsVR.AnisotropicFilter", "3 (8x) oder 4 (16x)",
             "Höherer AF in VR verbessert Texturen in der Ferne deutlich.")

    return tips


def _human_label(compound_key: str) -> str:
    """Return a human-readable label for a Section.Key compound key."""
    if compound_key in SETTING_DEFS:
        return SETTING_DEFS[compound_key]["label"]
    # Auto-generate: "Graphics.TerrainLoD" → "TerrainLoD"
    _, _, key = compound_key.partition(".")
    # CamelCase to spaced: "TerrainLoD" → "Terrain LoD"
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", key)
    return spaced


def _human_value(compound_key: str, raw: str) -> str:
    """Convert a raw value to its human-readable form if a mapping exists."""
    defn = SETTING_DEFS.get(compound_key)
    if defn and "values" in defn:
        return defn["values"].get(raw, raw)
    return raw


def _build_settings_table(
    current: dict[str, str],
    tips: list[dict],
    section_prefix: str,
) -> list[dict]:
    """Build a table of ALL settings for a given section prefix.

    Each row: Setting | Current Value | Status | Recommendation | Reason
    """
    tip_by_key = {t["key"]: t for t in tips}

    rows: list[dict] = []
    for key, raw in current.items():
        # Match prefix exactly: "Graphics." should not match "GraphicsVR."
        section = key.split(".")[0] + "."
        if section != section_prefix:
            continue

        label = _human_label(key)
        display = _human_value(key, raw)
        tip = tip_by_key.get(key)

        rows.append({
            "setting": label,
            "current": display,
            "status": "CHANGE" if tip else "OK",
            "recommendation": tip["suggestion"] if tip else "-",
            "reason": tip["reason"] if tip else "",
        })
    return rows


@mcp.tool()
def diagnose_msfs_config() -> dict:
    """Find ALL MSFS UserCfg.opt files on this PC and show what's inside each.

    Use this FIRST when MSFS settings seem wrong or changes aren't visible.
    It answers the critical question: which config file is MSFS 2024 actually using?

    Shows for each found file:
    - Full path
    - Last modification time (newest = actively used by MSFS)
    - Key settings (DLSS, Clouds, Terrain LoD, etc.)

    Use the path from the newest file as usercfg_path in other tools if
    auto-detection picks the wrong one.

    Verwende dieses Tool wenn:
    - "DLSS ist immer noch auf TAA obwohl ich es geändert habe"
    - "Einstellungen werden nicht übernommen"
    - "Wo speichert MSFS 2024 die Config?"
    """
    all_configs = _find_all_usercfg()

    if not all_configs:
        # Try harder with PowerShell on all drives
        try:
            ps_script = (
                "$drives = (Get-PSDrive -PSProvider FileSystem).Root; "
                "foreach($d in $drives) { "
                "  Get-ChildItem -Path $d -Recurse -Filter 'UserCfg.opt' "
                "  -ErrorAction SilentlyContinue | "
                "  Select-Object -ExpandProperty FullName "
                "}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=120,
            )
            if r.stdout.strip():
                return {
                    "status": "deep_search",
                    "gefunden_aber_nicht_validiert": r.stdout.strip().splitlines(),
                    "hinweis": (
                        "Diese Dateien wurden gefunden aber nicht als MSFS-Config erkannt. "
                        "Bitte gib den korrekten Pfad manuell über usercfg_path an."
                    ),
                }
        except Exception:
            pass

        return {
            "error": "Keine UserCfg.opt gefunden!",
            "gesucht": [str(p) for p in _USERCFG_CANDIDATES],
            "tipp": (
                "MSFS 2024 scheint keine Config-Datei zu haben. "
                "Starte MSFS 2024 einmal manuell und beende es — "
                "dabei wird UserCfg.opt erstellt. Dann nochmal versuchen."
            ),
        }

    # Mark which one we would use by default
    active_path = all_configs[0]["path"] if all_configs else None

    return {
        "status": "ok",
        "anzahl_gefunden": len(all_configs),
        "aktive_config": active_path,
        "erklärung": (
            "Die NEUESTE Datei (oben) ist die, die MSFS aktuell nutzt. "
            "Falls das die falsche ist, gib den korrekten Pfad über "
            "usercfg_path an (z.B. set_msfs_setting(..., usercfg_path='...'))"
        ),
        "configs": all_configs,
    }


@mcp.tool()
def analyze_msfs_graphics(
    usercfg_path: str = "",
) -> dict:
    """Analyze current MSFS graphics settings with detailed tables and recommendations.

    Reads UserCfg.opt, queries GPU info (model, VRAM), and returns the results
    as separate tables for Desktop, VR and Video/RayTracing settings.
    Each row shows: Setting | Current Value | Recommendation | Reason.

    Present the results to the user as formatted markdown tables, grouped by
    category (Video/RayTracing, Desktop-Grafik, VR-Grafik).  Mark settings
    that need changes clearly.

    Args:
        usercfg_path: Full path to UserCfg.opt (leave empty for default).
    """
    cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH

    if not cfg_path.exists():
        return {"error": f"UserCfg.opt not found at {cfg_path}"}

    # Read current settings
    current = _read_current_settings(cfg_path)

    # Get GPU info via the shared tier detection
    gpu_name, gpu_tier, vram_mb = _detect_gpu_tier()
    vram_gb = vram_mb / 1024

    # Extra GPU stats (temp, utilization)
    gpu_temp = 0
    gpu_util = 0
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        gpu_util = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
        pynvml.nvmlShutdown()
    except Exception:
        pass

    # Map GPU tier to old tier_name for _build_recommendations compatibility
    _TIER_TO_LEGACY = {
        "flagship": "high_end", "high_end": "high_end",
        "mid_high": "mid_high", "mid_range": "mid",
        "entry": "low_mid", "unknown": "mid",
    }
    tier_name = _TIER_TO_LEGACY.get(gpu_tier, "mid")
    tier_desc = f"GPU-Tier: {gpu_tier} ({gpu_name})"

    # Build recommendations
    tips = _build_recommendations(current, vram_mb, gpu_name)

    # Build separate tables per category
    video_rt_table = _build_settings_table(current, tips, "Video.") + \
                     _build_settings_table(current, tips, "RayTracing.")
    desktop_table = _build_settings_table(current, tips, "Graphics.")
    vr_table = _build_settings_table(current, tips, "GraphicsVR.")

    changes_needed = sum(1 for t in tips)

    return {
        "gpu_info": {
            "name": gpu_name,
            "vram_gb": round(vram_gb, 1),
            "temperature_c": gpu_temp,
            "utilization_percent": gpu_util,
            "gpu_tier": gpu_tier,
            "performance_tier": tier_name,
            "tier_description": tier_desc,
        },
        "video_and_raytracing": video_rt_table,
        "desktop_graphics": desktop_table,
        "vr_graphics": vr_table,
        "summary": {
            "total_settings_analyzed": len(current),
            "changes_recommended": changes_needed,
            "verdict": (
                "Alles optimal konfiguriert!"
                if changes_needed == 0
                else f"{changes_needed} Einstellung(en) sollten angepasst werden."
            ),
        },
        "display_instructions": (
            "IMPORTANT: You MUST display the results as THREE separate markdown tables "
            "(one per category). Use this EXACT format for each table:\n\n"
            "## Video & RayTracing\n"
            "| Einstellung | Aktuell | Status | Empfehlung | Grund |\n"
            "|---|---|---|---|---|\n"
            "| DLSS Mode | Quality | OK | - | |\n"
            "| ... | ... | ... | ... | ... |\n\n"
            "## Desktop-Grafik\n"
            "(same table format)\n\n"
            "## VR-Grafik\n"
            "(same table format)\n\n"
            "Rules:\n"
            "- Show ALL rows from each table array, not just the ones that need changes\n"
            "- For 'status' column: use a green checkmark for OK, red X for CHANGE\n"
            "- Show EVERY setting, even if it's OK\n"
            "- At the end show the summary verdict\n"
            "- Answer in German"
        ),
    }


@mcp.tool()
def optimize_msfs_graphics(
    mode: Literal["VR", "Desktop"],
    usercfg_path: str = "",
    custom_overrides: dict[str, str] | None = None,
    auto_restart: bool = True,
    dry_run: bool = False,
) -> dict:
    """Optimize MSFS 2024 graphics — automatically tuned for your GPU.

    Detects your GPU (e.g. RTX 5080 → "high_end" tier) and applies a preset
    specifically tuned for that GPU + Pimax VR resolution. This prevents
    settings that are too aggressive (e.g. Ultra on an RTX 5080 in Pimax VR).

    GPU Tiers: flagship (5090/4090), high_end (5080/4080/3090),
    mid_high (4070Ti/3080), mid_range (4070/3070), entry (4060/3060).

    Saves current settings as a snapshot BEFORE changes. Use
    restore_msfs_graphics to roll back.

    WICHTIG: MSFS muss neu gestartet werden damit Änderungen sichtbar werden!
    Bei auto_restart=True (Standard) wird MSFS automatisch beendet und neu
    gestartet.

    Args:
        mode: 'VR' (Pimax-optimiert, konservativ) oder 'Desktop' (höhere Qualität).
        usercfg_path: Pfad zu UserCfg.opt (leer = auto-detect MSFS 2024).
        custom_overrides: Zusätzliche Overrides {"Section.Key": "value", ...}
                          werden NACH dem GPU-Preset angewendet.
        auto_restart: MSFS automatisch neu starten (Standard: True).
        dry_run: Wenn True, wird nur zurückgegeben WAS sich ändern würde —
                 keine Datei wird angefasst, MSFS wird nicht beendet.
    """
    import time

    cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH

    if not cfg_path.exists():
        searched = [str(p) for p in _USERCFG_CANDIDATES]
        return {
            "error": f"UserCfg.opt nicht gefunden: {cfg_path}",
            "gesucht": searched,
        }

    # ── GPU-aware preset selection (works for dry_run too — no side effects) ──
    gpu_name, gpu_tier, vram_mb = _detect_gpu_tier()
    tier_presets = _VR_PRESETS_BY_TIER if mode == "VR" else _DESKTOP_PRESETS_BY_TIER
    effective_tier = gpu_tier if gpu_tier in tier_presets else "high_end"
    overrides = dict(tier_presets[effective_tier])
    if custom_overrides:
        overrides.update(custom_overrides)

    if dry_run:
        current_settings = _read_current_settings(cfg_path)
        diff = {
            k: {"old": current_settings.get(k, "<not set>"), "new": v}
            for k, v in overrides.items()
            if current_settings.get(k) != v
        }
        logger.info("optimize_msfs_graphics DRY RUN mode=%s tier=%s diff=%s",
                    mode, effective_tier, list(diff.keys()))
        return {
            "status": "dry_run",
            "mode": mode,
            "gpu": gpu_name,
            "gpu_tier": effective_tier,
            "would_apply": overrides,
            "diff": diff,
            "config_file": str(cfg_path),
            "note": "Nichts wurde geschrieben. Setze dry_run=False um wirklich anzuwenden.",
        }

    # ── CRITICAL ORDER: Kill FIRST → Write SECOND → Start THIRD ──
    # MSFS overwrites UserCfg.opt on exit. We must kill it before writing.
    msfs_was_running = _is_msfs_running()
    if msfs_was_running and auto_restart:
        _kill_msfs(timeout_s=30)
        _wait_for_file_unlocked(cfg_path, timeout_s=10)

    # Save current state before changing anything (AFTER kill so we get the final state)
    saved_version = _snapshot(cfg_path, f"Before applying '{mode}' preset")
    _prune_msfs_backups(cfg_path)

    original_text = cfg_path.read_text(encoding="utf-8")

    entries = _parse_usercfg(original_text)
    new_entries, not_applied = _apply_overrides(entries, overrides)
    new_text = _entries_to_text(new_entries)

    # Write AFTER MSFS is dead
    cfg_path.write_text(new_text, encoding="utf-8")

    # Verify the write stuck
    verify = _read_current_settings(cfg_path)
    verified_count = sum(
        1 for k, v in overrides.items()
        if k not in not_applied and verify.get(k) == v
    )
    total_applied = len(overrides) - len(not_applied)

    result = {
        "status": "ok",
        "mode": mode,
        "gpu": gpu_name,
        "gpu_tier": effective_tier,
        "vram_mb": vram_mb,
        "tier_hinweis": (
            f"GPU '{gpu_name}' erkannt → Tier '{effective_tier}'. "
            f"Preset ist auf diese GPU-Leistung abgestimmt."
        ),
        "file": str(cfg_path),
        "saved_as_version": saved_version,
        "restore_hint": f"Use restore_msfs_graphics with version={saved_version} to undo.",
        "applied": {k: v for k, v in overrides.items() if k not in not_applied},
        "skipped_keys_not_found": sorted(not_applied) if not_applied else [],
        "verifiziert": f"{verified_count}/{total_applied} Einstellungen in Datei bestätigt",
    }

    # Restart MSFS so changes are visible
    if msfs_was_running and auto_restart:
        time.sleep(2)
        try:
            vr_mode = mode == "VR"
            if vr_mode:
                vr_result = launch_msfs_vr(force_restart=False)
                result["neustart"] = (
                    f"MSFS beendet und in VR neu gestartet. "
                    f"{mode}-Preset mit {total_applied} Einstellungen aktiv."
                )
                result["vr_launch"] = vr_result
            else:
                subprocess.Popen(["cmd", "/c", "start", f"steam://run/{_MSFS2024_STEAM_APP_ID}"])
                result["neustart"] = (
                    f"MSFS beendet und neu gestartet. "
                    f"{mode}-Preset mit {total_applied} Einstellungen aktiv."
                )
        except Exception as exc:
            result["neustart"] = f"MSFS beendet, Neustart fehlgeschlagen: {exc}"
    elif msfs_was_running and not auto_restart:
        result["hinweis"] = (
            "MSFS läuft noch! Die Grafikänderungen werden erst nach einem "
            "Neustart von MSFS sichtbar. Sage 'starte MSFS in VR' um neu zu starten."
        )
    else:
        result["hinweis"] = (
            "Einstellungen gespeichert. Beim nächsten Start von MSFS werden "
            f"die {mode}-Einstellungen automatisch geladen."
        )

    return result


# ---------------------------------------------------------------------------
# German alias map + value maps for individual MSFS settings
# ---------------------------------------------------------------------------

# Maps German (and common shorthand) names → "Section.Key" in UserCfg.opt
_MSFS_SETTING_ALIASES: dict[str, str] = {
    # DLSS
    "dlss": "Video.DLSS",
    "dlss modus": "Video.DLSS",
    "dlss mode": "Video.DLSS",
    "upscaling": "Video.DLSS",
    "upscaler": "Video.DLSS",
    "taa": "Video.DLSS",           # user says "TAA" → they want to switch to DLSS or off
    "antialiasing": "Video.DLSS",
    "anti-aliasing": "Video.DLSS",
    "aa": "Video.DLSS",
    # DLSS Frame Generation
    "dlssg": "Video.DLSSG",
    "dlss fg": "Video.DLSSG",
    "frame generation": "Video.DLSSG",
    "framegen": "Video.DLSSG",
    "fg": "Video.DLSSG",
    # Render Scale — also mapped from "Schärfe / klares Bild" requests
    "render scale": "Video.RenderScale",
    "renderscale": "Video.RenderScale",
    "render auflösung": "Video.RenderScale",
    "auflösung": "Video.RenderScale",
    "klares bild": "Video.RenderScale",
    "klareres bild": "Video.RenderScale",
    "schärfer": "Video.RenderScale",
    "schaerfer": "Video.RenderScale",
    "schärfe": "Video.RenderScale",
    "schaerfe": "Video.RenderScale",
    "sharpness": "Video.RenderScale",
    "sharpen": "Video.RenderScale",
    "klarheit": "Video.RenderScale",
    # Terrain LoD
    "terrain lod": "GraphicsVR.TerrainLoD",
    "terrain": "GraphicsVR.TerrainLoD",
    "gelände": "GraphicsVR.TerrainLoD",
    "gelände detail": "GraphicsVR.TerrainLoD",
    "terrain lod desktop": "Graphics.TerrainLoD",
    "terrain desktop": "Graphics.TerrainLoD",
    # Objects LoD
    "object lod": "GraphicsVR.ObjectsLoD",
    "objekte": "GraphicsVR.ObjectsLoD",
    "objekt detail": "GraphicsVR.ObjectsLoD",
    "objects lod": "GraphicsVR.ObjectsLoD",
    "object lod desktop": "Graphics.ObjectsLoD",
    "objects desktop": "Graphics.ObjectsLoD",
    # Clouds
    "wolken": "GraphicsVR.CloudsQuality",
    "clouds": "GraphicsVR.CloudsQuality",
    "wolken qualität": "GraphicsVR.CloudsQuality",
    "wolken desktop": "Graphics.CloudsQuality",
    "clouds desktop": "Graphics.CloudsQuality",
    # Texture
    "texturen": "GraphicsVR.TextureResolution",
    "textur": "GraphicsVR.TextureResolution",
    "textures": "GraphicsVR.TextureResolution",
    "texture resolution": "GraphicsVR.TextureResolution",
    "texturen desktop": "Graphics.TextureResolution",
    # Anisotropic
    "anisotropic": "GraphicsVR.AnisotropicFilter",
    "anisotropisch": "GraphicsVR.AnisotropicFilter",
    "af": "GraphicsVR.AnisotropicFilter",
    "anisotropic desktop": "Graphics.AnisotropicFilter",
    # Shadows
    "schatten": "GraphicsVR.ShadowQuality",
    "shadows": "GraphicsVR.ShadowQuality",
    "shadow quality": "GraphicsVR.ShadowQuality",
    "schatten desktop": "Graphics.ShadowQuality",
    # SSAO
    "ssao": "GraphicsVR.SSContact",
    "ambient occlusion": "GraphicsVR.SSContact",
    "ssao desktop": "Graphics.SSContact",
    # Reflections
    "reflexionen": "GraphicsVR.Reflections",
    "reflections": "GraphicsVR.Reflections",
    "spiegelungen": "GraphicsVR.Reflections",
    "reflexionen desktop": "Graphics.Reflections",
    # Motion Blur
    "motion blur": "GraphicsVR.MotionBlur",
    "bewegungsunschärfe": "GraphicsVR.MotionBlur",
    "motion blur desktop": "Graphics.MotionBlur",
    # Depth of Field
    "dof": "Graphics.DepthOfField",
    "tiefenschärfe": "Graphics.DepthOfField",
    "depth of field": "Graphics.DepthOfField",
    # Buildings
    "gebäude": "Graphics.Buildings",
    "buildings": "Graphics.Buildings",
    # Trees
    "bäume": "Graphics.Trees",
    "trees": "Graphics.Trees",
    # Grass
    "gras": "Graphics.GrassAndBushes",
    "grass": "Graphics.GrassAndBushes",
    "büsche": "Graphics.GrassAndBushes",
    # RayTracing
    "raytracing": "RayTracing.Enabled",
    "ray tracing": "RayTracing.Enabled",
    "rt": "RayTracing.Enabled",
    "rt reflections": "RayTracing.Reflections",
    "rt reflexionen": "RayTracing.Reflections",
}

# Named value aliases → the numeric string that goes into UserCfg.opt
_MSFS_VALUE_ALIASES: dict[str, dict[str, str]] = {
    "Video.DLSS": {
        "off": "0", "aus": "0", "kein": "0", "keine": "0", "taa": "0",
        "dlaa": "1",
        "quality": "2", "qualität": "2",
        "balanced": "3", "ausgewogen": "3",
        "performance": "4", "leistung": "4",
        "ultra performance": "5", "ultra": "5",
    },
    "Video.DLSSG": {
        "off": "0", "aus": "0", "nein": "0",
        "on": "1", "an": "1", "ja": "1", "ein": "1",
    },
    "_quality_4": {  # applies to Clouds, Texture, Shadows, Buildings, Trees, Reflections
        "off": "0", "aus": "0",
        "low": "0", "niedrig": "0",
        "medium": "1", "mittel": "1",
        "high": "2", "hoch": "2",
        "ultra": "3",
    },
    "_on_off": {
        "off": "0", "aus": "0", "nein": "0",
        "on": "1", "an": "1", "ja": "1", "ein": "1",
    },
    "Graphics.GrassAndBushes": {
        "off": "0", "aus": "0",
        "low": "1", "niedrig": "1",
        "medium": "2", "mittel": "2",
        "high": "3", "hoch": "3",
        "ultra": "4",
    },
    "Graphics.AnisotropicFilter": {
        "2x": "1", "4x": "2", "8x": "3", "16x": "4",
        "2": "1", "4": "2", "8": "3", "16": "4",
    },
    "GraphicsVR.AnisotropicFilter": {
        "2x": "1", "4x": "2", "8x": "3", "16x": "4",
        "2": "1", "4": "2", "8": "3", "16": "4",
    },
}

# Map keys to their value-alias group
_MSFS_VALUE_GROUP: dict[str, str] = {
    "Video.DLSS": "Video.DLSS",
    "Video.DLSSG": "Video.DLSSG",
    "RayTracing.Enabled": "_on_off",
    "RayTracing.Reflections": "_on_off",
    "Graphics.SSContact": "_on_off",
    "GraphicsVR.SSContact": "_on_off",
    "Graphics.MotionBlur": "_on_off",
    "GraphicsVR.MotionBlur": "_on_off",
    "Graphics.DepthOfField": "_on_off",
    "Graphics.AnisotropicFilter": "Graphics.AnisotropicFilter",
    "GraphicsVR.AnisotropicFilter": "GraphicsVR.AnisotropicFilter",
    "Graphics.GrassAndBushes": "Graphics.GrassAndBushes",
}
# Default group for quality-type settings (Clouds, Texture, Shadows, etc.)
_QUALITY_KEYS = {
    "Graphics.CloudsQuality", "GraphicsVR.CloudsQuality",
    "Graphics.TextureResolution", "GraphicsVR.TextureResolution",
    "Graphics.ShadowQuality", "GraphicsVR.ShadowQuality",
    "Graphics.Reflections", "GraphicsVR.Reflections",
    "Graphics.Buildings", "Graphics.Trees",
}


@mcp.tool()
def set_msfs_setting(
    setting: str,
    value: str,
    usercfg_path: str = "",
    auto_restart: bool = True,
    restart_in_vr: bool = True,
) -> dict:
    """Change a single MSFS 2024 graphics setting and restart so it takes effect.

    WICHTIG: MSFS muss neu gestartet werden damit Änderungen sichtbar werden!
    Standardmäßig wird MSFS automatisch beendet und neu gestartet.

    Use this when the user says things like:
    - "DLSS auf Quality stellen"
    - "Wolken auf Ultra"
    - "Schatten auf Medium"
    - "TAA auf DLSS umstellen"
    - "RayTracing ausschalten"
    - "Terrain LoD auf 2.0"
    - "Texturen auf High"
    - "Motion Blur aus"
    - "Anisotropisch auf 16x"

    Args:
        setting: Setting name (German or English). Examples:
                 "dlss", "wolken", "schatten", "texturen", "terrain lod",
                 "raytracing", "motion blur", "ssao", "reflexionen", etc.
        value: New value. Can be a name ("quality", "ultra", "aus", "an")
               or a number ("2", "1.5", "0"). For LoD values use decimals (e.g. "2.0").
        usercfg_path: Path to UserCfg.opt (auto-detected if empty).
        auto_restart: Restart MSFS automatically so the change is visible (default True).
        restart_in_vr: If restarting, use VR startup sequence (default True).
    """
    import time

    cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH
    if not cfg_path.exists():
        # Report which paths were checked
        searched = [str(p) for p in _USERCFG_CANDIDATES if not p.exists()]
        return {
            "error": f"UserCfg.opt nicht gefunden: {cfg_path}",
            "gesucht": searched[:6],
            "tipp": "Gib den Pfad zu deiner UserCfg.opt über usercfg_path an.",
        }

    # Resolve setting alias → "Section.Key"
    setting_lower = setting.strip().lower()
    config_key = _MSFS_SETTING_ALIASES.get(setting_lower)

    if config_key is None:
        for k in SETTING_DEFS:
            if k.lower() == setting_lower or k.split(".", 1)[-1].lower() == setting_lower:
                config_key = k
                break

    if config_key is None:
        for alias, key in _MSFS_SETTING_ALIASES.items():
            if setting_lower in alias or alias in setting_lower:
                config_key = key
                break

    if config_key is None:
        available = sorted(set(_MSFS_SETTING_ALIASES.values()))
        return {
            "error": f"Unbekannte Einstellung: '{setting}'",
            "verfügbare_einstellungen": available,
            "beispiele": [
                "dlss → Video.DLSS",
                "wolken → CloudsQuality",
                "schatten → ShadowQuality",
                "terrain → TerrainLoD",
                "raytracing → RayTracing",
            ],
        }

    # Resolve value alias → numeric string
    value_lower = value.strip().lower()
    group = _MSFS_VALUE_GROUP.get(config_key)
    if group is None and config_key in _QUALITY_KEYS:
        group = "_quality_4"

    if group and group in _MSFS_VALUE_ALIASES:
        resolved_value = _MSFS_VALUE_ALIASES[group].get(value_lower, value.strip())
    else:
        resolved_value = value.strip()

    # Read current value BEFORE killing (for display purposes)
    current_settings = _read_current_settings(cfg_path)
    old_value = current_settings.get(config_key, "?")

    defn = SETTING_DEFS.get(config_key, {})
    label = defn.get("label", config_key)
    value_map = defn.get("values", {})
    old_display = value_map.get(str(old_value), str(old_value))
    new_display = value_map.get(str(resolved_value), str(resolved_value))

    # ── CRITICAL ORDER: Kill FIRST → Write SECOND → Start THIRD ──
    # MSFS overwrites UserCfg.opt on exit. If we write first and then
    # kill, MSFS saves its own settings during shutdown and our changes
    # are lost. So we MUST kill MSFS before touching the file.

    msfs_was_running = _is_msfs_running()
    if msfs_was_running:
        _kill_msfs(timeout_s=30)
        # Wait for MSFS to release the file lock instead of blind sleep
        _wait_for_file_unlocked(cfg_path, timeout_s=10)

    # Re-read the file AFTER kill (MSFS may have written to it during shutdown)
    current_settings = _read_current_settings(cfg_path)
    old_value_after_kill = current_settings.get(config_key, "?")
    old_display = value_map.get(str(old_value_after_kill), str(old_value_after_kill))

    # Save snapshot before changing
    saved_version = _snapshot(cfg_path, f"Before setting {config_key}={resolved_value}")

    # NOW write the change (MSFS is dead, can't overwrite us)
    original_text = cfg_path.read_text(encoding="utf-8")
    entries = _parse_usercfg(original_text)
    new_entries, not_applied = _apply_overrides(entries, {config_key: resolved_value})
    new_text = _entries_to_text(new_entries)
    cfg_path.write_text(new_text, encoding="utf-8")

    if config_key in not_applied:
        return {
            "error": (
                f"Einstellung '{config_key}' wurde in UserCfg.opt nicht gefunden. "
                f"Möglicherweise existiert der Schlüssel unter einem anderen Namen."
            ),
            "config_file": str(cfg_path),
            "saved_version": saved_version,
        }

    # Verify the write actually stuck
    verify_settings = _read_current_settings(cfg_path)
    verified = verify_settings.get(config_key) == resolved_value

    result = {
        "status": "ok",
        "einstellung": label,
        "key": config_key,
        "vorher": old_display,
        "nachher": new_display,
        "raw_value": resolved_value,
        "config_file": str(cfg_path),
        "saved_version": saved_version,
        "verifiziert": verified,
    }

    if not verified:
        result["warnung"] = (
            f"Wert in Datei nach Schreiben: {verify_settings.get(config_key, '?')} "
            f"(erwartet: {resolved_value}). Datei wurde möglicherweise nicht korrekt geschrieben."
        )

    # Restart MSFS so changes become visible
    if auto_restart and (msfs_was_running or restart_in_vr):
        time.sleep(2)
        action = "beendet und in VR neu gestartet" if msfs_was_running else "in VR gestartet"
        action_plain = "beendet und neu gestartet" if msfs_was_running else "gestartet"
        if restart_in_vr:
            vr_result = launch_msfs_vr(force_restart=False)
            result["neustart"] = (
                f"MSFS {action}. {label}: {old_display} → {new_display}"
            )
            result["vr_launch"] = vr_result
        else:
            try:
                subprocess.Popen(["cmd", "/c", "start", f"steam://run/{_MSFS2024_STEAM_APP_ID}"])
                result["neustart"] = (
                    f"MSFS {action_plain}. {label}: {old_display} → {new_display}"
                )
            except Exception as exc:
                result["neustart"] = f"MSFS-Aktion fehlgeschlagen: {exc}"
    elif not auto_restart:
        result["hinweis"] = (
            f"Einstellung gespeichert: {label} = {new_display}. "
            f"MSFS muss neu gestartet werden damit die Änderung sichtbar wird."
        )
    else:
        result["hinweis"] = (
            f"Einstellung gespeichert: {label} = {new_display}. "
            f"Wird beim nächsten MSFS-Start übernommen."
        )

    return result


@mcp.tool()
def restore_msfs_graphics(
    version: int = 0,
    usercfg_path: str = "",
) -> dict:
    """Restore MSFS graphics settings to a previously saved version.

    Every call to optimize_msfs_graphics automatically saves the config
    before making changes.  Use this tool to list all saved versions or
    roll back to a specific one.

    Args:
        version: Version number to restore.  Pass 0 (or omit) to list all
                 available versions without restoring.
        usercfg_path: Full path to UserCfg.opt (leave empty for default).
    """
    cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH
    history = _load_history(cfg_path)

    if not history:
        return {"error": "No saved versions found. History is empty."}

    # List mode
    if version == 0:
        summary = [
            {
                "version": h["version"],
                "timestamp": h["timestamp"],
                "label": h["label"],
            }
            for h in history
        ]
        return {
            "available_versions": summary,
            "total": len(summary),
            "hint": "Call again with version=N to restore that version.",
        }

    # Find the requested version
    entry = next((h for h in history if h["version"] == version), None)
    if entry is None:
        return {
            "error": f"Version {version} not found.",
            "available": [h["version"] for h in history],
        }

    # Save current state before restoring (so restore is also undoable)
    if cfg_path.exists():
        _snapshot(cfg_path, f"Before restoring to version {version}")

    cfg_path.write_text(entry["content"], encoding="utf-8")

    return {
        "status": "ok",
        "restored_version": version,
        "label": entry["label"],
        "timestamp": entry["timestamp"],
        "file": str(cfg_path),
    }


@mcp.tool()
def backup_msfs_graphics(
    action: Literal["create", "list", "restore_latest"],
    label: str = "Manual backup",
    usercfg_path: str = "",
) -> dict:
    """Manually create, list or restore MSFS graphics backups.

    Backups are stored as individual .opt files in UserCfg_backups/ next to
    UserCfg.opt.  You can also restore the most recent backup at any time.

    Args:
        action: 'create' a new backup, 'list' existing backups, or
                'restore_latest' to restore the last backup.
        label: Description for the backup (only used with 'create').
        usercfg_path: Full path to UserCfg.opt (leave empty for default).
    """
    cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH

    if action == "create":
        if not cfg_path.exists():
            return {"error": f"UserCfg.opt not found at {cfg_path}"}
        version = _snapshot(cfg_path, label)
        bdir = _backup_dir(cfg_path)
        return {
            "status": "ok",
            "saved_version": version,
            "backup_folder": str(bdir),
            "label": label,
        }

    if action == "list":
        bdir = _backup_dir(cfg_path)
        if not bdir.exists():
            return {"backups": [], "count": 0}
        files = sorted(bdir.glob("UserCfg_v*.opt"), reverse=True)
        backups = [
            {"file": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
             "modified": datetime.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
            for f in files
        ]
        return {"backups": backups, "count": len(backups), "folder": str(bdir)}

    if action == "restore_latest":
        latest = _backup_dir(cfg_path) / "UserCfg_latest.opt"
        if not latest.exists():
            return {"error": "No backup found. Create one first."}
        # Save current state before restoring
        if cfg_path.exists():
            _snapshot(cfg_path, "Before restoring latest backup")
        shutil.copy2(latest, cfg_path)
        return {
            "status": "ok",
            "restored_from": str(latest),
            "file": str(cfg_path),
        }

    return {"error": f"Unknown action: {action}"}


@mcp.tool()
def check_and_install_driver(
    auto_install: bool = False,
    download_dir: str = "",
) -> dict:
    """Check the installed NVIDIA driver against the latest available version.

    Reads the current driver version and GPU model via NVML, then queries
    the NVIDIA driver API for the newest WHQL-certified driver.  Returns a
    comparison and the download link.  If *auto_install* is True the
    installer is downloaded and launched in silent mode (``/s /noreboot``).

    Args:
        auto_install: If True, download the installer and run it silently.
                      Requires the tool to run with administrator privileges.
        download_dir: Directory to save the downloaded installer.
                      Defaults to the user's Downloads folder.
    """
    # 1. Current driver info via NVML
    try:
        current_version, gpu_name = _get_gpu_info()
    except Exception as exc:
        return {"error": f"Could not read GPU info via NVML: {exc}"}

    # 2. Map GPU name to NVIDIA API parameters (dynamic lookup, then static)
    ids = _lookup_pfid(gpu_name)
    if ids is None:
        ids = _match_gpu_model(gpu_name)
    if ids is None:
        return {
            "error": (
                f"GPU '{gpu_name}' not found via NVIDIA API or static table. "
                "Check manually at: https://www.nvidia.com/Download/index.aspx"
            ),
            "current_driver": current_version,
            "gpu_name": gpu_name,
        }
    psid, pfid = ids

    # 3. Query NVIDIA for the latest driver
    try:
        latest = _query_latest_driver(psid, pfid)
    except Exception as exc:
        return {
            "error": f"NVIDIA API request failed: {exc}",
            "current_driver": current_version,
            "gpu_name": gpu_name,
        }

    if latest is None:
        return {
            "error": "Could not parse a driver version from the NVIDIA API response.",
            "current_driver": current_version,
            "gpu_name": gpu_name,
        }

    # 4. Compare versions
    up_to_date = _version_tuple(current_version) >= _version_tuple(latest["version"])

    result: dict = {
        "gpu_name": gpu_name,
        "current_driver": current_version,
        "latest_driver": latest["version"],
        "driver_name": latest["name"],
        "release_date": latest["release_date"],
        "up_to_date": up_to_date,
        "download_url": latest["download_url"],
    }

    if up_to_date:
        result["message"] = "Your driver is already up to date."
        return result

    result["message"] = (
        f"Update available: {current_version} -> {latest['version']}"
    )

    if not auto_install:
        return result

    # 5. Download the installer
    dl_dir = Path(download_dir) if download_dir else Path.home() / "Downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    try:
        installer = _download(latest["download_url"], dl_dir)
    except Exception as exc:
        result["install_error"] = f"Download failed: {exc}"
        return result

    result["installer_path"] = str(installer)

    # 6. Launch in silent mode
    try:
        proc = subprocess.Popen(
            [str(installer), "/s", "/noreboot"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result["install_status"] = "installer_started"
        result["installer_pid"] = proc.pid
        result["message"] = (
            f"Installer launched in silent mode (PID {proc.pid}). "
            "A reboot may be required after installation completes."
        )
    except Exception as exc:
        result["install_error"] = (
            f"Could not launch installer: {exc}. "
            "Try running as administrator."
        )

    return result


# ---------------------------------------------------------------------------
# install_mod helpers
# ---------------------------------------------------------------------------

def _detect_community_folder(custom_path: str) -> Path | None:
    """Return the Community folder path, or None if not found."""
    if custom_path:
        p = Path(custom_path)
        if p.is_dir():
            return p
        return None
    for candidate in COMMUNITY_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def _download(url: str, dest_dir: Path) -> Path:
    """Download a file from *url* into *dest_dir* and return the local path."""
    with httpx.Client(follow_redirects=True, timeout=300) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()

            # Try to derive a filename from Content-Disposition or the URL
            filename = None
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd, re.IGNORECASE)
            if m:
                filename = m.group(1).strip()
            if not filename:
                filename = url.split("/")[-1].split("?")[0]
            if not filename:
                filename = "mod_download.zip"

            local_path = dest_dir / filename
            with open(local_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
    return local_path


def _extract(archive: Path, dest: Path) -> None:
    """Extract a .zip archive into *dest*."""
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif suffix in (".7z", ".rar"):
        # py7zr for .7z, rarfile for .rar — optional deps
        if suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(archive) as sz:
                sz.extractall(dest)
        else:
            import rarfile
            with rarfile.RarFile(archive) as rf:
                rf.extractall(dest)
    else:
        raise ValueError(f"Unsupported archive format: {suffix}")


def _find_mod_roots(extract_dir: Path) -> list[Path]:
    """Identify mod root folders inside the extracted tree.

    A mod root is a folder that contains a manifest.json or layout.json
    (MSFS add-on markers).  If none are found we fall back to the
    top-level directories.
    """
    roots: list[Path] = []
    for marker in ("manifest.json", "layout.json"):
        for hit in extract_dir.rglob(marker):
            root = hit.parent
            if root not in roots:
                roots.append(root)
    if not roots:
        # Fallback: top-level dirs inside the extract
        roots = [d for d in extract_dir.iterdir() if d.is_dir()]
    return roots


def _resolve_flightsim_to(url: str) -> str:
    """If *url* is a flightsim.to page URL, try to resolve the direct download link.

    Supports URLs like:
      - https://flightsim.to/file/12345/some-mod
      - https://flightsim.to/addon/12345/some-mod
    Tries the flightsim.to API first, then scrapes the page for download links.
    Returns the original URL unchanged if it's not a flightsim.to page.
    """
    # Only process flightsim.to page URLs (not already direct download links)
    m = re.match(r"https?://flightsim\.to/(?:file|addon)/(\d+)", url)
    if not m:
        return url

    addon_id = m.group(1)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }

    with httpx.Client(follow_redirects=True, timeout=30, headers=headers) as client:
        # Try 1: flightsim.to API endpoint
        try:
            api_url = f"https://flightsim.to/api/v1/addon/{addon_id}"
            resp = client.get(api_url)
            if resp.status_code == 200:
                data = resp.json()
                dl = data.get("download_url") or data.get("download", {}).get("url")
                if dl:
                    return dl
        except Exception:
            pass

        # Try 2: Fetch the page and look for download links
        try:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

            # Look for direct download links in the HTML
            patterns = [
                r'href="(https?://flightsim\.to/api/v1/download/[^"]+)"',
                r'href="(https?://flightsim\.to/file/\d+/[^"]*?/download[^"]*)"',
                r'href="(https?://cdn[^"]*flightsim[^"]*\.(?:zip|7z|rar)[^"]*)"',
                r'data-url="(https?://[^"]+\.(?:zip|7z|rar)[^"]*)"',
                r'"downloadUrl"\s*:\s*"(https?://[^"]+)"',
            ]
            for pattern in patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    return match.group(1)

            # Try 3: Construct the standard download page URL
            download_page = f"https://flightsim.to/file/{addon_id}/download"
            resp2 = client.get(download_page)
            if resp2.status_code == 200:
                for pattern in patterns:
                    match = re.search(pattern, resp2.text, re.IGNORECASE)
                    if match:
                        return match.group(1)
        except Exception:
            pass

    return url


@mcp.tool()
def install_mod(
    url: str,
    community_path: str = "",
) -> dict:
    """Download and install an MSFS 2024 mod into the Community folder.

    Accepts both flightsim.to PAGE URLs (e.g. https://flightsim.to/addon/12345/name)
    and direct download URLs.  For flightsim.to pages the download link is
    resolved automatically.

    Supported archive formats: .zip (built-in), .7z (needs py7zr), .rar (needs rarfile).

    Args:
        url: flightsim.to addon page URL or direct download URL (.zip, .7z, .rar).
        community_path: Full path to your MSFS 2024 Community folder.
                        Leave empty for auto-detection (Steam & MS Store).
    """
    community = _detect_community_folder(community_path)
    if community is None:
        return {
            "error": (
                "Community folder not found. "
                "Please pass the full path via the community_path parameter."
            )
        }

    # Resolve flightsim.to page URLs to direct download links
    resolved_url = _resolve_flightsim_to(url)

    with tempfile.TemporaryDirectory(prefix="msfs_mod_") as tmp:
        tmp_path = Path(tmp)

        # 1. Download
        try:
            archive = _download(resolved_url, tmp_path)
        except httpx.HTTPStatusError as exc:
            return {
                "error": f"Download failed: HTTP {exc.response.status_code}",
                "resolved_url": resolved_url,
                "hint": (
                    "Falls die automatische Auflösung nicht funktioniert hat: "
                    "Öffne die Seite im Browser, klicke auf Download, "
                    "kopiere die direkte Download-URL und gib sie hier ein."
                ),
            }
        except Exception as exc:
            return {"error": f"Download failed: {exc}", "resolved_url": resolved_url}

        # 2. Extract
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            _extract(archive, extract_dir)
        except Exception as exc:
            return {"error": f"Extraction failed: {exc}"}

        # 3. Locate mod root(s)
        mod_roots = _find_mod_roots(extract_dir)
        if not mod_roots:
            return {"error": "No mod folders found in the archive."}

        # 4. Move into Community
        installed: list[str] = []
        for root in mod_roots:
            dest = community / root.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(root, dest)
            installed.append(root.name)

    return {
        "status": "ok",
        "community_folder": str(community),
        "installed_mods": installed,
    }


# ---------------------------------------------------------------------------
# Pimax settings analysis & optimization
# ---------------------------------------------------------------------------

# Possible Pimax config file locations
_PIMAX_CONFIG_CANDIDATES = [
    # Pimax Play / Pimax Client data directories
    Path(os.environ.get("LOCALAPPDATA", "")) / "Pimax" / "PimaxPlay",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Pimax" / "Pimax Play",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Pimax" / "PimaxClient",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Pimax",
    Path(os.environ.get("LOCALAPPDATA", "")) / "PimaxPlay",
    Path(os.environ.get("LOCALAPPDATA", "")) / "PimaxVR",
    Path(os.environ.get("APPDATA", "")) / "Pimax" / "PimaxPlay",
    Path(os.environ.get("APPDATA", "")) / "Pimax",
    Path(os.environ.get("APPDATA", "")) / "PimaxPlay",
    Path(os.environ.get("PROGRAMDATA", "")) / "Pimax" / "PimaxPlay",
    Path(os.environ.get("PROGRAMDATA", "")) / "Pimax",
    # Program Files install directories
    Path(r"C:\Program Files\Pimax\PimaxPlay"),
    Path(r"C:\Program Files\Pimax\Pimax Play"),
    Path(r"C:\Program Files\Pimax\Runtime"),
    Path(r"C:\Program Files\Pimax\PimaxClient"),
    Path(r"C:\Program Files\Pimax"),
    Path(r"C:\Program Files (x86)\Pimax"),
    Path(r"D:\Program Files\Pimax"),
    Path(r"D:\Pimax"),
    # User profile locations
    Path(os.environ.get("USERPROFILE", "")) / "Pimax",
    Path(os.environ.get("USERPROFILE", "")) / ".pimax",
    Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Pimax",
]

_PIMAX_CONFIG_FILENAMES = [
    "settings.json", "config.json", "pimax.json",
    "PimaxPlay.json", "user_settings.json", "device_settings.json",
    "pimax_settings.json", "hmd_settings.json", "display_settings.json",
    "general.json", "preference.json", "preferences.json",
]

# Human-readable labels and value maps for Pimax settings
PIMAX_SETTING_DEFS: dict[str, dict] = {
    "renderResolution": {
        "label": "Render Resolution",
        "description": "SteamVR Supersampling-Multiplikator",
    },
    "renderQuality": {
        "label": "Render Quality",
        "values": {"0": "Low", "1": "Medium", "2": "High", "3": "Ultra"},
    },
    "refreshRate": {
        "label": "Refresh Rate (Hz)",
        "description": "Display-Wiederholrate",
    },
    "fov": {
        "label": "Field of View",
        "values": {"small": "Small (100°)", "normal": "Normal (140°)",
                   "large": "Large (170°)", "potato": "Potato (80°)"},
    },
    "fovLevel": {
        "label": "Field of View",
        "values": {"0": "Potato", "1": "Small", "2": "Normal", "3": "Large"},
    },
    "smartSmoothing": {
        "label": "Smart Smoothing",
        "values": {"true": "An", "false": "Aus", "True": "An", "False": "Aus",
                   "0": "Aus", "1": "An"},
    },
    "compulsorySmoothing": {
        "label": "Compulsory Smoothing",
        "values": {"true": "An", "false": "Aus", "True": "An", "False": "Aus",
                   "0": "Aus", "1": "An"},
    },
    "ffrLevel": {
        "label": "Fixed Foveated Rendering",
        "values": {"0": "Aus", "1": "Low", "2": "Medium", "3": "High", "4": "Ultra",
                   "off": "Aus", "low": "Low", "medium": "Medium", "high": "High"},
    },
    "parallelProjection": {
        "label": "Parallel Projection",
        "values": {"true": "An", "false": "Aus", "True": "An", "False": "Aus",
                   "0": "Aus", "1": "An"},
    },
    "brightness": {
        "label": "Backlight Brightness",
        "description": "Display-Helligkeit (Bereich variiert je nach Pimax-Version)",
    },
    "contrast": {
        "label": "Contrast",
    },
    "ipd": {
        "label": "IPD (mm)",
    },
    "ipdOffset": {
        "label": "IPD Offset",
    },
    "smoothingMode": {
        "label": "Smoothing Mode",
    },
    "eyeTracking": {
        "label": "Eye Tracking",
        "values": {"true": "An", "false": "Aus", "0": "Aus", "1": "An"},
    },
    "dynamicFoveatedRendering": {
        "label": "Dynamic Foveated Rendering",
        "values": {"true": "An", "false": "Aus", "0": "Aus", "1": "An"},
    },
    # ── Pimax Play echte Config-Keys (Device Settings → Games → Color Options) ──
    # Pimax Play Hardware-Farbeinstellungen (Device Settings → Games → Color Options)
    # Schieberegler-Bereich: 0–100, Neutralwert = 50 (kein Effekt)
    # ACHTUNG: saturation=0 → Graustufen (B&W)!  brightness=0 → schwarzes Bild!
    "piplay_color_brightness_0": {
        "label": "Helligkeit Linkes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=sehr dunkel, 50=neutral (kein Effekt), 100=sehr hell",
        "description": "Pimax Play Display-Helligkeit (Hardware-Ebene, kein GPU-Aufwand).",
    },
    "piplay_color_brightness_1": {
        "label": "Helligkeit Rechtes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=sehr dunkel, 50=neutral (kein Effekt), 100=sehr hell",
        "description": "Pimax Play Display-Helligkeit rechtes Auge.",
    },
    "piplay_color_contrast_0": {
        "label": "Kontrast Linkes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=kein Kontrast, 50=neutral, 100=max Kontrast",
        "description": "Pimax Play Display-Kontrast (Hardware-Ebene).",
    },
    "piplay_color_contrast_1": {
        "label": "Kontrast Rechtes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=kein Kontrast, 50=neutral, 100=max Kontrast",
        "description": "Pimax Play Display-Kontrast rechtes Auge.",
    },
    "piplay_color_saturation_0": {
        "label": "Sättigung Linkes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=GRAUSTUFEN (B&W!), 50=neutral, 100=maximal gesättigt",
        "description": "Pimax Play Display-Sättigung (Hardware-Ebene).",
    },
    "piplay_color_saturation_1": {
        "label": "Sättigung Rechtes Auge",
        "min": 0, "max": 100, "default": 50,
        "unit": "slider",
        "hint": "0=GRAUSTUFEN (B&W!), 50=neutral, 100=maximal gesättigt",
        "description": "Pimax Play Display-Sättigung rechtes Auge.",
    },
    "piplay_refreshrate": {
        "label": "Refresh Rate (Hz)",
        "description": "Pimax Play Display-Wiederholrate.",
    },
    "piplay_fov": {
        "label": "Field of View",
    },
    "piplay_smartsmoothing": {
        "label": "Smart Smoothing",
        "values": {"true": "An", "false": "Aus", "0": "Aus", "1": "An"},
    },
    "piplay_ffr": {
        "label": "Fixed Foveated Rendering",
    },
    "piplay_pp": {
        "label": "Parallel Projection",
        "values": {"true": "An", "false": "Aus", "0": "Aus", "1": "An"},
    },
    "piplay_ipd": {
        "label": "IPD (mm)",
    },
}

# Pimax presets: key → value overrides
PIMAX_PRESETS: dict[str, dict] = {
    "Performance": {
        "renderResolution": 1.0,
        "refreshRate": 90,
        "fov": "normal",
        "fovLevel": 2,
        "smartSmoothing": True,
        "compulsorySmoothing": False,
        "ffrLevel": 2,
        "parallelProjection": False,
    },
    "Balanced": {
        "renderResolution": 1.25,
        "refreshRate": 90,
        "fov": "normal",
        "fovLevel": 2,
        "smartSmoothing": True,
        "compulsorySmoothing": False,
        "ffrLevel": 1,
        "parallelProjection": False,
    },
    "Quality": {
        "renderResolution": 1.5,
        "refreshRate": 90,
        "fov": "large",
        "fovLevel": 3,
        "smartSmoothing": False,
        "compulsorySmoothing": False,
        "ffrLevel": 0,
        "parallelProjection": False,
    },
    "MSFS_VR_Optimized": {
        "renderResolution": 1.0,
        "refreshRate": 72,
        "fov": "normal",
        "fovLevel": 2,
        "smartSmoothing": True,
        "compulsorySmoothing": True,
        "ffrLevel": 2,
        "parallelProjection": True,
        "brightness": 1,
    },
}

# Pimax VRAM-based recommendations
PIMAX_VRAM_TIPS: list[tuple[int, str, list[tuple[str, str, str]]]] = [
    # (min_vram_gb, tier, [(setting, suggestion, reason), ...])
    (16, "high_end", [
        ("renderResolution", "1.25 – 1.5",
         "Deine RTX 5080 kann höhere Render-Auflösung in Pimax gut handeln."),
        ("ffrLevel", "0 (Aus) oder 1 (Low)",
         "FFR bei 16 GB VRAM kaum nötig — nur bei Engpässen aktivieren."),
        ("refreshRate", "90",
         "90 Hz ist ideal für MSFS. 120 Hz nur wenn stabile FPS möglich."),
    ]),
    (10, "mid_high", [
        ("renderResolution", "1.0 – 1.25",
         "Render Resolution moderat halten, DLSS übernimmt das Upscaling."),
        ("ffrLevel", "1 (Low) oder 2 (Medium)",
         "FFR auf Low/Medium spart VRAM bei kaum sichtbarem Verlust."),
    ]),
    (8, "mid", [
        ("renderResolution", "1.0",
         "Bei 8 GB VRAM Pimax Render Resolution auf 1.0 lassen."),
        ("ffrLevel", "2 (Medium) oder 3 (High)",
         "FFR unbedingt aktivieren um VRAM zu sparen."),
        ("fov", "Normal oder Small",
         "FOV reduzieren spart massiv GPU-Leistung."),
    ]),
    (0, "low", [
        ("renderResolution", "0.75 – 1.0", "Render Resolution minimieren."),
        ("ffrLevel", "3 (High)", "FFR auf Maximum für spielbare FPS."),
        ("fov", "Small oder Potato", "FOV stark reduzieren."),
    ]),
]


_PIMAX_CONFIG_KEYS = (
    "renderResolution", "refreshRate", "fov", "fovLevel",
    "ffrLevel", "smartSmoothing", "parallelProjection",
    "brightness", "contrast", "ipd", "renderQuality",
    "compulsorySmoothing", "eyeTracking",
)


def _looks_like_pimax_config(path: Path) -> bool:
    """Check if a JSON file looks like a Pimax settings file."""
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and any(k in data for k in _PIMAX_CONFIG_KEYS):
            return True
    except Exception:
        pass
    return False


def _find_pimax_config(custom_path: str = "") -> Path | None:
    """Find the Pimax settings JSON file.

    Search order:
    1. Custom path (if provided)
    2. Known config directories + filenames
    3. Any .json/.cfg in known directories that looks like Pimax config
    4. Registry-based install path
    5. Recursive search in Pimax install directories
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return p
        if p.is_dir():
            for fname in _PIMAX_CONFIG_FILENAMES:
                candidate = p / fname
                if candidate.exists():
                    return candidate
            # Search recursively in the given directory
            for json_file in p.rglob("*.json"):
                if _looks_like_pimax_config(json_file):
                    return json_file
        return None

    # Step 2: Known directories + known filenames
    for config_dir in _PIMAX_CONFIG_CANDIDATES:
        if not config_dir.exists():
            continue
        for fname in _PIMAX_CONFIG_FILENAMES:
            candidate = config_dir / fname
            if candidate.exists():
                return candidate

    # Step 3: Any .json/.cfg in known directories
    for config_dir in _PIMAX_CONFIG_CANDIDATES:
        if not config_dir.exists():
            continue
        for pattern in ("*.json", "*.cfg"):
            for f in config_dir.glob(pattern):
                if _looks_like_pimax_config(f):
                    return f

    # Step 4: Find Pimax install via registry, then search there
    try:
        result = _reg(
            ["query",
             r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             "/s", "/f", "Pimax", "/d"],
            timeout=10,
        )
        for line in result.stdout.splitlines():
            m = re.search(r"InstallLocation\s+REG_SZ\s+(.+)", line)
            if m:
                install_dir = Path(m.group(1).strip())
                if install_dir.exists():
                    for fname in _PIMAX_CONFIG_FILENAMES:
                        candidate = install_dir / fname
                        if candidate.exists():
                            return candidate
                    for f in install_dir.rglob("*.json"):
                        if _looks_like_pimax_config(f):
                            return f
    except Exception:
        pass

    # Step 5: Also check HKCU registry
    try:
        result = _reg(
            ["query",
             r"HKCU\SOFTWARE\Pimax",
             "/s", "/v", "ConfigPath"],
            timeout=10,
        )
        for line in result.stdout.splitlines():
            m = re.search(r"ConfigPath\s+REG_SZ\s+(.+)", line)
            if m:
                p = Path(m.group(1).strip())
                if p.is_file():
                    return p
                if p.is_dir():
                    for fname in _PIMAX_CONFIG_FILENAMES:
                        candidate = p / fname
                        if candidate.exists():
                            return candidate
    except Exception:
        pass

    # Step 6: Brute-force search in all Pimax-related Program Files dirs
    for root in [r"C:\Program Files", r"C:\Program Files (x86)", r"D:\Program Files"]:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for pimax_dir in root_path.glob("Pimax*"):
            if pimax_dir.is_dir():
                for f in pimax_dir.rglob("*.json"):
                    if _looks_like_pimax_config(f):
                        return f

    return None


def _read_pimax_registry() -> dict:
    """Read Pimax settings from the Windows registry."""
    settings: dict = {}
    reg_paths = [
        r"HKCU\SOFTWARE\Pimax",
        r"HKCU\SOFTWARE\Pimax\PimaxPlay",
        r"HKCU\SOFTWARE\Pimax\Pimax Play",
        r"HKLM\SOFTWARE\Pimax",
        r"HKLM\SOFTWARE\Pimax\PimaxPlay",
    ]
    for reg_path in reg_paths:
        try:
            result = _reg(["query", reg_path, "/s"], timeout=5)
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                # Match lines like: piplay_color_brightness_0    REG_DWORD    0x62
                m = re.match(r"(\S+)\s+REG_(?:DWORD|SZ|QWORD)\s+(.+)", line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    # Convert hex DWORD values
                    if val.startswith("0x"):
                        try:
                            settings[key] = int(val, 16)
                        except ValueError:
                            settings[key] = val
                    else:
                        try:
                            settings[key] = int(val)
                        except ValueError:
                            try:
                                settings[key] = float(val)
                            except ValueError:
                                settings[key] = val
        except Exception:
            continue
    return settings


def _read_pimax_settings(config_path: Path) -> dict:
    """Read Pimax settings from config file + registry.

    Registry values override JSON values since Pimax Play
    typically stores live settings in the registry.
    """
    # Read JSON config
    data: dict = {}
    try:
        text = config_path.read_text(encoding="utf-8")
        parsed = _json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        pass

    # Merge with registry values (registry wins for overlapping keys)
    reg = _read_pimax_registry()
    if reg:
        data.update(reg)

    return data


def _pimax_not_found_error() -> dict:
    """Return a helpful error when Pimax config is not found, with auto-search."""
    # Try to find it via PowerShell
    found_paths: list[str] = []
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             r'Get-ChildItem -Path "C:\","D:\" -Recurse -Filter "*.json" '
             r'-ErrorAction SilentlyContinue | '
             r'Where-Object { $_.DirectoryName -match "Pimax" } | '
             r'Select-Object -First 20 -ExpandProperty FullName'],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            found_paths = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
    except Exception:
        pass

    msg = "Pimax-Konfigurationsdatei nicht automatisch gefunden."
    if found_paths:
        msg += " Folgende Pimax-Dateien wurden auf dem System gefunden:"
        return {
            "error": msg,
            "found_pimax_files": found_paths,
            "hint": (
                "Gib den Pfad zur richtigen Datei über config_path an. "
                "Suche nach einer JSON-Datei die Einstellungen wie "
                "brightness, refreshRate, fov etc. enthält."
            ),
        }
    else:
        return {
            "error": msg,
            "searched_locations": [str(p) for p in _PIMAX_CONFIG_CANDIDATES[:8]],
            "hint": (
                "Ist Pimax Play installiert? Gib den Installationsordner "
                "oder den Config-Pfad über config_path an."
            ),
        }


def _pimax_human_value(key: str, raw) -> str:
    """Convert a Pimax setting value to human-readable form."""
    defn = PIMAX_SETTING_DEFS.get(key)
    if defn and "values" in defn:
        return defn["values"].get(str(raw), str(raw))
    return str(raw)


@mcp.tool()
def analyze_pimax_settings(
    config_path: str = "",
) -> dict:
    """Analyze current Pimax VR headset settings with recommendations.

    Reads the Pimax configuration file, displays all settings in a table,
    and provides GPU-aware optimization recommendations.

    Present the results as markdown tables with columns:
    Setting | Aktuell | Status | Empfehlung | Grund

    Args:
        config_path: Path to Pimax config file or folder (auto-detected if empty).
    """
    cfg = _find_pimax_config(config_path)
    if cfg is None:
        return _pimax_not_found_error()

    settings = _read_pimax_settings(cfg)
    if not settings:
        return {"error": f"Konnte {cfg} nicht als Pimax-Config lesen."}

    # GPU info for recommendations
    gpu_name = "Unknown"
    vram_mb = 0
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(gpu_name, bytes):
            gpu_name = gpu_name.decode()
        vram_mb = round(pynvml.nvmlDeviceGetMemoryInfo(handle).total / 1024 / 1024)
        pynvml.nvmlShutdown()
    except Exception:
        pass

    vram_gb = vram_mb / 1024

    # Build recommendations based on VRAM
    tip_by_key: dict[str, dict] = {}
    for min_gb, tier, tips_list in PIMAX_VRAM_TIPS:
        if vram_gb >= min_gb:
            for key, suggestion, reason in tips_list:
                if key in settings:
                    current = settings[key]
                    tip_by_key[key] = {
                        "suggestion": suggestion,
                        "reason": reason,
                    }
            break

    # MSFS-specific Pimax tips (always apply)
    if settings.get("parallelProjection") in (False, "false", 0, "0"):
        tip_by_key["parallelProjection"] = {
            "suggestion": "An",
            "reason": "MSFS 2024 benötigt Parallel Projection für korrekte Darstellung in Pimax.",
        }
    refresh = settings.get("refreshRate", 90)
    if isinstance(refresh, (int, float)) and refresh > 90:
        tip_by_key["refreshRate"] = {
            "suggestion": "72 oder 90",
            "reason": "MSFS schafft selten über 45 FPS in VR. 72/90 Hz mit Smart Smoothing ist flüssiger.",
        }

    # Build table
    table: list[dict] = []
    for key, raw in settings.items():
        defn = PIMAX_SETTING_DEFS.get(key, {})
        label = defn.get("label", key)
        display = _pimax_human_value(key, raw)
        tip = tip_by_key.get(key)

        table.append({
            "setting": label,
            "current": display,
            "status": "CHANGE" if tip else "OK",
            "recommendation": tip["suggestion"] if tip else "-",
            "reason": tip["reason"] if tip else "",
        })

    changes_needed = len(tip_by_key)

    return {
        "config_file": str(cfg),
        "gpu": gpu_name,
        "vram_gb": round(vram_gb, 1),
        "pimax_settings": table,
        "summary": {
            "total_settings": len(table),
            "changes_recommended": changes_needed,
            "verdict": (
                "Pimax-Einstellungen sind optimal!"
                if changes_needed == 0
                else f"{changes_needed} Einstellung(en) sollten angepasst werden."
            ),
        },
        "display_instructions": (
            "IMPORTANT: Display ALL rows as a markdown table with columns: "
            "Einstellung | Aktuell | Status | Empfehlung | Grund. "
            "Use green checkmarks for OK, red X for CHANGE. Show EVERY setting. "
            "Answer in German."
        ),
    }


@mcp.tool()
def diagnose_pimax() -> dict:
    """Full diagnostic of all Pimax config files, registry entries and processes.

    Use this to find WHERE Pimax stores its settings. Shows:
    - All Pimax-related JSON/config files and their contents
    - All Pimax registry entries with values
    - Running Pimax processes
    - Pimax install locations

    Call this when Pimax settings appear incorrect to debug.
    """
    result: dict = {"sources": []}

    # 1. Find ALL Pimax config files
    config_files: list[dict] = []
    search_roots = [
        Path(os.environ.get("LOCALAPPDATA", "")),
        Path(os.environ.get("APPDATA", "")),
        Path(os.environ.get("PROGRAMDATA", "")),
        Path(os.environ.get("USERPROFILE", "")),
    ]
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if "pimax" not in str(p).lower():
                    continue
                if p.suffix.lower() in (".json", ".cfg", ".ini", ".xml", ".config"):
                    entry: dict = {"path": str(p), "size": p.stat().st_size}
                    if p.suffix.lower() == ".json" and p.stat().st_size < 500_000:
                        try:
                            data = _json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(data, dict):
                                # Show all keys with brightness/contrast/color
                                relevant = {
                                    k: v for k, v in data.items()
                                    if any(term in k.lower() for term in
                                           ("bright", "contrast", "color", "render",
                                            "refresh", "fov", "smooth", "ffr", "ipd",
                                            "parallel", "resolution", "quality"))
                                }
                                entry["relevant_settings"] = relevant
                                entry["all_keys"] = list(data.keys())
                        except Exception:
                            pass
                    config_files.append(entry)
        except PermissionError:
            continue

    # Also check Program Files
    for pf in [r"C:\Program Files\Pimax", r"C:\Program Files (x86)\Pimax",
               r"D:\Program Files\Pimax", r"D:\Pimax"]:
        pf_path = Path(pf)
        if not pf_path.exists():
            continue
        try:
            for p in pf_path.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() in (".json", ".cfg", ".ini", ".xml", ".config", ".db", ".sqlite"):
                    entry = {"path": str(p), "size": p.stat().st_size}
                    if p.suffix.lower() == ".json" and p.stat().st_size < 500_000:
                        try:
                            data = _json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(data, dict):
                                relevant = {
                                    k: v for k, v in data.items()
                                    if any(term in k.lower() for term in
                                           ("bright", "contrast", "color", "render",
                                            "refresh", "fov", "smooth", "ffr", "ipd"))
                                }
                                entry["relevant_settings"] = relevant
                                entry["all_keys"] = list(data.keys())
                        except Exception:
                            pass
                    config_files.append(entry)
        except PermissionError:
            continue

    result["config_files"] = config_files

    # 2. Read ALL Pimax registry entries
    registry: dict[str, dict] = {}
    reg_roots = [
        r"HKCU\SOFTWARE\Pimax",
        r"HKLM\SOFTWARE\Pimax",
        r"HKCU\SOFTWARE\PimaxPlay",
        r"HKLM\SOFTWARE\PimaxPlay",
        r"HKCU\SOFTWARE\Pimax Play",
    ]
    for rp in reg_roots:
        try:
            r = _reg(["query", rp, "/s"], timeout=10)
            if r.returncode != 0:
                continue
            current_subkey = rp
            entries: dict = {}
            for line in r.stdout.splitlines():
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                if line_stripped.startswith("HKEY_"):
                    current_subkey = line_stripped
                    if current_subkey not in registry:
                        registry[current_subkey] = {}
                else:
                    m = re.match(r"(\S+)\s+(REG_\S+)\s+(.*)", line_stripped)
                    if m:
                        name, reg_type, val = m.group(1), m.group(2), m.group(3).strip()
                        display_val = val
                        if reg_type == "REG_DWORD" and val.startswith("0x"):
                            try:
                                display_val = f"{val} ({int(val, 16)})"
                            except ValueError:
                                pass
                        registry.setdefault(current_subkey, {})[name] = {
                            "type": reg_type,
                            "value": display_val,
                        }
        except Exception:
            continue

    result["registry"] = registry

    # 3. Running Pimax processes
    processes: list[str] = []
    try:
        r = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if "pimax" in line.lower() or "pitool" in line.lower():
                processes.append(line.strip().split(",")[0].strip('"'))
    except Exception:
        pass
    result["running_processes"] = processes

    # 4. Currently detected config
    cfg = _find_pimax_config()
    result["auto_detected_config"] = str(cfg) if cfg else None

    result["display_instructions"] = (
        "Show ALL config files with their relevant settings. "
        "Show ALL registry entries. Highlight any keys containing "
        "'brightness', 'contrast', or 'color'. "
        "This helps identify which source has the correct values."
    )

    return result


@mcp.tool()
def optimize_pimax_settings(
    preset: Literal["Performance", "Balanced", "Quality", "MSFS_VR_Optimized"] = "Balanced",
    custom_overrides: dict | None = None,
    config_path: str = "",
    restart_service: bool = True,
    force_restart: bool = False,
    dry_run: bool = False,
) -> dict:
    """Optimize Pimax VR headset settings with a preset.

    Saves the current config as backup before applying changes.
    Writes to BOTH the JSON config and the Pimax registry hive — Pimax Play
    reads its live settings from the registry, so JSON-only writes are
    silently ignored. Restarts Pimax Play afterwards so the changes become
    visible (skipped during active VR sessions unless force_restart=True).

    Available presets:
    - Performance: 1.0x, 90Hz, Normal FOV, FFR Medium, Smart Smoothing An
    - Balanced: 1.25x, 90Hz, Normal FOV, FFR Low, Smart Smoothing An
    - Quality: 1.5x, 90Hz, Large FOV, FFR Aus, Smart Smoothing Aus
    - MSFS_VR_Optimized: Speziell für Flight Sim — 1.0x, 72Hz, Parallel Projection An

    Args:
        preset: Which preset to apply.
        custom_overrides: Optional extra settings as {"key": value} applied after the preset.
        config_path: Path to Pimax config file (auto-detected if empty).
        restart_service: If True, restarts Pimax Play so changes take effect live.
        force_restart: If True, restarts Pimax even during a running VR session
                       (interrupts it). Use only with explicit user consent.
        dry_run: If True, only return what WOULD change. Does not write
                 anything, does not create a backup, does not restart Pimax.
    """
    cfg = _find_pimax_config(config_path)
    if cfg is None:
        return _pimax_not_found_error()

    current = _read_pimax_settings(cfg)
    if not current:
        return {"error": f"Konnte {cfg} nicht lesen."}

    overrides = dict(PIMAX_PRESETS.get(preset, {}))
    if custom_overrides:
        overrides.update(custom_overrides)

    diff: dict[str, dict] = {}
    applied: dict = {}
    for key, value in overrides.items():
        if key in current:
            old_val = current[key]
            if old_val != value:
                diff[key] = {"old": old_val, "new": value}
            applied[key] = value

    if dry_run:
        logger.info("optimize_pimax_settings DRY RUN preset=%s diff=%s", preset, diff)
        return {
            "status": "dry_run",
            "preset": preset,
            "config_file": str(cfg),
            "would_apply": {k: str(v) for k, v in applied.items()},
            "diff": diff,
            "not_found_in_config": [k for k in overrides if k not in applied],
            "note": "Nichts wurde geschrieben. Setze dry_run=False um wirklich anzuwenden.",
        }

    backup_file = _pimax_create_backup(cfg, current)

    # Apply changes to in-memory dict, then write JSON
    for key, value in applied.items():
        current[key] = value
    cfg.write_text(_json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write to registry — without this, Pimax Play silently ignores JSON changes
    registry_audit = _apply_pimax_settings_to_registry(applied, verify=True)

    logger.info("optimize_pimax_settings applied preset=%s changes=%s", preset, list(applied.keys()))
    result = {
        "status": "ok",
        "preset": preset,
        "config_file": str(cfg),
        "backup": str(backup_file),
        "applied": {k: str(v) for k, v in applied.items()},
        "diff": diff,
        "registry_written": registry_audit,
        "not_found_in_config": [k for k in overrides if k not in applied],
    }

    _pimax_apply_restart(result, restart_service, force_restart)
    return result


@mcp.tool()
def restore_pimax_settings(
    config_path: str = "",
) -> dict:
    """Restore Pimax settings from the latest backup.

    Args:
        config_path: Path to Pimax config file (auto-detected if empty).
    """
    cfg = _find_pimax_config(config_path)
    if cfg is None:
        return _pimax_not_found_error()

    backup_dir = cfg.parent / "pimax_backups"
    latest = backup_dir / "pimax_config_latest.json"

    if not latest.exists():
        # List available backups
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("pimax_config_*.json"), reverse=True)
            return {
                "error": "Kein 'latest' Backup gefunden.",
                "available_backups": [f.name for f in backups[:10]],
            }
        return {"error": "Kein Pimax-Backup vorhanden."}

    backup_data = latest.read_text(encoding="utf-8")
    cfg.write_text(backup_data, encoding="utf-8")

    return {
        "status": "ok",
        "restored_from": str(latest),
        "config_file": str(cfg),
        "hint": "Pimax Play neu starten, damit die Änderungen wirksam werden.",
    }


# ── Shared helpers for Pimax write/restart, used by set_pimax_setting,
#    optimize_pimax_settings, and improve_image_clarity ──────────────────

_PIMAX_BACKUP_KEEP = 20
_MSFS_BACKUP_KEEP = 20
_MSFS_HISTORY_KEEP = 30


def _prune_pimax_backups(backup_dir: Path, keep: int = _PIMAX_BACKUP_KEEP) -> int:
    """Delete oldest pimax_config_*.json backups, keeping the newest `keep`.

    Never deletes pimax_config_latest.json. Returns number of files removed.
    """
    if not backup_dir.exists():
        return 0
    backups = sorted(
        (
            p for p in backup_dir.glob("pimax_config_*.json")
            if p.name != "pimax_config_latest.json"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            deleted += 1
        except Exception as exc:
            logger.warning("Could not prune backup %s: %s", old, exc)
    if deleted:
        logger.info("Pruned %d old Pimax backup(s) from %s", deleted, backup_dir)
    return deleted


def _pimax_create_backup(cfg: Path, current_state: dict) -> Path:
    """Save a timestamped backup of `current_state` and update 'latest'.

    Auto-prunes old backups so the folder doesn't grow unbounded.
    Returns the path to the new timestamped backup file.
    """
    backup_dir = cfg.parent / "pimax_backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"pimax_config_{ts}.json"
    payload = _json.dumps(current_state, indent=2, ensure_ascii=False)
    backup_file.write_text(payload, encoding="utf-8")
    latest = backup_dir / "pimax_config_latest.json"
    latest.write_text(payload, encoding="utf-8")
    _prune_pimax_backups(backup_dir, keep=_PIMAX_BACKUP_KEEP)
    logger.info("Pimax backup created: %s", backup_file.name)
    return backup_file


def _prune_msfs_backups(cfg_path: Path, keep: int = _MSFS_BACKUP_KEEP) -> int:
    """Delete oldest UserCfg_v*.opt backups, keeping the newest `keep`.

    Never deletes UserCfg_latest.opt. Returns number of files removed.
    """
    backup_dir = cfg_path.parent / "UserCfg_backups"
    if not backup_dir.exists():
        return 0
    backups = sorted(
        (
            p for p in backup_dir.glob("UserCfg_v*.opt")
            if p.name != "UserCfg_latest.opt"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            deleted += 1
        except Exception as exc:
            logger.warning("Could not prune backup %s: %s", old, exc)
    if deleted:
        logger.info("Pruned %d old MSFS backup(s)", deleted)
    return deleted


def _wait_for_file_unlocked(path: Path, timeout_s: int = 10) -> bool:
    """Wait until `path` can be opened for writing (no other process holds it).

    Returns True once the file can be opened r+b, False on timeout.
    Replaces blind sleep() calls after killing processes that held the file.
    """
    import time as _time
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            with open(path, "r+b"):
                return True
        except (PermissionError, OSError):
            _time.sleep(0.3)
    logger.warning("File still locked after %ds: %s", timeout_s, path)
    return False


def _wait_for_process_exit(name: str, timeout_s: int = 30) -> bool:
    """Wait until the named process is no longer running. Returns True if exited."""
    import time as _time
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if not _is_running(name):
            return True
        _time.sleep(0.5)
    return False


def _apply_pimax_settings_to_registry(
    updates: dict[str, object],
    verify: bool = True,
) -> dict[str, dict]:
    """Write key→value pairs into the Pimax registry hives.

    Pimax Play reads live settings from the registry, so JSON-only writes
    are silently ignored. Only writes a key if it already exists in one of
    the candidate hives.

    If `verify` is True (default), reads each value back after writing and
    flags any mismatches in the result.

    Returns: dict of {key: {"hive": ..., "written_value": ..., "verified": bool, "actual": ...}}
    """
    result: dict[str, dict] = {}
    reg_paths_to_try = [
        r"HKCU\SOFTWARE\Pimax",
        r"HKCU\SOFTWARE\Pimax\PimaxPlay",
    ]
    for mk, new_val in updates.items():
        for rp in reg_paths_to_try:
            try:
                check = _reg(["query", rp, "/v", mk], timeout=5)
                if check.returncode != 0:
                    continue
                if isinstance(new_val, bool):
                    reg_val = "1" if new_val else "0"
                    reg_type = "REG_DWORD"
                elif isinstance(new_val, int):
                    reg_val = str(new_val)
                    reg_type = "REG_DWORD"
                elif isinstance(new_val, float):
                    reg_val = str(new_val)
                    reg_type = "REG_SZ"
                else:
                    reg_val = str(new_val)
                    reg_type = "REG_SZ"
                add_res = _reg_add(rp, mk, reg_type, reg_val, timeout=5)
                entry: dict = {
                    "hive": rp,
                    "written_value": reg_val,
                    "reg_type": reg_type,
                    "verified": None,
                }
                if verify:
                    readback = _reg(["query", rp, "/v", mk], timeout=5)
                    actual: str | None = None
                    if readback.returncode == 0:
                        m = re.search(
                            rf"{re.escape(mk)}\s+REG_(?:DWORD|SZ|QWORD)\s+(\S+)",
                            readback.stdout,
                        )
                        if m:
                            raw = m.group(1).strip()
                            if raw.startswith("0x"):
                                try:
                                    actual = str(int(raw, 16))
                                except ValueError:
                                    actual = raw
                            else:
                                actual = raw
                    entry["actual"] = actual
                    entry["verified"] = (actual == reg_val)
                    if not entry["verified"]:
                        logger.warning(
                            "Pimax registry write NOT verified: %s in %s "
                            "(wrote %r, read %r). Could be permission or type mismatch.",
                            mk, rp, reg_val, actual,
                        )
                if add_res.returncode != 0:
                    logger.warning(
                        "reg add for %s returned %d: %s",
                        mk, add_res.returncode, add_res.stderr.strip(),
                    )
                else:
                    logger.info("Pimax registry: %s = %r in %s", mk, reg_val, rp)
                result[mk] = entry
                break
            except Exception as exc:
                logger.exception("Failed to write Pimax registry key %s: %s", mk, exc)
                continue
    return result


def _pimax_apply_restart(
    result: dict,
    restart_service: bool,
    force_restart: bool = False,
) -> None:
    """Restart Pimax Play to apply changes; mutates `result` with status messages.

    By default Pimax is NOT restarted while a VR session is active because
    that crashes the session. Pass force_restart=True to override.
    """
    import time

    if not restart_service:
        result["hinweis"] = (
            "Einstellungen gespeichert (ohne Neustart). "
            "Setze restart_service=True um sofort zu übernehmen."
        )
        return

    vr_game_running = _is_msfs_running() or _is_running("vrserver.exe")
    pimax_is_running = any(_is_running(name) for name in _PIMAX_EXE_NAMES)

    if pimax_is_running and vr_game_running and not force_restart:
        result["hinweis"] = (
            "Einstellungen in Config/Registry gespeichert. "
            "Pimax Play wurde NICHT neu gestartet (VR-Session läuft — "
            "Neustart würde sie crashen). Damit die Änderung sichtbar wird: "
            "VR-Spiel beenden und 'Pimax neu starten' sagen, ODER diesen "
            "Aufruf mit force_restart=True wiederholen (unterbricht die VR-Session)."
        )
        return

    if not pimax_is_running:
        result["hinweis"] = (
            "Einstellungen gespeichert. Pimax Play läuft nicht — "
            "Änderungen werden beim nächsten Start übernommen."
        )
        return

    restarted = False
    for exe in _PIMAX_EXE_NAMES:
        if not _is_running(exe):
            continue
        try:
            subprocess.run(
                ["taskkill", "/IM", exe, "/F"],
                capture_output=True, timeout=10,
            )
            time.sleep(2)
            pimax_exe = _find_pimax()
            if pimax_exe:
                subprocess.Popen(
                    [pimax_exe],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                )
                restarted = True
        except Exception:
            pass
        break

    if restarted:
        if force_restart and vr_game_running:
            result["service_restart"] = (
                "Pimax Play wurde neu gestartet — Änderungen aktiv "
                "(VR-Session wurde unterbrochen wegen force_restart=True)."
            )
        else:
            result["service_restart"] = (
                "Pimax Play wurde neu gestartet — Änderungen sind sofort aktiv."
            )
    else:
        result["hinweis"] = "Pimax Play muss ggf. manuell neu gestartet werden."


@mcp.tool()
def set_pimax_setting(
    setting: str,
    value: str,
    config_path: str = "",
    restart_service: bool = True,
    force_restart: bool = False,
    dry_run: bool = False,
) -> dict:
    """Set a single Pimax setting directly (e.g. brightness, contrast, refresh rate).

    Use this when the user asks to change a specific Pimax setting like
    "Helligkeit erhöhen", "Contrast auf 5", "Refresh Rate auf 72" etc.

    Creates a backup before making changes. Optionally restarts the Pimax
    service so the change takes effect immediately.

    WICHTIG: Wenn der User in VR ist und sagt "ich sehe keine Änderung",
    dann liegt das daran dass restart_service während VR-Sessions blockiert
    ist (sonst Crash). Schlage dann vor: VR beenden und Pimax neu starten,
    ODER rufe dieses Tool nochmal mit force_restart=True auf.

    Für "klares Bild" / "schärfer" / "sharpness": dieses Tool ändert nur
    eine einzelne Einstellung. Verwende stattdessen improve_image_clarity,
    das mehrere Schärfe-relevante Einstellungen kombiniert.

    Setze dry_run=True um vorher zu sehen, was sich ändern würde,
    ohne tatsächlich zu schreiben.

    Known settings and typical values:
    - brightness / helligkeit: Integer slider value (as shown in Pimax Play). Sets both eyes.
    - contrast / kontrast: Integer slider value. Sets both eyes.
    - saturation / sättigung: Integer slider value. Sets both eyes.
    - renderResolution: 0.5-2.0 (e.g. 1.0, 1.25, 1.5)
    - refreshRate: 72, 90, 120, 144
    - fov / fovLevel: "small"/"normal"/"large" or 0-3
    - smartSmoothing: true/false
    - compulsorySmoothing: true/false
    - ffrLevel: 0 (off) - 4 (ultra). Niedriger = klareres Bild in der Peripherie.
    - parallelProjection: true/false
    - ipd: 55-75 (mm)
    - eyeTracking: true/false

    Supports relative values: "+10", "-20" to adjust from current.

    Args:
        setting: Setting key (e.g. "brightness", "contrast", "refreshRate").
        value: New value as string. Will be auto-converted to the correct type.
        config_path: Path to Pimax config (auto-detected if empty).
        restart_service: If True, restarts Pimax Play so changes take effect live.
        force_restart: If True, restarts Pimax even if a VR game is running
                       (this WILL interrupt the VR session). Use only when the
                       user explicitly accepts that.
        dry_run: If True, only return what WOULD change. No write, no backup, no restart.
    """
    cfg = _find_pimax_config(config_path)
    if cfg is None:
        return _pimax_not_found_error()

    current = _read_pimax_settings(cfg)
    if not current:
        return {"error": f"Konnte {cfg} nicht lesen."}

    # Normalize setting name — German aliases → search keywords
    # These map to a keyword used to find matching keys in the config
    _ALIASES = {
        "helligkeit": "brightness",
        "heller": "brightness",
        "dunkler": "brightness",
        "kontrast": "contrast",
        "sättigung": "saturation",
        "auflösung": "renderresolution",
        "render": "renderresolution",
        "render_resolution": "renderresolution",
        "render resolution": "renderresolution",
        "wiederholrate": "refreshrate",
        "refresh": "refreshrate",
        "refresh_rate": "refreshrate",
        "refresh rate": "refreshrate",
        "hz": "refreshrate",
        "sichtfeld": "fov",
        "field_of_view": "fov",
        "field of view": "fov",
        "fov_level": "fovlevel",
        "fov level": "fovlevel",
        "smart_smoothing": "smartsmoothing",
        "smart smoothing": "smartsmoothing",
        "smoothing": "smartsmoothing",
        "glättung": "smartsmoothing",
        "compulsory_smoothing": "compulsorysmoothing",
        "compulsory smoothing": "compulsorysmoothing",
        "ffr": "ffrlevel",
        "ffr_level": "ffrlevel",
        "ffr level": "ffrlevel",
        "foveated": "ffr",
        "foveated_rendering": "ffr",
        "parallel_projection": "parallelProjection",
        "parallel projection": "parallelProjection",
        "pp": "parallelProjection",
        "eye_tracking": "eyetracking",
        "eye tracking": "eyetracking",
        "augentracking": "eyetracking",
        "dynamic_foveated": "dynamicfoveated",
        "dynamic foveated": "dynamicfoveated",
        "dfr": "dynamicfoveated",
        "ipd_offset": "ipdoffset",
        "ipd offset": "ipdoffset",
        # Schärfe / Klarheit — höhere Render-Auflösung = klareres Bild
        "klares bild": "renderresolution",
        "klareres bild": "renderresolution",
        "schärfer": "renderresolution",
        "schaerfer": "renderresolution",
        "schärfe": "renderresolution",
        "schaerfe": "renderresolution",
        "sharper": "renderresolution",
        "sharpness": "renderresolution",
        "klarheit": "renderresolution",
        "supersampling": "renderresolution",
    }
    search_term = _ALIASES.get(setting.lower().strip(), setting.strip().lower())

    # Find ALL matching keys in config (e.g. "brightness" matches
    # "piplay_color_brightness_0" AND "piplay_color_brightness_1")
    matching_keys: list[str] = []

    # 1. Exact match
    if setting.strip() in current:
        matching_keys = [setting.strip()]
    else:
        # 2. Case-insensitive exact match
        for k in current:
            if k.lower() == search_term:
                matching_keys = [k]
                break

    # 3. Substring/fuzzy match — find all keys containing the search term
    if not matching_keys:
        for k in current:
            if search_term in k.lower():
                matching_keys.append(k)

    # 4. If still nothing, try the original setting name as substring
    if not matching_keys:
        for k in current:
            if setting.strip().lower() in k.lower():
                matching_keys.append(k)

    # 5. If still nothing, add as new key
    if not matching_keys:
        matching_keys = [setting.strip()]

    actual_key = matching_keys[0]  # primary key for display

    # Auto-convert value to the correct type
    parsed_value: object
    v_lower = value.strip().lower()
    if v_lower in ("true", "an", "ein", "on", "ja", "yes", "aktiv"):
        parsed_value = True
    elif v_lower in ("false", "aus", "off", "nein", "no", "deaktiv"):
        parsed_value = False
    else:
        # Try int, then float, else keep string
        try:
            parsed_value = int(value)
        except ValueError:
            try:
                parsed_value = float(value)
            except ValueError:
                parsed_value = value.strip()

    # Relative adjustments: "+10", "-20"
    is_relative = False
    v_stripped = value.strip()
    if v_stripped.startswith("+") or (v_stripped.startswith("-") and len(v_stripped) > 1):
        try:
            delta = float(v_stripped)
            is_relative = True
        except ValueError:
            pass

    # Compute new values BEFORE writing — supports dry_run
    applied: dict[str, dict] = {}
    new_values: dict[str, object] = {}
    for mk in matching_keys:
        old_val = current.get(mk, 0)
        if is_relative and isinstance(old_val, (int, float)):
            new_val = type(old_val)(old_val + delta)
        else:
            new_val = parsed_value
        defn = PIMAX_SETTING_DEFS.get(mk, {})

        # Clamp to defined range if available
        if defn.get("unit") == "slider" and isinstance(new_val, (int, float)):
            s_min = defn.get("min", 0)
            s_max = defn.get("max", 100)
            new_val = max(s_min, min(s_max, int(new_val)))

        new_values[mk] = new_val

        applied[mk] = {
            "label": defn.get("label", mk),
            "old": _pimax_human_value(mk, old_val),
            "new": _pimax_human_value(mk, new_val),
            "old_raw": old_val,
            "new_raw": new_val,
            "hint": defn.get("hint", ""),
        }

    # Safety warnings for color keys
    color_warnings: list[str] = []
    for mk, info in applied.items():
        nv = info["new_raw"]
        if "saturation" in mk and isinstance(nv, (int, float)) and nv == 0:
            color_warnings.append(
                f"⚠️ {info['label']} = 0 → Bild wird GRAUSTUFEN (schwarz-weiß)! "
                "Neutral = 50."
            )
        elif "saturation" in mk and isinstance(nv, (int, float)) and nv < 20:
            color_warnings.append(
                f"⚠️ {info['label']} = {nv} ist sehr niedrig — Farben wirken fast grau. "
                "Neutral = 50."
            )
        elif "brightness" in mk and isinstance(nv, (int, float)) and nv < 10:
            color_warnings.append(
                f"⚠️ {info['label']} = {nv} ist sehr niedrig — Bild fast schwarz! "
                "Neutral = 50."
            )

    if dry_run:
        logger.info("set_pimax_setting DRY RUN setting=%r value=%r → %s", setting, value, applied)
        return {
            "status": "dry_run",
            "setting": setting,
            "matched_keys": matching_keys,
            "would_change": {
                k: {"label": v["label"], "old": v["old"], "new": v["new"]}
                for k, v in applied.items()
            },
            "config_file": str(cfg),
            "note": "Nichts wurde geschrieben. Setze dry_run=False um wirklich anzuwenden.",
        }

    # Persist to in-memory dict
    for mk, nv in new_values.items():
        current[mk] = nv

    backup_file = _pimax_create_backup(cfg, current)

    # Write JSON config — only update keys that already exist in JSON
    try:
        json_data = _json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(json_data, dict):
            for mk in matching_keys:
                if mk in json_data:
                    json_data[mk] = current[mk]
            cfg.write_text(_json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not update Pimax JSON config %s: %s", cfg, exc)

    # Write to registry — Pimax Play reads live values from registry, not JSON
    registry_audit = _apply_pimax_settings_to_registry(
        {mk: current[mk] for mk in matching_keys}, verify=True,
    )

    logger.info(
        "set_pimax_setting setting=%r value=%r matched=%s",
        setting, value, matching_keys,
    )

    # Build result
    if len(applied) == 1:
        info = applied[actual_key]
        defn_main = PIMAX_SETTING_DEFS.get(actual_key, {})
        result = {
            "status": "ok",
            "setting": info["label"],
            "key": actual_key,
            "old_value": info["old"],
            "new_value": info["new"],
            "config_file": str(cfg),
            "backup": str(backup_file),
            "registry_audit": registry_audit,
        }
        if defn_main.get("hint"):
            result["wertebereich"] = defn_main["hint"]
    else:
        result = {
            "status": "ok",
            "changed_keys": {
                k: {"label": v["label"], "old": v["old"], "new": v["new"]}
                for k, v in applied.items()
            },
            "config_file": str(cfg),
            "backup": str(backup_file),
            "registry_audit": registry_audit,
        }

    if color_warnings:
        result["warnungen"] = color_warnings

    _pimax_apply_restart(result, restart_service, force_restart)
    return result


@mcp.tool()
def improve_image_clarity(
    target: Literal["pimax", "msfs", "both"] = "both",
    strength: Literal["mild", "medium", "strong"] = "medium",
    config_path: str = "",
    usercfg_path: str = "",
    auto_restart: bool = True,
    force_restart: bool = False,
    dry_run: bool = False,
) -> dict:
    """Make the VR image visibly clearer by adjusting multiple settings at once.

    USE THIS when the user says things like:
    - "klares Bild" / "klareres Bild"
    - "schärfer" / "Schärfe erhöhen"
    - "sharper image" / "improve clarity"
    - "ich sehe alles unscharf"
    - "kann man das schärfer machen?"

    Single-setting tools cannot fulfill those requests because clarity is
    not one slider — it's a combination of render resolution, foveated
    rendering, anti-aliasing mode, and texture/anisotropic quality.

    What this tool does:
        Pimax (writes JSON + registry, restarts Pimax Play unless VR-Session aktiv):
        - mild:    renderResolution=1.25, ffrLevel=1 (Low)
        - medium:  renderResolution=1.5,  ffrLevel=0 (Aus)
        - strong:  renderResolution=1.75, ffrLevel=0, smartSmoothing=False

        MSFS 2024 (kill MSFS → write UserCfg.opt → relaunch in VR):
        - mild:    DLSS=Quality, RenderScale=100
        - medium:  DLSS=Quality, RenderScale=110, AnisotropicFilter=16x, TextureRes=Ultra
        - strong:  DLSS=DLAA,    RenderScale=120, AnisotropicFilter=16x, TextureRes=Ultra

    Higher strength costs more GPU/VRAM. Default 'medium' is a good
    starting point for an RTX 4080/5080-class GPU.

    Args:
        target: "pimax", "msfs", or "both" (default).
        strength: "mild", "medium" (default), or "strong".
        config_path: Pimax config path (auto-detected if empty).
        usercfg_path: MSFS UserCfg.opt path (auto-detected if empty).
        auto_restart: Restart Pimax / MSFS so changes become visible.
        force_restart: For Pimax: restart even during an active VR session
                       (interrupts it). Only with explicit user consent.
        dry_run: If True, only return what WOULD change for both targets.
                 No writes, no kills, no restart.
    """
    return _apply_combo_profile(
        domain="clarity",
        target=target,
        strength=strength,
        pimax_profiles={
            "mild":   {"renderResolution": 1.25, "ffrLevel": 1},
            "medium": {"renderResolution": 1.5,  "ffrLevel": 0},
            "strong": {"renderResolution": 1.75, "ffrLevel": 0, "smartSmoothing": False},
        },
        msfs_profiles={
            "mild": {
                "Video.DLSS": "2",
                "Video.RenderScale": "100",
            },
            "medium": {
                "Video.DLSS": "2",
                "Video.RenderScale": "110",
                "GraphicsVR.AnisotropicFilter": "4",
                "GraphicsVR.TextureResolution": "3",
            },
            "strong": {
                "Video.DLSS": "1",
                "Video.RenderScale": "120",
                "GraphicsVR.AnisotropicFilter": "4",
                "GraphicsVR.TextureResolution": "3",
            },
        },
        config_path=config_path,
        usercfg_path=usercfg_path,
        auto_restart=auto_restart,
        force_restart=force_restart,
        dry_run=dry_run,
    )


def _apply_combo_profile(
    domain: str,
    target: str,
    strength: str,
    pimax_profiles: dict[str, dict],
    msfs_profiles: dict[str, dict[str, str]],
    config_path: str,
    usercfg_path: str,
    auto_restart: bool,
    force_restart: bool,
    dry_run: bool,
) -> dict:
    """Shared engine for multi-setting combo tools (clarity, performance, …).

    Applies a Pimax profile + MSFS profile in one coordinated step:
    backup → write JSON → write registry (Pimax only) → kill+relaunch MSFS.
    """
    import time

    out: dict = {
        "domain": domain,
        "target": target,
        "strength": strength,
        "dry_run": dry_run,
    }

    # ── Pimax ───────────────────────────────────────────────────────────────
    if target in ("pimax", "both"):
        cfg = _find_pimax_config(config_path)
        if cfg is None:
            out["pimax"] = _pimax_not_found_error()
        else:
            current = _read_pimax_settings(cfg)
            if not current:
                out["pimax"] = {"error": f"Konnte {cfg} nicht lesen."}
            else:
                profile = pimax_profiles[strength]
                diff = {
                    k: {"old": current.get(k, "<not set>"), "new": v}
                    for k, v in profile.items()
                    if current.get(k) != v
                }
                if dry_run:
                    out["pimax"] = {
                        "status": "dry_run",
                        "would_apply": {k: str(v) for k, v in profile.items()},
                        "diff": diff,
                        "config_file": str(cfg),
                    }
                else:
                    backup_file = _pimax_create_backup(cfg, current)
                    for k, v in profile.items():
                        current[k] = v
                    cfg.write_text(
                        _json.dumps(current, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    registry_audit = _apply_pimax_settings_to_registry(profile, verify=True)
                    logger.info("%s combo Pimax: applied %s", domain, list(profile.keys()))
                    pimax_result: dict = {
                        "status": "ok",
                        "applied": {k: str(v) for k, v in profile.items()},
                        "diff": diff,
                        "registry_audit": registry_audit,
                        "config_file": str(cfg),
                        "backup": str(backup_file),
                    }
                    _pimax_apply_restart(pimax_result, auto_restart, force_restart)
                    out["pimax"] = pimax_result

    # ── MSFS ────────────────────────────────────────────────────────────────
    if target in ("msfs", "both"):
        cfg_path = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH
        if not cfg_path.exists():
            out["msfs"] = {
                "error": f"UserCfg.opt nicht gefunden: {cfg_path}",
                "tipp": "Gib usercfg_path an oder starte MSFS einmal damit die Datei angelegt wird.",
            }
        else:
            updates = msfs_profiles[strength]
            current_settings = _read_current_settings(cfg_path)
            diff = {
                k: {"old": current_settings.get(k, "<not set>"), "new": v}
                for k, v in updates.items()
                if current_settings.get(k) != v
            }

            if dry_run:
                out["msfs"] = {
                    "status": "dry_run",
                    "would_apply": updates,
                    "diff": diff,
                    "config_file": str(cfg_path),
                }
            else:
                # Kill MSFS first — sonst überschreibt MSFS unsere Änderungen beim Beenden
                msfs_was_running = _is_msfs_running()
                if msfs_was_running:
                    _kill_msfs(timeout_s=30)
                    # Wait for the file lock to clear instead of blind sleep
                    _wait_for_file_unlocked(cfg_path, timeout_s=10)

                saved_version = _snapshot(
                    cfg_path, f"Before {domain} combo strength={strength}"
                )
                _prune_msfs_backups(cfg_path)

                text = cfg_path.read_text(encoding="utf-8")
                entries = _parse_usercfg(text)
                new_entries, not_applied = _apply_overrides(entries, updates)
                cfg_path.write_text(_entries_to_text(new_entries), encoding="utf-8")

                verify = _read_current_settings(cfg_path)
                applied_settings = {k: v for k, v in updates.items() if k not in not_applied}
                verified = {k: verify.get(k) == v for k, v in applied_settings.items()}
                logger.info(
                    "%s combo MSFS: applied %s, not_applied=%s",
                    domain, list(applied_settings.keys()), list(not_applied),
                )

                msfs_result: dict = {
                    "status": "ok",
                    "applied": applied_settings,
                    "not_applied": list(not_applied),
                    "diff": diff,
                    "verified": verified,
                    "saved_version": saved_version,
                    "config_file": str(cfg_path),
                }

                if auto_restart and msfs_was_running:
                    time.sleep(2)
                    vr_result = launch_msfs_vr(force_restart=False)
                    msfs_result["neustart"] = (
                        "MSFS in VR neu gestartet — Änderungen sind sofort aktiv."
                    )
                    msfs_result["vr_launch"] = vr_result
                elif msfs_was_running:
                    msfs_result["hinweis"] = (
                        "MSFS wurde beendet aber nicht neu gestartet (auto_restart=False)."
                    )
                else:
                    msfs_result["hinweis"] = "Wird beim nächsten MSFS-Start übernommen."

                out["msfs"] = msfs_result

    return out


@mcp.tool()
def improve_performance(
    target: Literal["pimax", "msfs", "both"] = "both",
    strength: Literal["mild", "medium", "strong"] = "medium",
    config_path: str = "",
    usercfg_path: str = "",
    auto_restart: bool = True,
    force_restart: bool = False,
    dry_run: bool = False,
) -> dict:
    """Improve VR FPS / reduce stutter by adjusting multiple settings at once.

    USE THIS when the user says things like:
    - "mehr FPS" / "bessere Performance"
    - "weniger Ruckler" / "weniger Stuttering"
    - "schneller" / "smoother"
    - "ist zu ruckelig"
    - "improve framerate" / "reduce stutter"

    The opposite of improve_image_clarity — trades visual quality for
    headroom. Use mild first if the user just wants a small boost,
    strong when they're really struggling.

    What this tool does:
        Pimax:
        - mild:    renderResolution=1.0,  ffrLevel=2 (Medium), smartSmoothing=True
        - medium:  renderResolution=0.9,  ffrLevel=3 (High),   smartSmoothing=True
        - strong:  renderResolution=0.75, ffrLevel=4 (Ultra),  smartSmoothing=True,
                   compulsorySmoothing=True

        MSFS 2024:
        - mild:    DLSS=Performance, RenderScale=90, Wolken=Medium
        - medium:  DLSS=Performance, RenderScale=80, Wolken=Low, Schatten=Medium,
                   Reflexionen=Low, Motion Blur=Off
        - strong:  DLSS=Ultra Performance, RenderScale=70, Wolken=Low, Schatten=Low,
                   Reflexionen=Low, SSContact=Off, Motion Blur=Off, RayTracing=Off

    Args:
        target: "pimax", "msfs", or "both" (default).
        strength: "mild", "medium" (default), or "strong".
        config_path: Pimax config path (auto-detected if empty).
        usercfg_path: MSFS UserCfg.opt path (auto-detected if empty).
        auto_restart: Restart Pimax / MSFS so changes become visible.
        force_restart: For Pimax: restart even during an active VR session.
        dry_run: If True, only show what would change without writing.
    """
    return _apply_combo_profile(
        domain="performance",
        target=target,
        strength=strength,
        pimax_profiles={
            "mild":   {"renderResolution": 1.0,  "ffrLevel": 2, "smartSmoothing": True},
            "medium": {"renderResolution": 0.9,  "ffrLevel": 3, "smartSmoothing": True},
            "strong": {
                "renderResolution": 0.75, "ffrLevel": 4,
                "smartSmoothing": True, "compulsorySmoothing": True,
            },
        },
        msfs_profiles={
            "mild": {
                "Video.DLSS": "4",
                "Video.RenderScale": "90",
                "GraphicsVR.CloudsQuality": "1",
            },
            "medium": {
                "Video.DLSS": "4",
                "Video.RenderScale": "80",
                "GraphicsVR.CloudsQuality": "0",
                "GraphicsVR.ShadowQuality": "1",
                "GraphicsVR.Reflections": "0",
                "GraphicsVR.MotionBlur": "0",
            },
            "strong": {
                "Video.DLSS": "5",
                "Video.RenderScale": "70",
                "GraphicsVR.CloudsQuality": "0",
                "GraphicsVR.ShadowQuality": "0",
                "GraphicsVR.Reflections": "0",
                "GraphicsVR.SSContact": "0",
                "GraphicsVR.MotionBlur": "0",
                "RayTracing.Enabled": "0",
            },
        },
        config_path=config_path,
        usercfg_path=usercfg_path,
        auto_restart=auto_restart,
        force_restart=force_restart,
        dry_run=dry_run,
    )


@mcp.tool()
def restart_pimax() -> dict:
    """Restart Pimax Play so that changed settings take effect.

    Use this after changing Pimax settings while a VR game was running.
    Typical workflow:
      1. User changes Pimax settings (brightness etc.) while in VR
      2. Settings are saved but Pimax is not restarted (VR session active)
      3. User exits VR game
      4. User says "Pimax neu starten" → this tool restarts Pimax Play

    Will NOT restart if a VR game (MSFS) is still running.
    """
    if _is_msfs_running():
        return {
            "error": "MSFS läuft noch! Bitte zuerst das VR-Spiel beenden, "
                     "dann 'Pimax neu starten' sagen.",
        }

    pimax_is_running = any(_is_running(name) for name in _PIMAX_EXE_NAMES)
    if not pimax_is_running:
        # Just start it
        pimax_exe = _find_pimax()
        if pimax_exe:
            subprocess.Popen(
                [pimax_exe],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            return {
                "status": "ok",
                "message": "Pimax Play gestartet. Gespeicherte Einstellungen werden geladen.",
            }
        return {"error": "Pimax Play konnte nicht gefunden werden."}

    # Kill and restart
    for exe in _PIMAX_EXE_NAMES:
        if _is_running(exe):
            try:
                subprocess.run(
                    ["taskkill", "/IM", exe, "/F"],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    import time
    time.sleep(3)

    pimax_exe = _find_pimax()
    if pimax_exe:
        subprocess.Popen(
            [pimax_exe],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return {
            "status": "ok",
            "message": "Pimax Play neu gestartet — alle gespeicherten Einstellungen sind jetzt aktiv.",
        }
    return {"error": "Pimax Play beendet, aber konnte nicht neu gestartet werden."}


@mcp.tool()
def status_check(
    config_path: str = "",
    usercfg_path: str = "",
) -> dict:
    """One-shot health check of the whole Pimax/MSFS/GPU stack.

    Call this at the START of a session to find out what's available
    before suggesting actions. Reports:
    - Pimax config: found path, currently running?, current resolution + brightness
    - MSFS UserCfg.opt: found path, MSFS running?, current DLSS + RenderScale
    - GPU: detected name, VRAM tier
    - Active VR session: yes/no (vrserver.exe)
    - Last log entries (helps debug "ich sehe keine Änderung")

    Args:
        config_path: Pimax config path (auto-detected if empty).
        usercfg_path: MSFS UserCfg.opt path (auto-detected if empty).
    """
    out: dict = {}

    # ── Pimax ──
    pimax_block: dict = {}
    cfg = _find_pimax_config(config_path)
    if cfg is None:
        pimax_block["config"] = "not_found"
    else:
        pimax_block["config_file"] = str(cfg)
        try:
            current = _read_pimax_settings(cfg)
            pimax_block["renderResolution"] = current.get("renderResolution")
            pimax_block["refreshRate"] = current.get("refreshRate")
            pimax_block["ffrLevel"] = current.get("ffrLevel")
            pimax_block["brightness_left"] = current.get("piplay_color_brightness_0")
            pimax_block["brightness_right"] = current.get("piplay_color_brightness_1")
        except Exception as exc:
            pimax_block["read_error"] = str(exc)
    pimax_block["pimax_running"] = any(_is_running(n) for n in _PIMAX_EXE_NAMES)
    out["pimax"] = pimax_block

    # ── MSFS ──
    msfs_block: dict = {}
    msfs_cfg = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH
    msfs_block["config_file"] = str(msfs_cfg)
    msfs_block["config_exists"] = msfs_cfg.exists()
    if msfs_cfg.exists():
        try:
            settings = _read_current_settings(msfs_cfg)
            dlss_raw = settings.get("Video.DLSS", "?")
            dlss_def = SETTING_DEFS.get("Video.DLSS", {}).get("values", {})
            msfs_block["dlss"] = dlss_def.get(str(dlss_raw), str(dlss_raw))
            msfs_block["render_scale"] = settings.get("Video.RenderScale")
            msfs_block["clouds_vr"] = settings.get("GraphicsVR.CloudsQuality")
            msfs_block["shadows_vr"] = settings.get("GraphicsVR.ShadowQuality")
        except Exception as exc:
            msfs_block["read_error"] = str(exc)
    msfs_block["msfs_running"] = _is_msfs_running()
    out["msfs"] = msfs_block

    # ── VR session ──
    out["vr_session_active"] = _is_running("vrserver.exe")

    # ── GPU ──
    try:
        gpu_name, gpu_tier, vram_mb = _detect_gpu_tier()
        out["gpu"] = {"name": gpu_name, "tier": gpu_tier, "vram_mb": vram_mb}
    except Exception as exc:
        out["gpu"] = {"error": str(exc)}

    # ── Recent log lines ──
    try:
        if _LOG_FILE.exists():
            lines = _LOG_FILE.read_text(encoding="utf-8").splitlines()
            out["recent_log"] = lines[-10:]
        else:
            out["recent_log"] = []
    except Exception as exc:
        out["recent_log"] = [f"<log read error: {exc}>"]

    return out


@mcp.tool()
def revert_last_change(
    target: Literal["pimax", "msfs", "auto"] = "auto",
    config_path: str = "",
    usercfg_path: str = "",
) -> dict:
    """Undo the most recent settings change.

    Convenience wrapper. The user just says "mach das rückgängig" / "revert"
    and this tool figures out which file to restore based on which has the
    more recent backup.

    Args:
        target: "pimax", "msfs", or "auto" (use whichever has the newer backup).
        config_path: Pimax config path.
        usercfg_path: MSFS UserCfg.opt path.
    """
    pimax_cfg = _find_pimax_config(config_path)
    msfs_cfg = _find_usercfg(usercfg_path) or DEFAULT_USERCFG_PATH

    pimax_latest = pimax_cfg.parent / "pimax_backups" / "pimax_config_latest.json" if pimax_cfg else None
    msfs_latest = msfs_cfg.parent / "UserCfg_backups" / "UserCfg_latest.opt" if msfs_cfg.exists() else None

    pimax_mtime = pimax_latest.stat().st_mtime if pimax_latest and pimax_latest.exists() else 0
    msfs_mtime = msfs_latest.stat().st_mtime if msfs_latest and msfs_latest.exists() else 0

    if target == "auto":
        if pimax_mtime == 0 and msfs_mtime == 0:
            return {
                "error": "Keine Backups gefunden — entweder wurde noch nichts geändert "
                         "oder die Backup-Ordner wurden gelöscht.",
                "pimax_backup_dir": str(pimax_latest.parent) if pimax_latest else None,
                "msfs_backup_dir": str(msfs_latest.parent) if msfs_latest else None,
            }
        target = "pimax" if pimax_mtime > msfs_mtime else "msfs"
        logger.info("revert_last_change auto-selected target=%s", target)

    if target == "pimax":
        if not pimax_latest or not pimax_latest.exists():
            return {"error": "Kein Pimax-Backup gefunden."}
        if not pimax_cfg:
            return {"error": "Pimax-Config-Pfad nicht gefunden."}
        pimax_cfg.write_text(pimax_latest.read_text(encoding="utf-8"), encoding="utf-8")
        # The latest backup is the JSON snapshot — registry is NOT auto-restored
        # (we don't have a registry snapshot). Warn the user.
        logger.info("Pimax config restored from %s", pimax_latest)
        return {
            "status": "ok",
            "target": "pimax",
            "restored_from": str(pimax_latest),
            "config_file": str(pimax_cfg),
            "warnung": (
                "JSON wiederhergestellt. Werte in der Windows-Registry werden NICHT "
                "automatisch zurückgesetzt — bei Bedarf 'Pimax neu starten' nutzen."
            ),
        }

    if target == "msfs":
        if not msfs_latest or not msfs_latest.exists():
            return {"error": "Kein MSFS-Backup gefunden."}
        # Kill MSFS first if running
        if _is_msfs_running():
            _kill_msfs(timeout_s=30)
            _wait_for_file_unlocked(msfs_cfg, timeout_s=10)
        msfs_cfg.write_text(msfs_latest.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("MSFS UserCfg.opt restored from %s", msfs_latest)
        return {
            "status": "ok",
            "target": "msfs",
            "restored_from": str(msfs_latest),
            "config_file": str(msfs_cfg),
            "hinweis": "MSFS muss gestartet (oder neu gestartet) werden damit die Änderung sichtbar wird.",
        }

    return {"error": f"Unbekannter target: {target!r}"}


@mcp.tool()
def adjust_pimax_brightness(
    direction: Literal["up", "down", "set"] = "up",
    amount: int = 10,
    target_value: int | None = None,
    config_path: str = "",
) -> dict:
    """Adjust Pimax headset brightness quickly.

    Shortcut tool for the most common request. Use this when the user says
    things like "Helligkeit erhöhen", "heller machen", "Brightness up/down".

    Changes both eyes simultaneously. Use diagnose_pimax first if
    values seem wrong — Pimax stores settings in multiple places.

    Args:
        direction: "up" to increase, "down" to decrease, "set" for absolute value.
        amount: How much to change (default 10). Ignored if direction is "set".
        target_value: Absolute value when direction is "set".
        config_path: Path to Pimax config (auto-detected if empty).
    """
    if direction == "set" and target_value is not None:
        val_str = str(target_value)
    elif direction == "up":
        val_str = f"+{amount}"
    else:
        val_str = f"-{amount}"

    return set_pimax_setting(
        setting="brightness",
        value=val_str,
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# MSFS VR Launch Sequence
# ---------------------------------------------------------------------------

# Known executable names for Pimax (varies by version)
_PIMAX_EXE_NAMES = [
    "PimaxPlay.exe", "Pimax Play.exe", "PimaxClient.exe",
    "Pimax Client.exe", "PiTool.exe", "Pimax.exe",
]

# Known paths for VR software and MSFS
_PIMAX_PLAY_CANDIDATES = [
    r"C:\Program Files\Pimax\PimaxPlay\PimaxPlay.exe",
    r"C:\Program Files (x86)\Pimax\PimaxPlay\PimaxPlay.exe",
    r"C:\Program Files\Pimax\Pimax Play\PimaxPlay.exe",
    r"C:\Program Files\Pimax\PimaxPlay\Pimax Play.exe",
    r"C:\Program Files\Pimax\Runtime\PimaxPlay.exe",
    r"C:\Program Files\Pimax\Pimax Client\PimaxClient.exe",
    r"C:\Program Files (x86)\Pimax\Pimax Client\PimaxClient.exe",
    r"C:\Program Files\PiTool\PiTool.exe",
    r"D:\Program Files\Pimax\PimaxPlay\PimaxPlay.exe",
    r"D:\Pimax\PimaxPlay\PimaxPlay.exe",
]

_STEAMVR_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\vrstartup.exe",
    r"D:\Steam\steamapps\common\SteamVR\bin\win64\vrstartup.exe",
    r"D:\SteamLibrary\steamapps\common\SteamVR\bin\win64\vrstartup.exe",
    r"E:\SteamLibrary\steamapps\common\SteamVR\bin\win64\vrstartup.exe",
]

# MSFS 2024 Steam App ID
_MSFS2024_STEAM_APP_ID = "2537590"

# Known MSFS 2024 process names
_MSFS_EXE_NAMES = [
    "FlightSimulator2024.exe",
    "FlightSimulator2024.Windows.exe",
]


def _kill_msfs(timeout_s: int = 30) -> dict:
    """Kill all running MSFS processes and wait for them to exit.

    Returns dict with status information.
    """
    import time
    killed = []
    for exe in _MSFS_EXE_NAMES:
        if _is_running(exe):
            try:
                subprocess.run(
                    ["taskkill", "/IM", exe, "/F"],
                    capture_output=True, timeout=10,
                )
                killed.append(exe)
            except Exception:
                pass

    if not killed:
        return {"status": "not_running", "message": "MSFS war nicht gestartet."}

    # Wait for processes to actually exit
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        still_running = [e for e in killed if _is_running(e)]
        if not still_running:
            break
        time.sleep(1)
    else:
        still_running = [e for e in killed if _is_running(e)]
        if still_running:
            return {
                "status": "partial",
                "killed": killed,
                "still_running": still_running,
                "message": f"Nicht alle MSFS-Prozesse konnten beendet werden: {still_running}",
            }

    # Brief safety wait for OS-level cleanup (GPU resources, etc.).
    # Callers that know the cfg path should use _wait_for_file_unlocked()
    # instead of relying on this fixed delay.
    time.sleep(1)
    logger.info("MSFS killed: %s", killed)
    return {"status": "killed", "processes": killed, "message": f"MSFS beendet: {', '.join(killed)}"}


def _is_msfs_running() -> bool:
    """Check if any MSFS process is currently running."""
    return any(_is_running(exe) for exe in _MSFS_EXE_NAMES)


def _find_exe(candidates: list[str], custom: str = "") -> str | None:
    """Return the first existing path from candidates, or custom if given."""
    if custom and Path(custom).exists():
        return custom
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _find_pimax() -> str | None:
    """Find PimaxPlay.exe using multiple strategies."""
    # 1. Try known paths
    for p in _PIMAX_PLAY_CANDIDATES:
        if Path(p).exists():
            return p

    # 2. Search registry for install location
    try:
        result = _reg(
            ["query",
             r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             "/s", "/f", "Pimax", "/d"],
            timeout=10,
        )
        for line in result.stdout.splitlines():
            m = re.search(r"InstallLocation\s+REG_SZ\s+(.+)", line)
            if m:
                install_dir = m.group(1).strip()
                for exe_name in _PIMAX_EXE_NAMES:
                    candidate = Path(install_dir) / exe_name
                    if candidate.exists():
                        return str(candidate)
    except Exception:
        pass

    # 3. Search common Program Files folders
    for root in [r"C:\Program Files", r"C:\Program Files (x86)", r"D:\Program Files"]:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for pimax_dir in root_path.glob("Pimax*"):
            if pimax_dir.is_dir():
                for exe_name in _PIMAX_EXE_NAMES:
                    for hit in pimax_dir.rglob(exe_name):
                        return str(hit)

    # 4. Check Start Menu shortcuts
    start_menus = [
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
    ]
    for sm in start_menus:
        for lnk in sm.rglob("*Pimax*"):
            if lnk.suffix == ".lnk":
                # Read shortcut target via PowerShell
                try:
                    target = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"],
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    if target and Path(target).exists():
                        return target
                except Exception:
                    pass

    return None


def _is_running(process_name: str) -> bool:
    """Check if a process with the given name is running."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
            capture_output=True, text=True, timeout=10,
        )
        return process_name.lower() in r.stdout.lower()
    except Exception:
        return False


def _wait_for_process(name: str, timeout_s: int = 30) -> bool:
    """Wait until a process is running, up to timeout_s seconds."""
    import time
    for _ in range(timeout_s):
        if _is_running(name):
            return True
        time.sleep(1)
    return False


@mcp.tool()
def launch_msfs_vr(
    pimax_path: str = "",
    steamvr_path: str = "",
    skip_pimax: bool = False,
    skip_steamvr: bool = False,
    pimax_wait_s: int = 10,
    steamvr_wait_s: int = 15,
    force_restart: bool = True,
    kill_wait_s: int = 30,
) -> dict:
    """Launch MSFS 2024 in VR mode with the correct startup sequence.

    If MSFS is already running it will be closed first and then restarted
    with VR (unless force_restart is False).

    Startreihenfolge:
      1. MSFS beenden (falls es läuft)
      2. Pimax Play starten (Headset-Runtime)
      3. SteamVR starten (VR-Runtime)
      4. MSFS 2024 über Steam starten

    Jeder Schritt wartet, bis die vorherige Anwendung bereit ist.

    Args:
        pimax_path: Custom path to PimaxPlay.exe (auto-detected if empty).
        steamvr_path: Custom path to vrstartup.exe (auto-detected if empty).
        skip_pimax: Set True if you don't use Pimax (e.g. Meta Quest, Valve Index).
        skip_steamvr: Set True to skip SteamVR (e.g. for Desktop-only mode).
        pimax_wait_s: Seconds to wait for Pimax Play to initialize (default 10).
        steamvr_wait_s: Seconds to wait for SteamVR to initialize (default 15).
        force_restart: If MSFS is already running, close it and restart in VR (default True).
        kill_wait_s: Seconds to wait for MSFS to fully exit before restarting (default 30).
    """
    import time
    steps: list[dict] = []

    # --- Step 0: Close MSFS if already running ---
    if _is_msfs_running():
        if not force_restart:
            return {
                "status": "already_running",
                "message": (
                    "MSFS läuft bereits. Setze force_restart=True um es "
                    "automatisch zu beenden und in VR neu zu starten."
                ),
            }
        kill_result = _kill_msfs(timeout_s=kill_wait_s)
        steps.append({
            "step": "MSFS beenden",
            "status": kill_result["status"],
            "detail": kill_result["message"],
        })
        if kill_result["status"] == "partial":
            return {
                "error": kill_result["message"],
                "steps": steps,
                "hint": "MSFS konnte nicht vollständig beendet werden. Bitte manuell schließen.",
            }

    # --- Step 1: Pimax Play ---
    if not skip_pimax:
        pimax_running = any(
            _is_running(name) for name in _PIMAX_EXE_NAMES
        )
        if pimax_running:
            steps.append({"step": "Pimax Play", "status": "already_running"})
        else:
            pimax = _find_exe(_PIMAX_PLAY_CANDIDATES, pimax_path) if pimax_path else _find_pimax()
            if pimax is None:
                return {
                    "error": (
                        "Pimax Play konnte nicht gefunden werden. "
                        "Bitte gib den Pfad an, z.B.: "
                        "'Starte MSFS VR mit pimax_path C:\\...\\PimaxPlay.exe'"
                    ),
                }
            subprocess.Popen([pimax])
            steps.append({"step": "Pimax Play", "status": "started", "path": pimax})
            time.sleep(pimax_wait_s)

    # --- Step 2: SteamVR ---
    if not skip_steamvr:
        if _is_running("vrserver.exe") or _is_running("vrstartup.exe"):
            steps.append({"step": "SteamVR", "status": "already_running"})
        else:
            steamvr = _find_exe(_STEAMVR_CANDIDATES, steamvr_path)
            if steamvr is None:
                try:
                    subprocess.Popen(["cmd", "/c", "start", "steam://run/250820"])
                    steps.append({"step": "SteamVR", "status": "started_via_steam"})
                except Exception as exc:
                    return {"error": f"Could not start SteamVR: {exc}", "steps": steps}
            else:
                subprocess.Popen([steamvr])
                steps.append({"step": "SteamVR", "status": "started", "path": steamvr})
            ready = _wait_for_process("vrserver.exe", timeout_s=steamvr_wait_s)
            if not ready:
                steps.append({"step": "SteamVR", "warning": "vrserver.exe not detected yet, continuing anyway"})

    # --- Step 3: MSFS 2024 via Steam ---
    try:
        subprocess.Popen(["cmd", "/c", "start", f"steam://run/{_MSFS2024_STEAM_APP_ID}"])
        steps.append({"step": "MSFS 2024", "status": "started", "steam_app_id": _MSFS2024_STEAM_APP_ID})
    except Exception as exc:
        return {"error": f"Could not launch MSFS 2024: {exc}", "steps": steps}

    return {
        "status": "ok",
        "launch_sequence": steps,
        "message": (
            "Startreihenfolge abgeschlossen: "
            + " → ".join(s["step"] for s in steps)
            + ". MSFS 2024 wird in VR geladen."
        ),
    }


# ---------------------------------------------------------------------------
# MSFS Stuck / Crash Recovery
# ---------------------------------------------------------------------------

# Additional processes that can block MSFS or cause hangs
_MSFS_RELATED_PROCESSES = [
    "FlightSimulator2024.exe",
    "FlightSimulator2024.Windows.exe",
    # Common helpers / launchers that may hang
    "GameBar.exe",
    "GameBarPresenceWriter.exe",
    "EAAntiCheat.GameServiceLauncher.exe",
    "EABackgroundService.exe",
]

# MSFS 2024 cache directories (shader cache + rolling cache)
_MSFS_CACHE_DIRS = [
    # MSFS 2024 shader caches
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024" / "ShaderCache",
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024" / "Shadercache",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "ShaderCache",
    # NVIDIA shader cache (shared, but clears MSFS shaders too)
    Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "DXCache",
    Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "GLCache",
    # DirectX shader cache
    Path(os.environ.get("LOCALAPPDATA", "")) / "D3DSCache",
]

# Rolling cache (user-configurable, but this is the default location)
_MSFS_ROLLING_CACHE = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024" / "SceneryIndexes",
]


def _safe_rmtree(folder: Path) -> dict:
    """Delete folder contents (not the folder itself). Returns stats."""
    if not folder.exists():
        return {"path": str(folder), "status": "not_found"}
    total_size = 0
    file_count = 0
    try:
        for item in folder.iterdir():
            if item.is_file():
                total_size += item.stat().st_size
                item.unlink()
                file_count += 1
            elif item.is_dir():
                for f in item.rglob("*"):
                    if f.is_file():
                        total_size += f.stat().st_size
                        file_count += 1
                shutil.rmtree(item, ignore_errors=True)
        return {
            "path": str(folder),
            "status": "cleared",
            "files_deleted": file_count,
            "size_freed_mb": round(total_size / 1024 / 1024, 1),
        }
    except Exception as exc:
        return {"path": str(folder), "status": "error", "error": str(exc)}


@mcp.tool()
def fix_msfs(
    action: Literal[
        "restart", "kill", "clear_shader_cache", "clear_rolling_cache", "full_reset"
    ] = "restart",
    restart_in_vr: bool = True,
    skip_pimax: bool = False,
    skip_steamvr: bool = False,
) -> dict:
    """Fix a stuck, frozen, or crashing MSFS 2024.

    Use this when the user says things like:
    - "MSFS steckt" / "MSFS hängt" / "MSFS reagiert nicht"
    - "MSFS ist eingefroren" / "MSFS ist abgestürzt"
    - "Schwarzer Bildschirm in MSFS"
    - "MSFS lädt nicht" / "MSFS startet nicht"
    - "MSFS Shader Cache leeren"

    Actions:
    - restart: Force-kill MSFS + related processes, then restart (default)
    - kill: Only force-kill, don't restart
    - clear_shader_cache: Kill MSFS + delete shader caches (fixes many crashes/stutters)
    - clear_rolling_cache: Kill MSFS + delete rolling cache/scenery indexes
    - full_reset: Kill + clear ALL caches + restart (last resort for persistent problems)

    Args:
        action: What to do (see above).
        restart_in_vr: If True, restart MSFS in VR mode via launch_msfs_vr (default True).
        skip_pimax: Skip Pimax when restarting in VR.
        skip_steamvr: Skip SteamVR when restarting in VR.
    """
    import time
    steps: list[dict] = []

    # --- Step 1: Force-kill MSFS and related processes ---
    killed = []
    for proc in _MSFS_RELATED_PROCESSES:
        if _is_running(proc):
            try:
                subprocess.run(
                    ["taskkill", "/IM", proc, "/F"],
                    capture_output=True, timeout=10,
                )
                killed.append(proc)
            except Exception:
                pass

    if killed:
        steps.append({
            "step": "Prozesse beendet",
            "killed": killed,
        })
        # Wait for processes to fully exit
        deadline = time.time() + 15
        while time.time() < deadline:
            still_alive = [p for p in killed if _is_running(p)]
            if not still_alive:
                break
            time.sleep(1)
        else:
            still_alive = [p for p in killed if _is_running(p)]
            if still_alive:
                # Try harder with wmic/taskkill /T (kill process tree)
                for proc in still_alive:
                    try:
                        subprocess.run(
                            ["taskkill", "/IM", proc, "/F", "/T"],
                            capture_output=True, timeout=10,
                        )
                    except Exception:
                        pass
                time.sleep(3)
                final_check = [p for p in still_alive if _is_running(p)]
                if final_check:
                    steps.append({
                        "step": "Warnung",
                        "message": f"Prozesse konnten nicht beendet werden: {final_check}",
                    })

        # Extra wait for GPU/file handles to release
        time.sleep(5)
    else:
        steps.append({"step": "Prozesse prüfen", "message": "MSFS war nicht aktiv."})

    # --- Step 2: Clear caches if requested ---
    if action in ("clear_shader_cache", "full_reset"):
        cache_results = []
        for cache_dir in _MSFS_CACHE_DIRS:
            result = _safe_rmtree(cache_dir)
            if result["status"] != "not_found":
                cache_results.append(result)
        if cache_results:
            total_freed = sum(r.get("size_freed_mb", 0) for r in cache_results)
            steps.append({
                "step": "Shader-Cache geleert",
                "caches": cache_results,
                "total_freed_mb": round(total_freed, 1),
                "hinweis": "Erster Start nach Cache-Leerung dauert länger (Shader werden neu kompiliert).",
            })
        else:
            steps.append({"step": "Shader-Cache", "message": "Keine Cache-Ordner gefunden."})

    if action in ("clear_rolling_cache", "full_reset"):
        rolling_results = []
        for rc_dir in _MSFS_ROLLING_CACHE:
            result = _safe_rmtree(rc_dir)
            if result["status"] != "not_found":
                rolling_results.append(result)
        if rolling_results:
            steps.append({
                "step": "Rolling Cache geleert",
                "caches": rolling_results,
            })

    # --- Step 3: Restart if requested ---
    if action == "kill":
        return {
            "status": "ok",
            "steps": steps,
            "message": "MSFS wurde beendet." + (
                " Shader-Cache geleert." if action == "clear_shader_cache" else ""
            ),
        }

    if action in ("restart", "clear_shader_cache", "clear_rolling_cache", "full_reset"):
        if restart_in_vr:
            # Use launch_msfs_vr for proper VR startup sequence
            vr_result = launch_msfs_vr(
                skip_pimax=skip_pimax,
                skip_steamvr=skip_steamvr,
                force_restart=False,  # We already killed MSFS
            )
            steps.append({
                "step": "Neustart in VR",
                "result": vr_result,
            })
        else:
            # Desktop restart
            try:
                subprocess.Popen(["cmd", "/c", "start", f"steam://run/{_MSFS2024_STEAM_APP_ID}"])
                steps.append({"step": "MSFS 2024 gestartet", "mode": "Desktop"})
            except Exception as exc:
                steps.append({"step": "Fehler", "message": f"Konnte MSFS nicht starten: {exc}"})

    # Build final message
    action_labels = {
        "restart": "MSFS beendet und neu gestartet",
        "kill": "MSFS beendet",
        "clear_shader_cache": "MSFS beendet, Shader-Cache geleert und neu gestartet",
        "clear_rolling_cache": "MSFS beendet, Rolling Cache geleert und neu gestartet",
        "full_reset": "MSFS beendet, alle Caches geleert und neu gestartet",
    }

    return {
        "status": "ok",
        "action": action,
        "steps": steps,
        "message": action_labels.get(action, action) + ".",
    }


# ===================================================================
# SYSTEM ADMINISTRATION TOOLS
# ===================================================================


def _reg(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a reg.exe command, trying elevated PowerShell if access is denied."""
    # First try normal
    r = subprocess.run(
        ["reg"] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode == 0:
        return r

    # Check if access denied
    err = (r.stderr + r.stdout).lower()
    if "zugriff" in err or "access" in err or "denied" in err or "verweigert" in err:
        # Retry with elevated PowerShell (Start-Process -Verb RunAs)
        # Build reg command as a single string
        reg_cmd = "reg " + " ".join(f'"{a}"' if " " in a else a for a in args)

        # Use PowerShell to run reg with elevation and capture output
        ps_script = (
            f"$p = Start-Process -FilePath 'reg.exe' "
            f"-ArgumentList '{' '.join(args)}' "
            f"-Verb RunAs -Wait -PassThru "
            f"-WindowStyle Hidden "
            f"-RedirectStandardOutput $env:TEMP\\reg_out.txt "
            f"-RedirectStandardError $env:TEMP\\reg_err.txt; "
            f"$out = Get-Content $env:TEMP\\reg_out.txt -Raw -ErrorAction SilentlyContinue; "
            f"$err = Get-Content $env:TEMP\\reg_err.txt -Raw -ErrorAction SilentlyContinue; "
            f"Write-Output $out"
        )
        try:
            r2 = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=30,
            )
            r2.returncode = 0 if r2.stdout.strip() else r2.returncode
            return r2
        except Exception:
            pass

    return r  # Return original result if elevation also fails


def _reg_query(reg_path: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Query a registry path, with auto-elevation on access denied."""
    return _reg(["query", reg_path], timeout=timeout)


def _reg_query_value(reg_path: str, value_name: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Query a specific registry value, with auto-elevation."""
    return _reg(["query", reg_path, "/v", value_name], timeout=timeout)


def _reg_add(reg_path: str, value_name: str, reg_type: str, data: str,
             timeout: int = 10) -> subprocess.CompletedProcess:
    """Add/set a registry value, with auto-elevation on access denied."""
    return _reg(["add", reg_path, "/v", value_name, "/t", reg_type,
                 "/d", data, "/f"], timeout=timeout)


def _ps(script: str, timeout: int = 30) -> str:
    """Execute a PowerShell command and return stdout."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0 and r.stderr.strip():
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def _ps_json(script: str, timeout: int = 60) -> list[dict]:
    """Execute PowerShell, convert output to JSON, parse and return as list."""
    raw = _ps(f"({script}) | ConvertTo-Json -Depth 4 -Compress", timeout=timeout)
    if not raw:
        return []
    data = _json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return data


# ---------------------------------------------------------------------------
# 1. get_system_info
# ---------------------------------------------------------------------------

@mcp.tool()
def get_system_info() -> dict:
    """Full Windows system overview: OS, CPU, RAM, disks, network, uptime.

    Returns a comprehensive snapshot useful for diagnostics and support.
    """
    try:
        os_info = _ps_json(
            "Get-CimInstance Win32_OperatingSystem "
            "| Select-Object Caption, Version, BuildNumber, "
            "OSArchitecture, LastBootUpTime, "
            "@{N='TotalRAM_GB';E={[math]::Round($_.TotalVisibleMemorySize/1MB,2)}}, "
            "@{N='FreeRAM_GB';E={[math]::Round($_.FreePhysicalMemory/1MB,2)}}"
        )
    except Exception as exc:
        return {"error": f"Failed to query OS info: {exc}"}

    try:
        cpu_info = _ps_json(
            "Get-CimInstance Win32_Processor "
            "| Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, "
            "MaxClockSpeed, LoadPercentage"
        )
    except Exception:
        cpu_info = []

    try:
        disk_info = _ps_json(
            "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' "
            "| Select-Object DeviceID, VolumeName, "
            "@{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, "
            "@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,2)}}, "
            "@{N='UsedPercent';E={[math]::Round(($_.Size-$_.FreeSpace)/$_.Size*100,1)}}"
        )
    except Exception:
        disk_info = []

    try:
        net_info = _ps_json(
            "Get-NetAdapter | Where-Object Status -eq 'Up' "
            "| Select-Object Name, InterfaceDescription, MacAddress, "
            "LinkSpeed, Status"
        )
    except Exception:
        net_info = []

    return {
        "os": os_info[0] if os_info else {},
        "cpu": cpu_info,
        "disks": disk_info,
        "network_adapters": net_info,
    }


# ---------------------------------------------------------------------------
# 2. manage_processes
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_processes(
    action: Literal["list", "search", "kill"],
    name: str = "",
    pid: int = 0,
    sort_by: Literal["cpu", "memory", "name"] = "memory",
    top_n: int = 25,
) -> dict:
    """List, search or kill Windows processes.

    Args:
        action: 'list' top processes, 'search' by name, or 'kill' by PID/name.
        name: Process name or pattern (for search/kill). Supports wildcards (*).
        pid: Process ID (for kill).
        sort_by: Sort order for list ('cpu', 'memory', or 'name').
        top_n: Number of processes to return (default 25).
    """
    sort_map = {
        "cpu": "CPU",
        "memory": "WorkingSet64",
        "name": "ProcessName",
    }
    sort_field = sort_map.get(sort_by, "WorkingSet64")

    if action == "list":
        procs = _ps_json(
            f"Get-Process | Sort-Object {sort_field} -Descending "
            f"| Select-Object -First {top_n} ProcessName, Id, "
            f"@{{N='CPU_s';E={{[math]::Round($_.CPU,1)}}}}, "
            f"@{{N='MemoryMB';E={{[math]::Round($_.WorkingSet64/1MB,1)}}}}"
        )
        return {"processes": procs}

    if action == "search":
        if not name:
            return {"error": "Parameter 'name' is required for search."}
        procs = _ps_json(
            f"Get-Process | Where-Object {{$_.ProcessName -like '*{name}*'}} "
            f"| Select-Object ProcessName, Id, "
            f"@{{N='CPU_s';E={{[math]::Round($_.CPU,1)}}}}, "
            f"@{{N='MemoryMB';E={{[math]::Round($_.WorkingSet64/1MB,1)}}}}"
        )
        return {"matches": procs, "count": len(procs)}

    if action == "kill":
        if pid:
            _ps(f"Stop-Process -Id {pid} -Force")
            return {"status": "ok", "killed_pid": pid}
        if name:
            _ps(f"Stop-Process -Name '{name}' -Force")
            return {"status": "ok", "killed_name": name}
        return {"error": "Provide 'pid' or 'name' to kill a process."}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 3. manage_services
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_services(
    action: Literal["list", "status", "start", "stop", "restart", "set_startup"],
    name: str = "",
    startup_type: Literal["Automatic", "Manual", "Disabled"] = "Manual",
    filter_running: bool = False,
) -> dict:
    """Manage Windows services: list, start, stop, restart, change startup type.

    Args:
        action: The operation to perform.
        name: Service name (required for status/start/stop/restart/set_startup).
        startup_type: For 'set_startup' – Automatic, Manual, or Disabled.
        filter_running: For 'list' – if True, show only running services.
    """
    if action == "list":
        where = "| Where-Object Status -eq 'Running' " if filter_running else ""
        svcs = _ps_json(
            f"Get-Service {where}"
            "| Select-Object Name, DisplayName, Status, StartType"
        )
        return {"services": svcs, "count": len(svcs)}

    if not name:
        return {"error": f"Parameter 'name' is required for action '{action}'."}

    if action == "status":
        svcs = _ps_json(
            f"Get-Service -Name '{name}' "
            "| Select-Object Name, DisplayName, Status, StartType"
        )
        return {"service": svcs[0] if svcs else {}}

    if action in ("start", "stop", "restart"):
        cmd_map = {
            "start": f"Start-Service -Name '{name}'",
            "stop": f"Stop-Service -Name '{name}' -Force",
            "restart": f"Restart-Service -Name '{name}' -Force",
        }
        _ps(cmd_map[action])
        new = _ps_json(
            f"Get-Service -Name '{name}' | Select-Object Name, Status, StartType"
        )
        return {"status": "ok", "service": new[0] if new else {}}

    if action == "set_startup":
        _ps(f"Set-Service -Name '{name}' -StartupType '{startup_type}'")
        return {"status": "ok", "name": name, "startup_type": startup_type}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 4. network_diagnostics
# ---------------------------------------------------------------------------

@mcp.tool()
def network_diagnostics(
    action: Literal["ipconfig", "ping", "traceroute", "dns", "connections", "wifi"],
    target: str = "",
    count: int = 4,
) -> dict:
    """Network diagnostics: IP config, ping, traceroute, DNS, connections, Wi-Fi.

    Args:
        action: The diagnostic to run.
        target: Hostname or IP (required for ping, traceroute, dns).
        count: Number of pings (default 4).
    """
    if action == "ipconfig":
        addrs = _ps_json(
            "Get-NetIPAddress -AddressFamily IPv4 "
            "| Select-Object InterfaceAlias, IPAddress, PrefixLength"
        )
        dns = _ps_json(
            "Get-DnsClientServerAddress -AddressFamily IPv4 "
            "| Select-Object InterfaceAlias, ServerAddresses"
        )
        gateway = _ps(
            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Select-Object -First 1).NextHop"
        )
        return {"addresses": addrs, "dns_servers": dns, "default_gateway": gateway}

    if action == "ping":
        if not target:
            return {"error": "Parameter 'target' is required for ping."}
        results = _ps_json(
            f"Test-Connection -ComputerName '{target}' -Count {count} "
            "| Select-Object Address, "
            "@{N='ResponseTimeMs';E={$_.ResponseTime}}, StatusCode"
        )
        return {"ping_results": results, "target": target}

    if action == "traceroute":
        if not target:
            return {"error": "Parameter 'target' is required for traceroute."}
        raw = _ps(f"tracert -d -w 1000 {target}", timeout=60)
        return {"traceroute": raw, "target": target}

    if action == "dns":
        if not target:
            return {"error": "Parameter 'target' is required for dns."}
        records = _ps_json(f"Resolve-DnsName '{target}'")
        return {"dns_records": records, "target": target}

    if action == "connections":
        conns = _ps_json(
            "Get-NetTCPConnection -State Established "
            "| Select-Object LocalAddress, LocalPort, "
            "RemoteAddress, RemotePort, OwningProcess "
            "| Sort-Object RemoteAddress"
        )
        return {"connections": conns, "count": len(conns)}

    if action == "wifi":
        profiles = _ps("netsh wlan show profiles", timeout=10)
        iface = _ps("netsh wlan show interfaces", timeout=10)
        return {"profiles": profiles, "interface": iface}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 5. manage_startup_programs
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_startup_programs(
    action: Literal["list", "disable", "enable"],
    name: str = "",
) -> dict:
    """View and manage Windows startup programs (autostart entries).

    Reads from the Run keys in the registry (HKLM + HKCU) and the
    common Startup folder.

    Args:
        action: 'list' all, 'disable' by name, or 'enable' a previously disabled entry.
        name: Name/key of the startup entry (required for disable/enable).
    """
    hklm = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
    hkcu = r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"

    if action == "list":
        entries: list[dict] = []
        for hive, label in [(hklm, "HKLM"), (hkcu, "HKCU")]:
            try:
                items = _ps_json(
                    f"Get-ItemProperty '{hive}' "
                    "| ForEach-Object {{ $_.PSObject.Properties "
                    "| Where-Object {{ $_.Name -notlike 'PS*' }} "
                    "| Select-Object Name, Value }}"
                )
                for item in items:
                    item["hive"] = label
                entries.extend(items)
            except Exception:
                pass

        # Also check shell:startup folder
        try:
            startup_folder = _ps(
                "[Environment]::GetFolderPath('Startup')"
            )
            if startup_folder:
                shortcuts = _ps_json(
                    f"Get-ChildItem '{startup_folder}' "
                    "| Select-Object Name, FullName"
                )
                for s in shortcuts:
                    s["hive"] = "StartupFolder"
                entries.extend(shortcuts)
        except Exception:
            pass

        return {"startup_entries": entries, "count": len(entries)}

    if not name:
        return {"error": f"Parameter 'name' is required for '{action}'."}

    if action == "disable":
        for hive in [hklm, hkcu]:
            try:
                _ps(f"Remove-ItemProperty -Path '{hive}' -Name '{name}' -ErrorAction Stop")
                return {"status": "ok", "disabled": name, "hive": hive}
            except Exception:
                continue
        return {"error": f"Startup entry '{name}' not found in registry Run keys."}

    if action == "enable":
        return {
            "error": (
                "To re-enable a startup entry, use run_shell_command to set the "
                "registry value, e.g.: "
                f"New-ItemProperty -Path '{hkcu}' -Name '{name}' "
                "-Value 'C:\\path\\to\\program.exe'"
            )
        }

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 6. set_power_plan
# ---------------------------------------------------------------------------

@mcp.tool()
def set_power_plan(
    action: Literal["list", "set"],
    plan: Literal["high_performance", "balanced", "power_saver", "ultimate"] = "balanced",
) -> dict:
    """List or switch Windows power plans.

    Args:
        action: 'list' available plans or 'set' the active plan.
        plan: Plan to activate – 'high_performance', 'balanced',
              'power_saver', or 'ultimate' (Ultimate Performance).
    """
    if action == "list":
        raw = _ps("powercfg /list")
        return {"power_plans": raw}

    guid_map = {
        "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
        "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
        "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
        "ultimate": "e9a42b02-d5df-448d-aa00-03f14749eb61",
    }
    guid = guid_map.get(plan)
    if not guid:
        return {"error": f"Unknown plan: {plan}"}

    try:
        _ps(f"powercfg /setactive {guid}")
    except RuntimeError:
        # Ultimate might not exist – try to add it first
        if plan == "ultimate":
            _ps(f"powercfg -duplicatescheme {guid}")
            _ps(f"powercfg /setactive {guid}")
        else:
            raise

    current = _ps("powercfg /getactivescheme")
    return {"status": "ok", "active_plan": current}


# ---------------------------------------------------------------------------
# 7. manage_firewall
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_firewall(
    action: Literal["status", "list_rules", "add_rule", "remove_rule", "enable", "disable"],
    rule_name: str = "",
    program: str = "",
    port: int = 0,
    protocol: Literal["TCP", "UDP", "Any"] = "TCP",
    direction: Literal["Inbound", "Outbound"] = "Inbound",
    allow: bool = True,
) -> dict:
    """Manage Windows Firewall: view status, list/add/remove rules.

    Args:
        action: Operation to perform.
        rule_name: Display name for the rule (required for add/remove).
        program: Path to .exe (for add_rule – program-based rule).
        port: Port number (for add_rule – port-based rule).
        protocol: TCP, UDP, or Any (for port rules).
        direction: Inbound or Outbound.
        allow: True = Allow, False = Block (for add_rule).
    """
    if action == "status":
        profiles = _ps_json(
            "Get-NetFirewallProfile | Select-Object Name, Enabled, "
            "DefaultInboundAction, DefaultOutboundAction"
        )
        return {"firewall_profiles": profiles}

    if action == "list_rules":
        rules = _ps_json(
            f"Get-NetFirewallRule -Direction {direction} -Enabled True "
            "| Select-Object -First 50 DisplayName, Direction, Action, "
            "Enabled, Profile"
        )
        return {"rules": rules, "direction": direction, "count": len(rules)}

    if action == "add_rule":
        if not rule_name:
            return {"error": "Parameter 'rule_name' is required."}
        fw_action = "Allow" if allow else "Block"
        cmd = (
            f"New-NetFirewallRule -DisplayName '{rule_name}' "
            f"-Direction {direction} -Action {fw_action}"
        )
        if port:
            cmd += f" -Protocol {protocol} -LocalPort {port}"
        if program:
            cmd += f" -Program '{program}'"
        _ps(cmd)
        return {
            "status": "ok",
            "rule": rule_name,
            "action": fw_action,
            "direction": direction,
        }

    if action == "remove_rule":
        if not rule_name:
            return {"error": "Parameter 'rule_name' is required."}
        _ps(f"Remove-NetFirewallRule -DisplayName '{rule_name}'")
        return {"status": "ok", "removed": rule_name}

    if action in ("enable", "disable"):
        enabled = "True" if action == "enable" else "False"
        _ps(
            f"Set-NetFirewallProfile -Profile Domain,Public,Private "
            f"-Enabled {enabled}"
        )
        return {"status": "ok", "firewall_enabled": action == "enable"}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 8. disk_analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def disk_analysis(
    action: Literal["usage", "large_files", "temp_cleanup"],
    path: str = "C:\\",
    top_n: int = 20,
    min_size_mb: int = 100,
) -> dict:
    """Analyse disk usage, find large files, or clean temp folders.

    Args:
        action: 'usage' for disk overview, 'large_files' to find big files,
                'temp_cleanup' to delete temp files.
        path: Root path for large_files scan (default C:\\).
        top_n: Number of large files to return.
        min_size_mb: Minimum file size in MB for large_files.
    """
    if action == "usage":
        disks = _ps_json(
            "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' "
            "| Select-Object DeviceID, VolumeName, "
            "@{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, "
            "@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,2)}}, "
            "@{N='UsedPercent';E={[math]::Round(($_.Size-$_.FreeSpace)/$_.Size*100,1)}}"
        )
        return {"disks": disks}

    if action == "large_files":
        files = _ps_json(
            f"Get-ChildItem -Path '{path}' -Recurse -File -ErrorAction SilentlyContinue "
            f"| Where-Object {{$_.Length -gt {min_size_mb}MB}} "
            f"| Sort-Object Length -Descending "
            f"| Select-Object -First {top_n} FullName, "
            f"@{{N='SizeMB';E={{[math]::Round($_.Length/1MB,1)}}}}, LastWriteTime",
            timeout=120,
        )
        return {"large_files": files, "count": len(files), "scanned_path": path}

    if action == "temp_cleanup":
        result = _ps(
            "$paths = @($env:TEMP, 'C:\\Windows\\Temp');\n"
            "$before = 0; $freed = 0;\n"
            "foreach ($p in $paths) {\n"
            "  $items = Get-ChildItem $p -Recurse -ErrorAction SilentlyContinue;\n"
            "  $before += ($items | Measure-Object Length -Sum).Sum;\n"
            "  Remove-Item \"$p\\*\" -Recurse -Force -ErrorAction SilentlyContinue\n"
            "}\n"
            "$after = 0;\n"
            "foreach ($p in $paths) {\n"
            "  $after += (Get-ChildItem $p -Recurse -ErrorAction SilentlyContinue "
            "| Measure-Object Length -Sum).Sum\n"
            "}\n"
            "[math]::Round(($before - $after)/1MB, 1)",
            timeout=120,
        )
        return {"status": "ok", "freed_mb": float(result) if result else 0}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 9. windows_update_status
# ---------------------------------------------------------------------------

@mcp.tool()
def windows_update_status(
    action: Literal["check", "history"] = "history",
    count: int = 15,
) -> dict:
    """Check Windows Update status or view update history.

    Args:
        action: 'history' for recent updates, 'check' for pending updates.
        count: Number of history entries to return.
    """
    if action == "history":
        updates = _ps_json(
            f"Get-HotFix | Sort-Object InstalledOn -Descending "
            f"| Select-Object -First {count} HotFixID, Description, InstalledOn"
        )
        return {"recent_updates": updates, "count": len(updates)}

    if action == "check":
        try:
            pending = _ps(
                "$Session = New-Object -ComObject Microsoft.Update.Session;\n"
                "$Searcher = $Session.CreateUpdateSearcher();\n"
                "$Results = $Searcher.Search('IsInstalled=0');\n"
                "$Results.Updates | ForEach-Object {\n"
                "  [PSCustomObject]@{\n"
                "    Title=$_.Title;\n"
                "    IsMandatory=$_.IsMandatory;\n"
                "    IsDownloaded=$_.IsDownloaded\n"
                "  }\n"
                "} | ConvertTo-Json -Compress",
                timeout=120,
            )
            if not pending:
                return {"pending_updates": [], "message": "System is up to date."}
            data = _json.loads(pending)
            if isinstance(data, dict):
                data = [data]
            return {"pending_updates": data, "count": len(data)}
        except Exception as exc:
            return {"error": f"Could not check for updates: {exc}"}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 10. manage_installed_software
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_installed_software(
    action: Literal["list", "search", "uninstall"],
    name: str = "",
) -> dict:
    """List, search or uninstall software on the system.

    Uses the Uninstall registry keys for fast enumeration (not Win32_Product).

    Args:
        action: 'list' all, 'search' by name, or 'uninstall' by name.
        name: Software name or pattern (for search/uninstall).
    """
    reg_paths = [
        r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    ]
    query = " + ".join(
        f"@(Get-ItemProperty '{p}' -ErrorAction SilentlyContinue "
        f"| Where-Object {{ $_.DisplayName -ne $null }})"
        for p in reg_paths
    )

    if action == "list":
        sw = _ps_json(
            f"({query}) | Sort-Object DisplayName "
            "| Select-Object DisplayName, DisplayVersion, Publisher, InstallDate",
            timeout=30,
        )
        return {"software": sw, "count": len(sw)}

    if not name:
        return {"error": f"Parameter 'name' is required for '{action}'."}

    if action == "search":
        sw = _ps_json(
            f"({query}) | Where-Object {{$_.DisplayName -like '*{name}*'}} "
            "| Select-Object DisplayName, DisplayVersion, Publisher, "
            "InstallDate, UninstallString",
            timeout=30,
        )
        return {"matches": sw, "count": len(sw)}

    if action == "uninstall":
        matches = _ps_json(
            f"({query}) | Where-Object {{$_.DisplayName -like '*{name}*'}} "
            "| Select-Object DisplayName, UninstallString",
            timeout=30,
        )
        if not matches:
            return {"error": f"No software found matching '{name}'."}
        if len(matches) > 1:
            return {
                "error": "Multiple matches found. Be more specific.",
                "matches": [m.get("DisplayName") for m in matches],
            }
        uninstall_cmd = matches[0].get("UninstallString", "")
        if not uninstall_cmd:
            return {"error": "No uninstall command found for this software."}
        # Run uninstaller silently
        if "msiexec" in uninstall_cmd.lower():
            uninstall_cmd = uninstall_cmd.replace("/I", "/X") + " /quiet /norestart"
        _ps(f"Start-Process cmd -ArgumentList '/c {uninstall_cmd}' -Wait", timeout=120)
        return {
            "status": "ok",
            "uninstalled": matches[0].get("DisplayName"),
        }

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 11. manage_users
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_users(
    action: Literal["list", "whoami", "add", "remove", "set_password"],
    username: str = "",
    password: str = "",
    admin: bool = False,
) -> dict:
    """Manage local Windows user accounts.

    Args:
        action: 'list', 'whoami', 'add', 'remove', or 'set_password'.
        username: Username (required for add/remove/set_password).
        password: Password (required for add/set_password).
        admin: If True, add the user to the Administrators group (for 'add').
    """
    if action == "whoami":
        info = _ps(
            "$u = [System.Security.Principal.WindowsIdentity]::GetCurrent();\n"
            "$p = New-Object System.Security.Principal.WindowsPrincipal($u);\n"
            "$isAdmin = $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator);\n"
            "\"User: $($u.Name) | Admin: $isAdmin\""
        )
        return {"info": info}

    if action == "list":
        users = _ps_json(
            "Get-LocalUser | Select-Object Name, Enabled, "
            "LastLogon, PasswordRequired, Description"
        )
        return {"users": users}

    if not username:
        return {"error": f"Parameter 'username' is required for '{action}'."}

    if action == "add":
        if not password:
            return {"error": "Parameter 'password' is required to add a user."}
        _ps(
            f"$pw = ConvertTo-SecureString '{password}' -AsPlainText -Force;\n"
            f"New-LocalUser -Name '{username}' -Password $pw -FullName '{username}'"
        )
        if admin:
            _ps(f"Add-LocalGroupMember -Group 'Administrators' -Member '{username}'")
        return {"status": "ok", "created": username, "admin": admin}

    if action == "remove":
        _ps(f"Remove-LocalUser -Name '{username}'")
        return {"status": "ok", "removed": username}

    if action == "set_password":
        if not password:
            return {"error": "Parameter 'password' is required."}
        _ps(
            f"$pw = ConvertTo-SecureString '{password}' -AsPlainText -Force;\n"
            f"Set-LocalUser -Name '{username}' -Password $pw"
        )
        return {"status": "ok", "username": username, "password_updated": True}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 12. manage_scheduled_tasks
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_scheduled_tasks(
    action: Literal["list", "create", "delete", "run", "disable", "enable"],
    task_name: str = "",
    program: str = "",
    arguments: str = "",
    schedule: Literal["daily", "weekly", "hourly", "at_logon", "once"] = "daily",
    time: str = "09:00",
) -> dict:
    """Manage Windows Task Scheduler entries.

    Args:
        action: 'list', 'create', 'delete', 'run', 'disable', or 'enable'.
        task_name: Name of the task (required for all except list).
        program: Path to executable (required for create).
        arguments: Command-line arguments for the program.
        schedule: Trigger type for create.
        time: Time of day for the trigger (HH:MM format).
    """
    if action == "list":
        tasks = _ps_json(
            "Get-ScheduledTask | Where-Object {$_.TaskPath -eq '\\\\'} "
            "| Select-Object TaskName, State, "
            "@{N='Triggers';E={($_.Triggers | ForEach-Object {$_.CimClass.CimClassName}) -join ', '}}",
            timeout=30,
        )
        return {"tasks": tasks, "count": len(tasks)}

    if not task_name:
        return {"error": f"Parameter 'task_name' is required for '{action}'."}

    if action == "create":
        if not program:
            return {"error": "Parameter 'program' is required for 'create'."}
        trigger_map = {
            "daily": f"New-ScheduledTaskTrigger -Daily -At '{time}'",
            "weekly": f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '{time}'",
            "hourly": "New-ScheduledTaskTrigger -Once -At '00:00' "
                      "-RepetitionInterval (New-TimeSpan -Hours 1)",
            "at_logon": "New-ScheduledTaskTrigger -AtLogon",
            "once": f"New-ScheduledTaskTrigger -Once -At '{time}'",
        }
        trigger_cmd = trigger_map.get(schedule, trigger_map["daily"])
        args_part = f" -Argument '{arguments}'" if arguments else ""
        _ps(
            f"$action = New-ScheduledTaskAction -Execute '{program}'{args_part};\n"
            f"$trigger = {trigger_cmd};\n"
            f"Register-ScheduledTask -TaskName '{task_name}' "
            f"-Action $action -Trigger $trigger -RunLevel Highest"
        )
        return {"status": "ok", "created": task_name, "schedule": schedule}

    if action == "delete":
        _ps(f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false")
        return {"status": "ok", "deleted": task_name}

    if action == "run":
        _ps(f"Start-ScheduledTask -TaskName '{task_name}'")
        return {"status": "ok", "started": task_name}

    if action == "disable":
        _ps(f"Disable-ScheduledTask -TaskName '{task_name}'")
        return {"status": "ok", "disabled": task_name}

    if action == "enable":
        _ps(f"Enable-ScheduledTask -TaskName '{task_name}'")
        return {"status": "ok", "enabled": task_name}

    return {"error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# 13. run_shell_command  (catch-all for everything else)
# ---------------------------------------------------------------------------

@mcp.tool()
def run_shell_command(
    command: str,
    use_powershell: bool = True,
    timeout: int = 30,
) -> dict:
    """Execute an arbitrary shell command (PowerShell or CMD).

    Use this as a fallback when no other tool covers the task.
    Requires care – commands run with the same privileges as the MCP server.

    Args:
        command: The command string to execute.
        use_powershell: True for PowerShell (default), False for CMD.
        timeout: Max seconds to wait (default 30).
    """
    try:
        if use_powershell:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=timeout,
            )
        else:
            r = subprocess.run(
                ["cmd", "/c", command],
                capture_output=True, text=True, timeout=timeout,
            )
        return {
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s."}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Browser control via Chrome DevTools Protocol (CDP)
# Same protocol used by the Chrome extension – no AppleScript needed.
# ---------------------------------------------------------------------------

import glob as _glob
import time as _time
import threading as _threading

try:
    import websocket as _websocket          # websocket-client package
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# CDP debug port – Chrome must be launched with --remote-debugging-port=9222
_CDP_PORT = 9222
_CDP_BASE = f"http://localhost:{_CDP_PORT}"

# ── Low-level CDP helpers ──────────────────────────────────────────────────────

def _cdp_ensure_chrome() -> bool:
    """Ensure Chrome is reachable on the CDP debug port (Windows).

    Logic:
    1. If CDP is already available → done.
    2. If Chrome is running WITHOUT the debug port → terminate it via taskkill,
       then relaunch with --remote-debugging-port.
    3. If Chrome is not running at all → launch it fresh with the flag.
    Returns True if CDP becomes available, False otherwise.
    """
    # ── 1. Already available? ─────────────────────────────────────────────────
    try:
        resp = httpx.get(f"{_CDP_BASE}/json/version", timeout=2)
        if resp.status_code == 200:
            return True
    except Exception:
        pass

    # ── Windows Chrome install locations ─────────────────────────────────────
    _CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google" / "Chrome" / "Application" / "chrome.exe"),
        r"C:\Program Files\Chromium\Application\chrome.exe",
    ]
    chrome_bin = next((p for p in _CHROME_PATHS if Path(p).exists()), None)
    if not chrome_bin:
        return False   # Chrome not installed

    # ── 2. Chrome running without debug port → terminate via taskkill ────────
    try:
        chk = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq chrome.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True,
        )
        if "chrome.exe" in chk.stdout.lower():
            # Gracefully close all Chrome windows first, then force-kill remainder
            subprocess.run(
                ["taskkill", "/im", "chrome.exe"],
                capture_output=True,
            )
            _time.sleep(1)
            subprocess.run(
                ["taskkill", "/f", "/im", "chrome.exe"],
                capture_output=True,
            )
            _time.sleep(1)
    except Exception:
        pass

    # ── 3. Launch Chrome with remote-debugging-port ───────────────────────────
    subprocess.Popen(
        [chrome_bin,
         f"--remote-debugging-port={_CDP_PORT}",
         "--no-first-run",
         "--no-default-browser-check",
         "--restore-last-session"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **( {"creationflags": subprocess.CREATE_NO_WINDOW}
            if platform.system() == "Windows" else {} ),
    )

    # Wait up to 8 s for CDP to become available
    for _ in range(16):
        _time.sleep(0.5)
        try:
            resp = httpx.get(f"{_CDP_BASE}/json/version", timeout=1)
            if resp.status_code == 200:
                return True
        except Exception:
            pass

    return False


def _cdp_list_tabs() -> list[dict]:
    """Return all open Chrome tabs via the CDP HTTP API."""
    try:
        resp = httpx.get(f"{_CDP_BASE}/json", timeout=3)
        return [t for t in resp.json() if t.get("type") == "page"]
    except Exception:
        return []


def _cdp_new_tab(url: str) -> dict | None:
    """Open a new tab at *url* and return its CDP descriptor."""
    try:
        resp = httpx.get(f"{_CDP_BASE}/json/new?{url}", timeout=5)
        return resp.json()
    except Exception:
        return None


def _cdp_exec(ws_url: str, method: str, params: dict | None = None,
              timeout: int = 15) -> dict:
    """Send a single CDP command over WebSocket and return the result.
    Falls back to AppleScript if websocket-client is not installed."""
    if not _WS_AVAILABLE:
        raise RuntimeError(
            "websocket-client not installed. Run: pip install websocket-client"
        )
    params = params or {}
    cmd_id = 1
    payload = _json.dumps({"id": cmd_id, "method": method, "params": params})
    result: dict = {}

    def _run():
        nonlocal result
        try:
            ws = _websocket.create_connection(ws_url, timeout=timeout)
            ws.send(payload)
            deadline = _time.time() + timeout
            while _time.time() < deadline:
                raw = ws.recv()
                msg = _json.loads(raw)
                if msg.get("id") == cmd_id:
                    result = msg.get("result", {})
                    break
            ws.close()
        except Exception as exc:
            result = {"__error": str(exc)}

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout + 2)
    return result


def _cdp_js(ws_url: str, js: str, timeout: int = 15) -> str:
    """Execute *js* in the tab at *ws_url* and return the string result."""
    res = _cdp_exec(ws_url, "Runtime.evaluate",
                    {"expression": js, "returnByValue": True, "awaitPromise": False},
                    timeout=timeout)
    if "__error" in res:
        return f"cdp-error:{res['__error']}"
    val = res.get("result", {})
    if val.get("type") == "string":
        return val.get("value", "")
    return str(val.get("value", ""))


def _cdp_navigate(ws_url: str, url: str, timeout: int = 15) -> None:
    """Navigate the tab to *url* via CDP."""
    _cdp_exec(ws_url, "Page.navigate", {"url": url}, timeout=timeout)
    _time.sleep(3)   # wait for DOMContentLoaded


def _get_active_tab_ws() -> str | None:
    """Return the WebSocket debugger URL of the currently active (foremost) tab."""
    tabs = _cdp_list_tabs()
    if not tabs:
        return None
    # Prefer the most recently opened visible page tab
    return tabs[0].get("webSocketDebuggerUrl")


# ── Public browser helpers (used by analyse / strategy functions) ─────────────

def _chrome_open_url(url: str) -> str | None:
    """Open *url* in Chrome via CDP. Returns the tab's WebSocket debugger URL."""
    if not _cdp_ensure_chrome():
        raise RuntimeError(
            f"Chrome is not reachable on port {_CDP_PORT}. "
            "Start Chrome with: --remote-debugging-port=9222"
        )
    tab = _cdp_new_tab(url)
    if tab and tab.get("webSocketDebuggerUrl"):
        _time.sleep(3)
        return tab["webSocketDebuggerUrl"]
    # Fallback: navigate in the first existing tab
    tabs = _cdp_list_tabs()
    if tabs:
        ws = tabs[0].get("webSocketDebuggerUrl", "")
        _cdp_navigate(ws, url)
        return ws
    return None


def _chrome_js(js: str, ws_url: str | None = None) -> str:
    """Execute *js* in the active Chrome tab via CDP.
    If *ws_url* is None the currently active tab is used."""
    if ws_url is None:
        ws_url = _get_active_tab_ws()
    if not ws_url:
        return "error:no-tab"
    return _cdp_js(ws_url, js)


def _latest_download(downloads_dir: Path, before: set[Path], timeout: int = 90) -> Path | None:
    """Poll *downloads_dir* until a new completed file appears (no .crdownload / .part)."""
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        current = {
            p for p in downloads_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() not in {".crdownload", ".part", ".tmp"}
            and not p.name.startswith(".")
        }
        new = current - before
        if new:
            return max(new, key=lambda p: p.stat().st_mtime)
        _time.sleep(1.5)
    return None


@mcp.tool()
def ensure_chrome_debug() -> dict:
    """Start (or restart) Google Chrome with --remote-debugging-port=9222.

    Called automatically by download_mod_via_browser, but you can also call
    this tool manually to prepare Chrome before triggering a download.

    • If Chrome is already running WITH the debug port → nothing changes.
    • If Chrome is running WITHOUT the debug port → Chrome is quit and
      relaunched with the flag (previous session is restored).
    • If Chrome is not running → launched fresh with the flag.
    """
    ok = _cdp_ensure_chrome()
    if ok:
        try:
            version = httpx.get(f"{_CDP_BASE}/json/version", timeout=2).json()
            tabs    = _cdp_list_tabs()
            return {
                "status": "ready",
                "browser": version.get("Browser", "unknown"),
                "cdp_port": _CDP_PORT,
                "open_tabs": len(tabs),
            }
        except Exception:
            return {"status": "ready", "cdp_port": _CDP_PORT}
    return {
        "status": "failed",
        "message": (
            "Chrome could not be started with --remote-debugging-port=9222. "
            r"Please make sure Google Chrome is installed (e.g. C:\Program Files\Google\Chrome\Application\chrome.exe)."
        ),
    }


@mcp.tool()
def browser_navigate(
    url: str,
    wait_seconds: int = 3,
) -> dict:
    """Open a URL in Chrome and return page info.

    Use this to navigate Chrome to any URL — e.g. to open a download page,
    login page, or any website. Chrome is started automatically with CDP
    if not already running.

    Args:
        url: The URL to open.
        wait_seconds: Seconds to wait for page load (default 3).
    """
    if not _cdp_ensure_chrome():
        return {
            "error": "Chrome konnte nicht gestartet werden. Ist Chrome installiert?"
        }

    ws = _chrome_open_url(url)
    if not ws:
        return {"error": "Tab konnte nicht geöffnet werden."}

    if wait_seconds > 3:
        _time.sleep(wait_seconds - 3)  # _chrome_open_url already waits 3s

    # Read page state
    page_info = _cdp_js(ws, """
    JSON.stringify({
        url: window.location.href,
        title: document.title,
        readyState: document.readyState,
        hasLogin: !!(document.querySelector('[href*="login"],[href*="signin"],'
            + '[href*="anmelden"],input[type="password"]')),
        bodyLength: (document.body && document.body.innerText || "").length
    })
    """)

    try:
        info = _json.loads(page_info)
    except Exception:
        info = {"url": url, "raw": page_info}

    return {
        "status": "ok",
        "tab_ws": ws,
        **info,
    }


@mcp.tool()
def browser_click(
    selector: str = "",
    text: str = "",
    tab_ws: str = "",
) -> dict:
    """Click an element in the current Chrome tab.

    Use CSS selector or text content to find the element.

    Args:
        selector: CSS selector (e.g. '#download-btn', '.btn-primary').
        text: Text content to search for (e.g. 'Download', 'Login').
              If both selector and text are given, selector takes priority.
        tab_ws: WebSocket URL of the tab (from browser_navigate). Empty = active tab.
    """
    if not selector and not text:
        return {"error": "Entweder 'selector' oder 'text' muss angegeben werden."}

    ws = tab_ws or _get_active_tab_ws()
    if not ws:
        return {"error": "Kein Chrome-Tab verfügbar. Erst browser_navigate aufrufen."}

    if selector:
        js_code = f"""
        (function(){{
            var el = document.querySelector('{selector}');
            if(el) {{ el.click(); return 'clicked: ' + (el.innerText||'').trim().substring(0,50); }}
            return 'not-found: {selector}';
        }})()
        """
    else:
        safe_text = text.replace("'", "\\'")
        js_code = f"""
        (function(){{
            var all = document.querySelectorAll('a,button,[role="button"],input[type="submit"]');
            for(var i=0; i<all.length; i++){{
                var t = (all[i].innerText||all[i].value||'').trim().toLowerCase();
                if(t.includes('{safe_text}'.toLowerCase())){{
                    all[i].click();
                    return 'clicked: ' + (all[i].innerText||all[i].value||'').trim().substring(0,50);
                }}
            }}
            return 'not-found: {safe_text}';
        }})()
        """

    result = _cdp_js(ws, js_code)
    return {"status": "ok" if "clicked" in result else "not_found", "result": result}


@mcp.tool()
def browser_type(
    selector: str,
    text: str,
    submit: bool = False,
    tab_ws: str = "",
) -> dict:
    """Type text into an input field in Chrome.

    Useful for filling login forms, search boxes, etc.

    Args:
        selector: CSS selector for the input (e.g. 'input[name="email"]', '#password').
        text: Text to type into the field.
        submit: If True, press Enter after typing (submits the form).
        tab_ws: WebSocket URL of the tab. Empty = active tab.
    """
    ws = tab_ws or _get_active_tab_ws()
    if not ws:
        return {"error": "Kein Chrome-Tab verfügbar."}

    safe_text = text.replace("\\", "\\\\").replace("'", "\\'")
    submit_code = (
        "el.form && el.form.submit();"
        if submit else ""
    )
    js_code = f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if(!el) return 'not-found: {selector}';
        el.focus();
        el.value = '{safe_text}';
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        {submit_code}
        return 'typed: ' + '{safe_text}'.substring(0,3) + '***';
    }})()
    """

    result = _cdp_js(ws, js_code)
    return {"status": "ok" if "typed" in result else "not_found", "result": result}


@mcp.tool()
def browser_read_page(
    tab_ws: str = "",
) -> dict:
    """Read the current page content from Chrome.

    Returns URL, title, visible text, and form fields.
    Useful to check if a login was successful or what's on the page.

    Args:
        tab_ws: WebSocket URL of the tab. Empty = active tab.
    """
    ws = tab_ws or _get_active_tab_ws()
    if not ws:
        return {"error": "Kein Chrome-Tab verfügbar."}

    js_code = r"""
    (function(){
        var inputs = [];
        document.querySelectorAll('input,select,textarea').forEach(function(el){
            if(el.type === 'hidden') return;
            inputs.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                value: el.type === 'password' ? '***' : (el.value||'').substring(0,50)
            });
        });

        var links = [];
        document.querySelectorAll('a[href]').forEach(function(a){
            var t = (a.innerText||'').trim();
            if(t && t.length < 80) links.push({text:t, href:a.href});
        });

        return JSON.stringify({
            url: window.location.href,
            title: document.title,
            bodyText: (document.body && document.body.innerText || '').substring(0, 2000),
            inputs: inputs.slice(0, 20),
            links: links.slice(0, 30)
        });
    })()
    """

    raw = _cdp_js(ws, js_code)
    try:
        return _json.loads(raw)
    except Exception:
        return {"url": "unknown", "raw": raw}


# ---------------------------------------------------------------------------
# JS snippets used by the browser-download tools
# ---------------------------------------------------------------------------

# Clicks the first visible "Download" / "Free Download" button on the page.
_JS_CLICK_MAIN_DOWNLOAD = r"""
(function(){
  var selectors = [
    '[data-action="download"]',
    'button[id*="download"]','a[id*="download"]',
    'a.download-btn','button.download-btn',
    '.btn-download','#btn-download',
    'a[href*="/download"]','a[href*="download"]'
  ];
  for(var i=0;i<selectors.length;i++){
    var els=document.querySelectorAll(selectors[i]);
    for(var j=0;j<els.length;j++){
      var t=(els[j].textContent||"").toLowerCase().trim();
      if(t.includes("download")||t===""){els[j].click();return"clicked:"+selectors[i];}
    }
  }
  var all=document.querySelectorAll("a,button");
  for(var k=0;k<all.length;k++){
    if((all[k].innerText||"").toLowerCase().includes("download")){
      all[k].click();return"fallback:"+all[k].innerText.trim().substring(0,50);
    }
  }
  return"not-found";
})()
"""

# Detects whether a modal/dialog is currently visible and returns its info.
_JS_DETECT_MODAL = r"""
(function(){
  var modalSelectors=[
    '.modal','[role="dialog"]','[aria-modal="true"]',
    '.dialog','#download-modal','[class*="modal"]','[class*="dialog"]',
    '[class*="popup"]','[class*="overlay"]'
  ];
  for(var i=0;i<modalSelectors.length;i++){
    var els=document.querySelectorAll(modalSelectors[i]);
    for(var j=0;j<els.length;j++){
      var el=els[j];
      var style=window.getComputedStyle(el);
      if(style.display!=="none"&&style.visibility!=="hidden"&&style.opacity!=="0"){
        // Count download buttons inside the modal
        var btns=el.querySelectorAll("a,button");
        var dlBtns=[];
        for(var k=0;k<btns.length;k++){
          if((btns[k].innerText||btns[k].href||"").toLowerCase().includes("download"))
            dlBtns.push(btns[k].innerText.trim().substring(0,60)+"||"+(btns[k].href||""));
        }
        return JSON.stringify({found:true,selector:modalSelectors[i],downloadButtons:dlBtns});
      }
    }
  }
  return JSON.stringify({found:false});
})()
"""

# Clicks ALL download buttons inside any visible modal, one per row/item.
# Returns JSON array of what was clicked.
_JS_CLICK_ALL_MODAL_DOWNLOADS = r"""
(function(){
  var modalSelectors=[
    '.modal','[role="dialog"]','[aria-modal="true"]',
    '.dialog','[class*="modal"]','[class*="dialog"]',
    '[class*="popup"]','[class*="overlay"]'
  ];
  var clicked=[];
  for(var i=0;i<modalSelectors.length;i++){
    var modals=document.querySelectorAll(modalSelectors[i]);
    for(var j=0;j<modals.length;j++){
      var modal=modals[j];
      var style=window.getComputedStyle(modal);
      if(style.display==="none"||style.visibility==="hidden"||style.opacity==="0") continue;
      // Find all download anchors/buttons in this modal
      var btns=modal.querySelectorAll("a,button");
      for(var k=0;k<btns.length;k++){
        var btn=btns[k];
        var txt=(btn.innerText||btn.textContent||btn.value||"").toLowerCase();
        var href=(btn.href||"").toLowerCase();
        if(txt.includes("download")||href.includes("download")||href.includes(".zip")||
           href.includes(".7z")||href.includes(".rar")){
          clicked.push((btn.innerText||btn.href||"btn").trim().substring(0,60));
          btn.click();
        }
      }
    }
  }
  return JSON.stringify(clicked.length>0?clicked:["no-modal-buttons-found"]);
})()
"""


def _install_archives(archives: list[Path], community: Path) -> dict:
    """Extract each archive and copy mod roots into *community*. Returns summary dict."""
    all_installed: list[str] = []
    errors: list[str] = []
    for archive in archives:
        if archive.suffix.lower() not in SUPPORTED_ARCHIVES:
            errors.append(f"{archive.name}: unsupported format {archive.suffix}")
            continue
        with tempfile.TemporaryDirectory(prefix="msfs_mod_") as tmp:
            tmp_path = Path(tmp)
            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            try:
                _extract(archive, extract_dir)
            except Exception as exc:
                errors.append(f"{archive.name}: extraction failed – {exc}")
                continue
            mod_roots = _find_mod_roots(extract_dir)
            if not mod_roots:
                errors.append(f"{archive.name}: no mod folders found")
                continue
            for root in mod_roots:
                dest = community / root.name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(root, dest)
                all_installed.append(root.name)
    return {"installed_mods": all_installed, "errors": errors}


# ---------------------------------------------------------------------------
# Intelligent page analysis – figures out HOW to download before acting
# ---------------------------------------------------------------------------

# JS that reads the full page state and returns a structured analysis JSON.
_JS_ANALYSE_PAGE = r"""
(function(){
  var result = {
    url: window.location.href,
    title: document.title,
    strategy: "unknown",
    directLinks: [],
    downloadButtons: [],
    modalTriggers: [],
    captcha: false,
    loginRequired: false,
    countdown: null,
    notes: []
  };

  // 1. Detect login wall
  var bodyText = (document.body && document.body.innerText || "").toLowerCase();
  if(bodyText.includes("sign in") || bodyText.includes("log in") || bodyText.includes("anmelden"))
    result.loginRequired = true;

  // 2. Detect captcha / human check
  if(bodyText.includes("captcha") || bodyText.includes("human") || document.querySelector("iframe[src*='recaptcha']"))
    result.captcha = true;

  // 3. Detect countdown timer (some sites delay the download button)
  var timerEl = document.querySelector("[id*='timer'],[class*='timer'],[id*='countdown'],[class*='countdown']");
  if(timerEl) result.countdown = (timerEl.innerText||"").trim().substring(0,20);

  // 4. Find all <a> tags with direct archive links
  var anchors = document.querySelectorAll("a[href]");
  for(var i=0;i<anchors.length;i++){
    var href = anchors[i].href || "";
    if(/\.(zip|7z|rar|exe|msi)(\?|$)/i.test(href)){
      result.directLinks.push({label:(anchors[i].innerText||"").trim().substring(0,60), href:href});
    }
  }

  // 5. Find all clickable download elements (buttons + anchors with download text)
  var clickables = document.querySelectorAll("a,button,[role='button']");
  for(var j=0;j<clickables.length;j++){
    var el = clickables[j];
    var txt = (el.innerText||el.textContent||el.value||"").toLowerCase().trim();
    var elHref = (el.href||"").toLowerCase();
    var isDownload = txt.includes("download") || txt.includes("herunterladen") ||
                     elHref.includes("download") || el.hasAttribute("download");
    if(!isDownload) continue;
    var style = window.getComputedStyle(el);
    var visible = style.display!=="none" && style.visibility!=="hidden" && style.opacity!=="0";
    var info = {
      tag: el.tagName,
      text: (el.innerText||"").trim().substring(0,60),
      href: el.href||"",
      id: el.id||"",
      cls: (el.className||"").substring(0,60),
      visible: visible,
      isModalTrigger: !!(el.getAttribute("data-bs-toggle")||el.getAttribute("data-toggle")||
                         el.getAttribute("data-target")||el.getAttribute("data-modal"))
    };
    if(info.isModalTrigger) result.modalTriggers.push(info);
    else result.downloadButtons.push(info);
  }

  // 6. Decide best strategy
  if(result.captcha){
    result.strategy = "captcha_blocked";
    result.notes.push("CAPTCHA detected – manual interaction needed");
  } else if(result.directLinks.length > 0){
    result.strategy = "direct_link";
    result.notes.push("Direct archive link(s) found – no click needed");
  } else if(result.countdown){
    result.strategy = "countdown_then_click";
    result.notes.push("Countdown timer found: " + result.countdown);
  } else if(result.modalTriggers.length > 0){
    result.strategy = "click_opens_modal";
    result.notes.push("Download button opens a modal with multiple files");
  } else if(result.downloadButtons.length > 0){
    result.strategy = "direct_click";
    result.notes.push("Download button found – single click");
  } else {
    result.strategy = "unknown";
    result.notes.push("No download element found on this page state");
  }

  return JSON.stringify(result);
})()
"""

# JS that waits for a countdown to reach zero then returns.
_JS_WAIT_COUNTDOWN = r"""
(function(){
  var max = 30, waited = 0;
  var check = setInterval(function(){
    waited++;
    var timers = document.querySelectorAll("[id*='timer'],[class*='timer'],[id*='countdown'],[class*='countdown']");
    var active = false;
    for(var i=0;i<timers.length;i++){
      var t = (timers[i].innerText||"").trim();
      if(t && /\d/.test(t) && parseInt(t) > 0){ active = true; break; }
    }
    if(!active || waited >= max){ clearInterval(check); }
  }, 1000);
  return "countdown-wait-started";
})()
"""


def _analyse_page(url: str) -> dict:
    """Open *url* in Chrome via CDP, analyse the page and return a strategy dict.
    The returned dict includes a '_ws_url' key so callers can reuse the same tab."""
    try:
        ws_url = _chrome_open_url(url)  # opens tab, waits 3 s
    except Exception as exc:
        return {"error": f"Cannot open Chrome via CDP: {exc}", "strategy": "error"}

    if not ws_url:
        return {"error": "CDP tab could not be opened", "strategy": "error"}

    for attempt in range(3):
        try:
            raw = _cdp_js(ws_url, _JS_ANALYSE_PAGE)
            if raw.startswith("{"):
                result = _json.loads(raw)
                result["_ws_url"] = ws_url   # carry tab reference forward
                return result
        except Exception:
            pass
        _time.sleep(1)

    return {"strategy": "unknown", "notes": ["Page analysis JS failed"], "_ws_url": ws_url}


def _execute_strategy(analysis: dict, dl_dir: Path, modal_timeout: int, timeout: int) -> dict:
    """Given a page analysis dict (with _ws_url), execute the download strategy.
    All JS is sent to the same CDP tab. Returns {"collected": [Path, ...], "log": [...]}"""
    strategy = analysis.get("strategy", "unknown")
    ws = analysis.get("_ws_url")            # CDP WebSocket URL of the open tab
    log: list[str] = [f"Strategy chosen: {strategy}", f"CDP tab: {ws or 'none'}"]
    log += analysis.get("notes", [])

    def js(code: str) -> str:
        return _cdp_js(ws, code) if ws else "error:no-ws"

    def snapshot() -> set[Path]:
        return {
            p for p in dl_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() not in {".crdownload", ".part", ".tmp"}
        }

    before = snapshot()

    # ── Strategy: direct archive link ────────────────────────────────────────
    if strategy == "direct_link":
        for link in analysis.get("directLinks", []):
            href = link.get("href", "")
            if href:
                log.append(f"CDP navigate to direct link: {href[:80]}")
                try:
                    if ws:
                        _cdp_navigate(ws, href)
                    else:
                        _chrome_open_url(href)
                except Exception as e:
                    log.append(f"  error: {e}")

    # ── Strategy: countdown then click ───────────────────────────────────────
    elif strategy == "countdown_then_click":
        log.append("Waiting for countdown via CDP…")
        js(_JS_WAIT_COUNTDOWN)
        _time.sleep(32)
        r = js(_JS_CLICK_MAIN_DOWNLOAD)
        log.append(f"Post-countdown click: {r}")

    # ── Strategy: click opens modal ───────────────────────────────────────────
    elif strategy == "click_opens_modal":
        r = js(_JS_CLICK_MAIN_DOWNLOAD)
        log.append(f"Modal trigger clicked via CDP: {r}")
        # Wait for modal, then click all items inside
        deadline = _time.time() + modal_timeout
        modal_found = False
        while _time.time() < deadline:
            try:
                raw = js(_JS_DETECT_MODAL)
                info = _json.loads(raw) if raw.startswith("{") else {}
                if info.get("found"):
                    modal_found = True
                    break
            except Exception:
                pass
            _time.sleep(0.8)
        if modal_found:
            log.append("Modal detected via CDP – clicking all download buttons inside")
            raw_clicks = js(_JS_CLICK_ALL_MODAL_DOWNLOADS)
            clicks = _json.loads(raw_clicks) if raw_clicks.startswith("[") else []
            log.append(f"Modal clicks: {clicks}")
        else:
            log.append("Modal did not appear – trying direct click via CDP as fallback")
            r = js(_JS_CLICK_MAIN_DOWNLOAD)
            log.append(f"Fallback click: {r}")

    # ── Strategy: direct click ────────────────────────────────────────────────
    elif strategy == "direct_click":
        r = js(_JS_CLICK_MAIN_DOWNLOAD)
        log.append(f"Download button clicked via CDP: {r}")
        # Also check if a modal pops up after click
        _time.sleep(1.5)
        try:
            raw = js(_JS_DETECT_MODAL)
            info = _json.loads(raw) if raw.startswith("{") else {}
            if info.get("found"):
                log.append("Unexpected modal appeared – clicking all buttons via CDP")
                raw_clicks = js(_JS_CLICK_ALL_MODAL_DOWNLOADS)
                log.append(f"Modal clicks: {raw_clicks}")
        except Exception:
            pass

    # ── Strategy: captcha / unknown – best-effort ─────────────────────────────
    else:
        log.append("Unknown strategy – attempting blind click via CDP")
        r = js(_JS_CLICK_MAIN_DOWNLOAD)
        log.append(f"Blind click: {r}")

    # ── Wait for files ────────────────────────────────────────────────────────
    collected: list[Path] = []
    deadline_dl = _time.time() + timeout
    while _time.time() < deadline_dl:
        current = snapshot()
        new = current - before - set(collected)
        if new:
            collected.extend(new)
            # Give a bit more time for additional files (multi-file modal)
            deadline_dl = max(deadline_dl, _time.time() + 8)
        _time.sleep(1.5)

    return {"collected": collected, "log": log}


@mcp.tool()
def download_mod_via_browser(
    url: str,
    downloads_dir: str = "",
    community_path: str = "",
    auto_install: bool = True,
    timeout: int = 120,
    modal_timeout: int = 8,
) -> dict:
    """Smart browser-assisted mod downloader for flightsim.to (and similar sites).

    Before clicking anything the tool ANALYSES the page and chooses the best
    download strategy automatically:

      • direct_link       – Archive link found in HTML → navigate directly, no click
      • direct_click      – Single visible Download button → click it
      • click_opens_modal – Button opens a modal with multiple files → click ALL items
      • countdown_then_click – Timer delays the button → wait, then click
      • captcha_blocked   – CAPTCHA detected → reports back, lets user know
      • unknown           – Nothing found → blind click as last resort

    After downloading, every archive (.zip / .7z / .rar) is extracted and
    copied into the MSFS Community folder (when auto_install=True).

    Works on Windows via Chrome DevTools Protocol (CDP). Chrome is started automatically.

    Args:
        url:            flightsim.to addon/file page URL.
        downloads_dir:  Chrome download directory (default: ~/Downloads).
        community_path: MSFS 2024 Community folder – leave empty for auto-detect.
        auto_install:   Extract & install into Community when True (default).
        timeout:        Seconds to wait for all files (default 120).
        modal_timeout:  Seconds to wait for a modal to appear (default 8).
    """
    dl_dir = Path(downloads_dir).expanduser() if downloads_dir else Path.home() / "Downloads"
    if not dl_dir.exists():
        return {"error": f"Downloads directory not found: {dl_dir}"}

    # ── 1. Analyse the page ───────────────────────────────────────────────────
    analysis = _analyse_page(url)

    if analysis.get("loginRequired"):
        return {
            "status": "login_required",
            "url": url,
            "strategy": "login_required",
            "message": (
                "Die Seite erfordert einen Login. Chrome wurde geöffnet — "
                "bitte logge dich im Browser ein. Danach ruf dieses Tool "
                "erneut mit derselben URL auf."
            ),
            "page_analysis": analysis,
        }

    if analysis.get("strategy") == "captcha_blocked":
        return {
            "status": "blocked",
            "strategy": "captcha_blocked",
            "message": "CAPTCHA erkannt. Bitte im Browser lösen, dann erneut aufrufen.",
            "page_analysis": analysis,
        }
    if "error" in analysis:
        return {"status": "error", "message": analysis["error"]}

    # ── 2. Execute strategy + collect files ───────────────────────────────────
    exec_result = _execute_strategy(analysis, dl_dir, modal_timeout, timeout)
    collected: list[Path] = exec_result["collected"]
    log: list[str] = exec_result["log"]

    if not collected:
        return {
            "status": "timeout",
            "strategy": analysis.get("strategy"),
            "log": log,
            "page_analysis": {
                "directLinks": analysis.get("directLinks", []),
                "downloadButtons": len(analysis.get("downloadButtons", [])),
                "modalTriggers": len(analysis.get("modalTriggers", [])),
                "notes": analysis.get("notes", []),
            },
            "message": f"No new files appeared in {dl_dir} within {timeout}s.",
        }

    result: dict = {
        "status": "downloaded",
        "strategy_used": analysis.get("strategy"),
        "files": [str(p) for p in collected],
        "filenames": [p.name for p in collected],
        "log": log,
    }

    # ── 3. Install ────────────────────────────────────────────────────────────
    if auto_install:
        community = _detect_community_folder(community_path)
        if community is None:
            result["install_status"] = "skipped – Community folder not found. Pass community_path."
            return result

        archives = [p for p in collected if p.suffix.lower() in SUPPORTED_ARCHIVES]
        if not archives:
            result["install_status"] = (
                f"skipped – none of the downloaded files are supported archives "
                f"({', '.join(SUPPORTED_ARCHIVES)})."
            )
            return result

        install_result = _install_archives(archives, community)
        result["install_status"] = "ok" if not install_result["errors"] else "partial"
        result["community_folder"] = str(community)
        result["installed_mods"] = install_result["installed_mods"]
        result["install_errors"] = install_result["errors"]

    return result


@mcp.tool()
def list_modal_downloads(url: str) -> dict:
    """Open a flightsim.to page, click the Download button, wait for a modal,
    and return a list of all downloadable items found in the modal WITHOUT
    downloading anything yet.  Useful to preview what a multi-file mod contains.

    Args:
        url: flightsim.to addon/file page URL.
    """
    try:
        ws = _chrome_open_url(url)   # CDP: open tab, return ws URL
    except Exception as exc:
        return {"error": f"Could not open Chrome via CDP: {exc}"}
    if not ws:
        return {"error": "CDP tab could not be opened"}

    _cdp_js(ws, _JS_CLICK_MAIN_DOWNLOAD)

    for _ in range(10):
        _time.sleep(1)
        try:
            raw = _cdp_js(ws, _JS_DETECT_MODAL)
            info = _json.loads(raw) if raw.startswith("{") else {"found": False}
        except Exception:
            info = {"found": False}
        if info.get("found"):
            return {
                "modal_found": True,
                "download_buttons": info.get("downloadButtons", []),
                "count": len(info.get("downloadButtons", [])),
                "cdp_tab": ws,
            }

    return {"modal_found": False, "message": "No modal appeared within 10 seconds."}


# ---------------------------------------------------------------------------
# Mod search helper – queries flightsim.to search API + page scrape
# ---------------------------------------------------------------------------

def _search_flightsim(query: str, max_results: int = 8) -> list[dict]:
    """Search flightsim.to for *query* and return a list of mod dicts.
    Each dict has keys: id, title, author, category, url, thumbnail.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/html, */*",
        "Referer": "https://flightsim.to/",
    }
    results: list[dict] = []

    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:

        # ── Try 1: JSON API ───────────────────────────────────────────────────
        try:
            resp = client.get(
                "https://flightsim.to/api/v1/search",
                params={"q": query, "per_page": max_results},
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", [])
                for item in items[:max_results]:
                    addon_id = item.get("id") or item.get("addon_id", "")
                    slug     = item.get("slug") or item.get("name", str(addon_id))
                    results.append({
                        "id":        str(addon_id),
                        "title":     item.get("title") or item.get("name", "Unknown"),
                        "author":    (item.get("user") or {}).get("username", ""),
                        "category":  item.get("category", ""),
                        "url":       f"https://flightsim.to/file/{addon_id}/{slug}",
                        "thumbnail": item.get("thumbnail") or item.get("image", ""),
                    })
                if results:
                    return results
        except Exception:
            pass

        # ── Try 2: Scrape the search results page ─────────────────────────────
        try:
            resp = client.get(
                "https://flightsim.to/search",
                params={"q": query},
            )
            if resp.status_code == 200:
                html = resp.text
                # Extract addon cards – pattern: /file/{id}/{slug}
                pattern = r'href="https://flightsim\.to/file/(\d+)/([^"]+)"[^>]*>.*?<[^>]+class="[^"]*title[^"]*"[^>]*>([^<]+)<'
                for m in re.finditer(pattern, html, re.S):
                    addon_id, slug, title = m.group(1), m.group(2), m.group(3).strip()
                    if not any(r["id"] == addon_id for r in results):
                        results.append({
                            "id":        addon_id,
                            "title":     title,
                            "author":    "",
                            "category":  "",
                            "url":       f"https://flightsim.to/file/{addon_id}/{slug}",
                            "thumbnail": "",
                        })
                    if len(results) >= max_results:
                        break

                # Fallback: simpler href scan
                if not results:
                    seen: set[str] = set()
                    for m in re.finditer(
                        r'href="(https://flightsim\.to/file/(\d+)/([^"?#]+))"', html
                    ):
                        href, addon_id, slug = m.group(1), m.group(2), m.group(3)
                        if addon_id in seen or not slug or slug.endswith("/download"):
                            continue
                        seen.add(addon_id)
                        results.append({
                            "id":        addon_id,
                            "title":     slug.replace("-", " ").title(),
                            "author":    "",
                            "category":  "",
                            "url":       href,
                            "thumbnail": "",
                        })
                        if len(results) >= max_results:
                            break
        except Exception:
            pass

    # ── Try 3: CDP – open Chrome, let JS render, read live DOM ───────────────
    if not results:
        results = _search_flightsim_via_cdp(query, max_results)

    return results


# JS run inside Chrome after the search page has rendered –
# extracts every addon card that is actually visible in the DOM.
_JS_EXTRACT_SEARCH_RESULTS = r"""
(function(){
  var cards = [];
  // flightsim.to renders cards as <a href="/file/{id}/{slug}"> with a title inside
  var selectors = [
    'a[href*="/file/"]',
    '[class*="card"] a[href*="/file/"]',
    '[class*="addon"] a[href*="/file/"]',
    '[class*="result"] a[href*="/file/"]',
  ];
  var seen = {};
  selectors.forEach(function(sel){
    document.querySelectorAll(sel).forEach(function(el){
      var href = el.href || "";
      var m = href.match(/\/file\/(\d+)\/([^/?#]+)/);
      if (!m || seen[m[1]]) return;
      seen[m[1]] = true;

      // Title: prefer dedicated title element, fall back to link text
      var titleEl = el.querySelector('[class*="title"],[class*="name"],h2,h3,h4,strong');
      var title = (titleEl ? titleEl.innerText : el.innerText || "").trim().substring(0,100);
      if (!title) title = m[2].replace(/-/g," ");

      // Author
      var authorEl = el.querySelector('[class*="author"],[class*="user"],[class*="username"]');
      var author = authorEl ? authorEl.innerText.trim() : "";

      // Category / tag
      var catEl = el.querySelector('[class*="category"],[class*="tag"],[class*="type"]');
      var category = catEl ? catEl.innerText.trim() : "";

      // Downloads / rating  (extra context)
      var dlEl = el.querySelector('[class*="download"],[class*="count"]');
      var downloads = dlEl ? dlEl.innerText.trim() : "";

      cards.push({
        id: m[1],
        title: title,
        author: author,
        category: category,
        downloads: downloads,
        url: "https://flightsim.to/file/" + m[1] + "/" + m[2]
      });
    });
  });
  return JSON.stringify(cards.slice(0,12));
})()
"""


def _search_flightsim_via_cdp(query: str, max_results: int = 8) -> list[dict]:
    """Open flightsim.to/search in Chrome via CDP, wait for JS to render,
    then read the fully populated DOM to extract search results."""
    try:
        search_url = f"https://flightsim.to/search?q={query.replace(' ', '+')}"
        ws = _chrome_open_url(search_url)   # opens tab, waits 3 s
        if not ws:
            return []

        # Give JS-rendered content a bit more time to appear
        _time.sleep(2)

        raw = _cdp_js(ws, _JS_EXTRACT_SEARCH_RESULTS, timeout=15)
        if not raw.startswith("["):
            return []

        items = _json.loads(raw)
        return [
            {
                "id":        item["id"],
                "title":     item["title"],
                "author":    item.get("author", ""),
                "category":  item.get("category", ""),
                "downloads": item.get("downloads", ""),
                "url":       item["url"],
                "thumbnail": "",
                "source":    "cdp",     # so caller knows this came from live DOM
            }
            for item in items[:max_results]
        ]
    except Exception:
        return []


@mcp.tool()
def find_and_install_mod(
    mod_name: str,
    choice_index: int = -1,
    community_path: str = "",
    downloads_dir: str = "",
    auto_install: bool = True,
) -> dict:
    """Find and install an MSFS mod by name from flightsim.to.

    Workflow:
      1. Search flightsim.to for *mod_name*.
      2. No results → tell the user, done.
      3. Exactly 1 result → open the page and download + install automatically.
      4. Multiple results → return the list so the user can pick one.
         Then call again with the same *mod_name* and *choice_index* (0-based)
         to proceed with the chosen entry.

    Args:
        mod_name:       Name or keyword to search for (e.g. "EDDF Frankfurt").
        choice_index:   0-based index from a previous multi-result call.
                        Pass -1 (default) to trigger a new search.
        community_path: MSFS 2024 Community folder – leave empty for auto-detect.
        downloads_dir:  Chrome download directory – leave empty for ~/Downloads.
        auto_install:   Extract & install after download (default True).
    """

    # ── A. User is confirming a previous multi-result search ─────────────────
    if choice_index >= 0:
        # Re-run the search to get the same list
        hits = _search_flightsim(mod_name)
        if not hits:
            return {"status": "no_results",
                    "message": f"No results found for '{mod_name}'."}
        if choice_index >= len(hits):
            return {
                "status": "invalid_choice",
                "message": f"Index {choice_index} is out of range (0–{len(hits)-1}).",
                "results": hits,
            }
        chosen = hits[choice_index]
        # Fall through to download
        return _trigger_download(chosen["url"], community_path, downloads_dir, auto_install)

    # ── B. Fresh search ───────────────────────────────────────────────────────
    hits = _search_flightsim(mod_name)

    if not hits:
        return {
            "status": "no_results",
            "message": (
                f"Keine Ergebnisse für '{mod_name}' auf flightsim.to gefunden. "
                "Bitte prüfe den Mod-Namen oder suche manuell auf https://flightsim.to"
            ),
        }

    if len(hits) == 1:
        # Single hit → download directly, no confirmation needed
        return _trigger_download(hits[0]["url"], community_path, downloads_dir, auto_install)

    # Multiple hits → return list and ask user to choose
    choices = [
        {"index": i, "title": h["title"], "author": h["author"],
         "category": h["category"], "url": h["url"]}
        for i, h in enumerate(hits)
    ]
    return {
        "status": "multiple_results",
        "message": (
            f"{len(hits)} Ergebnisse für '{mod_name}' gefunden. "
            "Bitte ruf das Tool erneut mit choice_index=N auf (0-basiert)."
        ),
        "results": choices,
    }


def _trigger_download(url: str, community_path: str,
                      downloads_dir: str, auto_install: bool) -> dict:
    """Internal helper – runs the full smart download flow for a known URL."""
    dl_dir = (Path(downloads_dir).expanduser()
              if downloads_dir else Path.home() / "Downloads")

    analysis = _analyse_page(url)

    # Handle login-required pages
    if analysis.get("loginRequired"):
        return {
            "status": "login_required",
            "url": url,
            "message": (
                "Die Seite erfordert einen Login. Chrome wurde geöffnet — "
                "bitte logge dich im Browser ein. Danach ruf dieses Tool "
                "erneut mit derselben URL auf."
            ),
        }

    if analysis.get("strategy") == "captcha_blocked":
        return {
            "status": "blocked",
            "url": url,
            "message": "CAPTCHA erkannt. Bitte im Browser lösen, dann erneut aufrufen.",
        }
    if "error" in analysis:
        return {"status": "error", "url": url, "message": analysis["error"]}

    exec_result = _execute_strategy(analysis, dl_dir, modal_timeout=8, timeout=120)
    collected: list[Path] = exec_result["collected"]
    log: list[str]        = exec_result["log"]

    if not collected:
        return {
            "status": "timeout",
            "url": url,
            "strategy": analysis.get("strategy"),
            "log": log,
            "message": f"Keine Datei in {dl_dir} innerhalb von 120s erschienen.",
        }

    result: dict = {
        "status": "downloaded",
        "url": url,
        "strategy_used": analysis.get("strategy"),
        "filenames": [p.name for p in collected],
        "log": log,
    }

    if auto_install:
        community = _detect_community_folder(community_path)
        if community is None:
            result["install_status"] = "skipped – Community-Ordner nicht gefunden."
            return result
        archives = [p for p in collected if p.suffix.lower() in SUPPORTED_ARCHIVES]
        if not archives:
            result["install_status"] = "skipped – keine unterstützten Archive gefunden."
            return result
        ir = _install_archives(archives, community)
        result["install_status"]  = "ok" if not ir["errors"] else "partial"
        result["community_folder"] = str(community)
        result["installed_mods"]   = ir["installed_mods"]
        result["install_errors"]   = ir["errors"]

    return result


# ===========================================================================
# ReShade Integration (reshade.me)
# ===========================================================================

import configparser as _configparser

# ── ReShade config search paths ───────────────────────────────────────────

# Common MSFS install locations (ReShade.ini lives next to the game .exe)
_RESHADE_GAME_DIRS: list[Path] = [
    # MSFS 2024 (Steam)
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Microsoft Flight Simulator 2024"),
    Path(r"D:\Steam\steamapps\common\Microsoft Flight Simulator 2024"),
    Path(r"D:\SteamLibrary\steamapps\common\Microsoft Flight Simulator 2024"),
    Path(r"E:\SteamLibrary\steamapps\common\Microsoft Flight Simulator 2024"),
    # MSFS 2024 (MS Store)
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Packages" / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "Local",
    # Generic game dirs
    Path(r"C:\Program Files (x86)\Steam\steamapps\common"),
    Path(r"D:\Steam\steamapps\common"),
    Path(r"D:\Games"),
    Path(r"E:\Games"),
]

_RESHADE_INI_NAMES = [
    "ReShade.ini", "reshade.ini", "Reshade.ini",
    "dxgi.ini", "d3d11.ini", "d3d12.ini", "opengl32.ini",
]

_RESHADE_PRESET_EXTENSIONS = {".ini", ".txt"}

# ── ReShade effect definitions ────────────────────────────────────────────

RESHADE_EFFECTS: dict[str, dict] = {
    # LUT / Color
    "LiftGammaGain": {
        "label": "Lift Gamma Gain",
        "category": "Color",
        "keys": {
            "RGB_Lift": {"label": "Lift (Schatten)", "default": "1.0,1.0,1.0"},
            "RGB_Gamma": {"label": "Gamma", "default": "1.0,1.0,1.0"},
            "RGB_Gain": {"label": "Gain (Lichter)", "default": "1.0,1.0,1.0"},
        },
    },
    "Tonemap": {
        "label": "Tonemap",
        "category": "Color",
        "keys": {
            "Gamma": {"label": "Gamma", "default": "1.0", "range": "0.0-2.0"},
            "Exposure": {"label": "Belichtung", "default": "0.0", "range": "-1.0-1.0"},
            "Saturation": {"label": "Sättigung", "default": "0.0", "range": "-1.0-1.0"},
            "Bleach": {"label": "Bleach", "default": "0.0", "range": "0.0-1.0"},
            "Defog": {"label": "Entnebelung", "default": "0.0", "range": "0.0-1.0"},
        },
    },
    "Vibrance": {
        "label": "Vibrance",
        "category": "Color",
        "keys": {
            "Vibrance": {"label": "Vibrance", "default": "0.15", "range": "-1.0-1.0"},
        },
    },
    "Colorfulness": {
        "label": "Colorfulness",
        "category": "Color",
        "keys": {
            "colorfullness": {"label": "Farbintensität", "default": "0.4", "range": "0.0-2.0"},
            "lim_luma": {"label": "Luminanz-Limit", "default": "0.7"},
        },
    },
    "Levels": {
        "label": "Levels",
        "category": "Color",
        "keys": {
            "BlackPoint": {"label": "Schwarzpunkt", "default": "16", "range": "0-255"},
            "WhitePoint": {"label": "Weißpunkt", "default": "235", "range": "0-255"},
        },
    },
    # Sharpening
    "CAS": {
        "label": "AMD CAS (Contrast Adaptive Sharpening)",
        "category": "Sharpening",
        "keys": {
            "Sharpness": {"label": "Schärfe", "default": "0.4", "range": "0.0-1.0"},
        },
    },
    "AdaptiveSharpen": {
        "label": "Adaptive Sharpen",
        "category": "Sharpening",
        "keys": {
            "curve_height": {"label": "Schärfe-Intensität", "default": "0.3", "range": "0.0-2.0"},
        },
    },
    "LumaSharpen": {
        "label": "LumaSharpen",
        "category": "Sharpening",
        "keys": {
            "sharp_strength": {"label": "Schärfe", "default": "0.65", "range": "0.1-3.0"},
            "sharp_clamp": {"label": "Clamp", "default": "0.035"},
        },
    },
    # Clarity / Detail
    "Clarity": {
        "label": "Clarity",
        "category": "Detail",
        "keys": {
            "ClarityRadius": {"label": "Radius", "default": "3"},
            "ClarityBlendMode": {"label": "Blend-Modus", "default": "2"},
            "ClarityOffset": {"label": "Offset", "default": "2.0"},
            "ClarityStrength": {"label": "Stärke", "default": "0.4", "range": "0.0-1.0"},
        },
    },
    # Bloom / Glow
    "MagicBloom": {
        "label": "Magic Bloom",
        "category": "Bloom",
        "keys": {
            "f2Sigma": {"label": "Bloom-Breite", "default": "0.5"},
            "f2Intensity": {"label": "Intensität", "default": "1.0", "range": "0.0-10.0"},
        },
    },
    # Anti-Aliasing
    "SMAA": {
        "label": "SMAA Anti-Aliasing",
        "category": "AA",
        "keys": {
            "EdgeDetectionType": {"label": "Kantenerkennung", "default": "1",
                                  "values": {"0": "Luminance", "1": "Color", "2": "Depth"}},
            "EdgeDetectionThreshold": {"label": "Schwellwert", "default": "0.1"},
        },
    },
    "FXAA": {
        "label": "FXAA Anti-Aliasing",
        "category": "AA",
        "keys": {
            "Subpix": {"label": "Sub-Pixel AA", "default": "0.25", "range": "0.0-1.0"},
            "EdgeThreshold": {"label": "Schwellwert", "default": "0.125"},
        },
    },
    # Depth effects (Vorsicht in VR — Performance-intensiv!)
    "MXAO": {
        "label": "MXAO (Ambient Occlusion)",
        "category": "Depth",
        "keys": {
            "MXAO_SAMPLE_COUNT": {"label": "Sample-Anzahl", "default": "24"},
            "MXAO_SAMPLE_RADIUS": {"label": "Radius", "default": "2.5"},
            "MXAO_AMOUNT": {"label": "Intensität", "default": "2.0"},
        },
    },
}

# ── ReShade VR presets ────────────────────────────────────────────────────

RESHADE_VR_PRESETS: dict[str, dict] = {
    "VR_Performance": {
        "description": "Minimale Last — nur Schärfe",
        "effects": {
            "CAS": {"enabled": True, "Sharpness": "0.5"},
            "Vibrance": {"enabled": True, "Vibrance": "0.15"},
        },
        "disable": ["MXAO", "MagicBloom", "SMAA", "FXAA", "Clarity", "AdaptiveSharpen"],
    },
    "VR_Balanced": {
        "description": "Gute Optik bei moderater Last",
        "effects": {
            "CAS": {"enabled": True, "Sharpness": "0.4"},
            "Clarity": {"enabled": True, "ClarityStrength": "0.4"},
            "Vibrance": {"enabled": True, "Vibrance": "0.2"},
            "Tonemap": {"enabled": True, "Gamma": "1.0", "Exposure": "0.0",
                        "Saturation": "0.05"},
        },
        "disable": ["MXAO", "MagicBloom", "FXAA"],
    },
    "VR_Quality": {
        "description": "Maximale Bildqualität",
        "effects": {
            "AdaptiveSharpen": {"enabled": True, "curve_height": "0.4"},
            "Clarity": {"enabled": True, "ClarityStrength": "0.5"},
            "Vibrance": {"enabled": True, "Vibrance": "0.25"},
            "Tonemap": {"enabled": True, "Gamma": "1.0", "Exposure": "0.0",
                        "Saturation": "0.1"},
            "Colorfulness": {"enabled": True, "colorfullness": "0.5"},
            "Levels": {"enabled": True, "BlackPoint": "16", "WhitePoint": "235"},
            "SMAA": {"enabled": True},
        },
        "disable": ["MXAO", "MagicBloom"],
    },
    "VR_MSFS_Optimized": {
        "description": "Speziell für MSFS 2024 in VR",
        "effects": {
            "CAS": {"enabled": True, "Sharpness": "0.6"},
            "Clarity": {"enabled": True, "ClarityStrength": "0.35",
                        "ClarityRadius": "3"},
            "Vibrance": {"enabled": True, "Vibrance": "0.2"},
            "Tonemap": {"enabled": True, "Gamma": "1.02", "Exposure": "0.05",
                        "Saturation": "0.05", "Defog": "0.0"},
            "Colorfulness": {"enabled": True, "colorfullness": "0.3"},
        },
        "disable": ["MXAO", "MagicBloom", "SMAA", "FXAA", "AdaptiveSharpen"],
    },
}


# ── ReShade helpers ───────────────────────────────────────────────────────

def _find_reshade_ini(game_dir: str = "") -> Path | None:
    """Find ReShade.ini — either in a specified game dir or by searching."""
    if game_dir:
        gd = Path(game_dir)
        for name in _RESHADE_INI_NAMES:
            candidate = gd / name
            if candidate.exists():
                return candidate
        # Recurse one level
        if gd.is_dir():
            for sub in gd.iterdir():
                if sub.is_dir():
                    for name in _RESHADE_INI_NAMES:
                        candidate = sub / name
                        if candidate.exists():
                            return candidate
        return None

    # Auto-search known game directories
    for gd in _RESHADE_GAME_DIRS:
        if not gd.exists():
            continue
        for name in _RESHADE_INI_NAMES:
            candidate = gd / name
            if candidate.exists():
                return candidate
        # Search subdirectories (one level)
        if gd.is_dir():
            try:
                for sub in gd.iterdir():
                    if sub.is_dir():
                        for name in _RESHADE_INI_NAMES:
                            candidate = sub / name
                            if candidate.exists():
                                return candidate
            except PermissionError:
                continue

    # Last resort: PowerShell search
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             r'Get-ChildItem -Path "C:\","D:\","E:\" -Recurse '
             r'-Include "ReShade.ini","dxgi.ini","d3d11.ini" '
             r'-ErrorAction SilentlyContinue '
             r'| Select-Object -First 5 -ExpandProperty FullName'],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            p = Path(line.strip())
            if p.is_file():
                return p
    except Exception:
        pass

    return None


def _read_reshade_ini(ini_path: Path) -> _configparser.ConfigParser:
    """Read a ReShade.ini preserving case and comments."""
    config = _configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=(";",),
        inline_comment_prefixes=(";",),
    )
    config.optionxform = str  # preserve case
    config.read(str(ini_path), encoding="utf-8")
    return config


def _write_reshade_ini(config: _configparser.ConfigParser, ini_path: Path) -> None:
    """Write ReShade.ini back to disk."""
    with open(ini_path, "w", encoding="utf-8") as f:
        config.write(f)


def _find_preset_file(ini_path: Path) -> Path | None:
    """Find the currently active preset file referenced by ReShade.ini."""
    config = _read_reshade_ini(ini_path)
    preset_path = None
    for section in config.sections():
        if config.has_option(section, "PresetPath"):
            preset_path = config.get(section, "PresetPath")
            break
    if not preset_path:
        return None

    # Resolve relative to ReShade.ini directory
    p = Path(preset_path)
    if not p.is_absolute():
        p = ini_path.parent / p
    return p if p.is_file() else None


def _read_preset(preset_path: Path) -> _configparser.ConfigParser:
    """Read a ReShade preset .ini file.

    ReShade preset files often have key=value pairs BEFORE the first [Section]
    header (e.g. ``Techniques=CAS,Vibrance``, ``TechniqueSorting=...``,
    ``PreprocessorDefinitions=``).  Python's configparser raises
    ``MissingSectionHeaderError`` when it encounters those lines, which would
    silently swallow the entire file and return an empty config (or crash).

    Fix: strip all lines that appear before the first ``[Section]`` header and
    parse only the section-based content.  The header-less lines (Techniques=,
    TechniqueSorting=) are managed exclusively via ``_update_reshade_techniques``,
    which uses raw regex operations on the full file text.
    """
    config = _configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=(";",),
        inline_comment_prefixes=(";",),
    )
    config.optionxform = str

    try:
        content = preset_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError):
        return config

    # Collect only lines from the first [Section] header onward
    section_lines: list[str] = []
    in_section = False
    for line in content.splitlines(keepends=True):
        if not in_section and line.strip().startswith("["):
            in_section = True
        if in_section:
            section_lines.append(line)

    if section_lines:
        try:
            config.read_string("".join(section_lines))
        except Exception:
            pass  # Return empty config on any remaining parse error

    return config


def _list_presets(ini_path: Path) -> list[dict]:
    """Find all preset files near the ReShade installation."""
    presets: list[dict] = []
    base = ini_path.parent

    # Check "reshade-presets", "Presets", "presets" folders
    for folder_name in ["reshade-presets", "Presets", "presets", "ReShade", "reshade"]:
        folder = base / folder_name
        if folder.is_dir():
            for f in folder.iterdir():
                if f.suffix.lower() in _RESHADE_PRESET_EXTENSIONS:
                    presets.append({"name": f.stem, "path": str(f)})

    # Also check files directly next to ReShade.ini
    for f in base.iterdir():
        if (f.suffix.lower() in _RESHADE_PRESET_EXTENSIONS
                and f.name.lower() not in {"reshade.ini", "dxgi.ini", "d3d11.ini",
                                            "d3d12.ini", "opengl32.ini"}
                and "preset" in f.name.lower()):
            presets.append({"name": f.stem, "path": str(f)})

    return presets


def _update_reshade_techniques(preset_path: Path,
                               enable: list[str],
                               disable: list[str]) -> None:
    """Add/remove effect names from the Techniques= and TechniqueSorting= lines.

    ReShade only runs effects listed in Techniques= — writing [Effect] sections
    alone does nothing.  This function reads the raw preset file and patches
    both lines so ReShade picks up the change without a restart (it polls the
    preset file for changes).
    """
    try:
        content = preset_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    # Parse existing Techniques list (entries may look like "CAS" or "CAS@CAS.fx")
    tech_match = re.search(r'^Techniques=(.*)$', content, re.MULTILINE)
    if tech_match:
        techs_raw = [t.strip() for t in tech_match.group(1).split(',') if t.strip()]
    else:
        techs_raw = []

    def _base(name: str) -> str:
        """Strip @filename suffix → base effect name."""
        return name.split('@')[0].strip()

    # Index by base name so "CAS@CAS.fx" and "CAS" are treated as the same
    bases_to_full: dict[str, str] = {_base(t): t for t in techs_raw}

    # Disable: remove by base name
    for eff in disable:
        bases_to_full.pop(eff, None)

    # Enable: add if not already present (use bare name — ReShade resolves .fx)
    for eff in enable:
        if eff not in bases_to_full:
            bases_to_full[eff] = eff

    new_techs = list(bases_to_full.values())
    tech_line = f"Techniques={','.join(new_techs)}"
    sort_line = f"TechniqueSorting={','.join(new_techs)}"

    if tech_match:
        content = re.sub(r'^Techniques=.*$', tech_line, content, flags=re.MULTILINE)
    else:
        content = tech_line + '\n' + content

    if re.search(r'^TechniqueSorting=', content, re.MULTILINE):
        content = re.sub(r'^TechniqueSorting=.*$', sort_line, content, flags=re.MULTILINE)
    else:
        content = re.sub(r'^(Techniques=.*)$', r'\1\n' + sort_line, content, flags=re.MULTILINE)

    preset_path.write_text(content, encoding="utf-8")


# ── ReShade MCP Tools ─────────────────────────────────────────────────────

@mcp.tool()
def analyze_reshade(
    game_dir: str = "",
) -> dict:
    """Analyze current ReShade installation and settings.

    Finds ReShade.ini, reads the active preset, lists all effects and their
    settings, and shows available presets.

    Present the results as a markdown table with columns:
    Effekt | Status | Einstellung | Wert | Beschreibung

    Args:
        game_dir: Game directory containing ReShade.ini (auto-detected if empty).
    """
    ini = _find_reshade_ini(game_dir)
    if ini is None:
        return {
            "error": (
                "ReShade.ini nicht gefunden. Ist ReShade installiert? "
                "Gib den Spielordner über game_dir an "
                "(z.B. den MSFS-Installationsordner)."
            ),
            "searched": [str(p) for p in _RESHADE_GAME_DIRS[:6]],
        }

    config = _read_reshade_ini(ini)

    # Active preset
    preset_path = _find_preset_file(ini)
    preset_data = _read_preset(preset_path) if preset_path else None

    # Read the Techniques= list from the raw preset file to determine what's active
    # (ReShade only runs effects in this list — section keys like Enabled=1 are ignored)
    active_techniques: set[str] = set()
    if preset_path and preset_path.exists():
        try:
            raw = preset_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'^Techniques=(.*)$', raw, re.MULTILINE)
            if m:
                for t in m.group(1).split(','):
                    base = t.strip().split('@')[0].strip()
                    if base:
                        active_techniques.add(base)
        except Exception:
            pass

    # Gather effect status and settings from the preset sections
    effects_table: list[dict] = []
    if preset_data:
        for section in preset_data.sections():
            defn = RESHADE_EFFECTS.get(section, {})
            label = defn.get("label", section)
            category = defn.get("category", "")

            # Enabled = present in Techniques= list (authoritative) or, as fallback,
            # via explicit Enabled= key inside the section
            if active_techniques:
                enabled = section in active_techniques
            else:
                # Fallback for old preset files that don't have a Techniques= line
                enabled = True
                for opt in preset_data.options(section):
                    if opt.lower() in ("enabled", "active"):
                        enabled = preset_data.getboolean(section, opt, fallback=True)
                        break

            params: dict[str, str] = {}
            for opt in preset_data.options(section):
                if opt.lower() not in ("enabled", "active"):
                    params[opt] = preset_data.get(section, opt)

            effects_table.append({
                "effect": label,
                "category": category,
                "enabled": enabled,
                "settings": params,
            })

    # Available presets
    available_presets = _list_presets(ini)

    # ReShade.ini sections
    ini_sections = {}
    for section in config.sections():
        ini_sections[section] = dict(config.items(section))

    return {
        "reshade_ini": str(ini),
        "active_preset": str(preset_path) if preset_path else None,
        "effects": effects_table,
        "available_presets": available_presets,
        "ini_sections": ini_sections,
        "game_dir": str(ini.parent),
        "display_instructions": (
            "Show effects as a markdown table with columns: "
            "Effekt | Kategorie | Status | Einstellungen. "
            "Use green checkmarks for enabled, red X for disabled. "
            "Answer in German."
        ),
    }


@mcp.tool()
def set_reshade_effect(
    effect: str,
    enabled: bool | None = None,
    settings: dict | None = None,
    game_dir: str = "",
) -> dict:
    """Enable/disable a ReShade effect or change its parameters.

    Use this when the user says things like "Schärfe erhöhen",
    "Bloom ausschalten", "Vibrance auf 0.3", etc.

    Args:
        effect: Effect name (e.g. "CAS", "Clarity", "Vibrance", "Tonemap",
                "SMAA", "FXAA", "MagicBloom", "MXAO", "AdaptiveSharpen",
                "LumaSharpen", "Colorfulness", "Levels", "LiftGammaGain").
        enabled: True to enable, False to disable, None to keep current.
        settings: Dict of parameter→value (e.g. {"Sharpness": "0.5"}).
        game_dir: Game directory with ReShade.ini (auto-detected if empty).
    """
    # Resolve German aliases
    _EFFECT_ALIASES = {
        "schärfe": "CAS",
        "sharpness": "CAS",
        "sharpen": "CAS",
        "cas": "CAS",
        "adaptive sharpen": "AdaptiveSharpen",
        "klarheit": "Clarity",
        "clarity": "Clarity",
        # Vibrance = subtile Sättigungsanhebung für bereits-gesättigte Farben
        "vibrance": "Vibrance",
        # Colorfulness = globale Farbintensität (direkterer Sättigungseffekt)
        "farbe": "Colorfulness",
        "sättigung": "Colorfulness",   # Colorfulness ist der präzisere Sättigungs-Effekt
        "saturation": "Colorfulness",  # Tonemap.Saturation wäre nur ein Parameter, nicht ein Effekt
        "farbintensität": "Colorfulness",
        "tonemap": "Tonemap",
        "tonemapping": "Tonemap",
        "gamma": "Tonemap",
        "belichtung": "Tonemap",
        "exposure": "Tonemap",
        "bloom": "MagicBloom",
        "glow": "MagicBloom",
        "blüte": "MagicBloom",
        "aa": "SMAA",
        "anti-aliasing": "SMAA",
        "antialiasing": "SMAA",
        "smaa": "SMAA",
        "fxaa": "FXAA",
        "ao": "MXAO",
        "ambient occlusion": "MXAO",
        "mxao": "MXAO",
        "farbintensität": "Colorfulness",
        "colorfulness": "Colorfulness",
        "levels": "Levels",
        "luma": "LumaSharpen",
        "lumasharpen": "LumaSharpen",
        "lift gamma gain": "LiftGammaGain",
        "liftgammagain": "LiftGammaGain",
    }
    resolved = _EFFECT_ALIASES.get(effect.lower().strip(), effect.strip())

    ini = _find_reshade_ini(game_dir)
    if ini is None:
        return {"error": "ReShade.ini nicht gefunden. Gib game_dir an."}

    preset_path = _find_preset_file(ini)
    if not preset_path:
        return {"error": "Kein aktives ReShade-Preset gefunden."}

    # Backup
    backup = preset_path.parent / f"{preset_path.stem}_backup{preset_path.suffix}"
    if not backup.exists():
        shutil.copy2(preset_path, backup)

    config = _read_preset(preset_path)

    if not config.has_section(resolved):
        config.add_section(resolved)

    applied: dict = {}

    # 1. Write per-parameter values first (configparser overwrites the whole file,
    #    so it MUST run before _update_reshade_techniques patches the Techniques= line)
    if settings:
        for key, val in settings.items():
            config.set(resolved, key, str(val))
            applied[key] = val
        with open(preset_path, "w", encoding="utf-8") as f:
            config.write(f)

    # 2. Update Techniques= list AFTER the configparser write (otherwise it gets overwritten)
    if enabled is not None:
        if enabled:
            _update_reshade_techniques(preset_path, enable=[resolved], disable=[])
        else:
            _update_reshade_techniques(preset_path, enable=[], disable=[resolved])
        applied["enabled"] = enabled

    defn = RESHADE_EFFECTS.get(resolved, {})

    # ReShade detects preset changes by file modification time.
    try:
        preset_path.touch()
    except Exception:
        pass

    return {
        "status": "ok",
        "effect": defn.get("label", resolved),
        "category": defn.get("category", ""),
        "applied": applied,
        "preset_file": str(preset_path),
        "hinweis": (
            "Preset-Datei aktualisiert (Techniques-Liste + Parameter). "
            "ReShade übernimmt Änderungen normalerweise live. "
            "Falls nicht sichtbar: Home-Taste → ReShade-Overlay → Preset neu laden."
        ),
    }


@mcp.tool()
def apply_reshade_preset(
    preset: Literal[
        "VR_Performance", "VR_Balanced", "VR_Quality", "VR_MSFS_Optimized", "custom"
    ] = "VR_Balanced",
    custom_preset_path: str = "",
    game_dir: str = "",
) -> dict:
    """Apply a VR-optimized ReShade preset.

    Presets:
    - VR_Performance: Nur Schärfe — minimale FPS-Last
    - VR_Balanced: Schärfe + Clarity + Vibrance — gute Optik
    - VR_Quality: Volle Bildverbesserung — maximale Qualität
    - VR_MSFS_Optimized: Speziell für MSFS 2024 in VR
    - custom: Eigene Preset-Datei laden (custom_preset_path nötig)

    Args:
        preset: Which preset to apply.
        custom_preset_path: Path to a custom .ini preset (only for preset="custom").
        game_dir: Game directory with ReShade.ini (auto-detected if empty).
    """
    ini = _find_reshade_ini(game_dir)
    if ini is None:
        return {"error": "ReShade.ini nicht gefunden. Gib game_dir an."}

    # Custom preset: just switch the PresetPath
    if preset == "custom":
        if not custom_preset_path:
            return {"error": "custom_preset_path muss angegeben werden."}
        cp = Path(custom_preset_path)
        if not cp.is_file():
            return {"error": f"Preset-Datei nicht gefunden: {custom_preset_path}"}
        config = _read_reshade_ini(ini)
        for section in config.sections():
            if config.has_option(section, "PresetPath"):
                config.set(section, "PresetPath", str(cp))
                break
        else:
            if not config.has_section("GENERAL"):
                config.add_section("GENERAL")
            config.set("GENERAL", "PresetPath", str(cp))
        _write_reshade_ini(config, ini)
        return {
            "status": "ok",
            "preset": cp.stem,
            "preset_file": str(cp),
        }

    # Built-in VR presets
    preset_def = RESHADE_VR_PRESETS.get(preset)
    if not preset_def:
        return {"error": f"Unbekanntes Preset: {preset}"}

    preset_path = _find_preset_file(ini)
    if not preset_path:
        # Create new preset file
        preset_path = ini.parent / "reshade-presets" / f"{preset}.ini"
        preset_path.parent.mkdir(exist_ok=True)

    # Backup
    if preset_path.exists():
        backup = preset_path.parent / f"{preset_path.stem}_before_{preset}{preset_path.suffix}"
        if not backup.exists():
            shutil.copy2(preset_path, backup)

    config = _read_preset(preset_path) if preset_path.exists() else _configparser.ConfigParser(
        interpolation=None)
    config.optionxform = str

    # Write per-effect parameters into their sections
    applied: dict = {}
    for eff_name, eff_settings in preset_def.get("effects", {}).items():
        if not config.has_section(eff_name):
            config.add_section(eff_name)
        for key, val in eff_settings.items():
            if key != "enabled":  # "enabled" handled via Techniques list below
                config.set(eff_name, key, str(val))
        applied[eff_name] = eff_settings

    with open(preset_path, "w", encoding="utf-8") as f:
        config.write(f)

    # CRITICAL: Update the Techniques= list — this is what actually activates effects
    enable_list = [e for e, s in preset_def.get("effects", {}).items() if s.get("enabled", True)]
    disable_list = preset_def.get("disable", [])
    _update_reshade_techniques(preset_path, enable=enable_list, disable=disable_list)

    # Point ReShade.ini to this preset
    ini_config = _read_reshade_ini(ini)
    for section in ini_config.sections():
        if ini_config.has_option(section, "PresetPath"):
            ini_config.set(section, "PresetPath", str(preset_path))
            break
    _write_reshade_ini(ini_config, ini)

    # Touch to help ReShade detect the change
    try:
        preset_path.touch()
    except Exception:
        pass

    return {
        "status": "ok",
        "preset": preset,
        "description": preset_def["description"],
        "effects_enabled": enable_list,
        "effects_disabled": disable_list,
        "preset_file": str(preset_path),
        "hinweis": (
            f"Preset '{preset}' angewendet (Techniques-Liste aktualisiert). ReShade lädt Änderungen "
            "normalerweise live. Falls nicht sichtbar: Home-Taste → "
            "ReShade-Overlay → Preset wechseln/neu laden."
        ),
    }


@mcp.tool()
def list_reshade_presets(
    game_dir: str = "",
) -> dict:
    """List all available ReShade presets for the game.

    Shows the active preset and all preset files found near the installation.

    Args:
        game_dir: Game directory with ReShade.ini (auto-detected if empty).
    """
    ini = _find_reshade_ini(game_dir)
    if ini is None:
        return {"error": "ReShade.ini nicht gefunden. Gib game_dir an."}

    active = _find_preset_file(ini)
    available = _list_presets(ini)

    # Add built-in VR presets
    builtin = [
        {
            "name": name,
            "type": "builtin_vr",
            "description": defn["description"],
        }
        for name, defn in RESHADE_VR_PRESETS.items()
    ]

    return {
        "reshade_ini": str(ini),
        "active_preset": str(active) if active else None,
        "file_presets": available,
        "builtin_vr_presets": builtin,
    }


@mcp.tool()
def uninstall_reshade(
    game_dir: str = "",
    backup: bool = True,
) -> dict:
    """Completely uninstall/remove ReShade from a game (MSFS 2024).

    Use this when the user says:
    - "ReShade deinstallieren" / "ReShade entfernen"
    - "ReShade komplett löschen"
    - "ReShade ausschalten"

    Removes all ReShade files:
    - DLL files (dxgi.dll, d3d11.dll, d3d12.dll, opengl32.dll, ReShade64.dll, etc.)
    - INI files (ReShade.ini, dxgi.ini, etc.)
    - Shader folders (reshade-shaders)
    - Preset files (optional)
    - Log files (ReShade.log)

    Creates a backup before deleting (default).

    Args:
        game_dir: Game directory containing ReShade (auto-detected if empty).
        backup: Create a backup folder before deleting (default True).
    """
    ini = _find_reshade_ini(game_dir)
    if ini is None:
        return {
            "status": "not_found",
            "message": (
                "ReShade nicht gefunden. Entweder bereits deinstalliert "
                "oder game_dir angeben."
            ),
        }

    base = ini.parent

    # All ReShade-related files and folders to remove
    reshade_dlls = [
        "dxgi.dll", "d3d11.dll", "d3d12.dll", "opengl32.dll",
        "ReShade64.dll", "ReShade32.dll", "ReShade.dll",
    ]
    reshade_inis = [
        "ReShade.ini", "reshade.ini", "Reshade.ini",
        "dxgi.ini", "d3d11.ini", "d3d12.ini", "opengl32.ini",
        "ReShadePreset.ini",
    ]
    reshade_logs = ["ReShade.log", "reshade.log", "dxgi.log"]
    reshade_dirs = [
        "reshade-shaders", "reshade-presets", "ReShade",
        "reshade", "Presets", "Shaders",
    ]

    # Collect all files/dirs that exist
    to_delete_files: list[Path] = []
    to_delete_dirs: list[Path] = []

    for name in reshade_dlls + reshade_inis + reshade_logs:
        p = base / name
        if p.exists():
            to_delete_files.append(p)

    for name in reshade_dirs:
        p = base / name
        if p.is_dir():
            to_delete_dirs.append(p)

    # Also find preset .ini files (not game configs)
    for f in base.iterdir():
        if (f.suffix.lower() == ".ini"
                and f.name.lower() not in {"usercfg.opt", "flight.ini", "config.ini"}
                and any(kw in f.name.lower() for kw in ["preset", "reshade", "shader"])):
            to_delete_files.append(f)

    if not to_delete_files and not to_delete_dirs:
        return {
            "status": "clean",
            "message": "Keine ReShade-Dateien gefunden. Bereits sauber.",
            "game_dir": str(base),
        }

    # Backup
    backup_path = None
    if backup:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = base / f"reshade_backup_{ts}"
        backup_dir.mkdir(exist_ok=True)
        for f in to_delete_files:
            try:
                shutil.copy2(f, backup_dir / f.name)
            except Exception:
                pass
        for d in to_delete_dirs:
            try:
                shutil.copytree(d, backup_dir / d.name, dirs_exist_ok=True)
            except Exception:
                pass
        backup_path = str(backup_dir)

    # Delete
    deleted_files: list[str] = []
    deleted_dirs: list[str] = []
    errors: list[str] = []

    for f in to_delete_files:
        try:
            f.unlink()
            deleted_files.append(f.name)
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")

    for d in to_delete_dirs:
        try:
            shutil.rmtree(d)
            deleted_dirs.append(d.name)
        except Exception as exc:
            errors.append(f"{d.name}/: {exc}")

    return {
        "status": "ok" if not errors else "partial",
        "game_dir": str(base),
        "gelöschte_dateien": deleted_files,
        "gelöschte_ordner": deleted_dirs,
        "fehler": errors if errors else None,
        "backup": backup_path,
        "message": (
            f"ReShade deinstalliert: {len(deleted_files)} Dateien, "
            f"{len(deleted_dirs)} Ordner gelöscht."
            + (f" Backup unter: {backup_path}" if backup_path else "")
        ),
    }


# ===========================================================================
# OpenXR Toolkit Integration — Registry-based VR graphics settings
# ===========================================================================

# OpenXR Toolkit stores ALL settings as DWORD values in the Windows Registry:
#   HKCU\SOFTWARE\OpenXR_Toolkit\<game.exe>
# Changes take effect on the next VR session (no restart of the game needed,
# but the OpenXR API layer re-reads on session start).

_OPENXR_REG_BASE = r"HKCU\SOFTWARE\OpenXR_Toolkit"
_OPENXR_MSFS2024_EXE = "FlightSimulator2024.exe"

# All known OpenXR Toolkit setting keys with metadata
OPENXR_SETTING_DEFS: dict[str, dict] = {
    # ── Upscaling & Schärfe ──
    "scaling_type": {
        "label": "Upscaler",
        "values": {"0": "Off", "1": "NIS", "2": "FSR", "3": "CAS"},
        "category": "upscaling",
    },
    "scaling": {
        "label": "Skalierung (%)",
        "category": "upscaling",
    },
    "anamorphic": {
        "label": "Anamorphic",
        "category": "upscaling",
    },
    "sharpness": {
        "label": "Schärfe",
        "category": "upscaling",
    },
    "mipmap_bias": {
        "label": "MipMap Bias",
        "category": "upscaling",
    },
    # ── Post-Processing / Farbe ──
    # Registry-Skalierung für Prozentwerte: gespeicherter Wert = Prozent × 10
    #   → 1000 = 100 % = Neutralwert (kein Effekt auf das Bild)
    #   →  800 =  80 % (gedämpft), 1500 = 150 % (verstärkt)
    # Additive Werte (vibrance, shadows): 0 = kein Effekt, positiv = Verstärkung
    # ACHTUNG: post_saturation = 0 → Graustufen (schwarz-weiß)!
    "post_process": {
        "label": "Post-Processing",
        "values": {"0": "Off", "1": "On"},
        "category": "color",
    },
    "post_brightness": {
        "label": "Helligkeit",
        "category": "color",
        "unit": "percent",          # Eingabe 0–200 → ×10 gespeichert
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 120 = heller, 80 = dunkler",
    },
    "post_contrast": {
        "label": "Kontrast",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 120 = mehr Kontrast, 80 = flacher",
    },
    "post_exposure": {
        "label": "Belichtung",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 130 = überbelichtet, 70 = unterbelichtet",
    },
    "post_saturation": {
        "label": "Sättigung",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 0 = GRAUSTUFEN (schwarz-weiß!), 150 = sattere Farben",
    },
    "post_vibrance": {
        "label": "Vibrance",
        "category": "color",
        "unit": "additive",         # 0 = kein Effekt, positiv = mehr Vibrance
        "min": 0, "max": 1000, "default": 0,
        "hint": "0 = kein Effekt, 200 = moderate Vibrance, 500 = stark",
    },
    "post_gain_r": {
        "label": "Farbkanal Rot",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 0 = kein Rot (Bild grün-blau), 150 = mehr Rot",
    },
    "post_gain_g": {
        "label": "Farbkanal Grün",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 0 = kein Grün, 150 = mehr Grün",
    },
    "post_gain_b": {
        "label": "Farbkanal Blau",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 0 = kein Blau, 150 = mehr Blau",
    },
    "post_highlights": {
        "label": "Lichter",
        "category": "color",
        "unit": "percent",
        "min": 0, "max": 2000, "default": 1000,
        "hint": "100 = normal, 80 = Lichter absenken (HDR-Effekt), 120 = heller",
    },
    "post_shadows": {
        "label": "Schatten",
        "category": "color",
        "unit": "additive",
        "min": 0, "max": 1000, "default": 0,
        "hint": "0 = kein Effekt, 200 = Schatten aufhellen, 500 = stark",
    },
    "post_sunglasses": {
        "label": "Sonnenbrille",
        "values": {"0": "Off", "1": "Light", "2": "Dark", "3": "Night"},
        "category": "color",
    },
    # ── Variable Rate Shading (Foveated Rendering) ──
    "vrs": {
        "label": "Foveated Rendering",
        "values": {"0": "Off", "1": "Preset", "2": "Custom"},
        "category": "vrs",
    },
    "vrs_inner": {
        "label": "VRS Innen",
        "values": {"0": "1x", "1": "1/2", "2": "1/4", "3": "1/8", "4": "1/16"},
        "category": "vrs",
    },
    "vrs_middle": {
        "label": "VRS Mitte",
        "values": {"0": "1x", "1": "1/2", "2": "1/4", "3": "1/8", "4": "1/16"},
        "category": "vrs",
    },
    "vrs_outer": {
        "label": "VRS Außen",
        "values": {"0": "1x", "1": "1/2", "2": "1/4", "3": "1/8", "4": "1/16"},
        "category": "vrs",
    },
    "vrs_inner_radius": {"label": "VRS Innen-Radius", "category": "vrs"},
    "vrs_outer_radius": {"label": "VRS Außen-Radius", "category": "vrs"},
    "vrs_x_offset": {"label": "VRS X-Offset", "category": "vrs"},
    "vrs_y_offset": {"label": "VRS Y-Offset", "category": "vrs"},
    "vrs_cull_mask": {
        "label": "VRS HAM Culling",
        "values": {"0": "Off", "1": "On"},
        "category": "vrs",
    },
    # ── World / FOV ──
    "world_scale": {"label": "Weltgröße (IPD)", "category": "world"},
    "fov": {"label": "FOV", "category": "world"},
    "fov_up": {"label": "FOV Oben", "category": "world"},
    "fov_down": {"label": "FOV Unten", "category": "world"},
    "zoom": {"label": "Zoom", "category": "world"},
    # ── Performance ──
    "turbo": {
        "label": "Turbo Mode",
        "values": {"0": "Off", "1": "On"},
        "category": "performance",
    },
    "motion_reprojection": {
        "label": "Motion Reprojection",
        "values": {"0": "Default", "1": "Off", "2": "On"},
        "category": "performance",
    },
    "motion_reprojection_rate": {
        "label": "Reprojection Rate",
        "values": {"1": "Off", "2": "45 Hz", "3": "30 Hz", "4": "22 Hz"},
        "category": "performance",
    },
    "frame_throttle": {"label": "Frame Throttle", "category": "performance"},
    "target_rate": {"label": "Ziel-FPS", "category": "performance"},
    "override_resolution": {
        "label": "Auflösung überschreiben",
        "values": {"0": "Off", "1": "On"},
        "category": "performance",
    },
    "resolution_height": {"label": "Auflösung Höhe", "category": "performance"},
}

# German aliases → registry key name
_OPENXR_ALIASES: dict[str, str] = {
    # Upscaling
    "upscaler": "scaling_type",
    "upscaling": "scaling_type",
    "nis": "scaling_type",
    "fsr": "scaling_type",
    "cas": "scaling_type",
    "skalierung": "scaling",
    "scale": "scaling",
    "schärfe": "sharpness",
    "sharpness": "sharpness",
    "sharpen": "sharpness",
    "mipmap": "mipmap_bias",
    # Post-Processing
    "post processing": "post_process",
    "postprocessing": "post_process",
    "nachbearbeitung": "post_process",
    "helligkeit": "post_brightness",
    "brightness": "post_brightness",
    "kontrast": "post_contrast",
    "contrast": "post_contrast",
    "belichtung": "post_exposure",
    "exposure": "post_exposure",
    "sättigung": "post_saturation",
    "saturation": "post_saturation",
    "vibrance": "post_vibrance",
    "farbintensität": "post_vibrance",
    "rot": "post_gain_r",
    "grün": "post_gain_g",
    "blau": "post_gain_b",
    "lichter": "post_highlights",
    "highlights": "post_highlights",
    "schatten": "post_shadows",
    "shadows": "post_shadows",
    "sonnenbrille": "post_sunglasses",
    "sunglasses": "post_sunglasses",
    # VRS / Foveated
    "foveated": "vrs",
    "foveated rendering": "vrs",
    "variable rate shading": "vrs",
    "vrs innen": "vrs_inner",
    "vrs mitte": "vrs_middle",
    "vrs außen": "vrs_outer",
    # World
    "weltgröße": "world_scale",
    "world scale": "world_scale",
    "ipd": "world_scale",
    "sichtfeld": "fov",
    "field of view": "fov",
    "zoom": "zoom",
    # Performance
    "turbo": "turbo",
    "reprojection": "motion_reprojection",
    "motion reprojection": "motion_reprojection",
    "reprojection rate": "motion_reprojection_rate",
    "frame throttle": "frame_throttle",
    "fps limit": "target_rate",
    "ziel fps": "target_rate",
    "target fps": "target_rate",
    "auflösung": "override_resolution",
}

# Named value aliases for enum-type settings
_OPENXR_VALUE_ALIASES: dict[str, dict[str, str]] = {
    "scaling_type": {
        "off": "0", "aus": "0", "kein": "0",
        "nis": "1", "nvidia": "1",
        "fsr": "2", "amd": "2", "fidelityfx": "2",
        "cas": "3",
    },
    "post_sunglasses": {
        "off": "0", "aus": "0",
        "light": "1", "hell": "1", "leicht": "1",
        "dark": "2", "dunkel": "2",
        "night": "3", "nacht": "3",
    },
    "vrs": {
        "off": "0", "aus": "0",
        "preset": "1", "voreinstellung": "1",
        "custom": "2", "benutzerdefiniert": "2",
    },
    "motion_reprojection": {
        "default": "0", "standard": "0",
        "off": "1", "aus": "1",
        "on": "2", "an": "2", "ein": "2",
    },
    "motion_reprojection_rate": {
        "off": "1", "aus": "1",
        "45": "2", "45hz": "2",
        "30": "3", "30hz": "3",
        "22": "4", "22hz": "4",
    },
    "_on_off": {
        "off": "0", "aus": "0", "nein": "0",
        "on": "1", "an": "1", "ein": "1", "ja": "1",
    },
}

_OPENXR_ON_OFF_KEYS = {
    "post_process", "turbo", "override_resolution", "vrs_cull_mask",
}


def _openxr_reg_path(game_exe: str = "") -> str:
    """Return the full registry path for a game's OpenXR Toolkit settings."""
    exe = game_exe or _OPENXR_MSFS2024_EXE
    return f"{_OPENXR_REG_BASE}\\{exe}"


def _read_openxr_settings(game_exe: str = "") -> dict[str, int]:
    """Read all OpenXR Toolkit settings from the registry for a game."""
    reg_path = _openxr_reg_path(game_exe)
    result: dict[str, int] = {}
    try:
        r = _reg_query(reg_path)
        if r.returncode != 0:
            return result
        for line in r.stdout.splitlines():
            m = re.match(r"\s+(\S+)\s+REG_DWORD\s+0x([0-9a-fA-F]+)", line)
            if m:
                key = m.group(1)
                val = int(m.group(2), 16)
                result[key] = val
    except Exception:
        pass
    return result


def _write_openxr_setting(key: str, value: int, game_exe: str = "") -> bool:
    """Write a single DWORD value to the OpenXR Toolkit registry."""
    reg_path = _openxr_reg_path(game_exe)
    try:
        r = _reg_add(reg_path, key, "REG_DWORD", str(value))
        return r.returncode == 0
    except Exception:
        return False


# ── OpenXR Runtime Management ─────────────────────────────────────────────

# Registry path where the active OpenXR runtime is stored
_OPENXR_RUNTIME_REG = r"HKLM\SOFTWARE\Khronos\OpenXR\1"
_OPENXR_RUNTIME_VALUE = "ActiveRuntime"

# Known OpenXR runtime JSON manifest paths
_OPENXR_RUNTIMES: dict[str, list[str]] = {
    "pimax": [
        r"C:\Program Files\Pimax\Runtime\PiOpenXR_64.json",
        r"C:\Program Files (x86)\Pimax\Runtime\PiOpenXR_64.json",
        r"D:\Program Files\Pimax\Runtime\PiOpenXR_64.json",
    ],
    "steamvr": [
        r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\steamxr_win64.json",
        r"D:\Steam\steamapps\common\SteamVR\steamxr_win64.json",
        r"D:\SteamLibrary\steamapps\common\SteamVR\steamxr_win64.json",
        r"E:\SteamLibrary\steamapps\common\SteamVR\steamxr_win64.json",
    ],
    "oculus": [
        r"C:\Program Files\Oculus\Support\oculus-runtime\oculus_openxr_64.json",
    ],
    "wmr": [
        r"C:\WINDOWS\system32\MixedRealityRuntime.json",
    ],
}

# German aliases for runtime names
_RUNTIME_ALIASES: dict[str, str] = {
    "pimax": "pimax",
    "pimaxplay": "pimax",
    "pimax play": "pimax",
    "steamvr": "steamvr",
    "steam vr": "steamvr",
    "steam": "steamvr",
    "oculus": "oculus",
    "meta": "oculus",
    "quest": "oculus",
    "meta quest": "oculus",
    "wmr": "wmr",
    "windows mixed reality": "wmr",
    "mixed reality": "wmr",
}


def _get_active_runtime() -> dict:
    """Read the currently active OpenXR runtime from the registry."""
    try:
        r = _reg(["query", _OPENXR_RUNTIME_REG, "/v", _OPENXR_RUNTIME_VALUE])
        for line in r.stdout.splitlines():
            m = re.search(r"ActiveRuntime\s+REG_SZ\s+(.+)", line)
            if m:
                path = m.group(1).strip()
                # Identify which runtime this is
                path_lower = path.lower()
                name = "unbekannt"
                for rt_name, candidates in _OPENXR_RUNTIMES.items():
                    for c in candidates:
                        if c.lower() in path_lower or Path(c).name.lower() in path_lower:
                            name = rt_name
                            break
                    if name != "unbekannt":
                        break
                return {"path": path, "name": name}
    except Exception:
        pass
    return {"path": None, "name": None}


def _find_runtime_path(runtime: str) -> str | None:
    """Find the JSON manifest path for a given runtime name."""
    rt = _RUNTIME_ALIASES.get(runtime.lower().strip(), runtime.lower().strip())
    candidates = _OPENXR_RUNTIMES.get(rt, [])
    for c in candidates:
        if Path(c).exists():
            return c

    # Search registry for installed runtimes
    try:
        r = _reg(["query", r"HKLM\SOFTWARE\Khronos\OpenXR\1\AvailableRuntimes"])
        for line in r.stdout.splitlines():
            if rt in line.lower():
                m = re.match(r"\s+(.+\.json)\s+REG_DWORD", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass

    # Search filesystem as last resort
    if rt == "pimax":
        for root in [r"C:\Program Files", r"C:\Program Files (x86)", r"D:\Program Files"]:
            rp = Path(root)
            if rp.exists():
                for hit in rp.glob("**/PiOpenXR_64.json"):
                    return str(hit)

    return None


@mcp.tool()
def set_openxr_runtime(
    runtime: str = "pimax",
) -> dict:
    """Set the active OpenXR runtime (e.g. to Pimax, SteamVR, Oculus).

    Use this when the user says things like:
    - "OpenXR Runtime auf Pimax stellen"
    - "OpenXR auf Pimax umstellen"
    - "Pimax als OpenXR Runtime"
    - "SteamVR Runtime aktivieren"
    - "OpenXR Runtime wurde verstellt"

    Writes to: HKLM\\SOFTWARE\\Khronos\\OpenXR\\1\\ActiveRuntime
    (requires Admin-Rechte — wird automatisch elevated).

    Args:
        runtime: Runtime name — "pimax", "steamvr", "oculus", "wmr",
                 or full path to the runtime JSON manifest.
    """
    # Check current runtime
    current = _get_active_runtime()

    # If user gave a full path, use it directly
    if runtime.lower().endswith(".json") or "\\" in runtime or "/" in runtime:
        target_path = runtime
        target_name = "custom"
        if not Path(target_path).exists():
            return {
                "error": f"Runtime-Datei nicht gefunden: {target_path}",
            }
    else:
        # Resolve name → path
        rt_key = _RUNTIME_ALIASES.get(runtime.lower().strip())
        if rt_key is None:
            return {
                "error": f"Unbekannte Runtime: '{runtime}'",
                "verfügbar": ["pimax", "steamvr", "oculus", "wmr"],
            }
        target_name = rt_key
        target_path = _find_runtime_path(rt_key)
        if target_path is None:
            return {
                "error": (
                    f"Runtime '{rt_key}' nicht gefunden. "
                    f"Gesucht: {_OPENXR_RUNTIMES.get(rt_key, [])}"
                ),
                "tipp": "Gib den vollständigen Pfad zur .json Datei an.",
            }

    # Already set?
    if current["path"] and Path(current["path"]).resolve() == Path(target_path).resolve():
        return {
            "status": "already_set",
            "runtime": target_name,
            "path": target_path,
            "message": f"OpenXR Runtime ist bereits auf {target_name} eingestellt.",
        }

    # Set the new runtime (HKLM — needs admin)
    r = _reg_add(_OPENXR_RUNTIME_REG, _OPENXR_RUNTIME_VALUE, "REG_SZ", target_path)

    # Verify
    new_current = _get_active_runtime()
    verified = (new_current["path"] and
                Path(new_current["path"]).resolve() == Path(target_path).resolve())

    if not verified:
        return {
            "error": (
                f"Konnte Runtime nicht umstellen. "
                f"Registry-Schreibzugriff auf HKLM benötigt Admin-Rechte."
            ),
            "vorher": current,
            "versucht": target_path,
            "tipp": (
                "Starte den MCP-Server als Administrator, oder führe manuell aus:\n"
                f'reg add "HKLM\\SOFTWARE\\Khronos\\OpenXR\\1" '
                f'/v ActiveRuntime /t REG_SZ /d "{target_path}" /f'
            ),
        }

    return {
        "status": "ok",
        "vorher": {
            "runtime": current["name"],
            "path": current["path"],
        },
        "nachher": {
            "runtime": target_name,
            "path": target_path,
        },
        "verifiziert": verified,
        "message": (
            f"OpenXR Runtime umgestellt: {current['name']} → {target_name}. "
            f"Wird beim nächsten VR-Start aktiv."
        ),
    }


@mcp.tool()
def get_openxr_runtime() -> dict:
    """Show the currently active OpenXR runtime and all available runtimes.

    Use this when the user asks:
    - "Welche OpenXR Runtime ist aktiv?"
    - "Zeige OpenXR Runtime"
    - "Ist Pimax als Runtime eingestellt?"

    Reads from: HKLM\\SOFTWARE\\Khronos\\OpenXR\\1
    """
    current = _get_active_runtime()

    # Find all available runtimes
    available: list[dict] = []
    for rt_name, candidates in _OPENXR_RUNTIMES.items():
        for c in candidates:
            if Path(c).exists():
                is_active = (current["path"] and
                             Path(current["path"]).resolve() == Path(c).resolve())
                available.append({
                    "runtime": rt_name,
                    "path": c,
                    "aktiv": is_active,
                })
                break  # Only first existing path per runtime

    # Also check AvailableRuntimes registry
    try:
        r = _reg(["query", r"HKLM\SOFTWARE\Khronos\OpenXR\1\AvailableRuntimes"])
        for line in r.stdout.splitlines():
            m = re.match(r"\s+(.+\.json)\s+REG_DWORD\s+0x0", line)
            if m:
                rt_path = m.group(1).strip()
                # Skip if already in our list
                if not any(a["path"] == rt_path for a in available):
                    is_active = (current["path"] and
                                 Path(current["path"]).resolve() == Path(rt_path).resolve())
                    available.append({
                        "runtime": Path(rt_path).stem,
                        "path": rt_path,
                        "aktiv": is_active,
                    })
    except Exception:
        pass

    return {
        "status": "ok",
        "aktive_runtime": current,
        "verfügbare_runtimes": available,
        "registry_path": _OPENXR_RUNTIME_REG,
        "hinweis": (
            "Zum Umstellen: set_openxr_runtime(runtime='pimax') verwenden. "
            "Oder sage einfach 'OpenXR auf Pimax stellen'."
        ),
    }


# ── OpenXR Toolkit MCP Tools ─────────────────────────────────────────────

@mcp.tool()
def analyze_openxr(
    game_exe: str = "",
) -> dict:
    """Analyze current OpenXR Toolkit VR graphics settings (Registry).

    WICHTIG: Dies ist das OpenXR Toolkit — NICHT die MSFS-Grafikeinstellungen!
    - OpenXR Toolkit = Upscaling (NIS/FSR/CAS), Schärfe, Foveated Rendering,
      Post-Processing (Helligkeit/Kontrast/Sättigung), Motion Reprojection
    - MSFS Grafik = DLSS, Wolken, Terrain LoD, Schatten (→ analyze_msfs_graphics)

    Reads all OpenXR Toolkit settings from the Windows Registry at:
      HKCU\\SOFTWARE\\OpenXR_Toolkit\\FlightSimulator2024.exe

    Use this tool when the user mentions:
    - "OpenXR" / "OpenXR Toolkit"
    - "VR Schärfe" / "VR Upscaling" / "NIS" / "FSR"
    - "Foveated Rendering" / "VRS"
    - "VR Helligkeit" / "VR Kontrast" (Post-Processing)
    - "Motion Reprojection"

    Args:
        game_exe: Game executable name (default: FlightSimulator2024.exe).
    """
    settings = _read_openxr_settings(game_exe)
    exe = game_exe or _OPENXR_MSFS2024_EXE

    if not settings:
        return {
            "error": (
                f"Keine OpenXR Toolkit Einstellungen gefunden für '{exe}'. "
                "Ist OpenXR Toolkit installiert und wurde MSFS 2024 schon "
                "einmal in VR gestartet?"
            ),
            "registry_path": _openxr_reg_path(game_exe),
        }

    # Group by category
    categories: dict[str, list[dict]] = {}
    for key, val in sorted(settings.items()):
        defn = OPENXR_SETTING_DEFS.get(key, {})
        label = defn.get("label", key)
        category = defn.get("category", "sonstige")
        value_map = defn.get("values", {})
        unit = defn.get("unit", "")
        setting_default = defn.get("default")

        # Format display value — percent settings show human-readable "%"
        if value_map:
            display = value_map.get(str(val), str(val))
        elif unit == "percent":
            pct = val // 10
            neutral_marker = " ✓ (neutral)" if val == setting_default else (
                " ⚠ (zu niedrig!)" if val < 500 else ""
            )
            display = f"{pct} %{neutral_marker}"
        elif unit == "additive":
            display = f"{val} (neutral=0)" if val == 0 else str(val)
        else:
            display = str(val)

        categories.setdefault(category, []).append({
            "key": key,
            "label": label,
            "value": val,
            "display": display,
            "neutral": setting_default,
        })

    return {
        "status": "ok",
        "game": exe,
        "registry_path": _openxr_reg_path(game_exe),
        "categories": categories,
        "display_instructions": (
            "Zeige die Einstellungen als Markdown-Tabellen gruppiert nach Kategorie. "
            "Kategorien: Upscaling, Farbe/Post-Processing, Foveated Rendering (VRS), "
            "World/FOV, Performance, Sonstige. Antworte auf Deutsch."
        ),
    }


@mcp.tool()
def set_openxr_setting(
    setting: str,
    value: str,
    game_exe: str = "",
) -> dict:
    """Change an OpenXR Toolkit VR setting in the Windows Registry.

    WICHTIG: Dies ändert OpenXR Toolkit Einstellungen — NICHT MSFS-Grafik!
    Für MSFS-Grafik (DLSS, Wolken, Terrain) → set_msfs_setting verwenden.

    OpenXR Toolkit steuert: Upscaling (NIS/FSR/CAS), Schärfe, Foveated
    Rendering (VRS), Post-Processing (Helligkeit/Kontrast/Sättigung), Reprojection.

    Writes directly to: HKCU\\SOFTWARE\\OpenXR_Toolkit\\FlightSimulator2024.exe
    Changes take effect at the next VR session start.

    FARBEINSTELLUNGEN — Wertebereich (gespeichert als Ganzzahl × 10):
      • 100  = 100 % = Neutralwert (kein Effekt, Originalfarben)
      • 0    = 0 % — ACHTUNG: Sättigung 0 = Graustufen! Helligkeit 0 = schwarz!
      • 150  = 150 % (Farbe verstärkt)
      • post_process MUSS aktiviert sein (wird automatisch eingeschaltet)
    Eingabe: "80" → 80 % (800 gespeichert), "100" → neutral (1000), "150" → 1500
    Relative: "+10" → +10 Prozentpunkte, "-5" → -5 Prozentpunkte
    Reset: "normal" oder "reset" → setzt Neutralwert wieder her (100 % / 1000)

    Use this when the user mentions "OpenXR" and wants to change:
    - "OpenXR Schärfe auf 80" / "NIS Upscaling aktivieren"
    - "OpenXR Foveated Rendering einschalten"
    - "OpenXR Helligkeit auf 110" / "OpenXR Kontrast auf 120"
    - "OpenXR Sättigung normal" (→ 100 % = 1000, keine Graustufen)
    - "Motion Reprojection an" / "Turbo Mode aus"
    - "Sonnenbrille auf Dark"

    Args:
        setting: Setting name (German or English), e.g. "schärfe",
                 "upscaler", "helligkeit", "sättigung", "foveated", "turbo".
        value: New value — name ("nis", "aus", "dark"), percentage ("80" = 80 %),
               multiplier ("1.5" = 150 %), relative ("+10", "-5"),
               or "normal"/"reset" for neutral default.
        game_exe: Game executable (default: FlightSimulator2024.exe).
    """
    exe = game_exe or _OPENXR_MSFS2024_EXE

    # Resolve alias → registry key
    setting_lower = setting.strip().lower()
    reg_key = _OPENXR_ALIASES.get(setting_lower)

    if reg_key is None:
        # Direct match against known keys
        if setting_lower in OPENXR_SETTING_DEFS:
            reg_key = setting_lower
        else:
            for k in OPENXR_SETTING_DEFS:
                if setting_lower in k or k in setting_lower:
                    reg_key = k
                    break

    if reg_key is None:
        return {
            "error": f"Unbekannte OpenXR-Einstellung: '{setting}'",
            "verfügbar": sorted(_OPENXR_ALIASES.keys()),
        }

    defn = OPENXR_SETTING_DEFS.get(reg_key, {})
    label = defn.get("label", reg_key)
    value_map = defn.get("values", {})
    is_percent = defn.get("unit") == "percent"   # brightness/contrast/saturation etc.
    is_additive = defn.get("unit") == "additive" # vibrance/shadows
    setting_default = defn.get("default")
    setting_min = defn.get("min", 0)
    setting_max = defn.get("max", 65535)
    setting_hint = defn.get("hint", "")

    # Read current value
    current = _read_openxr_settings(exe)
    old_val = current.get(reg_key, setting_default if setting_default is not None else 0)

    # Resolve value
    value_lower = value.strip().lower()
    resolved: int | None = None

    # 1. Reset/Neutral aliases → use default
    if value_lower in ("normal", "standard", "reset", "default", "zurücksetzen",
                       "neutral", "zurücksetzen", "zurück", "aus", "off") and \
            reg_key not in _OPENXR_ON_OFF_KEYS and not value_map:
        if value_lower in ("normal", "standard", "reset", "default",
                           "zurücksetzen", "zurück", "neutral"):
            if setting_default is not None:
                resolved = setting_default

    # 2. Check named value aliases (enum keys like scaling_type, vrs, post_sunglasses)
    if resolved is None:
        alias_group = _OPENXR_VALUE_ALIASES.get(reg_key)
        if alias_group is None and reg_key in _OPENXR_ON_OFF_KEYS:
            alias_group = _OPENXR_VALUE_ALIASES["_on_off"]
        if alias_group and value_lower in alias_group:
            resolved = int(alias_group[value_lower])

    # 3. Special: "NIS"/"FSR"/"CAS" as value for scaling_type
    if resolved is None and reg_key == "scaling_type":
        for name, num in _OPENXR_VALUE_ALIASES["scaling_type"].items():
            if value_lower == name:
                resolved = int(num)
                break

    # 4. Relative adjustment: "+10", "-5"  (for percent settings: relative to registry value)
    if resolved is None:
        v_stripped = value.strip()
        if v_stripped.startswith("+") or (v_stripped.startswith("-") and len(v_stripped) > 1):
            try:
                delta = int(v_stripped)
                if is_percent:
                    # "+10" means +10 percentage points → +100 in registry
                    resolved = old_val + delta * 10
                else:
                    resolved = old_val + delta
            except ValueError:
                pass

    # 5. Float multiplier for percent settings: "1.5" → 150 % → 1500 stored
    if resolved is None and is_percent:
        v_stripped = value.strip().rstrip('%')
        if '.' in v_stripped:
            try:
                f = float(v_stripped)
                if value.strip().endswith('%'):
                    resolved = int(f * 10)   # "150.5%" → 1505
                else:
                    resolved = int(f * 1000) # "1.5"   → 1500
            except ValueError:
                pass

    # 6. Percentage string "80%" or plain integer
    if resolved is None:
        v_stripped = value.strip()
        has_percent_suffix = v_stripped.endswith('%')
        v_clean = v_stripped.rstrip('%')
        try:
            raw_int = int(v_clean)
            if is_percent:
                if has_percent_suffix:
                    # Explicit "%": always multiply by 10
                    resolved = raw_int * 10
                elif 0 <= raw_int <= 200:
                    # Compact percentage: "80" → 80 % → 800 stored
                    # Range 0–200 covers all typical UI slider values (0–200 %)
                    resolved = raw_int * 10
                else:
                    # Value > 200: treat as raw registry value (e.g. "1500")
                    resolved = raw_int
            else:
                resolved = raw_int
        except ValueError:
            pass

    if resolved is None:
        hint_text = f" Tipp: {setting_hint}" if setting_hint else ""
        return {
            "error": f"Kann Wert '{value}' nicht interpretieren für '{label}'.",
            "erlaubte_werte": value_map if value_map else (
                f"Prozent 0–200 (z.B. '100' = neutral, '150' = 150 %) oder "
                f"Registry-Rohwert 0–{setting_max}" if is_percent else "Ganzzahl"
            ),
            "hinweis": hint_text.strip(),
        }

    # Clamp to valid range
    resolved = max(setting_min, min(setting_max, resolved))

    # Safety warnings before writing
    warnings_list: list[str] = []
    if reg_key == "post_saturation" and resolved == 0:
        warnings_list.append(
            "⚠️ Sättigung = 0 → Bild wird GRAUSTUFEN (schwarz-weiß)! "
            "Neutral = 1000 (100 %). Für normale Farben: 'sättigung 100'."
        )
    elif reg_key == "post_saturation" and resolved < 500:
        warnings_list.append(
            f"⚠️ Sättigung {resolved // 10} % ist sehr niedrig — Farben wirken fast grau. "
            "Neutral = 100 % (1000)."
        )
    if reg_key == "post_brightness" and resolved < 300:
        warnings_list.append(
            f"⚠️ Helligkeit {resolved // 10} % ist sehr niedrig — Bild wird sehr dunkel. "
            "Neutral = 100 % (1000)."
        )
    if reg_key in ("post_gain_r", "post_gain_g", "post_gain_b") and resolved == 0:
        warnings_list.append(
            f"⚠️ {label} = 0 → Farbkanal vollständig deaktiviert! Neutral = 100 % (1000)."
        )

    # Auto-enable post_process for color-category settings
    # (without it, all post-processing is disabled regardless of individual values)
    pp_enabled_now = False
    if defn.get("category") == "color" and reg_key != "post_process":
        current_pp = current.get("post_process", 0)
        if current_pp != 1:
            _write_openxr_setting("post_process", 1, exe)
            pp_enabled_now = True

    # Write to registry
    ok = _write_openxr_setting(reg_key, resolved, exe)

    old_display = value_map.get(str(old_val), (
        f"{old_val // 10} %" if is_percent else str(old_val)
    ))
    new_display = value_map.get(str(resolved), (
        f"{resolved // 10} %" if is_percent else str(resolved)
    ))

    if not ok:
        return {"error": f"Konnte '{reg_key}' nicht in Registry schreiben."}

    # Verify
    verify = _read_openxr_settings(exe)
    verified = verify.get(reg_key) == resolved

    result: dict = {
        "status": "ok",
        "einstellung": label,
        "key": reg_key,
        "vorher": old_display,
        "nachher": new_display,
        "raw_value": resolved,
        "registry_path": f"{_openxr_reg_path(exe)}\\{reg_key}",
        "verifiziert": verified,
        "hinweis": (
            "Einstellung in Registry geschrieben. Wird bei der nächsten "
            "VR-Session aktiv. Falls MSFS in VR läuft: VR-Modus beenden "
            "und neu starten (Ctrl+Tab oder MSFS neu starten)."
        ),
    }

    if setting_hint:
        result["wertebereich"] = setting_hint

    if pp_enabled_now:
        result["post_process_aktiviert"] = (
            "Post-Processing war deaktiviert — wurde automatisch eingeschaltet "
            "(Voraussetzung für alle Farbeffekte)."
        )

    if warnings_list:
        result["warnungen"] = warnings_list

    return result


@mcp.tool()
def apply_openxr_preset(
    preset: Literal["Performance", "Balanced", "Quality", "Auto"] = "Auto",
    game_exe: str = "",
) -> dict:
    """Apply a GPU-aware OpenXR Toolkit VR preset for MSFS 2024.

    Detects your GPU automatically and selects appropriate OpenXR Toolkit
    settings. "Auto" (default) picks the best preset for your GPU:
    - Flagship (5090/4090) → Quality
    - High-end (5080/4080/3090) → Balanced
    - Mid/Entry (4070 and below) → Performance

    Manual override: Performance / Balanced / Quality.

    Steuert: NIS/FSR Upscaling, Schärfe, Foveated Rendering (VRS),
    Post-Processing, Turbo Mode, Motion Reprojection.

    Args:
        preset: "Auto" (GPU-basiert), "Performance", "Balanced", oder "Quality".
        game_exe: Game executable (default: FlightSimulator2024.exe).
    """
    # GPU detection for Auto mode
    gpu_name, gpu_tier, vram_mb = _detect_gpu_tier()

    if preset == "Auto":
        _TIER_TO_OPENXR = {
            "flagship": "Quality",
            "high_end": "Balanced",
            "mid_high": "Performance",
            "mid_range": "Performance",
            "entry": "Performance",
            "unknown": "Balanced",
        }
        preset = _TIER_TO_OPENXR.get(gpu_tier, "Balanced")

    presets: dict[str, dict[str, int]] = {
        "Performance": {
            "scaling_type": 1,    # NIS
            "sharpness": 70,
            "post_process": 0,    # Off (spart FPS)
            "vrs": 1,             # Preset
            "vrs_inner": 0,       # 1x (scharf in der Mitte)
            "vrs_middle": 2,      # 1/4
            "vrs_outer": 3,       # 1/8
            "turbo": 1,           # On
            "motion_reprojection": 2,  # On
        },
        "Balanced": {
            "scaling_type": 1,    # NIS
            "sharpness": 50,
            "post_process": 1,    # On — PFLICHT für Farbeffekte
            "post_brightness": 1000,  # 100 % = neutral
            "post_contrast": 1100,    # 110 % = leicht mehr Kontrast
            "post_saturation": 1100,  # 110 % = leicht sattere Farben
            "post_vibrance": 150,     # Schwache Vibrance-Verstärkung
            "post_gain_r": 1000,      # 100 % = neutral (Rot)
            "post_gain_g": 1000,      # 100 % = neutral (Grün)
            "post_gain_b": 1000,      # 100 % = neutral (Blau)
            "vrs": 1,             # Preset
            "vrs_inner": 0,       # 1x
            "vrs_middle": 1,      # 1/2
            "vrs_outer": 2,       # 1/4
            "turbo": 0,           # Off
            "motion_reprojection": 0,  # Default
        },
        "Quality": {
            "scaling_type": 3,    # CAS (nur Schärfe, kein Upscaling)
            "sharpness": 40,
            "post_process": 1,    # On — PFLICHT für Farbeffekte
            "post_brightness": 1000,  # 100 % = neutral
            "post_contrast": 1050,    # 105 % = minimaler Kontrast-Boost
            "post_saturation": 1050,  # 105 % = minimal sattere Farben
            "post_vibrance": 100,     # Sehr dezente Vibrance
            "post_gain_r": 1000,      # 100 % = neutral
            "post_gain_g": 1000,      # 100 % = neutral
            "post_gain_b": 1000,      # 100 % = neutral
            "vrs": 0,             # Off (volle Auflösung)
            "turbo": 0,           # Off
            "motion_reprojection": 0,  # Default
        },
    }

    preset_def = presets.get(preset)
    if not preset_def:
        return {"error": f"Unbekanntes Preset: {preset}"}

    exe = game_exe or _OPENXR_MSFS2024_EXE

    # Backup current settings
    current = _read_openxr_settings(exe)

    # Apply all settings
    applied: dict[str, dict] = {}
    failed: list[str] = []
    for key, new_val in preset_def.items():
        old_val = current.get(key, 0)
        defn = OPENXR_SETTING_DEFS.get(key, {})
        value_map = defn.get("values", {})

        ok = _write_openxr_setting(key, new_val, exe)
        if ok:
            applied[key] = {
                "label": defn.get("label", key),
                "vorher": value_map.get(str(old_val), str(old_val)),
                "nachher": value_map.get(str(new_val), str(new_val)),
            }
        else:
            failed.append(key)

    return {
        "status": "ok" if not failed else "partial",
        "preset": preset,
        "gpu": gpu_name,
        "gpu_tier": gpu_tier,
        "game": exe,
        "geändert": applied,
        "fehlgeschlagen": failed if failed else None,
        "hinweis": (
            f"OpenXR Toolkit '{preset}'-Preset angewendet "
            f"(GPU: {gpu_name}, Tier: {gpu_tier}). "
            "Wird bei der nächsten VR-Session aktiv. "
            "MSFS VR beenden und neu starten damit es wirkt."
        ),
    }


# ===========================================================================
# GPU-aware VR Color Profiles
# Three-layer system: Pimax hardware → OpenXR post-processing → ReShade
# ===========================================================================
# All layers are tuned to maximize visual quality while staying within FPS
# budget for each GPU tier.  FPS impact is measured at Pimax-resolution
# (per eye ~2448×2448 in 90 Hz mode).
#
# Pimax  : hardware-level panel adjustments — zero GPU cost
# OpenXR : pre-render color grading via toolkit post-process — ~1-2 % FPS
# ReShade: post-render effects (sharpening, vibrance, tonemap) — 2-8 % FPS
# ---------------------------------------------------------------------------

_VR_COLOR_PROFILES_BY_TIER: dict[str, dict] = {
    "flagship": {
        "description": (
            "RTX 5090 / 4090 — Alle drei Ebenen maximiert. "
            "Tiefes Schwarz, satte Farben, HDR-ähnlicher Look, scharfes Bild."
        ),
        "fps_impact": "~4-7 %",   # OpenXR ~1-2% + CAS+Colorfulness+Vibrance+Tonemap ~3-5%
        # OpenXR post-processing (registry values, 1000 = 100 % = neutral)
        "openxr": {
            "post_process":    1,
            "post_brightness": 1000,   # 100 % — neutral (Pimax-Display hell genug)
            "post_contrast":   1120,   # 112 % — etwas mehr Kontrast
            "post_saturation": 1160,   # 116 % — sattere, lebendigere Farben
            "post_vibrance":   250,    # Dezente Vibrance-Verstärkung
            "post_exposure":   1000,   # 100 % — neutral
            "post_gain_r":     1025,   # 102.5 % — leicht wärmerer Weißpunkt
            "post_gain_g":     1000,   # 100 %
            "post_gain_b":      975,   # 97.5 % — minimal kühler für Blautöne
            "post_highlights":  940,   # 94 % — Lichter absenken (HDR-Look)
            "post_shadows":      60,   # Schatten leicht aufhellen (Schattendetail)
        },
        # ReShade effects
        "reshade_enable":  ["CAS", "Colorfulness", "Vibrance", "Tonemap"],
        "reshade_disable": ["MXAO", "MagicBloom", "SMAA", "FXAA",
                            "AdaptiveSharpen", "LumaSharpen"],
        "reshade_settings": {
            "CAS":          {"Sharpness": "0.60"},
            "Colorfulness": {"colorfullness": "0.50", "lim_luma": "0.75"},
            "Vibrance":     {"Vibrance": "0.20"},
            "Tonemap":      {"Gamma": "1.02", "Exposure": "0.03",
                             "Saturation": "0.05", "Defog": "0.0"},
        },
        # Pimax hardware display (0-100, 50 = neutral)
        "pimax": {
            "piplay_color_brightness_0": 52,
            "piplay_color_brightness_1": 52,
            "piplay_color_contrast_0":   56,
            "piplay_color_contrast_1":   56,
            "piplay_color_saturation_0": 62,
            "piplay_color_saturation_1": 62,
        },
    },
    "high_end": {
        "description": (
            "RTX 5080 / 4080 / 3090 Ti — ReShade (leicht) + OpenXR + Pimax. "
            "Sehr gute Farben, kein nennenswerter FPS-Verlust."
        ),
        "fps_impact": "~3-6 %",   # OpenXR ~1-2% + CAS+Vibrance+Tonemap ~2-4%
        "openxr": {
            "post_process":    1,
            "post_brightness": 1000,
            "post_contrast":   1100,   # 110 %
            "post_saturation": 1130,   # 113 %
            "post_vibrance":   180,
            "post_exposure":   1000,
            "post_gain_r":     1015,
            "post_gain_g":     1000,
            "post_gain_b":      985,
            "post_highlights":  955,
            "post_shadows":      40,
        },
        "reshade_enable":  ["CAS", "Vibrance", "Tonemap"],
        "reshade_disable": ["MXAO", "MagicBloom", "Colorfulness",
                            "AdaptiveSharpen", "LumaSharpen", "SMAA", "FXAA"],
        "reshade_settings": {
            "CAS":     {"Sharpness": "0.52"},
            "Vibrance": {"Vibrance": "0.18"},
            "Tonemap": {"Gamma": "1.01", "Exposure": "0.02",
                        "Saturation": "0.04", "Defog": "0.0"},
        },
        "pimax": {
            "piplay_color_brightness_0": 51,
            "piplay_color_brightness_1": 51,
            "piplay_color_contrast_0":   54,
            "piplay_color_contrast_1":   54,
            "piplay_color_saturation_0": 58,
            "piplay_color_saturation_1": 58,
        },
    },
    "mid_high": {
        "description": (
            "RTX 4070 Ti SUPER / 3080 Ti — CAS-Schärfe + OpenXR + Pimax. "
            "Farbeffekte nur über OpenXR (kein teures ReShade)."
        ),
        "fps_impact": "~2-4 %",   # OpenXR ~1-2% + CAS ~1-2%
        "openxr": {
            "post_process":    1,
            "post_brightness": 1000,
            "post_contrast":   1080,   # 108 %
            "post_saturation": 1110,   # 111 %
            "post_vibrance":   130,
            "post_exposure":   1000,
            "post_gain_r":     1010,
            "post_gain_g":     1000,
            "post_gain_b":      990,
            "post_highlights":  965,
            "post_shadows":      20,
        },
        "reshade_enable":  ["CAS"],   # Nur Schärfe — kein Farb-ReShade
        "reshade_disable": ["MXAO", "MagicBloom", "Colorfulness", "Vibrance",
                            "Tonemap", "AdaptiveSharpen", "LumaSharpen",
                            "SMAA", "FXAA"],
        "reshade_settings": {
            "CAS": {"Sharpness": "0.48"},
        },
        "pimax": {
            "piplay_color_brightness_0": 51,
            "piplay_color_brightness_1": 51,
            "piplay_color_contrast_0":   53,
            "piplay_color_contrast_1":   53,
            "piplay_color_saturation_0": 56,
            "piplay_color_saturation_1": 56,
        },
    },
    "mid_range": {
        "description": (
            "RTX 4070 / 3070 — Nur OpenXR + Pimax, kein ReShade. "
            "FPS haben Vorrang; trotzdem deutlich besser als Defaults."
        ),
        "fps_impact": "~1-2 %",   # Nur OpenXR Post-Processing, kein ReShade
        "openxr": {
            "post_process":    1,
            "post_brightness": 1000,
            "post_contrast":   1060,   # 106 %
            "post_saturation": 1090,   # 109 %
            "post_vibrance":    80,
            "post_exposure":   1000,
            "post_gain_r":     1000,
            "post_gain_g":     1000,
            "post_gain_b":     1000,
            "post_highlights":  975,
            "post_shadows":      10,
        },
        "reshade_enable":  [],
        "reshade_disable": ["MXAO", "MagicBloom", "Colorfulness", "Vibrance",
                            "Tonemap", "CAS", "AdaptiveSharpen", "LumaSharpen",
                            "SMAA", "FXAA", "Clarity"],
        "reshade_settings": {},
        "pimax": {
            "piplay_color_brightness_0": 50,
            "piplay_color_brightness_1": 50,
            "piplay_color_contrast_0":   52,
            "piplay_color_contrast_1":   52,
            "piplay_color_saturation_0": 54,
            "piplay_color_saturation_1": 54,
        },
    },
    "entry": {
        "description": (
            "RTX 4060 und darunter — Minimale OpenXR-Anpassungen, Pimax leicht. "
            "FPS haben absolute Priorität."
        ),
        "fps_impact": "< 1 %",
        "openxr": {
            "post_process":    1,
            "post_brightness": 1000,
            "post_contrast":   1040,   # 104 %
            "post_saturation": 1060,   # 106 %
            "post_vibrance":     0,    # Kein Vibrance
            "post_exposure":   1000,
            "post_gain_r":     1000,
            "post_gain_g":     1000,
            "post_gain_b":     1000,
            "post_highlights": 1000,
            "post_shadows":       0,
        },
        "reshade_enable":  [],
        "reshade_disable": ["MXAO", "MagicBloom", "Colorfulness", "Vibrance",
                            "Tonemap", "CAS", "AdaptiveSharpen", "LumaSharpen",
                            "SMAA", "FXAA", "Clarity"],
        "reshade_settings": {},
        "pimax": {
            "piplay_color_brightness_0": 50,
            "piplay_color_brightness_1": 50,
            "piplay_color_contrast_0":   51,
            "piplay_color_contrast_1":   51,
            "piplay_color_saturation_0": 52,
            "piplay_color_saturation_1": 52,
        },
    },
}

# Neutral defaults for full reset
_VR_COLOR_NEUTRAL: dict = {
    "openxr": {
        "post_process":    0,     # Off
        "post_brightness": 1000,  # 100 %
        "post_contrast":   1000,  # 100 %
        "post_saturation": 1000,  # 100 %
        "post_vibrance":      0,
        "post_exposure":   1000,  # 100 %
        "post_gain_r":     1000,  # 100 %
        "post_gain_g":     1000,
        "post_gain_b":     1000,
        "post_highlights": 1000,
        "post_shadows":       0,
    },
    "pimax": {
        "piplay_color_brightness_0": 50,
        "piplay_color_brightness_1": 50,
        "piplay_color_contrast_0":   50,
        "piplay_color_contrast_1":   50,
        "piplay_color_saturation_0": 50,
        "piplay_color_saturation_1": 50,
    },
}


@mcp.tool()
def apply_vr_color_profile(
    game_dir: str = "",
    config_path: str = "",
    game_exe: str = "",
    force_tier: str = "",
) -> dict:
    """Apply a GPU-optimised VR color profile (OpenXR + ReShade + Pimax combined).

    Detects your GPU automatically and applies the best three-layer color
    configuration that maximises visual quality while keeping FPS impact minimal:

      Ebene 1 — Pimax-Display (Hardware-Farben, KEIN GPU-Aufwand):
        Brightness / Contrast / Saturation direkt am Panel anpassen.

      Ebene 2 — OpenXR Toolkit Post-Processing (~1-2 % FPS):
        Helligkeit, Kontrast, Sättigung, Vibrance, Farbkanäle (R/G/B),
        Lichter und Schatten im VR-Bild.

      Ebene 3 — ReShade (2-6 % FPS, GPU-abhängig):
        CAS-Schärfe, Colorfulness, Vibrance, Tonemap —
        wird bei schwächeren GPUs automatisch deaktiviert.

    GPU-Tier-Zuordnung:
      flagship  (RTX 5090/4090)     — alle 3 Ebenen, maximale Werte
      high_end  (RTX 5080/4080/3090)— ReShade leicht + OpenXR + Pimax
      mid_high  (RTX 4070 Ti/3080)  — CAS + OpenXR + Pimax
      mid_range (RTX 4070/3070)     — nur OpenXR + Pimax
      entry     (RTX 4060 u.ä.)     — minimales OpenXR + Pimax

    Verwende diesen Befehl wenn der User sagt:
      - "Farben für VR optimieren"
      - "Beste Farben für meine GPU"
      - "Alles auf Maximum anpassen"
      - "VR Farbprofil"
      - "Farben schöner machen"

    Args:
        game_dir: Verzeichnis mit ReShade.ini (auto-erkannt wenn leer).
        config_path: Pimax-Konfigurationspfad (auto-erkannt wenn leer).
        game_exe: Spiel-EXE für OpenXR (Standard: FlightSimulator2024.exe).
        force_tier: GPU-Tier erzwingen ('flagship'/'high_end'/'mid_high'/
                    'mid_range'/'entry'). Leer = auto-Erkennung.
    """
    gpu_name, gpu_tier, vram_mb = _detect_gpu_tier()

    if force_tier and force_tier in _VR_COLOR_PROFILES_BY_TIER:
        gpu_tier = force_tier

    if gpu_tier not in _VR_COLOR_PROFILES_BY_TIER:
        gpu_tier = "mid_range"   # Safe default

    profile = _VR_COLOR_PROFILES_BY_TIER[gpu_tier]
    exe = game_exe or _OPENXR_MSFS2024_EXE

    results: dict[str, dict] = {}
    warnings: list[str] = []

    # ── Ebene 1: OpenXR Toolkit ──────────────────────────────────────────────
    openxr_applied: dict[str, int] = {}
    openxr_failed:  list[str] = []
    for key, val in profile["openxr"].items():
        ok = _write_openxr_setting(key, val, exe)
        if ok:
            openxr_applied[key] = val
        else:
            openxr_failed.append(key)
    results["openxr"] = {
        "status": "ok" if not openxr_failed else "partial",
        "angewendet": {
            k: (f"{v // 10} %" if k not in ("post_process", "post_vibrance", "post_shadows") else str(v))
            for k, v in openxr_applied.items()
        },
        "fehlgeschlagen": openxr_failed or None,
    }

    # ── Ebene 2: ReShade ─────────────────────────────────────────────────────
    ini = _find_reshade_ini(game_dir)
    if ini:
        preset_path = _find_preset_file(ini)
        if preset_path:
            # Write per-effect parameters
            rs_settings = profile.get("reshade_settings", {})
            config = _read_preset(preset_path)
            for eff_name, eff_params in rs_settings.items():
                if not config.has_section(eff_name):
                    config.add_section(eff_name)
                for k, v in eff_params.items():
                    config.set(eff_name, k, str(v))
            with open(preset_path, "w", encoding="utf-8") as f:
                config.write(f)
            # Update Techniques= list
            _update_reshade_techniques(
                preset_path,
                enable=profile.get("reshade_enable", []),
                disable=profile.get("reshade_disable", []),
            )
            try:
                preset_path.touch()
            except Exception:
                pass
            results["reshade"] = {
                "status": "ok",
                "aktive_effekte": profile.get("reshade_enable", []),
                "deaktivierte_effekte": profile.get("reshade_disable", []),
                "preset_file": str(preset_path),
            }
        else:
            results["reshade"] = {"status": "skipped", "grund": "Kein aktives Preset gefunden"}
            warnings.append("ReShade-Preset nicht gefunden — ReShade-Ebene übersprungen.")
    else:
        results["reshade"] = {"status": "skipped", "grund": "ReShade.ini nicht gefunden"}
        warnings.append("ReShade nicht gefunden — installiert und eingerichtet?")

    # ── Ebene 3: Pimax ───────────────────────────────────────────────────────
    cfg = _find_pimax_config(config_path)
    if cfg:
        pimax_updates = profile.get("pimax", {})
        registry_result = _apply_pimax_settings_to_registry(pimax_updates, verify=True)
        verified = sum(1 for v in registry_result.values() if v.get("verified"))
        results["pimax"] = {
            "status": "ok" if verified == len(pimax_updates) else "partial",
            "angewendet": pimax_updates,
            "registry_audit": registry_result,
        }
    else:
        results["pimax"] = {"status": "skipped", "grund": "Pimax-Config nicht gefunden"}
        warnings.append("Pimax nicht gefunden — Pimax Play installiert und konfiguriert?")

    return {
        "status": "ok" if not any(r.get("status") == "partial" for r in results.values()) else "partial",
        "gpu": gpu_name,
        "gpu_tier": gpu_tier,
        "vram_gb": round(vram_mb / 1024, 1),
        "profil_beschreibung": profile["description"],
        "fps_impact": profile["fps_impact"],
        "ebenen": results,
        "warnungen": warnings if warnings else None,
        "hinweis": (
            f"GPU-Farbprofil '{gpu_tier}' angewendet (GPU: {gpu_name}). "
            "OpenXR: wird bei nächstem VR-Start aktiv. "
            "ReShade: sofort wirksam (Preset aktualisiert). "
            "Pimax: sofort (Registry). "
            "Falls keine Änderung sichtbar: VR-Modus neu starten (Ctrl+Tab in MSFS)."
        ),
    }


@mcp.tool()
def reset_vr_colors(
    reset_openxr: bool = True,
    reset_pimax: bool = True,
    reset_reshade: bool = True,
    game_dir: str = "",
    config_path: str = "",
    game_exe: str = "",
) -> dict:
    """Reset all VR color settings to neutral defaults.

    Setzt OpenXR Toolkit, Pimax-Display und ReShade-Farbeffekte auf neutrale
    Standardwerte zurück.  Ideal wenn Farben falsch aussehen (B&W, zu dunkel,
    übersättigt) und man einen sauberen Neustart möchte.

    Verwende diesen Befehl wenn der User sagt:
      - "Farben zurücksetzen"
      - "Farben reparieren"
      - "Alles auf Standard"
      - "Schwarz-weiß Problem beheben"
      - "VR Farben funktionieren nicht"

    Args:
        reset_openxr:  OpenXR Post-Processing auf neutral zurücksetzen.
        reset_pimax:   Pimax-Display-Farben auf neutral (50/50/50).
        reset_reshade: Alle ReShade-Farbeffekte deaktivieren.
        game_dir:      ReShade-Verzeichnis (auto-erkannt).
        config_path:   Pimax-Config-Pfad (auto-erkannt).
        game_exe:      Spiel-EXE für OpenXR.
    """
    exe = game_exe or _OPENXR_MSFS2024_EXE
    results: dict[str, dict] = {}

    if reset_openxr:
        applied: dict = {}
        failed: list = []
        for key, val in _VR_COLOR_NEUTRAL["openxr"].items():
            ok = _write_openxr_setting(key, val, exe)
            if ok:
                applied[key] = val
            else:
                failed.append(key)
        results["openxr"] = {
            "status": "ok" if not failed else "partial",
            "zurückgesetzt": len(applied),
            "fehlgeschlagen": failed or None,
        }

    if reset_pimax:
        cfg = _find_pimax_config(config_path)
        if cfg:
            reg_result = _apply_pimax_settings_to_registry(
                _VR_COLOR_NEUTRAL["pimax"], verify=True
            )
            results["pimax"] = {
                "status": "ok",
                "zurückgesetzt": list(_VR_COLOR_NEUTRAL["pimax"].keys()),
            }
        else:
            results["pimax"] = {"status": "skipped", "grund": "Pimax nicht gefunden"}

    if reset_reshade:
        ini = _find_reshade_ini(game_dir)
        if ini:
            preset_path = _find_preset_file(ini)
            if preset_path:
                all_effects = list(RESHADE_EFFECTS.keys())
                _update_reshade_techniques(preset_path, enable=[], disable=all_effects)
                try:
                    preset_path.touch()
                except Exception:
                    pass
                results["reshade"] = {
                    "status": "ok",
                    "deaktivierte_effekte": all_effects,
                    "preset_file": str(preset_path),
                }
            else:
                results["reshade"] = {"status": "skipped", "grund": "Kein Preset gefunden"}
        else:
            results["reshade"] = {"status": "skipped", "grund": "ReShade nicht gefunden"}

    return {
        "status": "ok",
        "ergebnis": results,
        "hinweis": (
            "Alle Farbeinstellungen auf neutral zurückgesetzt. "
            "OpenXR: wirksam bei nächstem VR-Start. "
            "ReShade + Pimax: sofort wirksam. "
            "Verwende apply_vr_color_profile um GPU-optimierte Farben anzuwenden."
        ),
    }


@mcp.tool()
def check_nvidia_mcp_server_update() -> dict:
    """Check whether a newer version of the nvidia-mcp SERVER SOFTWARE itself (this MCP server, file: server.py) is available on GitHub.

    USE THIS when the user asks any of:
      - "Gibt es ein neues Update für den MCP-Server / nvidia-mcp / diesen Server?"
      - "Is there a new version of the MCP server?"
      - "Check for nvidia-mcp updates"
      - "Server aktualisieren?"

    DO NOT USE THIS for: NVIDIA graphics driver updates (use check_and_install_driver),
    Windows Updates, MSFS patches, ReShade updates, game updates, or any other software.
    This tool only checks the GitHub Releases of the nvidia-mcp server itself.

    Note: when running inside GameCopilot, the host application also updates this server
    automatically on each launch — the user does not need to run install_nvidia_mcp_server_update
    manually unless running this MCP server standalone.

    Returns: {"status": "current" | "update_available" | "error", "current_version": "...", "latest_version": "...", ...}
    """
    return _updater_check()


@mcp.tool()
def install_nvidia_mcp_server_update() -> dict:
    """Download and install the newest version of the nvidia-mcp SERVER SOFTWARE itself (this MCP server, file: server.py).

    USE THIS when the user asks to update the MCP server:
      - "Update den MCP-Server / installiere die neue Version"
      - "Install nvidia-mcp update"

    DO NOT USE THIS for: NVIDIA graphics drivers, Windows Updates, or any other software.
    This tool only updates the nvidia-mcp server itself.

    The new version becomes active on the next server restart. The previous server.py is
    saved as server.py.bak for rollback. SHA256 of the download is verified before swap.

    Note: when running inside GameCopilot, this is normally handled automatically by the
    host on each launch — only call this for an immediate manual update.
    """
    return _updater_apply()


@mcp.tool()
def get_nvidia_mcp_server_version() -> dict:
    """Return the version of the running nvidia-mcp SERVER SOFTWARE itself.

    USE THIS when the user asks: "Welche Version vom MCP-Server läuft?",
    "What nvidia-mcp version am I running?", "MCP-Server-Version".

    DO NOT USE THIS for the NVIDIA driver version, GPU info, or any other software version.
    """
    return {"version": __version__, "repo": _GITHUB_REPO}


if __name__ == "__main__":
    mcp.run()

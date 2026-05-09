"""Smoke test: import server.py and verify the FastMCP registry is populated.

Runs in CI on Linux/macOS without an NVIDIA driver — pynvml is lazy-imported,
so the server still starts. Tools that actually need pynvml/Windows just
return a structured error at call time rather than crashing import.

Run:  python tests/test_smoke.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class SmokeTest(unittest.TestCase):
    def test_version_marker_matches(self) -> None:
        first_line = (ROOT / "server.py").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, f'# __mcp_version__ = "{server.__version__}"')

    def test_fastmcp_name(self) -> None:
        self.assertEqual(server.mcp.name, "nvidia-gpu")

    def test_tool_count_baseline(self) -> None:
        n = len(server.mcp._tool_manager._tools)
        self.assertGreaterEqual(n, 50, f"too few tools registered: {n}")

    def test_resources_registered(self) -> None:
        n = len(server.mcp._resource_manager._resources)
        self.assertGreaterEqual(n, 5)

    def test_prompts_registered(self) -> None:
        n = len(server.mcp._prompt_manager._prompts)
        self.assertGreaterEqual(n, 3)

    def test_run_shell_disabled_by_default(self) -> None:
        # Without env opt-in, the tool must refuse.
        result = server.run_shell_command("echo hi")
        self.assertIn("error", result)
        self.assertIn("NVIDIA_MCP_ALLOW_SHELL", result["error"])

    def test_pynvml_guard(self) -> None:
        # On systems without pynvml, get_gpu_status returns a clean error
        # rather than crashing. (If pynvml IS available, just verify it returns a dict.)
        result = server.get_gpu_status(0)
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)

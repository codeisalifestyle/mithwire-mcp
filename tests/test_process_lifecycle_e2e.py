"""No Chrome may outlive its session -- or the server that launched it (ELE-174).

Runs ``scripts/smoke_process_lifecycle.py``: real stdio server, real headless
Chrome, driven over JSON-RPC exactly like an MCP client would. It cancels
in-flight ``session_start`` calls, SIGTERMs and SIGKILLs the server, and closes
its stdin, then asserts that no browser process (and no temp profile) is left
behind. The unit tests cover the logic with stubs; this is the only place where
the whole chain -- MCP SDK cancellation, signal handling, the engine's exit
guard, an actual Chrome -- is exercised together.

Slow (about a minute) and needs a working Chrome, so it is tagged
``stealth_e2e``. Run explicitly:

    pytest tests/test_process_lifecycle_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.stealth_e2e,
    pytest.mark.skipif(os.name != "posix", reason="POSIX signals and process tables"),
]

HARNESS = Path(__file__).resolve().parent.parent / "scripts" / "smoke_process_lifecycle.py"

# 'storm' (concurrent launches) is covered by the manual full run; the rest keep
# the CI lane short while still hitting every distinct mechanism.
SCENARIOS = "cancel-mid,cancel-late,sigterm,sigkill,client-exit"


def test_no_browser_outlives_its_session_or_server() -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--scenarios", SCENARIOS, "--grace", "20"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        "a browser process leaked (or the harness failed):\n"
        f"{result.stdout}\n{result.stderr}"
    )

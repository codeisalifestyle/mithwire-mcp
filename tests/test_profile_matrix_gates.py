"""Unit tests for the CI gate predicates in ``scripts/profile_matrix.py``.

The gates are pure functions over probe dicts, so they are pinned here
without launching a browser. The semantics under test are the load-bearing
policy decisions:

* ``ACCEPTED_FLAGS`` waives exactly the documented depth-layer compromises
  (worker-scope values) -- any OTHER deviceandbrowserinfo flag fails.
* Reachability problems are UNREACHABLE (retry-then-SKIP material), never
  FAIL: a probe that produced no verdict is not a detection signal.
* sannysoft fails only on ``failed`` cells; ``warn`` cells pass with a note.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "profile_matrix", REPO_ROOT / "scripts" / "profile_matrix.py"
)
profile_matrix = importlib.util.module_from_spec(_spec)
sys.modules["profile_matrix"] = profile_matrix
_spec.loader.exec_module(profile_matrix)

ci_gate_dab = profile_matrix.ci_gate_dab
ci_gate_sanny = profile_matrix.ci_gate_sanny
_overall = profile_matrix._overall
GatePass = profile_matrix.GatePass
GateFail = profile_matrix.GateFail
GateUnreachable = profile_matrix.GateUnreachable


class TestDabGate:
    def test_clean_human_passes(self):
        status, msg = ci_gate_dab({"isBot": False, "details": {"isHeadlessChrome": False}})
        assert status == GatePass
        assert msg == "human"

    def test_accepted_flag_passes_even_with_isbot_true(self):
        # The exact CI scenario: cross-OS spoof trips the worker flag, the
        # server says isBot=true off that single flag. Accepted by policy.
        status, msg = ci_gate_dab(
            {"isBot": True, "details": {"hasInconsistentWorkerValues": True}}
        )
        assert status == GatePass
        assert "accepted" in msg
        assert "hasInconsistentWorkerValues" in msg

    def test_unaccepted_flag_fails(self):
        status, msg = ci_gate_dab(
            {"isBot": True, "details": {"isAutomatedWithCDP": True}}
        )
        assert status == GateFail
        assert "isAutomatedWithCDP" in msg

    def test_unaccepted_flag_fails_even_if_isbot_false(self):
        # A true flag is a tell regardless of the aggregate verdict.
        status, _ = ci_gate_dab(
            {"isBot": False, "details": {"isWebGLInconsistent": True}}
        )
        assert status == GateFail

    def test_mixed_flags_fail_and_name_only_the_unaccepted(self):
        status, msg = ci_gate_dab(
            {
                "isBot": True,
                "details": {
                    "hasInconsistentWorkerValues": True,
                    "isHeadlessChrome": True,
                },
            }
        )
        assert status == GateFail
        assert msg.startswith("flags=isHeadlessChrome")
        assert "accepted: hasInconsistentWorkerValues" in msg

    def test_missing_probe_is_unreachable(self):
        assert ci_gate_dab({})[0] == GateUnreachable

    def test_probe_error_is_unreachable(self):
        assert ci_gate_dab({"error": "timeout"})[0] == GateUnreachable

    def test_no_verdict_is_unreachable(self):
        assert ci_gate_dab({"isBot": None})[0] == GateUnreachable


class TestSannyGate:
    def test_all_passed(self):
        status, msg = ci_gate_sanny({"total": 8, "passed": 8, "failed": [], "warn": []})
        assert status == GatePass
        assert msg == "8/8"

    def test_failed_cell_fails(self):
        status, msg = ci_gate_sanny(
            {"total": 8, "passed": 7, "failed": ["webdriver-result"], "warn": []}
        )
        assert status == GateFail
        assert "webdriver-result" in msg

    def test_warn_passes_with_note(self):
        status, msg = ci_gate_sanny(
            {"total": 8, "passed": 7, "failed": [], "warn": ["chrome-result"]}
        )
        assert status == GatePass
        assert "warn=chrome-result" in msg

    def test_missing_probe_is_unreachable(self):
        assert ci_gate_sanny({})[0] == GateUnreachable

    def test_zero_results_is_unreachable(self):
        assert ci_gate_sanny({"total": 0, "passed": 0})[0] == GateUnreachable


class TestOverall:
    def test_pass_pass(self):
        assert _overall((GatePass, ""), (GatePass, "")) == "PASS"

    def test_fail_beats_unreachable(self):
        # A real detection signal must red the build even if the other
        # probe also flaked on the same run.
        assert _overall((GateFail, ""), (GateUnreachable, "")) == "FAIL"

    def test_any_unreachable_skips(self):
        assert _overall((GatePass, ""), (GateUnreachable, "")) == "SKIP"

    def test_any_fail_fails(self):
        assert _overall((GatePass, ""), (GateFail, "")) == "FAIL"

#!/usr/bin/env python3
"""Run a directory of FingerprintConfig profiles through ``baseline_probe.py``
and emit a single PASS/FAIL matrix.

Use this for occasional matrix runs (after a stealth change, before a release,
or when adding a new profile preset) -- it is the integration counterpart to
the fast pytest suites under ``tests/test_fingerprint_*.py``.

## CI pass definition (single source of truth)

The product goal is **a human-like browser across configurations, with or
without spoofing**. For CI we need objective predicates that map cleanly to
that goal -- otherwise predicate drift becomes its own debate. Two gates,
both IP-independent (they read browser/fingerprint state, not the egress
IP's reputation, so they are stable from a datacenter CI runner):

1. **deviceandbrowserinfo.com /are_you_a_bot, with an accepted-flags
   allowlist.** The site computes a *server-side* verdict from a large
   client fingerprint (``POST /fingerprint_bot_test``, ~600 ms) and returns
   ``{isBot, details: {~20 boolean flags}}``. PASS = every ``True`` flag is
   in ``ACCEPTED_FLAGS`` (the ``isBot`` boolean itself is derived from the
   flags, so the allowlist subsumes it). ``ACCEPTED_FLAGS`` encodes the
   project's documented depth-layer compromises (see SITE_PARSING.md):
   today that is exactly ``hasInconsistentWorkerValues`` -- fingerprint
   overrides are applied per-document and do not reach Worker scopes, so a
   cross-OS spoof (mac profile on a Linux CI runner) makes the worker
   disagree with the main thread. Accepted by policy; most detectors don't
   probe this deep. Any flag OUTSIDE the allowlist is a real regression and
   fails the build.

2. **sannysoft 8/8.** Pure client-side, deterministic, zero network-
   reputation input. PASS = no ``failed`` result cell (``warn`` cells are
   reported but do not fail).

CreepJS and demo.fingerprint.com remain **INFORMATIONAL** columns: they're
invaluable for development and depth analysis (CreepJS surfaces consistency
issues the others miss), but their pass/fail nuances (probe-timing,
accepted depth-layer gaps, commercial-API rate limits) aren't suitable as
hard CI gates. Expected CreepJS baseline with a spoof profile: <=2 lies
(the worker Navigator mismatch above + the WebGL toString depth probe).

**Reachability is not detection.** If a gate site can't be reached or
returns no verdict (CI egress hiccup, site outage), the profile is retried
once and then marked SKIP -- neutral, visible in the report, but not a
FAIL. Backstop: if NO profile produces a PASS (e.g. everything skipped),
the run exits non-zero anyway, so flakiness can never silently green the
gate.

## What it does

1. Loads every ``*.json`` under ``--profiles-dir`` (default ``tests/profiles/``).
2. For each profile, launches ``baseline_probe.py`` with the chosen driver /
   headless mode / proxy, writing the per-profile JSON to ``--out-dir``.
3. Evaluates the two gates (``ci_gate_dab`` with ``ACCEPTED_FLAGS``,
   ``ci_gate_sanny``) and computes informational signals via
   ``INFO_SIGNALS``. Unreachable gate sites get one retry, then SKIP.
4. Prints a Markdown table to stdout (the gate columns drive each row's
   PASS/FAIL/SKIP) and exits non-zero if any row FAILed -- or if no row
   PASSed at all (an all-SKIP run is an infrastructure failure, not green).

Example:

    # Headful, direct (no proxy) -- exercises the spoof layer in isolation
    .venv/bin/python scripts/profile_matrix.py --driver bridge --headful

    # Stealth mode, linux profiles only, headless (CI configuration)
    .venv/bin/python scripts/profile_matrix.py --driver bridge --headless \
        --engine stealth --profiles-filter 'linux-*' --skip-fpcom \
        --skip-browserleaks --skip-captcha --skip-detection --skip-ipquality

    # Headful via mobile proxy with proxy timezone alignment (the realistic
    # production configuration the MCP runtime uses).
    .venv/bin/python scripts/profile_matrix.py --driver bridge --headful \
        --proxy "$PROXY" --align-to-proxy

The script does not import ``baseline_probe`` -- it talks to it via subprocess
to avoid coupling the two scripts' event loops and to allow the matrix runner
to keep going if a single profile run hangs (each subprocess has its own
hard timeout).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PROBE = REPO_ROOT / "scripts" / "baseline_probe.py"
DEFAULT_PROFILES_DIR = REPO_ROOT / "tests" / "profiles"


def _python_interpreter() -> str:
    """Prefer the venv interpreter so the subprocess sees the same deps we do.

    Falls back to ``sys.executable`` (the current process) which is the right
    choice whenever the matrix is run as ``.venv/bin/python ...``.
    """
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


# ---------------------------------------------------------------------------
# CI GATES -- the hard pass/fail predicates.
#
# Gate functions return ``(status, msg)`` where status is one of:
#   "pass"        -- the predicate holds
#   "fail"        -- the predicate is violated (real detection signal)
#   "unreachable" -- the probe never produced a verdict (network/site issue;
#                    NOT a detection signal -- eligible for retry, then SKIP)
# ---------------------------------------------------------------------------

GatePass = "pass"
GateFail = "fail"
GateUnreachable = "unreachable"

# Depth-layer detection signals the project accepts BY POLICY (documented in
# SITE_PARSING.md). A flag listed here does not fail the gate; any flag NOT
# listed here does. Widening this set is a reviewed, deliberate decision.
#
# As of the ELE-38 fix, navigator.platform is now spoofed in worker contexts
# via the bootstrap JS, so same-OS-family profiles (e.g. linux-* on Linux
# CI, mac-* on macOS) should pass with zero accepted flags.  Cross-OS
# profiles (mac-* on Linux CI) may still trip hasInconsistentWorkerValues
# for deeper signals the bootstrap cannot reach (e.g. UA string in
# SharedWorker), so the flag stays accepted until those gaps close.
ACCEPTED_FLAGS: frozenset[str] = frozenset({
    "hasInconsistentWorkerValues",
})


def ci_gate_dab(probe: dict[str, Any]) -> tuple[str, str]:
    """deviceandbrowserinfo.com /are_you_a_bot, modulo ``ACCEPTED_FLAGS``.

    The site computes a server-side verdict and renders the response JSON
    verbatim into the page (``code.language-json``). PASS = every ``True``
    flag in the ``details`` map is in ``ACCEPTED_FLAGS``. The ``isBot``
    boolean is itself computed from those flags, so it is reported but not
    independently gated -- with only accepted flags set the site may still
    say ``isBot=true``, and that exact case is the accepted compromise.

    A missing/erroring probe or a non-boolean ``isBot`` is UNREACHABLE, not
    FAIL: no verdict was produced, so there is nothing to gate on.
    """
    if not probe or probe.get("error"):
        return GateUnreachable, f"err:{probe.get('error') or 'missing'}"
    is_bot = probe.get("isBot")
    if is_bot not in (True, False):
        return GateUnreachable, f"no-verdict ({is_bot!r})"
    flags_true = sorted(k for k, v in (probe.get("details") or {}).items() if v is True)
    unaccepted = [f for f in flags_true if f not in ACCEPTED_FLAGS]
    accepted = [f for f in flags_true if f in ACCEPTED_FLAGS]
    if unaccepted:
        msg = "flags=" + ",".join(unaccepted)
        if accepted:
            msg += " (accepted: " + ",".join(accepted) + ")"
        return GateFail, msg
    if accepted:
        return GatePass, "human (accepted: " + ",".join(accepted) + ")"
    return GatePass, "human"


def ci_gate_sanny(probe: dict[str, Any]) -> tuple[str, str]:
    """sannysoft's 8 result cells show no ``failed``.

    Pure client-side and deterministic -- no network-reputation input, so it
    is a stable co-gate even from a datacenter egress. ``warn`` cells are
    reported but do not fail (legitimate browsers produce occasional warns).
    """
    if not probe or probe.get("error"):
        return GateUnreachable, f"err:{probe.get('error') or 'missing'}"
    total = probe.get("total") or 0
    if total <= 0:
        return GateUnreachable, "no-results"
    failed = probe.get("failed") or []
    if failed:
        return GateFail, "failed=" + ",".join(failed)
    warn = probe.get("warn") or []
    passed = probe.get("passed")
    base = f"{passed}/{total}"
    if warn:
        return GatePass, base + " warn=" + ",".join(warn)
    return GatePass, base


# ---------------------------------------------------------------------------
# INFO signals -- measured + reported, do NOT gate CI.
#
# Each function returns a short display string ("8/8", "lies=2", "score=2") or
# an error marker. The matrix prints these as observability so a developer
# can spot drift; the CI workflow only consumes the DAB gate.
# ---------------------------------------------------------------------------

def info_creep(probe: dict[str, Any]) -> str:
    if not probe or probe.get("error"):
        return f"err:{probe.get('error') or 'missing'}"
    if not probe.get("ready"):
        return "not-ready"
    return f"lies={probe.get('lieNodes')}"


def info_fpcom(probe: dict[str, Any]) -> str:
    if not probe or probe.get("error"):
        return f"err:{probe.get('error') or 'missing'}"
    bot = probe.get("bot") or "n/a"
    score = probe.get("suspect_score")
    return f"bot={bot} score={score if score is not None else 'n/a'}"


INFO_SIGNALS: dict[str, Any] = {
    "creepjs": info_creep,
    "fingerprintcom": info_fpcom,
}


def _run_baseline(
    *,
    profile: Path,
    driver: str,
    headless: bool,
    proxy: str | None,
    align: bool,
    skip_fpcom: bool,
    engine: str,
    skip_browserleaks: bool,
    skip_captcha: bool,
    skip_detection: bool,
    skip_ipquality: bool,
    out_path: Path,
    timeout_s: float,
) -> tuple[bool, str]:
    """Invoke baseline_probe.py for a single profile; return (ok, message)."""
    cmd = [
        _python_interpreter(),
        str(BASELINE_PROBE),
        "--driver", driver,
        "--headless" if headless else "--headful",
        "--label", profile.stem,
        "--fingerprint", str(profile),
        "--out", str(out_path),
        "--engine", engine,
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    if align:
        cmd += ["--align-to-proxy"]
    if skip_fpcom:
        cmd += ["--skip-fpcom"]
    if skip_browserleaks:
        cmd += ["--skip-browserleaks"]
    if skip_captcha:
        cmd += ["--skip-captcha"]
    if skip_detection:
        cmd += ["--skip-detection"]
    if skip_ipquality:
        cmd += ["--skip-ipquality"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout_s:.0f}s"
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", errors="replace").splitlines()[-3:]
        return False, "baseline_probe error: " + " | ".join(tail)
    if not out_path.exists():
        return False, "no output file produced"
    return True, "ok"


def _evaluate(
    result: dict[str, Any],
) -> tuple[tuple[str, str], tuple[str, str], dict[str, str]]:
    """Apply both CI gates and gather informational signals.

    Returns ``(dab_gate, sanny_gate, info_signals)``. Each gate is a
    ``(status, msg)`` pair (see the gate section); ``info_signals`` is a
    dict of short display strings keyed by site name -- observability only.
    """
    probes = result.get("probes") or {}
    dab = ci_gate_dab(probes.get("deviceandbrowserinfo") or {})
    sanny = ci_gate_sanny(probes.get("sannysoft") or {})
    info = {site: fn(probes.get(site) or {}) for site, fn in INFO_SIGNALS.items()}
    return dab, sanny, info


def _overall(dab: tuple[str, str], sanny: tuple[str, str]) -> str:
    """Combine the two gates into a row verdict.

    FAIL beats everything (a real detection signal must red the build even
    if the other probe also flaked); otherwise any unreachable gate makes
    the row SKIP (no verdict to act on); otherwise PASS.
    """
    statuses = {dab[0], sanny[0]}
    if GateFail in statuses:
        return "FAIL"
    if GateUnreachable in statuses:
        return "SKIP"
    return "PASS"


def _gate_cell(gate: tuple[str, str]) -> str:
    status, msg = gate
    label = {GatePass: "PASS", GateFail: "FAIL", GateUnreachable: "SKIP"}[status]
    return f"{label} {msg}"


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    """One-line-per-profile table. The two gate columns drive the row's
    OVERALL verdict (PASS / FAIL / SKIP); the remaining columns are tagged
    ``[info]`` so a reader cannot mistake them for gates.
    """
    info_cols = [f"{k} [info]" for k in INFO_SIGNALS]
    columns = (["profile", "driver", "mode", "egress",
                "gate: DAB", "gate: sannysoft"] + info_cols + ["OVERALL"])
    out = ["| " + " | ".join(columns) + " |",
           "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        cells = [
            row["profile"],
            row["driver"],
            "headless" if row["headless"] else "headful",
            row.get("egress") or "-",
            _gate_cell(row["dab"]),
            _gate_cell(row["sanny"]),
        ]
        for site in INFO_SIGNALS:
            cells.append(row["info"].get(site, "-"))
        cells.append(row["overall"])
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
                        help="Directory of FingerprintConfig JSON files (default: tests/profiles/).")
    parser.add_argument("--driver", choices=["mithwire", "bridge"], default="bridge",
                        help="Stealth driver under test. ``raw`` is excluded -- it has no spoof layer.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless (CI-friendly).")
    parser.add_argument("--headful", dest="headless", action="store_false",
                        help="Run Chrome headful (default; the production identity).")
    parser.set_defaults(headless=False)
    parser.add_argument("--proxy", default=None,
                        help="Proxy spec passed to baseline_probe.py (colon or URL form).")
    parser.add_argument("--align-to-proxy", action="store_true",
                        help="Pin browser timezone to the proxy egress IP (requires --proxy).")
    parser.add_argument("--engine", choices=["cdp", "stealth"], default="cdp",
                        help="Engine mode passed to baseline_probe.py (stealth uses CloakBrowser).")
    parser.add_argument("--skip-fpcom", action="store_true",
                        help="Skip the demo.fingerprint.com capture (rate-limited; speeds runs).")
    parser.add_argument("--skip-browserleaks", action="store_true",
                        help="Skip BrowserLeaks probes (flaky third-party site).")
    parser.add_argument("--skip-captcha", action="store_true",
                        help="Skip captcha probes (reCAPTCHA, Turnstile).")
    parser.add_argument("--skip-detection", action="store_true",
                        help="Skip third-party detection sites (BrowserScan, incolumitas, pixelscan).")
    parser.add_argument("--skip-ipquality", action="store_true",
                        help="Skip IP-quality sites (OVP.js). Auto-skipped without a proxy.")
    parser.add_argument("--profiles-filter", default=None,
                        help="Glob pattern to filter profile filenames (e.g. 'linux-*'). Default: all profiles.")
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp/profile-matrix"),
                        help="Per-profile JSON outputs go here.")
    parser.add_argument("--per-profile-timeout", type=float, default=240.0,
                        help="Subprocess timeout per profile (s).")
    parser.add_argument("--report", type=Path, default=None,
                        help="Optional path to write the Markdown report (also printed to stdout).")
    args = parser.parse_args()

    if not args.profiles_dir.is_dir():
        print(f"profiles directory not found: {args.profiles_dir}", file=sys.stderr)
        return 2

    glob_pattern = args.profiles_filter if args.profiles_filter else "*.json"
    if not glob_pattern.endswith(".json"):
        glob_pattern += ".json"
    profiles = sorted(args.profiles_dir.glob(glob_pattern))
    if not profiles:
        print(f"no profiles matching '{glob_pattern}' in {args.profiles_dir}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale outputs so a removed profile does not linger in the report.
    for stale in args.out_dir.glob("*.json"):
        try:
            stale.unlink()
        except OSError:
            pass

    def probe_once(profile: Path, out_path: Path) -> dict[str, Any]:
        """Run one profile through baseline_probe and evaluate the gates.

        Harness-level failures (subprocess error/timeout, missing or
        unparseable output) are UNREACHABLE on both gates: no verdict was
        produced, so they are retry-then-SKIP material, never FAIL.
        """
        started = time.monotonic()
        ok, msg = _run_baseline(
            profile=profile,
            driver=args.driver,
            headless=args.headless,
            proxy=args.proxy,
            align=args.align_to_proxy,
            skip_fpcom=args.skip_fpcom,
            engine=args.engine,
            skip_browserleaks=args.skip_browserleaks,
            skip_captcha=args.skip_captcha,
            skip_detection=args.skip_detection,
            skip_ipquality=args.skip_ipquality,
            out_path=out_path,
            timeout_s=args.per_profile_timeout,
        )
        elapsed = time.monotonic() - started
        base = {
            "profile": profile.stem,
            "driver": args.driver,
            "headless": args.headless,
            "elapsed_s": elapsed,
        }
        if ok:
            try:
                result = json.loads(out_path.read_text())
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"json parse: {exc}"
        if not ok:
            unreachable = (GateUnreachable, "no-data")
            return {
                **base,
                "egress": None,
                "dab": unreachable,
                "sanny": unreachable,
                "info": {site: "no-data" for site in INFO_SIGNALS},
                "overall": "SKIP",
                "error": msg,
            }
        dab, sanny, info = _evaluate(result)
        egress = ((result.get("probes") or {}).get("ipapi") or {}).get("ip") or \
                 ((result.get("proxy_exit") or {}).get("exit_ip"))
        return {
            **base,
            "egress": egress,
            "dab": dab,
            "sanny": sanny,
            "info": info,
            "overall": _overall(dab, sanny),
        }

    rows: list[dict[str, Any]] = []
    for profile in profiles:
        out_path = args.out_dir / f"{profile.stem}.json"
        row = probe_once(profile, out_path)
        if row["overall"] == "SKIP":
            # Reachability is not detection -- give the profile one more
            # attempt before accepting a neutral SKIP.
            print(
                f"[RETRY {profile.stem}]  gate site unreachable "
                f"(dab={row['dab'][1]}, sanny={row['sanny'][1]}) -- retrying once",
                file=sys.stderr,
            )
            row = probe_once(profile, out_path)
        rows.append(row)
        print(
            f"[{row['overall']} {row['profile']}]  {row['elapsed_s']:.1f}s  "
            f"dab={row['dab'][1]}  sanny={row['sanny'][1]}  egress={row['egress']}",
            file=sys.stderr,
        )

    report = _markdown_table(rows)
    print()
    print(report)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n")

    if any(row["overall"] == "FAIL" for row in rows):
        return 1
    if not any(row["overall"] == "PASS" for row in rows):
        # Every profile skipped: the harness or the gate sites are broken.
        # Refuse to report green on zero evidence.
        print("no profile produced a PASS verdict -- treating as failure",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # ``shutil`` is imported for symmetry with baseline_probe.py and to keep
    # this script easy to extend (e.g. cleaning up the out dir on demand).
    _ = shutil
    raise SystemExit(main())

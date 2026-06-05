#!/usr/bin/env python3
"""Run a directory of FingerprintConfig profiles through ``baseline_probe.py``
and emit a single PASS/FAIL matrix.

Use this for occasional matrix runs (after a stealth change, before a release,
or when adding a new profile preset) -- it is the integration counterpart to
the fast pytest suites under ``tests/test_fingerprint_*.py``.

## CI pass definition (single source of truth)

The product goal is **a human-like browser across configurations, with or
without spoofing**. For CI we need a single objective predicate that maps
cleanly to that goal -- otherwise predicate drift becomes its own debate.

CI gate = **deviceandbrowserinfo.com /are_you_a_bot says human**:

* ``isBot is False`` AND
* no boolean in the server-side ``details`` map is ``True``.

That site computes a server-side verdict from a large client fingerprint
(``POST /fingerprint_bot_test``, ~600 ms), so it captures multiple anti-detect
dimensions in one call AND is unambiguous to parse (the result IS the JSON,
not a render artifact). Sannysoft, CreepJS, and demo.fingerprint.com remain
in the matrix as **INFORMATIONAL** columns: they're invaluable for
development and depth analysis (CreepJS surfaces consistency issues the
others miss), but their pass/fail nuances (probe-timing, accepted
depth-layer gaps, commercial-API rate limits) aren't suitable as hard CI
gates. Treat them as observability; the build does not red on their
results.

## What it does

1. Loads every ``*.json`` under ``--profiles-dir`` (default ``tests/profiles/``).
2. For each profile, launches ``baseline_probe.py`` with the chosen driver /
   headless mode / proxy, writing the per-profile JSON to ``--out-dir``.
3. Evaluates the DAB result against ``CI_GATE`` (pass/fail). Computes
   informational signals for the other sites via ``INFO_SIGNALS``.
4. Prints a Markdown table to stdout (the GATE column drives PASS/FAIL; the
   info columns are observability) and exits non-zero if any DAB gate failed.

Example:

    # Headful, direct (no proxy) -- exercises the spoof layer in isolation
    .venv/bin/python scripts/profile_matrix.py --driver bridge --headful

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
# CI GATE -- the single hard pass/fail predicate.
# ---------------------------------------------------------------------------

def ci_gate_dab(probe: dict[str, Any]) -> tuple[bool, str]:
    """deviceandbrowserinfo.com /are_you_a_bot says HUMAN.

    The site computes a server-side verdict and renders the response JSON
    verbatim into the page (``code.language-json``). We require:

    * ``isBot`` is exactly ``False`` (not just falsy) AND
    * no flag in the ``details`` map is ``True``.

    Any other outcome -- ``isBot=true``, any True flag, a missing/erroring
    probe, ``no-verdict`` from the harness wrapper -- fails the gate.
    """
    if not probe or probe.get("error"):
        return False, f"err:{probe.get('error') or 'missing'}"
    is_bot = probe.get("isBot")
    if is_bot is True:
        flags_true = sorted(k for k, v in (probe.get("details") or {}).items() if v is True)
        return False, "isBot=true flags=" + ",".join(flags_true) if flags_true else "isBot=true"
    if is_bot is not False:
        return False, f"no-verdict ({is_bot!r})"
    flags_true = sorted(k for k, v in (probe.get("details") or {}).items() if v is True)
    if flags_true:
        return False, "flags=" + ",".join(flags_true)
    return True, "human"


# ---------------------------------------------------------------------------
# INFO signals -- measured + reported, do NOT gate CI.
#
# Each function returns a short display string ("8/8", "lies=2", "score=2") or
# an error marker. The matrix prints these as observability so a developer
# can spot drift; the CI workflow only consumes the DAB gate.
# ---------------------------------------------------------------------------

def info_sanny(probe: dict[str, Any]) -> str:
    if not probe or probe.get("error"):
        return f"err:{probe.get('error') or 'missing'}"
    passed = probe.get("passed")
    failed = probe.get("failed") or []
    total = probe.get("total") or 0
    suffix = f" failed={','.join(failed)}" if failed else ""
    return f"{passed}/{total}{suffix}"


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
    "sannysoft": info_sanny,
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
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    if align:
        cmd += ["--align-to-proxy"]
    if skip_fpcom:
        cmd += ["--skip-fpcom"]
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


def _evaluate(result: dict[str, Any]) -> tuple[tuple[bool, str], dict[str, str]]:
    """Apply the CI gate (DAB) and gather informational signals.

    Returns ``(gate_verdict, info_signals)`` where ``gate_verdict`` is the
    ``(ok, msg)`` from ``ci_gate_dab`` and ``info_signals`` is a dict of
    short display strings keyed by site name. Only ``gate_verdict`` drives
    PASS/FAIL; the info dict is observability.
    """
    probes = result.get("probes") or {}
    gate = ci_gate_dab(probes.get("deviceandbrowserinfo") or {})
    info = {site: fn(probes.get(site) or {}) for site, fn in INFO_SIGNALS.items()}
    return gate, info


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    """One-line-per-profile table. The first result column ("CI gate") is
    the only one that drives the overall PASS/FAIL -- it shows the DAB
    verdict explicitly. The remaining columns are tagged ``[info]`` so a
    reader cannot mistake them for gates.
    """
    info_cols = [f"{k} [info]" for k in INFO_SIGNALS]
    columns = ["profile", "driver", "mode", "egress", "CI gate (DAB)"] + info_cols + ["OVERALL"]
    out = ["| " + " | ".join(columns) + " |",
           "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        cells = [
            row["profile"],
            row["driver"],
            "headless" if row["headless"] else "headful",
            row.get("egress") or "-",
        ]
        gate_ok, gate_msg = row["gate"]
        cells.append(("PASS " if gate_ok else "FAIL ") + gate_msg)
        for site in INFO_SIGNALS:
            cells.append(row["info"].get(site, "-"))
        cells.append("PASS" if row["overall"] else "FAIL")
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
                        help="Directory of FingerprintConfig JSON files (default: tests/profiles/).")
    parser.add_argument("--driver", choices=["nodriver", "bridge"], default="bridge",
                        help="Stealth driver under test. ``raw`` is excluded -- it has no spoof layer.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless (CI-friendly).")
    parser.add_argument("--headful", dest="headless", action="store_false",
                        help="Run Chrome headful (default; the production identity).")
    parser.set_defaults(headless=False)
    parser.add_argument("--proxy", default=None,
                        help="Proxy spec passed to baseline_probe.py (colon or URL form).")
    parser.add_argument("--align-to-proxy", action="store_true",
                        help="Pin browser timezone to the proxy egress IP (requires --proxy).")
    parser.add_argument("--skip-fpcom", action="store_true",
                        help="Skip the demo.fingerprint.com capture (rate-limited; speeds runs).")
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

    profiles = sorted(args.profiles_dir.glob("*.json"))
    if not profiles:
        print(f"no .json profiles in {args.profiles_dir}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale outputs so a removed profile does not linger in the report.
    for stale in args.out_dir.glob("*.json"):
        try:
            stale.unlink()
        except OSError:
            pass

    rows: list[dict[str, Any]] = []
    for profile in profiles:
        out_path = args.out_dir / f"{profile.stem}.json"
        started = time.monotonic()
        ok, msg = _run_baseline(
            profile=profile,
            driver=args.driver,
            headless=args.headless,
            proxy=args.proxy,
            align=args.align_to_proxy,
            skip_fpcom=args.skip_fpcom,
            out_path=out_path,
            timeout_s=args.per_profile_timeout,
        )
        elapsed = time.monotonic() - started
        if not ok:
            rows.append({
                "profile": profile.stem,
                "driver": args.driver,
                "headless": args.headless,
                "egress": None,
                "gate": (False, "no-data"),
                "info": {site: "no-data" for site in INFO_SIGNALS},
                "overall": False,
                "elapsed_s": elapsed,
                "error": msg,
            })
            print(f"[FAIL {profile.stem}]  {msg}  ({elapsed:.1f}s)", file=sys.stderr)
            continue
        try:
            result = json.loads(out_path.read_text())
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "profile": profile.stem,
                "driver": args.driver,
                "headless": args.headless,
                "egress": None,
                "gate": (False, "no-parse"),
                "info": {site: "no-parse" for site in INFO_SIGNALS},
                "overall": False,
                "elapsed_s": elapsed,
                "error": f"json parse: {exc}",
            })
            continue
        gate, info = _evaluate(result)
        overall = gate[0]  # CI gate IS the overall verdict (DAB only).
        egress = ((result.get("probes") or {}).get("ipapi") or {}).get("ip") or \
                 ((result.get("proxy_exit") or {}).get("exit_ip"))
        rows.append({
            "profile": profile.stem,
            "driver": args.driver,
            "headless": args.headless,
            "egress": egress,
            "gate": gate,
            "info": info,
            "overall": overall,
            "elapsed_s": elapsed,
        })
        tag = "PASS" if overall else "FAIL"
        print(f"[{tag} {profile.stem}]  {elapsed:.1f}s  gate={gate[1]}  egress={egress}", file=sys.stderr)

    report = _markdown_table(rows)
    print()
    print(report)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n")
    return 0 if all(row["overall"] for row in rows) else 1


if __name__ == "__main__":
    # ``shutil`` is imported for symmetry with baseline_probe.py and to keep
    # this script easy to extend (e.g. cleaning up the out dir on demand).
    _ = shutil
    raise SystemExit(main())

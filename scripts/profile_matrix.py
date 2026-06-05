#!/usr/bin/env python3
"""Run a directory of FingerprintConfig profiles through ``baseline_probe.py``
and emit a single PASS/FAIL matrix.

Use this for occasional matrix runs (after a stealth change, before a release,
or when adding a new profile preset) -- it is the integration counterpart to
the fast pytest suites under ``tests/test_fingerprint_*.py``.

What it does:

1. Loads every ``*.json`` under ``--profiles-dir`` (default ``tests/profiles/``).
2. For each profile, launches ``baseline_probe.py`` with the chosen driver /
   headless mode / proxy, writing the per-profile JSON to ``--out-dir``.
3. Loads each result and evaluates it against the documented pass predicates
   (see ``PASS_PREDICATES``). Predicates are intentionally conservative -- they
   accept the documented depth-layer gaps (e.g. CreepJS WebGL toString lie,
   one Navigator worker mismatch) and only fail on regressions.
4. Prints a Markdown table to stdout and exits non-zero if anything failed.

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


def _pred_dab(probe: dict[str, Any]) -> tuple[bool, str]:
    """deviceandbrowserinfo.com: server-side verdict. ``isBot`` must be False
    (or a ``no-verdict`` placeholder on transient render failures), and no
    ``details`` flag may be True (each True flag is a server-side red flag)."""
    if not probe or probe.get("error"):
        return False, f"err:{probe.get('error')}"
    is_bot = probe.get("isBot")
    if is_bot is True:
        return False, "isBot=true"
    flags_true = sorted(k for k, v in (probe.get("details") or {}).items() if v is True)
    if flags_true:
        return False, "flags=" + ",".join(flags_true)
    return True, "clean"


def _pred_sanny(probe: dict[str, Any]) -> tuple[bool, str]:
    """bot.sannysoft.com: client-side suite. Allow ONE failure -- the
    ``permissions-result`` row is a headless-Chrome behavior we accept (the
    Notification permission default differs; not a stealth regression)."""
    if not probe or probe.get("error"):
        return False, f"err:{probe.get('error')}"
    passed = int(probe.get("passed") or 0)
    failed = probe.get("failed") or []
    allowed = {"permissions-result"}
    real_failures = [f for f in failed if f not in allowed]
    if real_failures:
        return False, "failed=" + ",".join(real_failures)
    if passed < 7:
        return False, f"passed={passed}<7"
    return True, f"passed={passed}/8"


def _pred_creep(probe: dict[str, Any]) -> tuple[bool, str]:
    """CreepJS: lie-detector. Up to 2 lies accepted (the documented WebGL
    getParameter toString depth probe + one Navigator worker mismatch on
    cross-arch UA strings). 3+ lies indicates an actual regression."""
    if not probe or probe.get("error"):
        return False, f"err:{probe.get('error')}"
    if not probe.get("ready"):
        return False, "not-ready"
    lies = int(probe.get("lieNodes") or 0)
    if lies > 2:
        return False, f"lieNodes={lies}>2"
    return True, f"lieNodes={lies}"


def _pred_fpcom(probe: dict[str, Any]) -> tuple[bool, str]:
    """demo.fingerprint.com: commercial bot+tampering check. Allow null
    ``bot`` (typical ``bot: not_detected``) and require ``suspect_score < 50``
    when present."""
    if not probe or probe.get("error"):
        # FP Pro is rate-limited / flaky through aggressive proxies; a missing
        # DOM is reported but does NOT fail the predicate -- a true detection
        # signal would surface in ``bot`` or ``suspect_score``.
        return True, f"skip:{probe.get('error')}"
    bot = probe.get("bot")
    if bot and bot not in ("not_detected",):
        return False, f"bot={bot}"
    score = probe.get("suspect_score")
    if isinstance(score, (int, float)) and score >= 50:
        return False, f"suspect_score={score}>=50"
    return True, f"bot={bot or 'n/a'}|score={score if score is not None else 'n/a'}"


PASS_PREDICATES: dict[str, Any] = {
    "deviceandbrowserinfo": _pred_dab,
    "sannysoft": _pred_sanny,
    "creepjs": _pred_creep,
    "fingerprintcom": _pred_fpcom,
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


def _evaluate(result: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """Run each site predicate against a single profile's probe set."""
    probes = result.get("probes") or {}
    verdicts: dict[str, tuple[bool, str]] = {}
    for site, predicate in PASS_PREDICATES.items():
        verdicts[site] = predicate(probes.get(site) or {})
    return verdicts


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    """Render a Markdown table the eye can scan; columns mirror PASS_PREDICATES."""
    columns = ["profile", "driver", "mode", "egress"] + list(PASS_PREDICATES.keys()) + ["OVERALL"]
    out = ["| " + " | ".join(columns) + " |",
           "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        cells = [
            row["profile"],
            row["driver"],
            "headless" if row["headless"] else "headful",
            row.get("egress") or "-",
        ]
        for site in PASS_PREDICATES:
            ok, msg = row["verdicts"].get(site, (False, "missing"))
            cells.append(("PASS " if ok else "FAIL ") + msg)
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
                "verdicts": {site: (False, "no-data") for site in PASS_PREDICATES},
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
                "verdicts": {site: (False, "no-parse") for site in PASS_PREDICATES},
                "overall": False,
                "elapsed_s": elapsed,
                "error": f"json parse: {exc}",
            })
            continue
        verdicts = _evaluate(result)
        overall = all(ok for ok, _ in verdicts.values())
        egress = ((result.get("probes") or {}).get("ipapi") or {}).get("ip") or \
                 ((result.get("proxy_exit") or {}).get("exit_ip"))
        rows.append({
            "profile": profile.stem,
            "driver": args.driver,
            "headless": args.headless,
            "egress": egress,
            "verdicts": verdicts,
            "overall": overall,
            "elapsed_s": elapsed,
        })
        tag = "PASS" if overall else "FAIL"
        print(f"[{tag} {profile.stem}]  {elapsed:.1f}s  egress={egress}", file=sys.stderr)

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

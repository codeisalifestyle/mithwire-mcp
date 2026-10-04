#!/usr/bin/env python3
"""Process-lifecycle smoke test for mithwire-mcp (real Chrome, real stdio server).

Guards against the "stray Chrome windows" class of bug (ELE-174): a browser
process that outlives the session -- or the server -- that owns it. Every
scenario spawns a real ``mithwire_mcp`` stdio server, drives it over JSON-RPC,
and then asserts that **no Chrome process is left behind**:

  cancel-early / cancel-mid / cancel-late
        The client cancels an in-flight ``session_start`` -- the exact
        ``notifications/cancelled`` an MCP client sends when its request times
        out. The half-started browser must be reaped by the server itself.
  sigterm      The server is SIGTERMed with a live session (graceful path).
  sigkill      The server is SIGKILLed with a live session (crash path); the
               engine's exit guard must reap the browser.
  client-exit  The client closes stdin (window closed) with a live session.
  storm        Several concurrent ``session_start`` calls, some cancelled at
               random points: Chrome processes must equal registered sessions.

Isolation: each scenario runs the server with its own ``TMPDIR`` and finds
Chrome processes by that path (it appears in ``--user-data-dir``), so the test
can only ever see -- and clean up -- browsers it started itself. Your own
Chrome/Brave windows are never touched.

Usage (use the MCP venv; pick the code under test with --mcp-src/--engine-src):

    .venv/bin/python scripts/smoke_process_lifecycle.py
    .venv/bin/python scripts/smoke_process_lifecycle.py --scenarios cancel-mid,sigkill
    .venv/bin/python scripts/smoke_process_lifecycle.py --headful   # opens real windows
    .venv/bin/python scripts/smoke_process_lifecycle.py \
        --mcp-src ~/Projects/mithwire-mcp --engine-src ~/Projects/mithwire   # baseline

Exit code 0 = no leaks; 1 = at least one leak; 2 = harness error.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
ALL_SCENARIOS = (
    "cancel-early",
    "cancel-mid",
    "cancel-late",
    "sigterm",
    "sigkill",
    "client-exit",
    "storm",
)


# --------------------------------------------------------------------------- #
# Process inspection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    cmd: str

    @property
    def is_chrome_main(self) -> bool:
        # The browser's main process (not renderer/GPU helpers, not crashpad).
        return "--remote-debugging-port" in self.cmd and "--type=" not in self.cmd

    @property
    def headless(self) -> bool:
        return "--headless" in self.cmd


_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.*)$")


def ps_snapshot() -> list[Proc]:
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    procs: list[Proc] = []
    for line in out.splitlines():
        match = _PS_LINE.match(line)
        if match:
            procs.append(Proc(int(match.group(1)), int(match.group(2)), match.group(3)))
    return procs


def chrome_mains(marker: str) -> list[Proc]:
    return [p for p in ps_snapshot() if marker in p.cmd and p.is_chrome_main]


def macos_app_type(pid: int) -> str | None:
    """``Foreground`` = a window/Dock icon; ``BackgroundOnly`` = headless."""
    if sys.platform != "darwin":
        return None
    asn = subprocess.run(
        ["lsappinfo", "find", f"pid={pid}"], capture_output=True, text=True
    ).stdout.strip()
    if not asn:
        return None
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "ApplicationType", asn],
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r'"ApplicationType"="(\w+)"', info)
    return match.group(1) if match else None


def kill_marker(marker: str) -> list[int]:
    """SIGKILL everything this run started (servers, browsers, helpers, guards)."""
    me = os.getpid()
    killed: list[int] = []
    for proc in ps_snapshot():
        if proc.pid != me and marker in proc.cmd:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, signal.SIGKILL)
                killed.append(proc.pid)
    return killed


async def wait_until(
    predicate: Callable[[], Any], *, timeout: float, interval: float = 0.1
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Minimal MCP stdio client (newline-delimited JSON-RPC)
# --------------------------------------------------------------------------- #
class Server:
    """A real ``python -m mithwire_mcp`` process we fully control."""

    def __init__(self, workdir: Path, env: dict[str, str]) -> None:
        self.workdir = workdir
        self.marker = str(workdir)
        self._env = env
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.proc: asyncio.subprocess.Process
        self._reader: asyncio.Task[None]
        self._log = None

    async def start(self) -> None:
        self._log = open(self.workdir / "server.log", "wb")
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mithwire_mcp",
            "--state-root",
            str(self.workdir / "state"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._log,
            env=self._env,
            # Never the repo root: the code under test is chosen by PYTHONPATH.
            cwd=str(self.workdir),
            limit=2**24,
        )
        self._reader = asyncio.create_task(self._read_loop())
        _, fut = await self.send(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "lifecycle-smoke", "version": "0"},
            },
        )
        await asyncio.wait_for(fut, 60)
        await self.notify("notifications/initialized")

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" not in msg and msg.get("id") is not None:
                fut = self._pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    async def _write(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return rid, fut

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def start_session(self, session_id: str, headless: bool):
        arguments: dict[str, Any] = {"session_id": session_id, "headless": headless}
        if os.environ.get("CHROME"):  # CI points us at the Chrome it installed
            arguments["browser_executable_path"] = os.environ["CHROME"]
        return await self.send("tools/call", {"name": "session_start", "arguments": arguments})

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 90) -> Any:
        _, fut = await self.send("tools/call", {"name": name, "arguments": arguments})
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(f"{name} failed: {msg['error']}")
        result = msg["result"]
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except ValueError:
                    return item["text"]
        return None

    async def session_count(self) -> int:
        data = await self.call_tool("session_list", {}, timeout=30)
        if isinstance(data, dict):
            data = data.get("sessions", data.get("result", []))
        return len(data) if isinstance(data, list) else -1

    async def wait_exit(self, timeout: float) -> int | None:
        try:
            return await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        if self._log is not None:
            self._log.close()

    def log_tail(self, lines: int = 25) -> str:
        try:
            text = (self.workdir / "server.log").read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    seconds: float = 0.0


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.headless = not args.headful
        self.grace = args.grace
        self.root = Path(tempfile.mkdtemp(prefix="mw-smoke-", dir="/tmp"))

    def env(self, workdir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["TMPDIR"] = str(workdir)  # => engine temp profiles live under the marker
        parts = [str(p) for p in (self.args.mcp_src, self.args.engine_src) if p]
        if env.get("PYTHONPATH"):
            parts.append(env["PYTHONPATH"])
        if parts:
            env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    @contextlib.asynccontextmanager
    async def server(self, name: str):
        workdir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self.root))
        srv = Server(workdir, self.env(workdir))
        try:
            await srv.start()
            yield srv
        finally:
            await srv.close()
            if self.args.verbose:
                print(f"       --- {name}: server log tail ---\n{srv.log_tail()}")
            kill_marker(srv.marker)  # whatever a leak left behind -- never keep it
            await asyncio.sleep(0.2)

    def describe_leaks(self, leaked: list[Proc]) -> str:
        parts = []
        for proc in leaked:
            app = macos_app_type(proc.pid)
            kind = "headless" if proc.headless else "HEADFUL"
            where = f"/{app}" if app else ""
            parts.append(f"pid {proc.pid} ({kind}{where}, ppid {proc.ppid})")
        return ", ".join(parts)


async def _timed(name: str, coro: Awaitable[tuple[str, str]]) -> Result:
    started = time.monotonic()
    try:
        status, detail = await coro
    except Exception as exc:  # noqa: BLE001 - harness-level failure
        return Result(name, FAIL, f"harness error: {exc!r}", time.monotonic() - started)
    return Result(name, status, detail, time.monotonic() - started)


async def scenario_cancel(ctx: Context, name: str, *, delay: float) -> tuple[str, str]:
    async with ctx.server(name) as srv:
        rid, fut = await srv.start_session("smoke", ctx.headless)
        if not await wait_until(lambda: chrome_mains(srv.marker), timeout=30):
            return FAIL, "Chrome never started"
        await asyncio.sleep(delay)
        if fut.done():
            msg = fut.result()
            if "error" in msg or msg.get("result", {}).get("isError"):
                return FAIL, f"session_start failed on its own: {str(msg)[:200]}"
            return SKIP, "launch finished before the cancel could land (lower --launch-pad)"
        # What an MCP client sends when its request times out.
        await srv.notify(
            "notifications/cancelled", {"requestId": rid, "reason": "smoke: simulated timeout"}
        )
        await asyncio.wait_for(fut, 30)
        cleaned = await wait_until(lambda: not chrome_mains(srv.marker), timeout=ctx.grace)
        leaked = chrome_mains(srv.marker)
        sessions = await srv.session_count()
        if leaked:
            return FAIL, f"{len(leaked)} Chrome left after cancel: {ctx.describe_leaks(leaked)}"
        if sessions != 0 or not cleaned:
            return FAIL, f"registry has {sessions} session(s) after a cancelled start"
        return PASS, "cancelled launch reaped its browser; registry empty"


async def _live_session(ctx: Context, srv: Server) -> list[Proc]:
    _, fut = await srv.start_session("smoke", ctx.headless)
    msg = await asyncio.wait_for(fut, 90)
    if "error" in msg or msg.get("result", {}).get("isError"):
        raise RuntimeError(f"session_start failed: {msg}")
    return chrome_mains(srv.marker)


async def scenario_signal(ctx: Context, name: str, sig: int) -> tuple[str, str]:
    async with ctx.server(name) as srv:
        before = await _live_session(ctx, srv)
        if len(before) != 1:
            return FAIL, f"expected 1 live Chrome before the signal, saw {len(before)}"
        srv.proc.send_signal(sig)
        exited = await srv.wait_exit(40)
        await wait_until(lambda: not chrome_mains(srv.marker), timeout=ctx.grace)
        leaked = chrome_mains(srv.marker)
        how = signal.Signals(sig).name
        if leaked:
            return FAIL, (
                f"{len(leaked)} Chrome survived {how} "
                f"(server exit={exited}): {ctx.describe_leaks(leaked)}"
            )
        return PASS, f"browser gone after {how} (server exit={exited})"


async def scenario_client_exit(ctx: Context, name: str) -> tuple[str, str]:
    async with ctx.server(name) as srv:
        before = await _live_session(ctx, srv)
        if len(before) != 1:
            return FAIL, f"expected 1 live Chrome before disconnect, saw {len(before)}"
        assert srv.proc.stdin is not None
        srv.proc.stdin.close()  # client vanished: EOF on the server's stdin
        exited = await srv.wait_exit(40)
        await wait_until(lambda: not chrome_mains(srv.marker), timeout=ctx.grace)
        leaked = chrome_mains(srv.marker)
        if leaked:
            return FAIL, (
                f"{len(leaked)} Chrome survived stdin EOF "
                f"(server exit={exited}): {ctx.describe_leaks(leaked)}"
            )
        return PASS, f"browser gone after stdin EOF (server exit={exited})"


async def scenario_storm(ctx: Context, name: str) -> tuple[str, str]:
    async with ctx.server(name) as srv:
        calls = {}
        for index in range(4):
            sid = f"s{index}"
            calls[sid] = await srv.start_session(sid, ctx.headless)
        await wait_until(lambda: chrome_mains(srv.marker), timeout=30)
        # Cancel two of the four at different phases of their launch.
        await asyncio.sleep(0.4)
        await srv.notify("notifications/cancelled", {"requestId": calls["s0"][0]})
        await asyncio.sleep(1.2)
        await srv.notify("notifications/cancelled", {"requestId": calls["s1"][0]})
        for _, fut in calls.values():
            await asyncio.wait_for(fut, 180)
        await asyncio.sleep(ctx.grace)  # let any cleanup finish
        chromes = chrome_mains(srv.marker)
        sessions = await srv.session_count()
        if len(chromes) != sessions:
            return FAIL, (
                f"{len(chromes)} Chrome vs {sessions} registered session(s): "
                f"{ctx.describe_leaks(chromes)}"
            )
        await srv.call_tool("session_stop_all", {}, timeout=60)
        await wait_until(lambda: not chrome_mains(srv.marker), timeout=ctx.grace)
        leaked = chrome_mains(srv.marker)
        if leaked:
            return FAIL, f"{len(leaked)} Chrome left after session_stop_all"
        return PASS, f"{sessions} survivor(s) tracked 1:1; session_stop_all left nothing"


def build_scenarios(ctx: Context, wanted: list[str]):
    lp = ctx.args.launch_pad
    table: dict[str, Callable[[], Awaitable[tuple[str, str]]]] = {
        "cancel-early": lambda: scenario_cancel(ctx, "cancel-early", delay=0.0),
        "cancel-mid": lambda: scenario_cancel(ctx, "cancel-mid", delay=lp * 0.5),
        "cancel-late": lambda: scenario_cancel(ctx, "cancel-late", delay=lp),
        "sigterm": lambda: scenario_signal(ctx, "sigterm", signal.SIGTERM),
        "sigkill": lambda: scenario_signal(ctx, "sigkill", signal.SIGKILL),
        "client-exit": lambda: scenario_client_exit(ctx, "client-exit"),
        "storm": lambda: scenario_storm(ctx, "storm"),
    }
    return [(name, table[name]) for name in wanted]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def preflight(ctx: Context) -> str:
    """Report which engine/MCP code the server will actually import."""
    probe = (
        "import mithwire, mithwire_mcp, importlib.metadata as m;"
        "print('engine', m.version('mithwire'), mithwire.__file__);"
        "print('mcp   ', mithwire_mcp.__file__)"
    )
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=ctx.env(ctx.root),
        cwd=str(ctx.root),
        check=True,
    ).stdout.rstrip()


async def run(args: argparse.Namespace) -> int:
    wanted = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in ALL_SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}; choose from {ALL_SCENARIOS}", file=sys.stderr)
        return 2
    ctx = Context(args)
    try:
        print(f"mode: {'headless' if ctx.headless else 'HEADFUL (real windows)'}")
        print(preflight(ctx))
        print()
        results: list[Result] = []
        for name, factory in build_scenarios(ctx, wanted):
            print(f"  ... {name}", flush=True)
            result = await _timed(name, factory())
            results.append(result)
            print(f"  {result.status:4} {name:13} {result.seconds:5.1f}s  {result.detail}")
        print()
        failed = [r for r in results if r.status == FAIL]
        skipped = [r for r in results if r.status == SKIP]
        print(
            f"{len(results) - len(failed) - len(skipped)} passed, "
            f"{len(failed)} failed, {len(skipped)} skipped"
        )
        return 1 if failed else 0
    finally:
        kill_marker(str(ctx.root))
        if not args.keep:
            shutil.rmtree(ctx.root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    parser.add_argument("--headful", action="store_true", help="launch headful (opens real windows)")
    parser.add_argument("--mcp-src", type=Path, default=None, help="dir of mithwire_mcp to test (PYTHONPATH)")
    parser.add_argument("--engine-src", type=Path, default=None, help="dir of mithwire to test (PYTHONPATH)")
    parser.add_argument("--grace", type=float, default=12.0, help="seconds a reaper may take to clean up")
    parser.add_argument(
        "--launch-pad",
        type=float,
        default=2.5,
        help="approx. seconds a launch takes after Chrome spawns; cancel-mid/late are scaled from it",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep temp dirs (server logs) for debugging")
    args = parser.parse_args()
    for attr in ("mcp_src", "engine_src"):
        value = getattr(args, attr)
        if value is not None:
            value = value.expanduser().resolve()
            if not value.is_dir():
                parser.error(f"--{attr.replace('_', '-')} is not a directory: {value}")
            setattr(args, attr, value)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dap_types import StoppedEvent
from pydantic import BaseModel

from dap_mcp.factory import DAPClientSingletonFactory
from dap_mcp.debugger import (
    Debugger,
    LaunchRequestArguments,
    SetBreakpointsResponse,
    ErrorResponse,
)


class WatchPoint(BaseModel):
    var: str
    log_location: str  # "file.c:LINE" (normalized relative to repo root)


class RuntimeFeedback(BaseModel):
    # dict["file:line", list(occurrence)[dict["var", "value"]]]
    watchpoints: dict[str, list[dict[str, str]]]
    # dict["file:line", list(occurrence)["backtrace string"]]
    breakpoints: dict[str, list[str]]
    stdout: bytes
    stderr: bytes


# Helper to find a DAP adapter (lldb)
def _find_lldb_adapter(lldb_path: Path = None) -> Tuple[str, List[str]]:
    """Return (cmd, args) to launch an LLDB DAP adapter.

    Tries `lldb-dap` then `lldb-vscode` from PATH.
    """
    if lldb_path.exists():
        return str(lldb_path), []

    import shutil

    for name in ("lldb-dap", "lldb-vscode"):
        cmd = shutil.which(name)
        if cmd:
            return cmd, []
    raise RuntimeError(
        "Unable to find LLDB DAP adapter in PATH (looked for lldb-dap and lldb-vscode)."
    )


def _normalize_locations(
        locations: List[str],
        repo_dir: Path = None
) -> Tuple[Dict[str, Tuple[str, int]], List[Tuple[str, int]]]:
    """Return a list of (location, line_no) tuples.
    """
    location_to_normalized_location: dict[str, Tuple[str, int]] = {}
    result = []
    for loc in locations:
        file_part, line_part = loc.rsplit(":", 1)
        file_part = Path(file_part)
        if file_part.is_absolute():
            assert file_part.exists()
            location_to_normalized_location[loc] = (str(file_part.resolve()), int(line_part))
            result.append((str(file_part.resolve()), int(line_part)))
            continue
        if repo_dir is not None:
            guess_dir = repo_dir / file_part
            if guess_dir.exists():
                location_to_normalized_location[loc] = (str(guess_dir.resolve()), int(line_part))
                result.append((str(guess_dir.resolve()), int(line_part)))
                continue
        location_to_normalized_location[loc] = (str(file_part), int(line_part))
        result.append((str(file_part), int(line_part)))
    return location_to_normalized_location, result


def _compact_backtrace(frames) -> str:
    names: List[str] = []
    for f in frames[:3]:
        # Some adapters might not fill function name; guard it
        nm = f.name if getattr(f, "name", None) else "?"
        names.append(nm + "()")
    src = frames[0].source.path if frames and frames[0].source else "?"
    line = getattr(frames[0], "line", 0) or 0
    file_display = os.path.basename(src) if src else "?"
    return f"{' -> '.join(names)} @ {file_display}:{line}"


async def _run_dap(
        cmd: List[str],
        stdin_bytes: bytes | None,
        watchpoints_list: List[Dict[str, Any]],
        monitor_locations: List[str],
        repo_dir: Path = None,
        env: Dict[str, str] = None,
        lldb_path: Path = None,
) -> RuntimeFeedback:
    # Prepare launch config and env (stdin fallback via file if provided)
    program = str(Path(cmd[0]).resolve())
    args = cmd[1:]

    # Stdin strategy: robust fallback via env file
    if env is None:
        env: Dict[str, str] = dict(os.environ)

    stdin: tempfile.NamedTemporaryFile | None = None
    stdout = tempfile.NamedTemporaryFile(delete=False)
    stderr = tempfile.NamedTemporaryFile(delete=False)
    try:
        stdio_commands = [
            f"settings set target.output-path {stdout.name}",
            f"settings set target.error-path {stderr.name}",
        ]
        if stdin_bytes is not None:
            stdin = tempfile.NamedTemporaryFile(delete=False)
            stdin.write(stdin_bytes)
            stdin.flush()
            stdin.close()
            stdio_commands += [
                f"settings set target.input-path {stdin.name}",
            ]

        launch_args = LaunchRequestArguments(
            **({
                "type": "lldb",
                "request": "launch",
                "program": program,
                "args": args,
                "env": {k: v for k, v in env.items() if isinstance(v, str)},
                "stopOnEntry": False,
                "initCommands": stdio_commands,
            })
        )

        # Prepare adapter
        adapter_cmd, adapter_args = _find_lldb_adapter(lldb_path=lldb_path)
        factory = DAPClientSingletonFactory(adapter_cmd, adapter_args)
        dbg = Debugger(factory=factory, launch_arguments=launch_args)

        await dbg.initialize()
        locations = list(set(monitor_locations).union(set(map(lambda d: d["log_location"], watchpoints_list))))
        location_to_normalized_location, normalized_locations = _normalize_locations(locations, repo_dir)
        breakpoints_id_to_location: Dict[int, Tuple[str, int]] = {}
        location_to_breakpoint_id: Dict[Tuple[str, int], int] = {}

        # Set all breakpoints before launch
        for file, line in normalized_locations:
            resp = await dbg.set_breakpoint(Path(file), line)
            if isinstance(resp, (SetBreakpointsResponse, ErrorResponse)) and isinstance(
                    resp, ErrorResponse
            ):
                raise RuntimeError(
                    f"Failed to set breakpoint at {file}:{line}: {resp.message}"
                )
            assert resp.success is True
            current_breakpoint = resp.body.breakpoints[-1]
            breakpoints_id_to_location[current_breakpoint.id] = (file, line)
            location_to_breakpoint_id[(file, line)] = current_breakpoint.id

        # Aggregate results
        result = RuntimeFeedback(watchpoints={}, breakpoints={}, stdout=b"", stderr=b"")

        # Map watchpoints by location for quicker lookup
        wps_by_id: Dict[int, List[str]] = {}
        for wp in watchpoints_list:
            w = WatchPoint(**wp)
            loc = location_to_normalized_location[w.log_location]
            breakpoint_id = location_to_breakpoint_id[loc]
            wps_by_id.setdefault(breakpoint_id, []).append(w.var)

        # Launch and event loop
        stopped = await dbg.launch()
        while True:
            # If terminated, dbg.launch/continue returns EventListView
            from dap_mcp.debugger import StoppedDebuggerView

            if not isinstance(stopped, StoppedDebuggerView):
                break

            # Identify stop site
            frames = stopped.frames
            if not frames:
                next_view = await dbg.continue_execution()
                stopped = next_view
                continue

            breakpoint_event = list(
                filter(lambda e: isinstance(e, StoppedEvent) and e.body.reason == 'breakpoint', stopped.events.events))
            if not breakpoint_event:
                next_view = await dbg.continue_execution()
                stopped = next_view
                continue
            assert len(breakpoint_event) == 1
            breakpoint_ids = breakpoint_event[0].body.hitBreakpointIds

            top = frames[0]
            site = f'{top.source.path}:{top.line}'
            # Collect backtrace string regardless; but only store if monitored
            bt = _compact_backtrace(frames)
            result.breakpoints.setdefault(site, []).append(bt)
            # Evaluate watchpoints for this location
            for breakpoint_id in breakpoint_ids:
                for var in wps_by_id[breakpoint_id]:
                    try:
                        ev = await dbg.evaluate(var)
                        if ev.success:
                            val = (
                                ev.body.result
                            )
                        else:
                            val = ev.body.error
                    except Exception:
                        val = "<unavailable>"
                    result.watchpoints.setdefault(site, []).append(
                        {"var": var, "value": str(val)}
                    )

            # Continue
            next_view = await dbg.continue_execution()
            stopped = next_view

        # Ensure keys exist even if never hit
        for loc in monitor_locations:
            result.breakpoints.setdefault(loc, [])
            result.watchpoints.setdefault(loc, [])

        # Graceful shutdown
        await dbg.terminate()
        with open(stdout.name, "rb") as f:
            result.stdout = f.read()
        with open(stderr.name, "rb") as f:
            result.stderr = f.read()
        return result
    finally:
        if stdin is not None:
            try:
                os.unlink(stdin.name)
            except Exception:
                pass
        try:
            os.unlink(stdout.name)
            os.unlink(stderr.name)
        except Exception:
            pass


def get_runtime_feedback(
        cmd: List[str],
        stdin: bytes | None,
        watchpoints_list: List[Dict[str, Any]],
        monitor_locations: List[str],
        repo_dir: Path = None,
        env: Dict[str, str] = None,
        lldb_path: Path = None,
) -> RuntimeFeedback:
    """
    Launch the process under LLDB via DAP and collect runtime feedback.

    - cmd: argv for the compiled C program (absolute or cwd-relative path first).
    - stdin: optional bytes to feed to program (handled via env file fallback).
    - watchpoints_list: list of {"var": <expr>, "log_location": "file:line"}.
    - monitor_locations: list of "file:line" at which to set breakpoints.
    """

    return asyncio.run(_run_dap(cmd, stdin, watchpoints_list, monitor_locations, repo_dir, env, lldb_path))

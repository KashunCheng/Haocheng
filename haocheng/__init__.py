from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


# Helper to find a DAP adapter (lldb)
def _find_lldb_adapter() -> Tuple[str, List[str]]:
    """Return (cmd, args) to launch an LLDB DAP adapter.

    Tries `lldb-dap` then `lldb-vscode` from PATH.
    """
    import shutil

    for name in ("lldb-dap", "lldb-vscode"):
        cmd = shutil.which(name)
        if cmd:
            return cmd, []
    raise RuntimeError(
        "Unable to find LLDB DAP adapter in PATH (looked for lldb-dap and lldb-vscode)."
    )


def _normalize_locations(
        monitor_locations: List[str],
) -> Tuple[Dict[Tuple[str, int], str], Dict[str, List[int]]]:
    """Return mappings for locations.

    - abs_to_rel[(abs_path, line)] -> rel_key ("file:line")
    - file_to_lines[abs_path] -> [line,...] for setBreakpoints grouping
    """
    abs_to_rel: Dict[Tuple[str, int], str] = {}
    file_to_lines: Dict[str, List[int]] = {}
    for loc in monitor_locations:
        file_part, line_part = loc.rsplit(":", 1)
        rel_key = f"{file_part}:{int(line_part)}"
        abs_path = str(Path(file_part).resolve())
        line = int(line_part)
        abs_to_rel[(abs_path, line)] = rel_key
        file_to_lines.setdefault(abs_path, [])
        if line not in file_to_lines[abs_path]:
            file_to_lines[abs_path].append(line)
    return abs_to_rel, file_to_lines


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
        cwd: Path = None,
        env: Dict[str, str] = None
) -> RuntimeFeedback:
    # Prepare launch config and env (stdin fallback via file if provided)
    program = str(Path(cmd[0]).resolve())
    args = cmd[1:]
    if cwd is None:
        cwd = str(Path.cwd())

    # Stdin strategy: robust fallback via env file
    if env is None:
        env: Dict[str, str] = dict(os.environ)

    tmp_file: tempfile.NamedTemporaryFile | None = None
    try:
        stdio_commands = []
        if stdin_bytes is not None:
            tmp_file = tempfile.NamedTemporaryFile(delete=False)
            tmp_file.write(stdin_bytes)
            tmp_file.flush()
            tmp_file.close()
            stdio_commands = [
                f"settings set target.input-path {tmp_file.name}",
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
        adapter_cmd, adapter_args = _find_lldb_adapter()
        factory = DAPClientSingletonFactory(adapter_cmd, adapter_args)
        dbg = Debugger(factory=factory, launch_arguments=launch_args)

        await dbg.initialize()

        abs_to_rel, file_to_lines = _normalize_locations(monitor_locations)

        # Set all breakpoints before launch
        for abs_file, lines in file_to_lines.items():
            for line in lines:
                resp = await dbg.set_breakpoint(Path(abs_file), line)
                if isinstance(resp, (SetBreakpointsResponse, ErrorResponse)) and isinstance(
                        resp, ErrorResponse
                ):
                    raise RuntimeError(
                        f"Failed to set breakpoint at {abs_file}:{line}: {resp.message}"
                    )

        # Aggregate results
        result = RuntimeFeedback(watchpoints={}, breakpoints={})

        # Map watchpoints by location for quicker lookup
        wps_by_loc: Dict[str, List[WatchPoint]] = {}
        for wp in watchpoints_list:
            w = WatchPoint(**wp)
            key = f"{w.log_location.split(':')[0]}:{int(w.log_location.split(':')[1])}"
            wps_by_loc.setdefault(key, []).append(w)

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

            top = frames[0]
            src_path = (
                str(Path(top.source.path).resolve()) if top.source and top.source.path else ""
            )
            site = (src_path, int(getattr(top, "line", 0) or 0))
            rel_key = abs_to_rel.get(site)
            # Collect backtrace string regardless; but only store if monitored
            bt = _compact_backtrace(frames)
            if rel_key:
                result.breakpoints.setdefault(rel_key, []).append(bt)
                # Evaluate watchpoints for this location
                for w in wps_by_loc.get(rel_key, []):
                    try:
                        ev = await dbg.evaluate(w.var)
                        val = (
                            ev.body.result
                            if hasattr(ev, "body") and hasattr(ev.body, "result")
                            else "<unavailable>"
                        )
                    except Exception:
                        val = "<unavailable>"
                    result.watchpoints.setdefault(rel_key, []).append(
                        {"var": w.var, "value": str(val)}
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
        return result
    finally:
        if tmp_file is not None:
            try:
                os.unlink(tmp_file.name)
            except Exception:
                pass


def get_runtime_feedback(
        cmd: List[str],
        stdin: bytes | None,
        watchpoints_list: List[Dict[str, Any]],
        monitor_locations: List[str],
) -> RuntimeFeedback:
    """
    Launch the process under LLDB via DAP and collect runtime feedback.

    - cmd: argv for the compiled C program (absolute or cwd-relative path first).
    - stdin: optional bytes to feed to program (handled via env file fallback).
    - watchpoints_list: list of {"var": <expr>, "log_location": "file:line"}.
    - monitor_locations: list of "file:line" at which to set breakpoints.
    """

    return asyncio.run(_run_dap(cmd, stdin, watchpoints_list, monitor_locations))

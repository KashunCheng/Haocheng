from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

try:
    from dap_types import StoppedEvent
    from pydantic import BaseModel, Field
    from dap_mcp.factory import DAPClientSingletonFactory
    from dap_mcp.debugger import (
        Debugger,
        LaunchRequestArguments,
        SetBreakpointsResponse,
        ErrorResponse,
    )

    DAP_AVAILABLE = True
except ImportError as e:
    # Handle missing dependencies gracefully
    print(f"Warning: Missing dependencies for DAP debugging: {e}")
    DAP_AVAILABLE = False


    # Create placeholder classes
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)


    StoppedEvent = None
    DAPClientSingletonFactory = None
    Debugger = None
    LaunchRequestArguments = None
    SetBreakpointsResponse = None
    ErrorResponse = None
    def Field(**kwargs):
        return []


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


# New Pydantic models for the updated API schema
class BreakpointSpec(BaseModel):
    """Breakpoint request schema.

    location: "filename:line"
    hit_limit: integer (default 10)
    inline_expr: optional list of expressions to evaluate on hit
    print_call_stack: whether to include call stack string per hit
    """

    location: str
    hit_limit: int = 10
    inline_expr: list[str] = Field(default_factory=list)
    print_call_stack: bool = False


class InlineExprValue(BaseModel):
    name: str
    value: str


class BreakpointHitInfo(BaseModel):
    callstack: str
    inline_expr: list[InlineExprValue]


class BreakpointReport(BaseModel):
    id: int
    file_path: str
    line: int
    function_name: str
    hit_times: int
    hits_info: list[BreakpointHitInfo]


class RuntimeFeedbackV2(BaseModel):
    """Output schema for get_runtime_feedback (new format)."""

    stderr: str
    exit_code: int
    signal: int
    breakpoints: list[BreakpointReport]


def _convert_breakpoint_specs(
    specs: List[Dict[str, Any]] | List[BreakpointSpec],
) -> Tuple[List[Dict[str, str]], List[str], Dict[str, int], Dict[str, bool]]:
    """Convert BreakpointSpec list into internal watchpoints + monitor locations.

    Returns:
    - watchpoints_list: list of {"var", "log_location"}
    - monitor_locations: list of "file:line" locations (deduplicated, insertion order preserved)
    - hit_limit_by_loc: dict mapping location -> hit_limit
    - stack_flag_by_loc: dict mapping location -> print_call_stack
    """
    wp_list: List[Dict[str, str]] = []
    monitor_locations: List[str] = []
    seen: set[str] = set()
    hit_limit_by_loc: Dict[str, int] = {}
    stack_flag_by_loc: Dict[str, bool] = {}

    for item in specs:
        # Allow dicts or model instances
        if isinstance(item, dict):
            bp = BreakpointSpec(**item)
        else:
            bp = item  # type: ignore[assignment]

        loc = bp.location
        if loc not in seen:
            monitor_locations.append(loc)
            seen.add(loc)
        hit_limit_by_loc[loc] = int(bp.hit_limit) if bp.hit_limit is not None else 10
        stack_flag_by_loc[loc] = bool(bp.print_call_stack)

        if bp.inline_expr:
            for expr in bp.inline_expr:
                wp_list.append({"var": str(expr), "log_location": loc})

    return wp_list, monitor_locations, hit_limit_by_loc, stack_flag_by_loc


# Helper to find a DAP adapter (lldb)
def _find_lldb_adapter(lldb_path: Optional[Path] = None) -> Tuple[str, List[str]]:
    """Return (cmd, args) to launch an LLDB DAP adapter.

    Tries `lldb-dap` then `lldb-vscode` from PATH.
    """
    # If a path was provided, prefer an adapter next to it
    if lldb_path:
        p = Path(lldb_path)
        # If user already pointed at an adapter, use it
        if p.name in ("lldb-dap", "lldb-vscode") and p.exists():
            return str(p), []
        # Otherwise try siblings in the same directory
        for name in ("lldb-dap", "lldb-vscode"):
            cand = p.parent / name
            if cand.exists():
                return str(cand), []
        # Fall through to PATH search

    # Search PATH
    for name in ("lldb-dap", "lldb-vscode"):
        cmd = shutil.which(name)
        if cmd:
            return cmd, []

    raise RuntimeError(
        "Unable to find LLDB DAP adapter. Install lldb-dap or lldb-vscode and ensure it is on PATH."
    )


def _normalize_locations(
        locations: List[str],
        repo_dir: Optional[Path] = None
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
        stdin_bytes: Optional[bytes],
        watchpoint_locations: List[Dict[str, Any]],
        breakpoint_locations: List[str],
        repo_dir: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        lldb_path: Optional[Path] = None,
) -> RuntimeFeedback:
    if not DAP_AVAILABLE:
        raise RuntimeError(
            "DAP debugging is not available. Please install required dependencies (dap_types, pydantic, dap_mcp).")
    # Prepare launch config and env (stdin fallback via file if provided)
    program = str(Path(cmd[0]).resolve())
    args = cmd[1:]

    # Stdin strategy: robust fallback via env file
    if env is None:
        env_dict: Dict[str, str] = dict(os.environ)
    else:
        env_dict = env

    stdin: Optional[tempfile._TemporaryFileWrapper] = None
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

        assert LaunchRequestArguments is not None, "DAP classes not available"
        launch_args = LaunchRequestArguments(
            **({
                "type": "lldb",
                "request": "launch",
                "program": program,
                "args": args,
                "env": {k: v for k, v in env_dict.items() if isinstance(v, str)},
                "stopOnEntry": False,
                "initCommands": stdio_commands,
            })
        )

        # Prepare adapter
        adapter_cmd, adapter_args = _find_lldb_adapter(lldb_path=lldb_path)
        assert DAPClientSingletonFactory is not None and Debugger is not None, "DAP classes not available"
        factory = DAPClientSingletonFactory(adapter_cmd, adapter_args)
        dbg = Debugger(factory=factory, launch_arguments=launch_args)

        await dbg.initialize()
        locations = list(set(breakpoint_locations).union(set(map(lambda d: d["log_location"], watchpoint_locations))))
        location_to_normalized_location, normalized_locations = _normalize_locations(locations, repo_dir)
        breakpoints_id_to_location: Dict[int, Tuple[str, int]] = {}
        location_to_breakpoint_id: Dict[Tuple[str, int], int] = {}

        # Set all breakpoints before launch
        for file, line in normalized_locations:
            resp = await dbg.set_breakpoint(Path(file), line)
            # Check for error response if DAP classes are available
            if DAP_AVAILABLE and SetBreakpointsResponse and ErrorResponse:
                if isinstance(resp, (SetBreakpointsResponse, ErrorResponse)) and isinstance(
                        resp, ErrorResponse
                ):
                    raise RuntimeError(
                        f"Failed to set breakpoint at {file}:{line}: {resp.message}"
                    )
            if hasattr(resp, 'success') and not resp.success:
                raise RuntimeError(f"Failed to set breakpoint at {file}:{line}")
            assert resp.success is True
            current_breakpoint = resp.body.breakpoints[-1]
            breakpoints_id_to_location[current_breakpoint.id] = (file, line)
            location_to_breakpoint_id[(file, line)] = current_breakpoint.id

        # Aggregate results
        result = RuntimeFeedback(watchpoints={}, breakpoints={}, stdout=b"", stderr=b"")

        # Map watchpoints by location for quicker lookup
        wps_by_id: Dict[int, List[str]] = {}
        for wp in watchpoint_locations:
            w = WatchPoint(**wp)
            loc = location_to_normalized_location[w.log_location]
            breakpoint_id = location_to_breakpoint_id[loc]
            wps_by_id.setdefault(breakpoint_id, []).append(w.var)

        # Launch and event loop
        stopped = await dbg.launch()
        while True:
            # If terminated, dbg.launch/continue returns EventListView
            try:
                from dap_mcp.debugger import StoppedDebuggerView
                if not isinstance(stopped, StoppedDebuggerView):
                    break
            except ImportError:
                # Without DAP classes, we can't do proper type checking
                # Assume stopped is valid if it has required attributes
                if not hasattr(stopped, 'frames') or not hasattr(stopped, 'events'):
                    break

            # Identify stop site
            frames = stopped.frames
            if not frames:
                next_view = await dbg.continue_execution()
                stopped = next_view
                continue

            # Filter for breakpoint events, handle missing StoppedEvent class
            if DAP_AVAILABLE and StoppedEvent is not None:
                # Store StoppedEvent to avoid linter warnings about None
                stopped_event_cls = StoppedEvent

                def is_breakpoint_event(e):
                    return isinstance(e, stopped_event_cls) and hasattr(e, 'body') and hasattr(e.body,
                                                                                               'reason') and e.body.reason == 'breakpoint'

                breakpoint_event = list(filter(is_breakpoint_event, stopped.events.events))
            else:
                # Fallback: check by attributes
                breakpoint_event = list(
                    filter(lambda e: hasattr(e, 'body') and hasattr(e.body, 'reason') and e.body.reason == 'breakpoint',
                           stopped.events.events))
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
        for loc in breakpoint_locations:
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


class RuntimeDebugger:
    """Runtime debugger class for collecting runtime feedback via LLDB DAP."""

    def __init__(self, config=None):
        """Initialize the RuntimeDebugger with configuration."""
        self.lldb_path: Optional[Path] = None
        self.env: Optional[Dict[str, str]] = None
        self.repo_dir: Optional[Path] = None

        if config:
            self._load_from_config(config)
        else:
            self._load_default_config()

    def _load_from_config(self, config):
        """Load configuration from config object."""
        # Load lldb_path
        if config.lldb_path:
            path = Path(config.lldb_path)
            if path.exists():
                self.lldb_path = path
            else:
                print(f"Warning: lldb_path {config.lldb_path} does not exist, using auto-detection")
                self.lldb_path = self._auto_find_lldb_path()
        else:
            # No lldb_path configured, use auto-detection
            self.lldb_path = self._auto_find_lldb_path()

        # Load env
        if config.debugger_env:
            self.env = config.debugger_env.copy()

        # Auto-update PATH with lldb directory if lldb_path was auto-detected
        self._update_path_with_lldb()

        # Load repo_dir
        if config.source_code_dir:
            path = Path(config.source_code_dir)
            if path.exists() and path.is_dir():
                self.repo_dir = path
            else:
                print(f"Warning: repo_dir {config.source_code_dir} does not exist or is not a directory")
                self.repo_dir = None

    def _auto_find_lldb_path(self) -> Optional[Path]:
        """Auto-detect lldb path from /usr/bin/lldb*, preferring highest version."""
        import glob
        import re

        # Find all lldb binaries in /usr/bin/
        lldb_paths = glob.glob('/usr/bin/lldb*')
        if not lldb_paths:
            return None

        # Filter to main lldb binaries (exclude lldb-argdumper, lldb-server, etc.)
        main_lldb_paths = []
        for path in lldb_paths:
            filename = Path(path).name
            # Match lldb or lldb-<version>
            if re.match(r'^lldb(-\d+)?$', filename):
                main_lldb_paths.append(path)

        if not main_lldb_paths:
            return None

        # Sort by version number (highest first)
        def version_key(path):
            filename = Path(path).name
            # Extract version number, default to 0 for plain 'lldb'
            match = re.search(r'-(\d+)$', filename)
            return int(match.group(1)) if match else 0

        # Sort in descending order (highest version first)
        main_lldb_paths.sort(key=version_key, reverse=True)

        # Return the highest version that exists and is executable
        for path in main_lldb_paths:
            lldb_path = Path(path)
            if lldb_path.exists() and lldb_path.is_file():
                # Resolve symlinks to get the actual executable
                try:
                    resolved_path = lldb_path.resolve()
                    if resolved_path.exists() and resolved_path.is_file():
                        print(f"Auto-detected lldb path: {resolved_path}")
                        return resolved_path
                except Exception:
                    continue

        return None

    def _update_path_with_lldb(self):
        """Update PATH environment variable to include lldb directory."""
        if not self.lldb_path:
            return

        # Get the directory containing the lldb executable
        lldb_dir = str(self.lldb_path.parent)

        # Initialize env if it's None
        if self.env is None:
            self.env = os.environ.copy()

        # Get current PATH from env
        current_path = self.env.get('PATH', '')

        # Check if lldb_dir is already in PATH
        path_dirs = current_path.split(os.pathsep) if current_path else []
        if lldb_dir not in path_dirs:
            # Prepend lldb_dir to PATH (like export PATH=/usr/lib/llvm-20/bin:$PATH)
            new_path = lldb_dir + (os.pathsep + current_path if current_path else '')
            self.env['PATH'] = new_path

    def _load_default_config(self):
        """Load default configuration when no config is provided."""
        self.lldb_path = self._auto_find_lldb_path()  # Auto-detect lldb path
        self.env = None  # Will use os.environ
        self.repo_dir = None  # Will be determined at runtime

        # Auto-update PATH with lldb directory
        self._update_path_with_lldb()

    async def _run_dap_async(
            self,
            cmd: List[str],
            stdin_bytes: Optional[bytes],
            watchpoint_locations: List[Dict[str, Any]],
            breakpoint_locations: List[str],
    ) -> RuntimeFeedback:
        """Async version of runtime debugging."""
        return await _run_dap(
            cmd,
            stdin_bytes,
            watchpoint_locations,
            breakpoint_locations,
            self.repo_dir,
            self.env,
            self.lldb_path
        )

    def get_runtime_feedback(
            self,
            cmd: List[str],
            stdin: Optional[bytes] = None,
            timeout_sec: Optional[int] = None,  # currently unused; reserved for future timeouts
            breakpoints: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Run program under LLDB DAP and return feedback in the new schema.

        Args:
        - cmd: program argv (first item is path to binary)
        - stdin: optional bytes to feed to the program
        - timeout_sec: optional timeout (not enforced yet)
        - breakpoints: list of breakpoint specs matching BreakpointSpec

        Returns a dict matching RuntimeFeedbackV2 schema.
        """
        if not breakpoints:
            breakpoints = []

        # Convert new breakpoint schema into internal watchpoints and monitor locations
        wp_list, monitor_locations, hit_limit_by_loc, stack_flag_by_loc = _convert_breakpoint_specs(breakpoints)

        # Execute via existing DAP runner to collect raw data
        raw = asyncio.run(self._run_dap_async(cmd, stdin, wp_list, monitor_locations))

        # Build report per monitor location
        reports: List[BreakpointReport] = []

        # Helper to parse file path and line from location string
        def parse_loc(loc: str) -> tuple[str, int]:
            try:
                f, ln = loc.rsplit(":", 1)
                return f, int(ln)
            except Exception:
                return loc, 0

        # Helper to extract function name from compact backtrace
        def fn_from_bt(bt: str) -> str:
            # Expected format: "f1() -> f2() -> f3() @ file:line"
            try:
                head = bt.split(" @ ", 1)[0]
                first = head.split(" -> ", 1)[0]
                return first.rstrip("()").strip()
            except Exception:
                return ""

        # For deterministic ordering, iterate using the input monitor_locations
        for idx, loc in enumerate(monitor_locations, start=1):
            bts = raw.breakpoints.get(loc) or []
            file_path, line_no = parse_loc(loc)
            hit_limit = hit_limit_by_loc.get(loc, 10)
            want_stack = stack_flag_by_loc.get(loc, False)

            # Inline expr names configured for this location
            expr_names = [wp["var"] for wp in wp_list if wp["log_location"] == loc]
            per_hit_expr_count = len(expr_names)

            # Collect inline expr values grouped per hit
            flat_values = [e for e in (raw.watchpoints.get(loc) or []) if e.get("var") in expr_names]
            grouped_values: List[List[InlineExprValue]] = []
            if per_hit_expr_count > 0:
                # Chunk the flat list into groups of per_hit_expr_count
                for start in range(0, min(len(flat_values), hit_limit * per_hit_expr_count), per_hit_expr_count):
                    chunk = flat_values[start:start + per_hit_expr_count]
                    grouped_values.append([
                        InlineExprValue(name=str(v.get("var", "")), value=str(v.get("value", ""))) for v in chunk
                    ])
            else:
                # No inline expressions; still may have hits
                for _ in range(min(len(bts), hit_limit)):
                    grouped_values.append([])

            # Align callstacks with grouped values by hit index
            hits_info: List[BreakpointHitInfo] = []
            total_hits = min(len(bts), hit_limit)
            for i in range(total_hits):
                callstack_str = bts[i] if want_stack and i < len(bts) else ""
                exprs = grouped_values[i] if i < len(grouped_values) else []
                hits_info.append(BreakpointHitInfo(callstack=callstack_str, inline_expr=exprs))

            function_name = fn_from_bt(bts[0]) if bts else ""
            report = BreakpointReport(
                id=idx,
                file_path=file_path,
                line=line_no,
                function_name=function_name,
                hit_times=total_hits,
                hits_info=hits_info,
            )
            reports.append(report)

        # Prepare final output; exit_code/signal are currently 0 until extended
        out = RuntimeFeedbackV2(
            stderr=(raw.stderr.decode(errors="replace") if isinstance(raw.stderr, (bytes, bytearray)) else str(raw.stderr)),
            exit_code=0,
            signal=0,
            breakpoints=reports,
        )
        return out.model_dump()

from __future__ import annotations

try:
    import asyncio
    import time
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from typing import Any, Dict, List, Tuple, Optional

    from dap_types import StoppedEvent, StackFrame
    from pydantic import BaseModel, Field
    from dap_mcp.factory import DAPClientSingletonFactory
    from dap_mcp.debugger import (
        Debugger,
        LaunchRequestArguments,
        SetBreakpointsResponse,
        ErrorResponse, StoppedDebuggerView,
)


    class WatchPoint(BaseModel):
        var: str
        log_location: str  # "file.c:LINE" (normalized relative to repo root)


    class RuntimeFeedback(BaseModel):
        stdout: bytes
        stderr: bytes
        reports: dict[int, BreakpointReport]


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

        @property
        def file_path(self) -> str:
            fp, _ = self.location.rsplit(":", 1)
            return fp

        @property
        def line_no(self) -> int:
            _, ln = self.location.rsplit(":", 1)
            return int(ln)


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
            breakpoints: List[BreakpointSpec], repo_dir: Optional[Path] = None
    ) -> None:
        """Normalize BreakpointSpec locations to absolute file paths in place."""
        for bp in breakpoints:
            loc = bp.location
            file_str, line_part = loc.rsplit(":", 1)
            file_path = Path(file_str)
            if file_path.is_absolute():
                if file_path.exists():
                    bp.location = f"{file_path}:{line_part}"
                    continue
            if repo_dir is not None:
                guess = repo_dir / file_path
                if guess.exists():
                    bp.location = f"{guess}:{line_part}"
                    continue


    def _format_single_frame(frame: StackFrame) -> str:
        if frame.source:
            return f"{frame.name} at {frame.source.name}:{frame.line}"
        return f"{frame.name}"


    def _compact_backtrace(frames: list[StackFrame]) -> str:
        backtrace = []
        for fid, frame in enumerate(frames):
            backtrace.append(f"{'*' if fid == 0 else ' '} #{fid}: {_format_single_frame(frame)}")
        return "\n".join(backtrace)


    async def _run_dap(
            cmd: List[str],
            stdin_bytes: Optional[bytes],
            breakpoints: List[BreakpointSpec],
            repo_dir: Optional[Path] = None,
            env: Optional[Dict[str, str]] = None,
            lldb_path: Optional[Path] = None,
            timeout_sec: Optional[float] = None,
    ) -> RuntimeFeedback:
        # Aggregate results
        result = RuntimeFeedback(stdout=b"", stderr=b"", reports={})

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

            launch_args = LaunchRequestArguments(
                **(
                    {
                        "type": "lldb",
                        "request": "launch",
                        "program": program,
                        "args": args,
                        "env": {
                            k: v for k, v in env_dict.items() if isinstance(v, str)
                        },
                        "stopOnEntry": False,
                        "initCommands": stdio_commands,
                    }
                )
            )

            # Prepare adapter
            adapter_cmd, adapter_args = _find_lldb_adapter(lldb_path=lldb_path)
            factory = DAPClientSingletonFactory(adapter_cmd, adapter_args)
            dbg = Debugger(factory=factory, launch_arguments=launch_args)

            await dbg.initialize()
            # Normalize and set breakpoints for all locations provided in specs
            _normalize_locations(breakpoints, repo_dir)
            id_to_spec: Dict[int, BreakpointSpec] = {}

            for spec in breakpoints:
                resp = await dbg.set_breakpoint(Path(spec.file_path), spec.line_no)
                if isinstance(
                        resp, (SetBreakpointsResponse, ErrorResponse)
                ) and isinstance(resp, ErrorResponse):
                    raise RuntimeError(
                        f"Failed to set breakpoint at {spec.location}: {resp.message}"
                    )
                if hasattr(resp, "success") and not resp.success:
                    raise RuntimeError(f"Failed to set breakpoint at {spec.location}")
                assert resp.success is True
                current_breakpoint = resp.body.breakpoints[-1]
                id_to_spec[current_breakpoint.id] = spec
                result.reports[current_breakpoint.id] = BreakpointReport(
                    id=current_breakpoint.id,
                    file_path=spec.file_path,
                    line=spec.line_no,
                    function_name="",
                    hit_times=0,
                    hits_info=[],
                )

            # Launch and event loop with timeout tracking
            start_ts = time.monotonic()

            async def _with_timeout(awaitable):
                if timeout_sec is None:
                    return await awaitable
                elapsed = time.monotonic() - start_ts
                remaining = timeout_sec - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                return await asyncio.wait_for(awaitable, timeout=remaining)

            try:
                stopped = await _with_timeout(dbg.launch())
            except asyncio.TimeoutError:
                stopped = None
            while True:
                # If terminated, dbg.launch/continue returns EventListView
                # Identify stop site
                if not isinstance(stopped, StoppedDebuggerView):
                    break
                frames = stopped.frames
                if not frames:
                    try:
                        next_view = await _with_timeout(dbg.continue_execution())
                    except asyncio.TimeoutError:
                        break
                    stopped = next_view
                    continue

                # Store StoppedEvent to avoid linter warnings about None
                stopped_event_cls = StoppedEvent

                def is_breakpoint_event(e):
                    return (
                            isinstance(e, stopped_event_cls)
                            and hasattr(e, "body")
                            and hasattr(e.body, "reason")
                            and e.body.reason == "breakpoint"
                    )

                breakpoint_event = list(
                    filter(is_breakpoint_event, stopped.events.events)
                )
                if not breakpoint_event:
                    try:
                        next_view = await _with_timeout(dbg.continue_execution())
                    except asyncio.TimeoutError:
                        break
                    stopped = next_view
                    continue
                assert len(breakpoint_event) == 1
                breakpoint_ids = breakpoint_event[0].body.hitBreakpointIds

                top = frames[0]
                # Collect backtrace string regardless; but only store if monitored
                bt = _compact_backtrace(frames)
                function_name = frames[0].name if frames else "<unavailable>"
                # Evaluate inline expressions for the breakpoint spec
                for breakpoint_id in breakpoint_ids:
                    spec = id_to_spec.get(breakpoint_id)
                    report = result.reports[breakpoint_id]
                    assert spec is not None
                    assert report is not None
                    report.function_name = function_name
                    report.hit_times += 1
                    if report.hit_times >= spec.hit_limit:
                        response = await dbg.remove_breakpoint(Path(spec.file_path), spec.line_no)
                        assert isinstance(response, SetBreakpointsResponse)
                        assert response.success is True
                    report.file_path = top.source.path
                    report.line = top.line
                    hit_info = BreakpointHitInfo(
                        callstack=bt if spec.print_call_stack else "", inline_expr=[]
                    )

                    for var in spec.inline_expr:
                        try:
                            ev = await dbg.evaluate(var)
                            if ev.success:
                                val = ev.body.result
                            else:
                                val = ev.body.error.format
                        except Exception:
                            val = "<unavailable>"
                        hit_info.inline_expr.append(
                            InlineExprValue(
                                name=var,
                                value=val,
                            )
                        )
                    report.hits_info.append(hit_info)

                # Continue
                try:
                    next_view = await _with_timeout(dbg.continue_execution())
                except asyncio.TimeoutError:
                    break
                stopped = next_view

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
                    print(
                        f"Warning: lldb_path {config.lldb_path} does not exist, using auto-detection"
                    )
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
                    print(
                        f"Warning: repo_dir {config.source_code_dir} does not exist or is not a directory"
                    )
                    self.repo_dir = None

        @staticmethod
        def _auto_find_lldb_path() -> Optional[Path]:
            """Auto-detect lldb path from /usr/bin/lldb*, preferring the highest version."""
            import glob
            import re

            # Find all lldb binaries in /usr/bin/
            lldb_paths = glob.glob("/usr/bin/lldb*")
            if not lldb_paths:
                return None

            # Filter to main lldb binaries (exclude lldb-argdumper, lldb-server, etc.)
            main_lldb_paths = []
            for path in lldb_paths:
                filename = Path(path).name
                # Match lldb or lldb-<version>
                if re.match(r"^lldb(-\d+)?$", filename):
                    main_lldb_paths.append(path)

            if not main_lldb_paths:
                return None

            # Sort by version number (highest first)
            def version_key(path):
                filename = Path(path).name
                # Extract version number, default to 0 for plain 'lldb'
                match = re.search(r"-(\d+)$", filename)
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
            current_path = self.env.get("PATH", "")

            # Check if lldb_dir is already in PATH
            path_dirs = current_path.split(os.pathsep) if current_path else []
            if lldb_dir not in path_dirs:
                # Prepend lldb_dir to PATH (like export PATH=/usr/lib/llvm-20/bin:$PATH)
                new_path = lldb_dir + (
                    os.pathsep + current_path if current_path else ""
                )
                self.env["PATH"] = new_path

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
                breakpoints: List[BreakpointSpec],
                timeout_sec: Optional[float] = None,
        ) -> RuntimeFeedback:
            """Async version of runtime debugging."""
            return await _run_dap(
                cmd,
                stdin_bytes,
                breakpoints,
                self.repo_dir,
                self.env,
                self.lldb_path,
                timeout_sec,
            )

        def get_runtime_feedback(
                self,
                cmd: List[str],
                stdin: Optional[bytes] = None,
                timeout_sec: Optional[
                    int
                ] = None,  # currently unused; reserved for future timeouts
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

            # Parse breakpoint specs (dicts -> Pydantic models)
            specs: List[BreakpointSpec] = []
            hit_limit_by_loc: Dict[str, int] = {}
            stack_flag_by_loc: Dict[str, bool] = {}
            for item in breakpoints:
                bp = BreakpointSpec(**item)
                specs.append(bp)
                hit_limit_by_loc[bp.location] = (
                    int(bp.hit_limit) if bp.hit_limit is not None else 10
                )
                stack_flag_by_loc[bp.location] = bool(bp.print_call_stack)

            # Execute via DAP runner to collect raw data
            raw = asyncio.run(self._run_dap_async(cmd, stdin, specs, timeout_sec=float(timeout_sec) if timeout_sec else None))

            # Prepare final output; exit_code/signal are currently 0 until extended
            out = RuntimeFeedbackV2(
                stderr=(raw.stderr.decode(errors="replace")),
                exit_code=0,
                signal=0,
                breakpoints=list(raw.reports.values()),
            )
            return out.model_dump()

except ImportError as e:
    # Handle missing dependencies gracefully
    print(f"Warning: Missing dependencies for DAP debugging: {e}")

from __future__ import annotations

try:
    # Standard libs
    import asyncio
    import time
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from typing import Any, Dict, List, Tuple, Optional

    # DAP types and client
    from dap_types import StoppedEvent, StackFrame, ExitedEvent
    from pydantic import BaseModel, Field
    from dap_mcp.factory import DAPClientSingletonFactory
    from dap_mcp.debugger import (
        Debugger,
        LaunchRequestArguments,
        SetBreakpointsResponse,
        ErrorResponse,
        StoppedDebuggerView,
        EventListView,
    )

    class RuntimeFeedback(BaseModel):
        stdout: bytes
        stderr: bytes
        reports: dict[int, BreakpointReport]
        timeout: bool
        exit_code: Optional[int]
        signal: Optional[str]

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
        exit_code: Optional[int]
        signal: Optional[str]
        breakpoints: list[BreakpointReport]

    # Locate LLDB's DAP adapter. Prefer an explicit adapter path if provided,
    # otherwise try `lldb-dap` then `lldb-vscode` on PATH.
    def _find_lldb_adapter(lldb_path: Optional[Path] = None) -> Tuple[str, List[str]]:
        if lldb_path:
            p = Path(lldb_path)
            if p.name in ("lldb-dap", "lldb-vscode") and p.exists():
                return str(p), []
            # Try siblings next to the provided path
            for name in ("lldb-dap", "lldb-vscode"):
                cand = p.parent / name
                if cand.exists():
                    return str(cand), []
        for name in ("lldb-dap", "lldb-vscode"):
            cmd = shutil.which(name)
            if cmd:
                return cmd, []
        raise RuntimeError("LLDB DAP adapter not found (lldb-dap or lldb-vscode)")

    def _normalize_locations(
        breakpoints: List[BreakpointSpec], repo_dir: Optional[Path] = None
    ) -> None:
        """Normalize BreakpointSpec locations to absolute file paths in-place.

        Tests pass absolute file paths already; this also supports repo-relative paths
        when a `repo_dir` is provided.
        """
        for bp in breakpoints:
            file_str, line_part = bp.location.rsplit(":", 1)
            file_path = Path(file_str)
            if file_path.is_absolute() and file_path.exists():
                bp.location = f"{file_path}:{line_part}"
                continue
            if repo_dir is not None:
                guess = (repo_dir / file_path).resolve()
                if guess.exists():
                    bp.location = f"{guess}:{line_part}"
                    continue

    def _format_single_frame(frame: StackFrame) -> str:
        return (
            f"{frame.name} at {frame.source.name}:{frame.line}"
            if frame.source
            else frame.name
        )

    def _compact_backtrace(frames: list[StackFrame]) -> str:
        """Return a compact, multi-line callstack string for the stopped thread."""
        return "\n".join(
            f"{'*' if i == 0 else ' '} #{i}: {_format_single_frame(f)}"
            for i, f in enumerate(frames)
        )

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
        result = RuntimeFeedback(
            stdout=b"",
            stderr=b"",
            reports={},
            timeout=False,
            exit_code=None,
            signal=None,
        )

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

            # Prepare adapter and debugger
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
                    result.timeout = True
                    raise asyncio.TimeoutError()
                return await asyncio.wait_for(awaitable, timeout=remaining)

            try:
                stopped = await _with_timeout(dbg.launch())
            except asyncio.TimeoutError:
                stopped = None
            while True:
                # If terminated, dbg.launch/continue returns EventListView
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

                # Identify a breakpoint stop
                breakpoint_event = list(
                    filter(
                        lambda e: isinstance(e, StoppedEvent)
                        and e.body.reason == "breakpoint",
                        stopped.events.events,
                    )
                )
                if not breakpoint_event:
                    # Handle non-breakpoint stops (e.g., exception/signals)
                    stopped_event: List[StoppedEvent] = list(
                        filter(
                            lambda e: isinstance(e, StoppedEvent), stopped.events.events
                        )
                    )
                    assert len(stopped_event) == 1
                    reason = stopped_event[0].body.reason
                    if reason != "exception":
                        print(f"Warning: Unhandled stopping reason {reason}")
                        try:
                            next_view = await _with_timeout(dbg.continue_execution())
                        except asyncio.TimeoutError:
                            break
                        stopped = next_view
                        continue
                    result.signal = stopped_event[0].body.description
                    break
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
                        response = await dbg.remove_breakpoint(
                            Path(spec.file_path), spec.line_no
                        )
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

                # Continue execution
                try:
                    next_view = await _with_timeout(dbg.continue_execution())
                except asyncio.TimeoutError:
                    break
                stopped = next_view

            if isinstance(stopped, EventListView):
                exited_events = list(
                    filter(lambda e: isinstance(e, ExitedEvent), stopped.events)
                )
                if exited_events:
                    exited_event: ExitedEvent = exited_events[0]
                    result.exit_code = exited_event.body.exitCode

            # Graceful shutdown and stdio collection
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
            timeout_sec: Optional[int] = None,
            breakpoints: Optional[List[Dict[str, Any]]] = None,
        ) -> Dict[str, Any]:
            """Run a program under LLDB DAP and return a summarized report.

            - cmd: program argv (first item is the binary)
            - stdin: optional bytes to feed into the program
            - timeout_sec: optional timeout applied across launch/continues
            - breakpoints: list of dicts parsed as BreakpointSpec
            """
            specs: List[BreakpointSpec] = [
                BreakpointSpec(**bs) for bs in (breakpoints or [])
            ]
            raw = asyncio.run(
                self._run_dap_async(
                    cmd,
                    stdin,
                    specs,
                    timeout_sec=float(timeout_sec) if timeout_sec else None,
                )
            )
            out = RuntimeFeedbackV2(
                stderr=raw.stderr.decode(errors="replace"),
                exit_code=raw.exit_code,
                signal=raw.signal,
                breakpoints=list(raw.reports.values()),
            )
            return out.model_dump()

except ImportError as e:
    # Handle missing dependencies gracefully
    print(f"Warning: Missing dependencies for DAP debugging: {e}")

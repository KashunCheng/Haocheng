from __future__ import annotations

try:
    # Standard libs
    import asyncio
    import time
    import os
    import shutil
    import subprocess
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
        """Output schema for run (new format)."""

        stderr: str
        exit_code: Optional[int]
        signal: Optional[str]
        breakpoints: list[BreakpointReport]
        has_timeout: bool

    # Locate LLDB's DAP adapter. Prefer an explicit adapter path if provided,
    # otherwise try `lldb-dap` then `lldb-vscode` on PATH.
    def _find_lldb_adapter(lldb_path: Optional[Path] = None) -> Tuple[str, List[str]]:
        if lldb_path:
            p = Path(lldb_path)
            if p.name in ("lldb-dap", "lldb-dap-20", "lldb-vscode") and p.exists():
                return str(p), []
            # Try siblings next to the provided path
            for name in ("lldb-dap", "lldb-dap-20", "lldb-vscode"):
                cand = p.parent / name
                if cand.exists():
                    return str(cand), []
        for name in ("lldb-dap", "lldb-dap-20", "lldb-vscode"):
            cmd = shutil.which(name)
            if cmd:
                return cmd, []
        raise RuntimeError("LLDB DAP adapter not found (lldb-dap or lldb-vscode)")

    def _get_source_map(lldb_path: str, executable_path: str) -> list[Path]:
        llvm_dwarfdump_path = lldb_path.replace("lldb-dap", "llvm-dwarfdump")
        llvm_dwarfdump_path = llvm_dwarfdump_path.replace(
            "lldb-vscode", "llvm-dwarfdump"
        )
        if not Path(llvm_dwarfdump_path).exists():
            return []
        out = subprocess.check_output(
            [llvm_dwarfdump_path, "--show-sources", executable_path],
            text=True,
            errors="ignore",
        )
        return [
            Path(line.strip()).resolve() for line in out.splitlines() if line.strip()
        ]

    def _normalize_locations(
        breakpoints: List[BreakpointSpec],
        repo_dir: Optional[Path] = None,
        source_map: Optional[List[Path]] = None,
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
            if source_map is not None:
                guess = [p for p in source_map if p.name == file_path.name]
                if guess:
                    bp.location = f"{guess[0]}:{line_part}"
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
        result: RuntimeFeedback = RuntimeFeedback(
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
            source_map = _get_source_map(adapter_cmd, program)
            _normalize_locations(breakpoints, repo_dir, source_map)
            id_to_spec: Dict[int, BreakpointSpec] = {}

            for spec in breakpoints:
                try:
                    resp = await dbg.set_breakpoint(Path(spec.file_path), spec.line_no)
                    if isinstance(resp, ErrorResponse):
                        # Log the error but don't raise exception - create empty report instead
                        print(
                            f"Warning: Failed to set breakpoint at {spec.location}: {resp.message}"
                        )
                        continue
                    if not isinstance(resp, SetBreakpointsResponse):
                        print(
                            f"Warning: Unexpected response type for breakpoint at {spec.location}"
                        )
                        continue
                    if not resp.success:
                        print(f"Warning: Failed to set breakpoint at {spec.location}")
                        continue

                    if not resp.body.breakpoints:
                        print(f"Warning: No breakpoints returned for {spec.location}")
                        continue

                    current_breakpoint = resp.body.breakpoints[-1]
                    if current_breakpoint.id is None:
                        print(f"Warning: Breakpoint ID is None for {spec.location}")
                        continue

                    id_to_spec[current_breakpoint.id] = spec
                    result.reports[current_breakpoint.id] = BreakpointReport(
                        id=current_breakpoint.id,
                        file_path=spec.file_path,
                        line=spec.line_no,
                        function_name="",
                        hit_times=0,
                        hits_info=[],
                    )
                except Exception as e:
                    # Handle any other exceptions gracefully
                    print(
                        f"Warning: Exception setting breakpoint at {spec.location}: {e}"
                    )
                    continue

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
                result.timeout = True
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
                        result.timeout = True
                        break
                    stopped = next_view
                    continue

                # Identify a breakpoint stop
                breakpoint_event = [
                    e
                    for e in stopped.events.events
                    if isinstance(e, StoppedEvent) and e.body.reason == "breakpoint"
                ]
                if not breakpoint_event:
                    # Handle non-breakpoint stops (e.g., exception/signals)
                    stopped_event = [
                        e for e in stopped.events.events if isinstance(e, StoppedEvent)
                    ]
                    if len(stopped_event) != 1:
                        print(
                            f"Warning: Expected 1 stopped event, got {len(stopped_event)}"
                        )
                        try:
                            next_view = await _with_timeout(dbg.continue_execution())
                        except asyncio.TimeoutError:
                            result.timeout = True
                            break
                        stopped = next_view
                        continue
                    reason = stopped_event[0].body.reason
                    if reason != "exception":
                        print(f"Warning: Unhandled stopping reason {reason}")
                        try:
                            next_view = await _with_timeout(dbg.continue_execution())
                        except asyncio.TimeoutError:
                            result.timeout = True
                            break
                        stopped = next_view
                        continue
                    result.signal = stopped_event[0].body.description
                    break
                if len(breakpoint_event) != 1:
                    print(
                        f"Warning: Expected 1 breakpoint event, got {len(breakpoint_event)}"
                    )
                    try:
                        next_view = await _with_timeout(dbg.continue_execution())
                    except asyncio.TimeoutError:
                        result.timeout = True
                        break
                    stopped = next_view
                    continue

                bp_event = breakpoint_event[0]
                if not hasattr(bp_event.body, "hitBreakpointIds"):
                    print("Warning: Breakpoint event missing hitBreakpointIds")
                    try:
                        next_view = await _with_timeout(dbg.continue_execution())
                    except asyncio.TimeoutError:
                        result.timeout = True
                        break
                    stopped = next_view
                    continue

                breakpoint_ids = bp_event.body.hitBreakpointIds
                if breakpoint_ids is None:
                    continue

                top = frames[0]
                # Collect backtrace string regardless; but only store if monitored
                bt = _compact_backtrace(frames)
                function_name = frames[0].name if frames else "<unavailable>"
                # Evaluate inline expressions for the breakpoint spec
                for breakpoint_id in breakpoint_ids:
                    spec = id_to_spec.get(breakpoint_id)
                    if spec is None:
                        print(
                            f"Warning: No spec found for breakpoint ID {breakpoint_id}"
                        )
                        continue

                    report = result.reports.get(breakpoint_id)
                    if report is None:
                        print(
                            f"Warning: No report found for breakpoint ID {breakpoint_id}"
                        )
                        continue
                    report.function_name = function_name
                    report.hit_times += 1
                    if report.hit_times >= spec.hit_limit:
                        try:
                            response = await dbg.remove_breakpoint(
                                Path(spec.file_path), spec.line_no
                            )
                            if (
                                not isinstance(response, SetBreakpointsResponse)
                                or not response.success
                            ):
                                print(
                                    f"Warning: Failed to remove breakpoint at {spec.location}"
                                )
                        except Exception as e:
                            print(
                                f"Warning: Exception removing breakpoint at {spec.location}: {e}"
                            )

                    if top.source and top.source.path:
                        report.file_path = top.source.path
                    report.line = top.line
                    hit_info = BreakpointHitInfo(
                        callstack=bt if spec.print_call_stack else "", inline_expr=[]
                    )

                    for var in spec.inline_expr:
                        try:
                            ev = await dbg.evaluate(var)
                            if isinstance(ev, ErrorResponse):
                                # Simplify error messages from ErrorResponse
                                error_msg = ev.message or ""
                                if "use of undeclared identifier" in error_msg:
                                    val = f"<use of undeclared identifier '{var}'>"
                                elif "no member named" in error_msg:
                                    val = f"<no member named in {var}>"
                                elif "cannot be used" in error_msg:
                                    val = f"<{var} cannot be used>"
                                elif "not found" in error_msg.lower():
                                    val = f"<{var} not found>"
                                elif "undefined" in error_msg.lower():
                                    val = f"<{var} undefined>"
                                else:
                                    val = f"<evaluation error for {var}>"
                            else:
                                # Use safe attribute access with getattr and default values
                                val = "<evaluation_failed>"
                                try:
                                    success = getattr(ev, "success", False)
                                    if success:
                                        body = getattr(ev, "body", None)
                                        if body is not None:
                                            eval_result = getattr(body, "result", None)
                                            if eval_result is not None:
                                                val = str(eval_result)
                                            else:
                                                val = "<no_result>"
                                    else:
                                        body = getattr(ev, "body", None)
                                        if body is not None:
                                            error_obj = getattr(body, "error", None)
                                            if error_obj is not None:
                                                error_format = getattr(
                                                    error_obj, "format", None
                                                )
                                                error_message = getattr(
                                                    error_obj, "message", None
                                                )
                                                if error_format is not None:
                                                    # Extract the key error message from the format
                                                    error_text = str(error_format)
                                                    # Look for common error patterns and simplify them
                                                    if (
                                                        "use of undeclared identifier"
                                                        in error_text
                                                    ):
                                                        val = f"<use of undeclared identifier '{var}'>"
                                                    elif (
                                                        "no member named" in error_text
                                                    ):
                                                        val = f"<no member named in {var}>"
                                                    elif "cannot be used" in error_text:
                                                        val = f"<{var} cannot be used>"
                                                    elif (
                                                        "not found"
                                                        in error_text.lower()
                                                    ):
                                                        val = f"<{var} not found>"
                                                    elif (
                                                        "undefined"
                                                        in error_text.lower()
                                                    ):
                                                        val = f"<{var} undefined>"
                                                    else:
                                                        # For other errors, just use a generic message
                                                        val = f"<evaluation error for {var}>"
                                                elif error_message is not None:
                                                    val = f"<{error_message}>"
                                                else:
                                                    val = "<error_no_details>"
                                except (AttributeError, TypeError):
                                    val = "<attribute_error>"
                        except Exception:
                            val = "<runtime_value_unavailable>"
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
                    result.timeout = True
                    break
                stopped = next_view

            if isinstance(stopped, EventListView):
                exited_events = [
                    e for e in stopped.events if isinstance(e, ExitedEvent)
                ]
                if exited_events:
                    exited_event = exited_events[0]
                    if isinstance(exited_event, ExitedEvent):
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
                path = (
                    config.source_code_dir
                    if isinstance(config.source_code_dir, Path)
                    else Path(config.source_code_dir)
                )
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

        def run(
            self,
            cmd: List[str],
            stdin: Optional[bytes] = None,
            timeout_sec: Optional[int] = None,
            breakpoints: Optional[List[Dict[str, Any]]] = None,
        ) -> RuntimeFeedbackV2:
            """Run a program under LLDB DAP and collect runtime debugging information.

            Args:
                cmd: Program command line arguments list, first element is the executable path
                stdin: Optional standard input bytes data
                timeout_sec: Optional timeout in seconds, applied to launch and continue execution
                breakpoints: List of breakpoint configurations, each element is a dictionary

            Breakpoint Configuration Schema:
                [
                    {
                        "location": "filename:line (e.g., parser.c:120)",
                        "hit_limit": "integer (default: 10)",
                        "inline_expr": [
                            "list of expression strings (optional, e.g., len, *(buf+offset), ctx->len, varA-varB)"
                        ],
                        "print_call_stack": "boolean (default: false)"
                    }
                ]

            Returns:
                RuntimeFeedbackV2: Debugging feedback object containing the following fields:
                    - stderr: Program standard error output (str)
                    - exit_code: Program exit code (int | None)
                    - signal: Signal received by program (str | None)
                    - breakpoints: List of breakpoint reports (List[BreakpointReport])
                    - has_timeout: Whether timeout occurred (bool)

            Breakpoint Report Schema:
                [
                    {
                        "id": "breakpoint ID (int)",
                        "file_path": "file path (str)",
                        "line": "line number (int)",
                        "function_name": "function name (str)",
                        "hit_times": "hit count (int, always <= hit_limit)",
                        "hits_info": [
                            {
                                "callstack": "call stack string (str)",
                                "inline_expr": [
                                    {
                                        "name": "expression name (str)",
                                        "value": "expression value (str)"
                                    }
                                ]
                            }
                        ]
                    }
                ]

            Raises:
                RuntimeError: Only when LLDB DAP adapter is not found on the system.
                             All other errors (invalid breakpoint locations, file not found,
                             invalid line numbers, expression evaluation failures) are handled
                             gracefully and logged as warnings without raising exceptions.

            Note on Error Handling:
                The debugger is designed for high availability and will never terminate
                due to invalid input. Specific error handling behaviors:

                - Invalid file paths: Breakpoint creation skipped, warning logged
                - Invalid line numbers: Breakpoint creation skipped, warning logged
                - Expression evaluation failures: Error message returned as value
                - Timeout: Execution stopped gracefully, timeout flag set
                - Missing breakpoint IDs: Execution continues, warning logged

                All errors are captured in the returned RuntimeFeedbackV2 object or
                logged as warnings to stdout.
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
                has_timeout=raw.timeout,
            )
            return out

        def run_dict(
            self,
            cmd: List[str],
            stdin: Optional[bytes] = None,
            timeout_sec: Optional[int] = None,
            breakpoints: Optional[List[Dict[str, Any]]] = None,
        ) -> Dict[str, Any]:
            """Run a program under LLDB DAP and return debugging information as a dictionary.

            This method is a convenience wrapper around `run()` that returns the result
            as a dictionary instead of a Pydantic model.

            Args:
                cmd: Program command line arguments list, first element is the executable path
                stdin: Optional standard input bytes data
                timeout_sec: Optional timeout in seconds, applied to launch and continue execution
                breakpoints: List of breakpoint configurations, each element is a dictionary

            Breakpoint Configuration Schema:
                [
                    {
                        "location": "filename:line (e.g., parser.c:120)",
                        "hit_limit": "integer (default: 10)",
                        "inline_expr": [
                            "list of expression strings (optional, e.g., len, *(buf+offset), ctx->len, varA-varB)"
                        ],
                        "print_call_stack": "boolean (default: false)"
                    }
                ]

            Returns:
                Dict[str, Any]: Dictionary containing debugging feedback with the following structure:
                {
                    "stderr": "string",
                    "exit_code": "integer | null",
                    "signal": "string | null",
                    "breakpoints": [
                        {
                            "id": "integer",
                            "file_path": "string",
                            "line": "integer",
                            "function_name": "string",
                            "hit_times": "integer (always <= hit_limit)",
                            "hits_info": [
                                {
                                    "callstack": "string",
                                    "inline_expr": [
                                        {
                                            "name": "string",
                                            "value": "string"
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                    "has_timeout": "boolean"
                }

            Raises:
                RuntimeError: Only when LLDB DAP adapter is not found on the system.
                             All other errors (invalid breakpoint locations, file not found,
                             invalid line numbers, expression evaluation failures) are handled
                             gracefully and logged as warnings without raising exceptions.

            Note on Error Handling:
                The debugger is designed for high availability and will never terminate
                due to invalid input. See the run() method documentation for detailed
                error handling behavior.
            """
            return self.run(cmd, stdin, timeout_sec, breakpoints).model_dump()

except ImportError as e:
    # Handle missing dependencies gracefully
    print(f"Warning: Missing dependencies for DAP debugging: {e}")

# AGENTS.MD — codex-cli

**Goal:** Implement `get_runtime_feedback` using `dap-mcp` to drive LLDB’s DAP server, plus end‑to‑end tests and two minimal C fixtures (one uses stdin, one not). Breakpoints should fire multiple times inside a loop; at each stop we collect variable values (watchpoints) and a compact backtrace string.

---

## 0) Scope & Success Criteria

### Deliverables

* `src/codex_cli/runtime_feedback.py` with production‑ready `get_runtime_feedback`.
* Two C fixtures compiled with `clang -O0 -g` (and friends):

  * `fixtures/loop_basic.c` (no stdin)
  * `fixtures/loop_stdin.c` (stdin path)
* Pytest suite covering both fixtures:

  * Launch under LLDB DAP via `dap-mcp`.
  * Set breakpoints in loop body.
  * For each stop, collect:

    * Watchpoint values for configured variables at that `file:line`.
    * A compact backtrace string for that stop.
* Deterministic assertions for counts and sample values.

### Acceptance checklist

* [ ] Works on macOS (lldb-dap) and Linux (lldb-dap/lldb-vscode).
* [ ] Compiles fixtures with `-O0 -g -fno-omit-frame-pointer`.
* [ ] Supports stdin (see §4.2 for robust options).
* [ ] `RuntimeFeedback.watchpoints` is a `dict[str, list[dict[var,value]]]` keyed by `"file:line"`.
* [ ] `RuntimeFeedback.breakpoints` is a `dict[str, list[str]]` keyed by `"file:line"` with compact backtrace strings.
* [ ] Breakpoint triggers ≥2 times per test (loop body).
* [ ] CI job runs tests headlessly.

---

## 1) Repo layout (proposed)

```
.
├─ pyproject.toml / requirements.txt
├─ src/
│  └─ codex_cli/
│     ├─ __init__.py
│     └─ runtime_feedback.py   # implements get_runtime_feedback
├─ fixtures/
│  ├─ loop_basic.c
│  └─ loop_stdin.c
└─ tests/
   ├─ test_runtime_feedback_basic.py
   └─ test_runtime_feedback_stdin.py
```

---

## 2) Interfaces & Data Contracts

### 2.1 Pydantic Models

```python
class WatchPoint(BaseModel):
    var: str               # variable/expression to evaluate at a location
    log_location: str      # "file.c:LINE" (normalized relative to repo root)

class RuntimeFeedback(BaseModel):
    # dict["file:line"] -> list of occurrences, each: {"var": str, "value": str}
    watchpoints: dict[str, list[dict[str, str]]]
    # dict["file:line"] -> list of backtrace strings per stop
    breakpoints: dict[str, list[str]]
```

### 2.2 `get_runtime_feedback`

```python
def get_runtime_feedback(
    cmd: list[str],
    stdin: bytes | None,
    watchpoints_list: list[dict],
    monitor_locations: list[str],
) -> RuntimeFeedback:
    """
    - cmd: argv for the compiled C program (absolute or cwd-relative path first).
    - stdin: optional bytes to feed to program.
    - watchpoints_list: list of {"var": <expr>, "log_location": "file:line"}.
    - monitor_locations: list of "file:line" at which to set breakpoints.

    Semantics:
    * Launch the process under LLDB via DAP.
    * Normalize all file paths to absolute paths for DAP breakpoint requests.
    * Set breakpoints at every monitor location.
    * Continue the program; on every stop due to breakpoint:
        - Evaluate any watchpoints whose log_location == this location using DAP `evaluate` in top frame.
        - Record {var, value (stringified)} into `watchpoints[location]`.
        - Collect a compact backtrace string (e.g., "main -> work -> loop_body @ file.c:LINE").
        - Append trace to `breakpoints[location]`.
    * Continue until program exits; return aggregated RuntimeFeedback.
    """
```

### 2.3 Location format

* External/config format: `"file.c:LINE"` (project‑root relative).
* Internal DAP: must be absolute path; we will map relative → absolute during setup.
* We return keys in the original normalized relative format for stability.

---

## 3) Implementation Plan (DAP + LLDB via `dap-mcp`)

### 3.1 DAP lifecycle (happy path)

1. **Spawn DAP server** (lldb-dap/lldb-vscode) and connect with `dap-mcp` client.
2. `initialize` → capabilities; `launch` with program, args, cwd, env.
3. `setBreakpoints` for each source file (group locations by file).
4. `configurationDone`.
5. Event loop:

   * On `stopped`(reason=breakpoint):

     * `threads` → pick top thread.
     * `stackTrace`(threadId) → frames.
     * For frame 0, `scopes` → locals; evaluate each watchpoint expression matching this location using `evaluate` (context="watch" or "repl").
     * Build compact backtrace string from frames (function names + top 2–3 frames + stop site).
     * Append to result dicts.
     * `continue` (or `resume`) until next stop.
6. On `exited`/`terminated`, shutdown and return `RuntimeFeedback`.

> Note: LLDB DAP method naming may differ slightly across versions; keep an adapter thin layer in code to tolerate `lldb-dap` vs `lldb-vscode`.

### 3.2 Watchpoint evaluation rules

* Match by `log_location` (exact `"file:line"`).
* Evaluate expression as written (e.g., `i`, `sum`, `arr[k]`).
* If out of scope or error → record value as `"<unavailable>"`.

### 3.3 Backtrace string format

* Example: `"loop_basic() -> main() @ loop_basic.c:17"`
* Heuristic: concatenate up to 3 function names from top frames; append `@ file:line` for the stop site.

### 3.4 Error/edge handling

* Timeouts for launch/first stop/exit (configurable; default 10s/30s/30s).
* If a breakpoint cannot be set (file mismatch), raise with a hint to re‑normalize paths.
* If program exits without hitting any breakpoints, return empty lists for those keys.
* Always attempt graceful DAP shutdown on exceptions.

---

## 4) C Fixtures (line numbers chosen so loop body is obvious)

### 4.1 `fixtures/loop_basic.c` (no stdin)

```c
#include <stdio.h>

int work_basic(int n) {
    int sum = 0;
    for (int i = 0; i < n; i++) {
        sum += i;                  // BREAK HERE (loop body)
        // (watch i, sum)
    }
    return sum;                    // convenient post-loop anchor
}

int main(void) {
    int n = 5;
    int s = work_basic(n);
    printf("sum=%d\n", s);
    return 0;
}
```

**Monitor location example:** `loop_basic.c:7` (adjust if line offsets differ).

### 4.2 `fixtures/loop_stdin.c` (stdin, with robust fallback)

```c
#include <stdio.h>
#include <stdlib.h>

static int read_n(void) {
    char *path = getenv("CODEX_STDIN_FILE");
    int n = 0;
    if (path) {
        FILE *f = fopen(path, "rb");
        if (!f) return 0;
        if (fscanf(f, "%d", &n) != 1) n = 0;
        fclose(f);
    } else {
        if (scanf("%d", &n) != 1) n = 0;  // true stdin path
    }
    return n;
}

int work_stdin(int n) {
    int acc = 1;
    for (int i = 1; i <= n; i++) {
        acc *= i;                   // BREAK HERE (loop body)
        // (watch i, acc)
    }
    return acc;
}

int main(void) {
    int n = read_n();
    int a = work_stdin(n);
    printf("acc=%d\n", a);
    return 0;
}
```

**Monitor location example:** `loop_stdin.c:20` (adjust once committed).
**Why the env fallback?** LLDB DAP support for piping stdin varies. Tests will first try to feed stdin directly; if not supported, they write bytes to a temp file and set `CODEX_STDIN_FILE`.

### 4.3 Compile flags

* Always: `-O0 -g -fno-omit-frame-pointer -fno-inline -Wall`.
* macOS: you may also use `-fno-pie -no-pie` only if needed (older toolchains); recent clang defaults are fine.
* Output binaries into `build/` or test tmp dirs.

---

## 5) Tests (pytest)

### 5.1 Common test utilities

* Helper to compile a fixture with clang to a temp binary; return abs path.
* Helper to normalize `"file:line"` → `(abs_path, int(line))` and back.
* LLDB DAP server discovery: try `lldb-dap` then `lldb-vscode` from PATH.

### 5.2 `test_runtime_feedback_basic.py`

**Arrange**

* Compile `loop_basic.c`.
* `monitor_locations = ["fixtures/loop_basic.c:7"]` (verify actual line).
* `watchpoints_list = [{"var": "i", "log_location": same}, {"var": "sum", "log_location": same}]`.

**Act**

* Call `get_runtime_feedback([binary_path], None, watchpoints_list, monitor_locations)`.

**Assert**

* `breakpoints[loc]` length == 5 (loop iterations) or expected N.
* `watchpoints[loc]` length == N and contains ascending `i` and cumulative `sum` values.
* Backtrace strings are nonempty and include `loop_basic`/`main` function names.

### 5.3 `test_runtime_feedback_stdin.py`

**Arrange**

* Compile `loop_stdin.c`.
* `monitor_locations = ["fixtures/loop_stdin.c:20"]` (verify).
* `watchpoints_list = [{"var": "i", "log_location": loc}, {"var": "acc", "log_location": loc}]`.
* Choose stdin bytes: `b"4\n"` (expect iterations 1..4).

**Act**

* First attempt: pass `stdin=b"4\n"` (true stdin).
* If adapter lacks stdin support: write to temp file and set `env["CODEX_STDIN_FILE"] = tmp_path`; pass `stdin=None`.

**Assert**

* Expect 4 breakpoint hits; `i` sequence `[1,2,3,4]`; `acc` `[1,2,6,24]`.

---

## 6) Implementation Notes & Pseudocode

### 6.1 DAP wiring (sketch)

```python
async def _run_lldb_dap(cmd, cwd, env, abs_bkpts, stdin_bytes):
    # create client, initialize
    # launch: program=cmd[0], args=cmd[1:], cwd=cwd, env=env
    # (stdin strategy) see §6.3
    # setBreakpoints per file
    # configurationDone
    # event loop: collect on breakpoint stops; continue
    # shutdown; return collected data
```

### 6.2 Collecting at a stop

```python
# "location" is normalized "file:line"
frames = await client.stackTrace(threadId)
trace = _compact_trace(frames)
results.breakpoints[location].append(trace)
for wp in watchpoints_by_location[location]:
    try:
        v = await client.evaluate(expr=wp.var, frameId=frames.stackFrames[0].id)
        val = v.result
    except Exception:
        val = "<unavailable>"
    results.watchpoints[location].append({"var": wp.var, "value": str(val)})
```

### 6.3 Handling stdin

Order of preference:

1. **Direct stdin pipe** if your `dap-mcp` transport exposes a writable stdin stream to the debuggee (e.g., via `runInTerminal` + PTY). Write `stdin_bytes` after `launch`.
2. **File fallback**: write `stdin_bytes` to a temp file and set `CODEX_STDIN_FILE` env for the process; fixture reads from the file when present.

### 6.4 Path normalization

* Keep a mapping `{(abs_file, line) → rel_key}`.
* DAP `setBreakpoints` requires absolute file URIs; use `os.path.realpath`.

---

## 7) Developer Runbook

### 7.1 Setup

```
# Python deps
pip install dap-mcp pydantic pytest

# Tooling
which clang
which lldb-dap || which lldb-vscode
```

### 7.2 Local run

```
pytest -q
```

### 7.3 CI (GitHub Actions sketch)

```yaml
name: test
on: [push, pull_request]
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: sudo apt-get update && sudo apt-get install -y lldb clang
      - run: pip install -r requirements.txt
      - run: pytest -q
```

---

## 8) Agent Roles & Tasks

### 8.1 Architect Agent

* Finalize API and path normalization rules.
* Decide stdin strategy (prefer direct PTY; ensure env fallback remains).

### 8.2 Implementer Agent (Python)

* Implement `get_runtime_feedback` per §3 and §6.
* Build an internal async helper to manage the DAP session.
* Provide robust error messages (missing files, bad lines, DAP failures).

### 8.3 Fixture Agent (C)

* Author both C files with clear loop body and stable line numbers.
* Keep code minimal and portable.

### 8.4 Test Agent (Pytest)

* Implement compile helper, path normalization helper.
* Author two tests with deterministic assertions (counts and sample values).
* Ensure tests skip with a clear message if `lldb-dap`/`lldb-vscode` not found.

### 8.5 CI Agent

* Add GitHub Actions job installing `lldb` & `clang`.
* Cache pip to speed up runs.

---

## 9) Edge Cases & Tips

* **Optimizations**: ensure `-O0 -g`; inlining can skip breakpoints.
* **Scope issues**: if a variable is optimized out or out-of-scope, expect `<unavailable>`.
* **Path mismatches**: verify that the `file:line` you pass matches the compiled source line (re‑open compiled file if in doubt).
* **Concurrency**: handle multiple threads by always choosing the stopped thread from the event payload.
* **Timeouts**: expose settings via env or kwargs for CI stability.
* **Portability**: try both `lldb-dap` and `lldb-vscode` names from PATH.

---

## 10) Example Configuration Values

```python
monitor_locations = [
    "fixtures/loop_basic.c:7",
    "fixtures/loop_stdin.c:20",
]
watchpoints_list = [
    {"var": "i",   "log_location": "fixtures/loop_basic.c:7"},
    {"var": "sum", "log_location": "fixtures/loop_basic.c:7"},
    {"var": "i",   "log_location": "fixtures/loop_stdin.c:20"},
    {"var": "acc", "log_location": "fixtures/loop_stdin.c:20"},
]
```

---

## 11) Definition of Done (DoD)

* All acceptance items checked.
* CI is green on Linux; local run verified on macOS with system LLDB.
* Code is typed, docstring’d, and has clear exceptions.
* Tests assert both **counts** and **values** at watchpoints, and nonempty backtraces.

---

## 12) Nice‑to‑Have Extensions (post‑MVP)

* Support data breakpoints (hardware watchpoints) if LLDB DAP offers them.
* Structured backtrace objects (function, file, line) instead of a flat string.
* Add `--json` CLI to run the feedback collector outside pytest.
* Add GDB DAP interchangeability behind a small adapter.

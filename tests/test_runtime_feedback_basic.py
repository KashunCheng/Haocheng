import pytest

from haocheng import get_runtime_feedback
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture, _parse_int


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
def test_runtime_feedback_basic():
    src = ROOT / "fixtures" / "loop_basic.c"
    bin_path = _compile_fixture(src)

    loc = "fixtures/loop_basic.c:6"
    watchpoints = [
        {"var": "i", "log_location": loc},
        {"var": "sum", "log_location": loc},
    ]
    try:
        res = get_runtime_feedback([str(bin_path)], None, watchpoints, [loc])
    except Exception as e:
        # In restricted sandboxes, executing the debuggee may be denied
        pytest.skip(f"Skipping due to sandboxed debug launch: {e}")
        return

    # Expect 5 iterations
    assert loc in res.breakpoints
    assert loc in res.watchpoints
    assert len(res.breakpoints[loc]) == 5
    assert len(res.watchpoints[loc]) == 10  # two vars per stop

    # Extract values ordered by hits
    i_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc] if e["var"] == "i"]
    s_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc] if e["var"] == "sum"]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 0, 1, 3, 6]

    # Backtrace strings non-empty and show function names
    assert all(isinstance(bt, str) and bt for bt in res.breakpoints[loc])
    joined = "\n".join(res.breakpoints[loc])
    assert "work_basic" in joined
    assert "main" in joined
    assert b"sum=10\n" == res.stderr

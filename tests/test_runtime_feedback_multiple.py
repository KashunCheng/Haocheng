import pytest

from haocheng import _run_dap
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture, _parse_int


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
@pytest.mark.asyncio
async def test_runtime_feedback_multiple():
    src = ROOT / "fixtures" / "loop_multiple.c"
    bin_path = _compile_fixture(src)

    loc1 = f"{src}:6"
    loc2 = f"{src}:7"
    watchpoints = [
        {"var": "i", "log_location": loc1},
        {"var": "sum", "log_location": loc1},
        {"var": "i", "log_location": loc2},
        {"var": "sum", "log_location": loc2},
    ]
    res = await _run_dap([str(bin_path)], None, watchpoints, [loc1, loc2])

    # Expect 5 iterations
    assert loc1 in res.breakpoints
    assert loc2 in res.breakpoints
    assert loc1 in res.watchpoints
    assert loc2 in res.watchpoints
    assert len(res.breakpoints[loc1]) == 5
    assert len(res.watchpoints[loc1]) == 10  # two vars per stop

    # Extract values ordered by hits
    i_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc1] if e["var"] == "i"]
    s_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc1] if e["var"] == "sum"]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 1, 3, 6, 10]

    assert len(res.breakpoints[loc2]) == 5
    assert len(res.watchpoints[loc2]) == 10  # two vars per stop

    # Extract values ordered by hits
    i_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc2] if e["var"] == "i"]
    s_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc2] if e["var"] == "sum"]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 2, 5, 9, 14]

    # Backtrace strings non-empty and show function names
    assert all(isinstance(bt, str) and bt for bt in res.breakpoints[loc1])
    joined = "\n".join(res.breakpoints[loc1])
    assert "work_multiple" in joined
    assert "main" in joined
    assert b"sum=15\n" == res.stdout

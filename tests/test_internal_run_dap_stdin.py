import pytest

from haocheng import _run_dap, BreakpointSpec
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture, _parse_int


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
@pytest.mark.asyncio
async def test_runtime_feedback_stdin():
    src = ROOT / "fixtures" / "loop_stdin.c"
    bin_path = _compile_fixture(src)

    loc = f"{src}:13"
    specs = [
        BreakpointSpec(location=loc, inline_expr=["i", "acc"], hit_limit=10, print_call_stack=True)
    ]
    res = await _run_dap([str(bin_path)], b"4\n", specs)

    # Expect 4 iterations (1..4)
    assert loc in res.breakpoints
    assert loc in res.watchpoints
    assert len(res.breakpoints[loc]) == 4
    assert len(res.watchpoints[loc]) == 8  # two vars per stop

    i_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc] if e["var"] == "i"]
    a_vals = [_parse_int(e["value"]) for e in res.watchpoints[loc] if e["var"] == "acc"]
    assert i_vals == [1, 2, 3, 4]
    assert a_vals == [1, 1, 2, 6]

    assert all(isinstance(bt, str) and bt for bt in res.breakpoints[loc])
    joined = "\n".join(res.breakpoints[loc])
    assert "work_stdin" in joined
    assert "main" in joined
    assert b"acc=24\n" == res.stdout

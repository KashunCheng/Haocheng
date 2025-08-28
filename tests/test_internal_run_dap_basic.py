import pytest

from haocheng import _run_dap, BreakpointSpec
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture, _parse_int


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
@pytest.mark.asyncio
async def test_runtime_feedback_basic():
    src = ROOT / "fixtures" / "loop_basic.c"
    bin_path = _compile_fixture(src)

    loc = f"{src}:6"
    specs = [
        BreakpointSpec(location=loc, inline_expr=["i", "sum"], hit_limit=10, print_call_stack=True)
    ]
    res = await _run_dap([str(bin_path)], None, specs)

    # Find the report by file and line
    reports = list(res.reports.values())
    bp = next(r for r in reports if r.file_path == str(src) and r.line == 6)

    # Expect 5 iterations
    assert bp.hit_times == 5
    assert len(bp.hits_info) == 5

    # Extract values ordered by hits
    i_vals = [_parse_int(hit.inline_expr[0].value) for hit in bp.hits_info]
    s_vals = [_parse_int(hit.inline_expr[1].value) for hit in bp.hits_info]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 0, 1, 3, 6]

    # Backtrace strings non-empty and show function names
    joined = "\n".join(h.callstack for h in bp.hits_info)
    assert "work_basic" in joined
    assert "main" in joined
    assert b"sum=10\n" == res.stderr

import pytest

from haocheng import _run_dap, BreakpointSpec
from tests import (
    _which_lldb_adapter,
    _can_spawn_adapter,
    ROOT,
    _compile_fixture,
    _parse_int,
)


@pytest.mark.skipif(
    not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH"
)
@pytest.mark.skipif(
    not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter"
)
@pytest.mark.asyncio
async def test_runtime_feedback_multiple():
    src = ROOT / "fixtures" / "loop_multiple.c"
    bin_path = _compile_fixture(src)

    loc1 = f"{src}:6"
    loc2 = f"{src}:7"
    specs = [
        BreakpointSpec(
            location=loc1, inline_expr=["i", "sum"], hit_limit=10, print_call_stack=True
        ),
        BreakpointSpec(
            location=loc2, inline_expr=["i", "sum"], hit_limit=10, print_call_stack=True
        ),
    ]
    res = await _run_dap([str(bin_path)], None, specs)

    reports = list(res.reports.values())
    bp1 = next(r for r in reports if r.file_path == str(src) and r.line == 6)
    bp2 = next(r for r in reports if r.file_path == str(src) and r.line == 7)

    # Expect 5 iterations on both breakpoints
    assert bp1.hit_times == 5
    assert bp2.hit_times == 5
    assert len(bp1.hits_info) == 5
    assert len(bp2.hits_info) == 5

    # Extract values ordered by hits for loc1
    i_vals = [_parse_int(hit.inline_expr[0].value) for hit in bp1.hits_info]
    s_vals = [_parse_int(hit.inline_expr[1].value) for hit in bp1.hits_info]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 1, 3, 6, 10]

    # Extract values ordered by hits for loc2
    i_vals = [_parse_int(hit.inline_expr[0].value) for hit in bp2.hits_info]
    s_vals = [_parse_int(hit.inline_expr[1].value) for hit in bp2.hits_info]
    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 2, 5, 9, 14]

    # Backtrace strings non-empty and show function names
    joined = "\n".join(h.callstack for h in bp1.hits_info)
    assert "work_multiple" in joined
    assert "main" in joined
    assert b"sum=15\n" == res.stdout

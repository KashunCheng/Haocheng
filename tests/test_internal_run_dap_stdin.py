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

    # Find the report by file and line
    reports = list(res.reports.values())
    bp = next(r for r in reports if r.file_path == str(src) and r.line == 13)

    # Expect 4 iterations (1..4)
    assert bp.hit_times == 4
    assert len(bp.hits_info) == 4

    i_vals = [_parse_int(hit.inline_expr[0].value) for hit in bp.hits_info]
    a_vals = [_parse_int(hit.inline_expr[1].value) for hit in bp.hits_info]
    assert i_vals == [1, 2, 3, 4]
    assert a_vals == [1, 1, 2, 6]

    joined = "\n".join(h.callstack for h in bp.hits_info)
    assert "work_stdin" in joined
    assert "main" in joined
    assert b"acc=24\n" == res.stdout

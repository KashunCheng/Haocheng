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
async def test_continue_timeout():
    src = ROOT / "fixtures" / "timeout_continue.c"
    bin_path = _compile_fixture(src)

    # Set a breakpoint that will hit once, then program loops forever without further breakpoints
    loc = f"{src}:5"
    specs = [
        BreakpointSpec(
            location=loc, inline_expr=["x"], hit_limit=10, print_call_stack=False
        )
    ]

    # Small timeout should trigger during continue after the first hit
    res = await _run_dap([str(bin_path)], None, specs, timeout_sec=0.5)

    reports = list(res.reports.values())
    assert len(reports) == 1
    bp = reports[0]
    assert bp.file_path == str(src)
    assert bp.line == 5
    assert bp.hit_times == 1
    assert len(bp.hits_info) == 1
    # Inline expr x should be 0 at the hit (executed before increment)
    assert _parse_int(bp.hits_info[0].inline_expr[0].value) == 0
    # Timeout should mark exit_code as None
    assert res.exit_code is None
    assert res.timeout

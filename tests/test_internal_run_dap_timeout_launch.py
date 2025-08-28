import pytest

from haocheng import _run_dap, BreakpointSpec
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture


@pytest.mark.skipif(
    not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH"
)
@pytest.mark.skipif(
    not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter"
)
@pytest.mark.asyncio
async def test_launch_timeout():
    src = ROOT / "fixtures" / "timeout_launch.c"
    bin_path = _compile_fixture(src)

    # Set a breakpoint on an unreachable line after the infinite loop
    loc = f"{src}:6"
    specs = [
        BreakpointSpec(
            location=loc, inline_expr=[], hit_limit=10, print_call_stack=False
        )
    ]

    # Use a very short timeout to force launch timeout
    res = await _run_dap([str(bin_path)], None, specs, timeout_sec=0.5)

    # Expect that no hits occurred due to timeout
    reports = list(res.reports.values())
    assert len(reports) == 1
    bp = reports[0]
    assert bp.file_path == str(src)
    assert bp.line == 6
    assert bp.hit_times == 0
    assert len(bp.hits_info) == 0
    # Timeout should mark exit_code as None
    assert res.exit_code is None
    assert res.timeout

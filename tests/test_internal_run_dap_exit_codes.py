import signal
import pytest

from haocheng import _run_dap, BreakpointSpec
from tests import _which_lldb_adapter, _can_spawn_adapter, ROOT, _compile_fixture


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
@pytest.mark.asyncio
async def test_exit_code_nonzero():
    src = ROOT / "fixtures" / "exit_code_1.c"
    bin_path = _compile_fixture(src)

    # No breakpoint needed for exit code capture
    specs: list[BreakpointSpec] = []
    res = await _run_dap([str(bin_path)], None, specs)

    assert res.exit_code == 1


@pytest.mark.skipif(not _which_lldb_adapter(), reason="lldb-dap/lldb-vscode not found in PATH")
@pytest.mark.skipif(not _can_spawn_adapter(), reason="Sandbox cannot execute lldb adapter")
@pytest.mark.asyncio
async def test_exit_code_sigsegv():
    src = ROOT / "fixtures" / "sigsegv.c"
    bin_path = _compile_fixture(src)

    specs: list[BreakpointSpec] = []
    res = await _run_dap([str(bin_path)], None, specs)

    # Use negative code convention for signals; abs() equals SIGSEGV
    assert res.exit_code is None
    assert res.signal == 'EXC_BAD_ACCESS (code=1, address=0x0)'


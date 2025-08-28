import pytest

from haocheng import RuntimeDebugger
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
def test_get_runtime_feedback_basic_api():
    src = ROOT / "fixtures" / "loop_basic.c"
    bin_path = _compile_fixture(src)

    # New API breakpoint specification
    breakpoints = [
        {
            "location": f"{src}:6",
            "hit_limit": 10,
            "inline_expr": ["i", "sum"],
            "print_call_stack": True,
        }
    ]

    dbg = RuntimeDebugger()
    out = dbg.get_runtime_feedback_dict(
        [str(bin_path)], stdin=None, breakpoints=breakpoints
    )

    # Schema checks
    assert isinstance(out, dict)
    for key in ("stderr", "exit_code", "signal", "breakpoints"):
        assert key in out
    assert isinstance(out["breakpoints"], list) and len(out["breakpoints"]) == 1

    bp = out["breakpoints"][0]
    assert bp["file_path"] == str(src)
    assert bp["line"] == 6
    assert isinstance(bp["function_name"], str) and bp["function_name"]
    assert bp["hit_times"] == 5  # 0..4
    assert isinstance(bp["hits_info"], list) and len(bp["hits_info"]) == 5

    # Check inline expressions grouped per hit
    i_vals = []
    s_vals = []
    for hit in bp["hits_info"]:
        assert "callstack" in hit  # requested
        assert isinstance(hit["inline_expr"], list) and len(hit["inline_expr"]) == 2
        # Preserve order of inline_expr as configured
        assert hit["inline_expr"][0]["name"] == "i"
        assert hit["inline_expr"][1]["name"] == "sum"
        i_vals.append(_parse_int(hit["inline_expr"][0]["value"]))
        s_vals.append(_parse_int(hit["inline_expr"][1]["value"]))

    assert i_vals == [0, 1, 2, 3, 4]
    assert s_vals == [0, 0, 1, 3, 6]

    # stderr captured as string in new API
    assert "sum=10\n" in out["stderr"]
    assert not out["has_timeout"]

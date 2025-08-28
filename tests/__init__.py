import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _which_clang() -> str | None:
    return shutil.which("clang")


def _which_lldb_adapter() -> str | None:
    for name in ("lldb-dap", "lldb-vscode"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _can_spawn_adapter() -> bool:
    adapter = _which_lldb_adapter()
    if not adapter:
        return False
    try:
        # Try a quick help run to validate sandbox allows exec
        subprocess.run(
            [adapter, "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return True
    except Exception:
        return False


def _compile_fixture(source: Path) -> Path:
    clang = _which_clang()
    if not clang:
        pytest.skip("clang not found in PATH")
    out_dir = Path(tempfile.mkdtemp(prefix="haocheng_build_"))
    out_path = out_dir / source.stem
    cflags = [
        "-O0",
        "-g",
        "-fno-omit-frame-pointer",
        "-fno-inline",
        "-Wall",
    ]
    cmd = [clang, *cflags, str(source), "-o", str(out_path)]
    subprocess.run(cmd, check=True)
    return out_path.resolve()


def _parse_int(value: str) -> int:
    m = re.findall(r"-?\d+", value)
    if not m:
        raise AssertionError(f"No integer found in value: {value!r}")
    return int(m[-1])

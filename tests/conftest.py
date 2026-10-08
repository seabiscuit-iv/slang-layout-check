import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "slang_layout_check.py")
EXAMPLES = os.path.join(ROOT, "examples")
DATA = os.path.join(ROOT, "tests", "data")

sys.path.insert(0, ROOT)
import slang_layout_check as slc  # noqa: E402


def _has_libclang() -> bool:
    if importlib.util.find_spec("clang") is None:
        return False
    try:
        slc.load_cindex()
        return True
    except slc.ToolError:
        return False


def _has_slangc() -> bool:
    try:
        slc.find_slangc(os.environ.get("SLANGC"))
        return True
    except slc.ToolError:
        return False


HAS_LIBCLANG = _has_libclang()
HAS_SLANGC = _has_slangc()

needs_libclang = pytest.mark.skipif(not HAS_LIBCLANG, reason="libclang Python bindings not installed")
needs_slangc = pytest.mark.skipif(not HAS_SLANGC, reason="slangc not found (set SLANGC or VULKAN_SDK)")


def run_cli(*args, cwd=None):
    """Run the checker as a subprocess; returns CompletedProcess with text output."""
    cmd = [sys.executable, SCRIPT] + list(args)
    if os.environ.get("SLANGC") and "--slangc" not in args:
        cmd += ["--slangc", os.environ["SLANGC"]]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                          cwd=cwd or ROOT, timeout=600)


@pytest.fixture
def tmp_header(tmp_path):
    """Write a header (with slang_check.h available) and return its path."""
    def make(text, name="h.h"):
        p = tmp_path / name
        p.write_text(text)
        return str(p)
    return make


@pytest.fixture
def cindex():
    if not HAS_LIBCLANG:
        pytest.skip("libclang Python bindings not installed")
    return slc.load_cindex()


def which(name):
    return shutil.which(name)

#!/usr/bin/env python3
"""slang-layout-check: verify that C++ structs and Slang structs share one memory layout.

Annotate a C++ struct with the Slang struct it mirrors (see include/slang_check.h):

    struct [[slang_check("ShaderParticle")]] Particle { float pos[3]; float speed; };

This tool then compares the total size and every field's byte offset and size:

* C++ layout comes from libclang (the `libclang` PyPI wheel) parsing your real
  headers with your include dirs, defines, standard and target triple.
* Slang layout comes from `slangc -reflection-json` on a small generated wrapper
  shader that imports your shaders and binds each referenced struct.

Exit codes: 0 = all layouts match, 1 = mismatch or unsupported construct,
2 = tool / environment / usage error.

Single file, Python 3.8+, stdlib + libclang only.
"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "slang_layout_check: error: Python 3.8 or newer is required (found %d.%d)\n"
        % sys.version_info[:2]
    )
    sys.exit(2)

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

TOOL = "slang_layout_check"
# Versions this release was tested against. Other versions may work; the Slang
# reflection JSON in particular is not a stable, versioned format.
TESTED_LIBCLANG_VERSION = "18.1.1"
TESTED_SLANG_VERSION = "2026.8"

# include/slang_check.h emits clang::annotate("slang_check:" name), so plain
# clang::annotate() uses by other tools are never mistaken for ours.
ANNOTATION_PREFIX = "slang_check:"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_ERROR = 2

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_INCLUDE_DIR = os.path.join(HERE, "include")


class ToolError(Exception):
    """A usage or environment problem. Reported as `error:` with exit code 2."""


# =============================================================================
# Data model (shared by the C++ and Slang sides)
# =============================================================================


@dataclass
class TypeLayout:
    """Byte layout of one type.

    kind is one of: struct, array, scalar, vector, matrix, other.
    """

    kind: str
    name: str
    size: int
    fields: List["FieldLayout"] = field(default_factory=list)  # struct
    element: Optional["TypeLayout"] = None  # array
    count: Optional[int] = None  # array length / vector width
    stride: Optional[int] = None  # array element stride
    scalar: Optional[str] = None  # "bool" for booleans, else a type name
    # Constructs inside a nested struct we cannot describe (bitfields, ...).
    # Only reported if the comparison actually descends into this struct.
    problems: List[str] = field(default_factory=list)


@dataclass
class FieldLayout:
    name: str
    offset: int  # bytes, relative to the enclosing struct
    layout: TypeLayout
    line: Optional[int] = None


@dataclass
class CppStruct:
    cpp_name: str  # qualified, e.g. gfx::Particle
    slang_name: str  # from the annotation
    file: str
    line: int
    layout: Optional[TypeLayout]  # None if the struct itself is unsupported
    problems: List[str] = field(default_factory=list)  # unsupported constructs


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning" | "note"
    message: str
    file: Optional[str] = None
    line: Optional[int] = None


@dataclass
class StructResult:
    cpp: CppStruct
    slang: Optional[TypeLayout]
    diagnostics: List[Diagnostic]

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)


def _run(cmd: Sequence[str], input_text: Optional[str] = None, timeout: int = 120):
    """Run a command, returning CompletedProcess or None if it could not start."""
    try:
        return subprocess.run(
            list(cmd),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


# =============================================================================
# C++ side: libclang setup and flag assembly
# =============================================================================

# libclang's CXTypeLayoutError codes, returned by get_size / get_offset.
_LAYOUT_ERRORS = {
    -1: "invalid type",
    -2: "incomplete type",
    -3: "dependent type (templates are not supported)",
    -4: "type does not have a constant size",
    -5: "invalid field name",
    -6: "undeduced type",
}


def _layout_error(code: int) -> str:
    return _LAYOUT_ERRORS.get(code, "libclang layout error %d" % code)


def load_cindex(libclang_path: Optional[str] = None):
    """Import clang.cindex and make sure the shared library loads."""
    try:
        import clang.cindex as ci  # noqa: WPS433 (runtime import is deliberate)
    except ImportError as e:
        raise ToolError(
            "cannot import clang.cindex (%s).\n  Install the pinned bindings with:\n"
            "    %s -m pip install libclang==%s"
            % (e, sys.executable, TESTED_LIBCLANG_VERSION)
        )
    if libclang_path:
        try:
            ci.Config.set_library_file(libclang_path)
        except Exception as e:  # already loaded
            raise ToolError("cannot use --libclang %s: %s" % (libclang_path, e))
    try:
        ci.Index.create()
    except Exception as e:
        raise ToolError("the libclang shared library could not be loaded: %s" % e)
    return ci


def libclang_version(ci) -> Tuple[str, Optional[int]]:
    """Return (version string, major) of the loaded libclang."""
    text = "unknown"
    try:
        fn = ci.conf.lib.clang_getClangVersion
        fn.argtypes = []
        fn.restype = ci._CXString
        res = fn()
        text = res if isinstance(res, str) else ci._CXString.from_result(res)
    except Exception:
        pass
    m = re.search(r"version (\d+)\.", text)
    return text, (int(m.group(1)) if m else None)


def _compiler_flavor(cxx: Optional[str]) -> Optional[str]:
    """'msvc' for cl/clang-cl, 'gnu' for gcc/clang style drivers, None if unknown."""
    if not cxx:
        return None
    base = os.path.basename(cxx).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base in ("cl", "clang-cl") or base.startswith("clang-cl"):
        return "msvc"
    return "gnu"


def _clang_major(exe: str) -> Optional[int]:
    proc = _run([exe, "--version"], timeout=30)
    if proc is None or proc.returncode != 0:
        return None
    out = proc.stdout + proc.stderr
    # Apple clang's version numbers do not track LLVM's, so never treat it as a match.
    if "Apple" in out:
        return None
    m = re.search(r"clang version (\d+)\.", out)
    return int(m.group(1)) if m else None


def find_matching_resource_dir(major: int, cxx: Optional[str]) -> Optional[str]:
    """Find a clang whose major version equals libclang's and return its resource dir.

    The libclang wheel ships no builtin headers (stddef.h, stdint.h, ...).  A
    resource dir from a *different* clang major is not safe: e.g. clang 22's
    builtin headers use predefined macros (__INT32_C) that clang 18 lacks.
    """
    names = ["clang-%d" % major, "clang++-%d" % major]
    if cxx and _compiler_flavor(cxx) == "gnu" and "clang" in os.path.basename(cxx):
        names.append(cxx)
    names += ["clang", "clang++"]
    extra = [
        "/usr/lib/llvm-%d/bin/clang" % major,
        "/opt/homebrew/opt/llvm@%d/bin/clang" % major,
        "/usr/local/opt/llvm@%d/bin/clang" % major,
    ]
    seen = set()
    for name in names + extra:
        exe = shutil.which(name) if not os.path.isabs(name) else (name if os.path.isfile(name) else None)
        if not exe or exe in seen:
            continue
        seen.add(exe)
        if _clang_major(exe) != major:
            continue
        proc = _run([exe, "-print-resource-dir"], timeout=30)
        if proc and proc.returncode == 0:
            rd = proc.stdout.strip()
            if os.path.isfile(os.path.join(rd, "include", "stddef.h")):
                return rd
    return None


def host_compiler_include_dirs(cxx: str) -> Tuple[List[str], List[str]]:
    """Ask a GCC/Clang-style compiler for its system include dirs.

    Returns (include dirs, framework dirs) in search order.
    """
    proc = _run([cxx, "-E", "-x", "c++", "-", "-v"], input_text="", timeout=60)
    if proc is None:
        return [], []
    dirs: List[str] = []
    frameworks: List[str] = []
    active = False
    for line in proc.stderr.splitlines():
        if line.startswith("#include <...> search starts here"):
            active = True
            continue
        if line.startswith("End of search list"):
            break
        if active:
            entry = line.strip()
            if entry.endswith("(framework directory)"):
                frameworks.append(entry[: -len("(framework directory)")].strip())
            elif entry:
                dirs.append(os.path.normpath(entry))
    return dirs, frameworks


def _dumpmachine(cxx: str) -> Optional[str]:
    proc = _run([cxx, "-dumpmachine"], timeout=30)
    if proc and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


# Flags from a compile database that can influence struct layout or parsing.
_KEEP_WITH_VALUE = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-isysroot",
                    "--sysroot", "-target", "-D", "-U", "-iframework", "-F"}
_KEEP_EXACT = {"-m32", "-m64", "-mms-bitfields", "-malign-double", "-fms-extensions",
               "-fms-compatibility", "-fshort-wchar", "-fshort-enums", "-funsigned-char",
               "-fsigned-char", "-fpack-struct", "-pthread"}
_KEEP_PREFIX = ("-I", "-D", "-U", "-std=", "--target=", "--sysroot=", "-fpack-struct=",
                "-isystem", "-F", "-march=", "-mabi=")
_PATH_FLAGS = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-iframework", "-F"}


def _sanitize_gnu_args(args: List[str], directory: str) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _KEEP_WITH_VALUE and i + 1 < len(args):
            val = args[i + 1]
            if a in _PATH_FLAGS and not os.path.isabs(val):
                val = os.path.normpath(os.path.join(directory, val))
            out += [a, val]
            i += 2
            continue
        if a in _KEEP_EXACT:
            out.append(a)
        elif a.startswith(_KEEP_PREFIX):
            for p in ("-I", "-isystem", "-F"):
                if a.startswith(p) and len(a) > len(p):
                    val = a[len(p):]
                    if not os.path.isabs(val):
                        a = p + os.path.normpath(os.path.join(directory, val))
                    break
            out.append(a)
        i += 1
    return out


def _sanitize_msvc_args(args: List[str], directory: str) -> List[str]:
    """Translate the layout-relevant subset of cl.exe / clang-cl flags."""
    out: List[str] = []
    i = 0

    def path(v: str) -> str:
        return v if os.path.isabs(v) else os.path.normpath(os.path.join(directory, v))

    while i < len(args):
        a = args[i]
        low = a.lower()
        nxt = args[i + 1] if i + 1 < len(args) else None
        if low in ("/i", "-i") and nxt is not None:
            out.append("-I" + path(nxt)); i += 2; continue
        if low in ("/d", "-d") and nxt is not None:
            out.append("-D" + nxt); i += 2; continue
        if low in ("/u", "-u") and nxt is not None:
            out.append("-U" + nxt); i += 2; continue
        if low in ("/external:i", "-external:i", "/imsvc", "-imsvc") and nxt is not None:
            out += ["-isystem", path(nxt)]; i += 2; continue
        if low in ("/fi", "-fi") and nxt is not None:
            out += ["-include", path(nxt)]; i += 2; continue
        if a[:2] in ("/I", "-I") and len(a) > 2:
            out.append("-I" + path(a[2:]))
        elif a[:2] in ("/D", "-D") and len(a) > 2:
            out.append("-D" + a[2:])
        elif a[:2] in ("/U", "-U") and len(a) > 2:
            out.append("-U" + a[2:])
        elif low.startswith(("/external:i", "-external:i")):
            out += ["-isystem", path(a[len("/external:I"):])]
        elif low.startswith(("/std:", "-std:")):
            std = a[5:].lower()
            out.append("-std=" + ("c++2c" if std == "c++latest" else std))
        elif low.startswith(("/zp", "-zp")):
            out.append("-fpack-struct=" + (a[3:] or "1"))
        i += 1
    return out


def flags_from_compile_commands(ci, db_path: str, headers: Sequence[str]) -> Tuple[List[str], bool]:
    """Extract layout-relevant flags from compile_commands.json.

    Headers rarely have their own entry, so we try (in order): the header itself,
    a source file with the same stem next to it, then the first C++ command.
    Returns (flags, is_msvc_driver).
    """
    db_dir = db_path if os.path.isdir(db_path) else os.path.dirname(os.path.abspath(db_path))
    try:
        db = ci.CompilationDatabase.fromDirectory(db_dir)
    except Exception as e:
        raise ToolError("cannot load compile database from %s: %s" % (db_dir, e))

    def first(cmds):
        if cmds is None:
            return None
        for c in cmds:
            return c
        return None

    cmd = None
    for h in headers:
        cmd = first(db.getCompileCommands(os.path.abspath(h)))
        if cmd:
            break
        stem = os.path.splitext(os.path.abspath(h))[0]
        for ext in (".cpp", ".cc", ".cxx", ".c++"):
            if os.path.isfile(stem + ext):
                cmd = first(db.getCompileCommands(stem + ext))
                if cmd:
                    break
        if cmd:
            break
    if cmd is None:
        all_cmds = list(db.getAllCompileCommands() or [])
        cpp = [c for c in all_cmds if os.path.splitext(c.filename)[1].lower() in (".cpp", ".cc", ".cxx", ".c++")]
        cmd = (cpp or all_cmds or [None])[0]
    if cmd is None:
        raise ToolError("compile database %s contains no commands" % db_dir)

    args = list(cmd.arguments)
    driver, rest = args[0], args[1:]
    if _compiler_flavor(driver) == "msvc" or "--driver-mode=cl" in rest:
        return _sanitize_msvc_args(rest, cmd.directory), True
    return _sanitize_gnu_args(rest, cmd.directory), False


@dataclass
class CxxConfig:
    args: List[str]
    target: Optional[str]
    msvc: bool
    builtin_headers: str  # human-readable description of how builtins were found
    warnings: List[str] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)  # shown only if parsing fails


def _pick_std(opts, user_flags: Sequence[str]) -> Optional[str]:
    if any(f.startswith("-std=") for f in user_flags):
        return None  # user/compile-db flag wins
    std = (opts.std or "").strip()
    if not std and opts.compile_features:
        nums = [int(m) for m in re.findall(r"cxx_std_(\d+)", opts.compile_features)]
        if nums:
            std = str(max(nums))
    if not std:
        std = "17"
    if not std.startswith(("c++", "gnu++")):
        std = "c++" + std
    return "-std=" + std


def build_clang_args(opts, ci, libclang_major: Optional[int]) -> CxxConfig:
    """Assemble the libclang command line from CLI options and the environment."""
    warnings: List[str] = []
    hints: List[str] = []
    db_flags: List[str] = []
    db_msvc = False
    if not opts.cxx and sys.platform != "win32":
        # The host compiler supplies the target triple and, if no matching clang
        # is installed, the system include dirs.
        opts.cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if opts.compile_commands:
        db_flags, db_msvc = flags_from_compile_commands(ci, opts.compile_commands, opts.headers)

    user = db_flags + ["-I" + d for d in opts.include_dirs] + ["-D" + d for d in opts.defines] + list(opts.cxx_flags)

    target = opts.target
    if not target:
        for i, f in enumerate(user):
            if f == "-target" and i + 1 < len(user):
                target = user[i + 1]
            elif f.startswith("--target="):
                target = f.split("=", 1)[1]
    flavor = _compiler_flavor(opts.cxx)
    if not target and flavor == "gnu":
        target = _dumpmachine(opts.cxx)
    if target:
        msvc = "msvc" in target
    else:
        # libclang's default triple on Windows is *-pc-windows-msvc.
        msvc = db_msvc or flavor == "msvc" or (sys.platform == "win32" and flavor is None)

    args = ["-x", "c++"]
    std = _pick_std(opts, user)
    if std:
        args.append(std)
    args += ["-DSLANG_LAYOUT_CHECK", "-I" + SHIPPED_INCLUDE_DIR]
    if target and not any(f == "-target" or f.startswith("--target=") for f in user):
        args += ["-target", target]
    if msvc:
        # Match MSVC's ABI (long = 4 bytes, wchar_t = 2, MS record layout) and
        # let the newest MSVC STL accept libclang's older clang version.
        args += ["-fms-compatibility", "-fms-extensions", "-D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH"]

    # --- builtin headers (stddef.h, stdint.h, ...): the libclang wheel has none.
    builtin = "default search paths"
    user_controls = any(f in ("-resource-dir", "-nostdinc", "-nobuiltininc") for f in user)
    if user_controls:
        builtin = "user-supplied flags"
    elif opts.resource_dir and opts.resource_dir != "none":
        args += ["-resource-dir", opts.resource_dir]
        builtin = "--resource-dir %s" % opts.resource_dir
    else:
        rd = None
        if opts.resource_dir != "none" and libclang_major:
            rd = find_matching_resource_dir(libclang_major, opts.cxx)
        if rd:
            args += ["-resource-dir", rd]
            builtin = "clang %d resource dir %s" % (libclang_major, rd)
        elif msvc:
            builtin = "MSVC / Windows SDK headers (found by libclang)"
        elif opts.cxx and flavor == "gnu":
            dirs, fws = host_compiler_include_dirs(opts.cxx)
            if dirs:
                for d in dirs:
                    args += ["-isystem", d]
                for d in fws:
                    args += ["-iframework", d]
                builtin = "system include dirs of %s" % opts.cxx
                mismatched = [d for d in dirs if re.search(r"[/\\]clang[/\\]\d+", d)]
                if mismatched and libclang_major and not any(
                    re.search(r"[/\\]clang[/\\]%d([/\\.]|$)" % libclang_major, d) for d in mismatched
                ):
                    hints.append(
                        "builtin headers came from a different clang version (%s); install clang %d "
                        "or pass --resource-dir <clang-%d resource dir>" % (mismatched[0], libclang_major, libclang_major)
                    )
            else:
                warnings.append("could not query include dirs from --cxx %s" % opts.cxx)
        else:
            warnings.append(
                "no clang %s found and no --cxx given: builtin headers such as <stddef.h> may be "
                "missing (pass --cxx <your compiler> or --resource-dir)" % (libclang_major or "")
            )

    if sys.platform == "darwin" and not any(f in ("-isysroot",) or f.startswith("--sysroot") for f in user):
        proc = _run(["xcrun", "--show-sdk-path"], timeout=30)
        if proc and proc.returncode == 0 and proc.stdout.strip():
            args += ["-isysroot", proc.stdout.strip()]

    args += user
    return CxxConfig(args=args, target=target, msvc=msvc, builtin_headers=builtin, warnings=warnings, hints=hints)


# =============================================================================
# C++ side: parse_cpp
# =============================================================================


class CppParseError(Exception):
    def __init__(self, diagnostics: List[Diagnostic]):
        super().__init__("C++ parsing failed")
        self.diagnostics = diagnostics


def _cursor_file(c) -> Optional[str]:
    f = c.location.file
    return f.name if f is not None else None


def _in_system_header(loc) -> bool:
    try:
        return bool(loc.is_in_system_header)
    except AttributeError:  # very old bindings
        return False


def qualified_name(ci, cursor) -> str:
    parts = []
    c = cursor
    while c is not None and c.kind != ci.CursorKind.TRANSLATION_UNIT:
        if c.kind != ci.CursorKind.LINKAGE_SPEC:
            parts.append(c.spelling or "(anonymous)")
        c = c.semantic_parent
    return "::".join(reversed(parts))


def _is_anonymous_member(name: str) -> bool:
    return not name or "(anonymous" in name or "(unnamed" in name


def cpp_type_layout(ci, ctype, path: str) -> Tuple[Optional[TypeLayout], List[str]]:
    """Describe a field's type. Returns (layout or None, problems)."""
    TK = ci.TypeKind
    canon = ctype.get_canonical()
    size = canon.get_size()
    if size < 0:
        return None, ["field '%s' of type '%s': cannot compute size (%s)" % (path, ctype.spelling, _layout_error(size))]
    k = canon.kind
    if k == TK.RECORD:
        decl = canon.get_declaration()
        if decl.kind == ci.CursorKind.UNION_DECL:
            return TypeLayout("other", ctype.spelling, size), []
        layout = cpp_record_layout(ci, canon, path + ".", ctype.spelling)
        return layout, []
    if k == TK.CONSTANTARRAY:
        el, probs = cpp_type_layout(ci, canon.element_type, path + "[]")
        if el is None:
            return None, probs
        return (
            TypeLayout("array", ctype.spelling, size, element=el, count=canon.element_count,
                       stride=canon.element_type.get_canonical().get_size()),
            probs,
        )
    if k in (TK.INCOMPLETEARRAY, TK.VARIABLEARRAY, TK.DEPENDENTSIZEDARRAY):
        return None, ["field '%s': flexible / variable-length arrays are not supported" % path]
    if k == TK.BOOL:
        return TypeLayout("scalar", "bool", size, scalar="bool"), []
    if k in (TK.POINTER, TK.MEMBERPOINTER, TK.LVALUEREFERENCE, TK.RVALUEREFERENCE):
        return TypeLayout("other", ctype.spelling, size), []
    return TypeLayout("scalar", ctype.spelling, size, scalar=canon.spelling), []


def cpp_record_layout(ci, rtype, prefix: str, display: str) -> TypeLayout:
    """Layout of a struct/class type. Unsupported constructs land in .problems."""
    CK = ci.CursorKind
    decl = rtype.get_declaration()
    layout = TypeLayout("struct", display, rtype.get_size())
    where = prefix[:-1] if prefix else "struct"
    has_virtual = False
    for ch in decl.get_children():
        if ch.kind == CK.CXX_BASE_SPECIFIER:
            layout.problems.append(
                "%s inherits from '%s': inheritance is not supported (copy the base's fields into the struct)"
                % (where, ch.type.spelling)
            )
        elif ch.kind in (CK.CXX_METHOD, CK.DESTRUCTOR) and ch.is_virtual_method():
            has_virtual = True
    if has_virtual:
        layout.problems.append("%s has virtual functions (hidden vtable pointer): not supported" % where)

    for f in rtype.get_fields():
        name = f.spelling
        if _is_anonymous_member(name):
            what = "union" if "union" in name else "struct"
            layout.problems.append(
                "%sanonymous %s member at line %d is not supported (give it a field name)"
                % ("in '%s': " % where if prefix else "", what, f.location.line)
            )
            continue
        path = prefix + name
        if f.is_bitfield():
            layout.problems.append("field '%s' is a bitfield: not supported (Slang has no bitfields)" % path)
            continue
        off = rtype.get_offset(name)
        if off < 0:
            layout.problems.append("field '%s': cannot compute offset (%s)" % (path, _layout_error(off)))
            continue
        flayout, probs = cpp_type_layout(ci, f.type, path)
        layout.problems.extend(probs)
        if flayout is None:
            continue
        layout.fields.append(FieldLayout(name, off // 8, flayout, f.location.line))
    return layout


def _collect_struct(ci, cursor, annotations: List[str]) -> CppStruct:
    CK = ci.CursorKind
    names = sorted(set(a[len(ANNOTATION_PREFIX):] for a in annotations))
    cs = CppStruct(
        cpp_name=qualified_name(ci, cursor),
        slang_name=names[0],
        file=os.path.normpath(_cursor_file(cursor) or "<unknown>"),
        line=cursor.location.line,
        layout=None,
    )
    if len(names) > 1:
        cs.problems.append("struct has several slang_check annotations (%s); use exactly one" % ", ".join(names))
    if cursor.kind in (CK.CLASS_TEMPLATE, CK.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION):
        cs.problems.append("templates are not supported (annotate a concrete, non-template struct)")
        return cs
    if cursor.kind == CK.UNION_DECL:
        cs.problems.append("unions are not supported (annotate a struct)")
        return cs
    size = cursor.type.get_size()
    if size < 0:
        cs.problems.append("cannot compute struct size (%s)" % _layout_error(size))
        return cs
    cs.layout = cpp_record_layout(ci, cursor.type, "", cs.cpp_name)
    cs.problems.extend(cs.layout.problems)  # top-level problems always count
    return cs


def _format_clang_diag(d) -> Tuple[Optional[str], Optional[int], str]:
    loc = d.location
    return (os.path.normpath(loc.file.name) if loc.file else None, loc.line if loc.file else None, d.spelling)


_BUILTIN_HEADER_RE = re.compile(r"'(stddef|stdint|stdarg|stdbool|stdalign|float|limits|inttypes)\.h' file not found")


def _diag_hints(message: str) -> List[str]:
    hints = []
    if _BUILTIN_HEADER_RE.search(message):
        hints.append(
            "libclang could not find clang's builtin headers. Install clang %s (so `clang-%s "
            "-print-resource-dir` works), pass --cxx <your compiler>, or pass --resource-dir <dir>."
            % (TESTED_LIBCLANG_VERSION.split(".")[0], TESTED_LIBCLANG_VERSION.split(".")[0])
        )
    if "misplaced attributes" in message or "an attribute list cannot appear here" in message:
        hints.append('put the attribute after the struct keyword: struct [[slang_check("Name")]] MyStruct { ... };')
    if "STL1000" in message:
        hints.append("MSVC STL / libclang version mismatch; pass --target *-pc-windows-msvc so MSVC mode is enabled")
    return hints


def parse_cpp(ci, headers: Sequence[str], clang_args: Sequence[str], ignore_system_errors: bool = False):
    """Parse headers with libclang and collect every annotated struct.

    Returns (structs, warning diagnostics, list of all included files).
    Raises CppParseError if libclang reported errors.
    """
    tu_name = os.path.join(tempfile.gettempdir(), "__slang_layout_check_tu.cpp")
    content = "".join('#include "%s"\n' % os.path.abspath(h).replace("\\", "/") for h in headers)
    try:
        tu = ci.Index.create().parse(
            tu_name,
            args=list(clang_args),
            unsaved_files=[(tu_name, content)],
            options=ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES,
        )
    except ci.TranslationUnitLoadError as e:
        raise ToolError("libclang failed to parse the headers (%s). Arguments: %s" % (e, " ".join(clang_args)))

    errors: List[Diagnostic] = []
    warnings: List[Diagnostic] = []
    for d in tu.diagnostics:
        file, line, msg = _format_clang_diag(d)
        system = d.location.file is not None and _in_system_header(d.location)
        if d.severity >= ci.Diagnostic.Error:
            if system and ignore_system_errors and d.severity < ci.Diagnostic.Fatal:
                continue
            errors.append(Diagnostic("fatal error" if d.severity >= ci.Diagnostic.Fatal else "error", msg, file, line))
            for h in _diag_hints(msg):
                errors.append(Diagnostic("note", h, file, line))
        elif "unknown attribute 'slang_check'" in msg:
            errors.append(Diagnostic("error", msg + " (this struct would not be checked)", file, line))
            errors.append(Diagnostic("note", "add #include <slang_check.h> to this header", file, line))
    if errors:
        raise CppParseError(errors)

    CK = ci.CursorKind
    records = {CK.STRUCT_DECL, CK.CLASS_DECL, CK.UNION_DECL, CK.CLASS_TEMPLATE,
               CK.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION}
    containers = records | {CK.NAMESPACE, CK.LINKAGE_SPEC}
    structs: List[CppStruct] = []
    seen = set()

    def walk(cursor):
        for c in cursor.get_children():
            if c.location.file is None or _in_system_header(c.location):
                continue
            if c.kind in records and c.is_definition():
                anns = [a.spelling for a in c.get_children()
                        if a.kind == CK.ANNOTATE_ATTR and a.spelling.startswith(ANNOTATION_PREFIX)]
                key = (_cursor_file(c), c.location.line, c.location.column)
                if anns and key not in seen:
                    seen.add(key)
                    structs.append(_collect_struct(ci, c, anns))
            if c.kind in containers:
                walk(c)

    walk(tu.cursor)
    included = sorted({os.path.normpath(inc.include.name) for inc in tu.get_includes()} |
                      {os.path.normpath(os.path.abspath(h)) for h in headers})
    return structs, warnings, included


# =============================================================================
# Slang side: run_slangc
# =============================================================================

WRAPPER_ENTRY = "__slc_entry"
# Flags the checker supplies itself; dropping them lets users share one flag
# list between the real shader build and the check.
_SLANG_STRIP_WITH_VALUE = {"-o", "-entry", "-stage", "-reflection-json", "-depfile", "-output-dir"}


def find_slangc(explicit: Optional[str]) -> str:
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        raise ToolError("--slangc %s does not exist" % explicit)
    found = shutil.which("slangc")
    if found:
        return found
    sdk = os.environ.get("VULKAN_SDK")
    if sdk:
        exe = "slangc.exe" if os.name == "nt" else "slangc"
        for sub in ("Bin", "bin"):
            p = os.path.join(sdk, sub, exe)
            if os.path.isfile(p):
                return p
    raise ToolError(
        "slangc not found. Pass --slangc <path>, put slangc on PATH, or install the Vulkan SDK "
        "(which sets VULKAN_SDK). Tested with Slang %s." % TESTED_SLANG_VERSION
    )


def slangc_version(slangc: str) -> str:
    proc = _run([slangc, "-v"], timeout=30)
    if proc is None:
        return "unknown"
    return (proc.stdout.strip() or proc.stderr.strip() or "unknown").splitlines()[0]


def sanitize_slang_flags(flags: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Drop flags the checker controls; ensure exactly one -target."""
    out: List[str] = []
    warnings: List[str] = []
    i = 0
    targets = 0
    while i < len(flags):
        f = flags[i]
        if f in _SLANG_STRIP_WITH_VALUE:
            i += 2
            continue
        if f == "-target" and i + 1 < len(flags):
            targets += 1
            if targets == 1:
                out += [f, flags[i + 1]]
            i += 2
            continue
        if not f.startswith("-") and f.lower().endswith((".slang", ".hlsl")):
            i += 1  # a source file from a shared flag list; shaders come from --shader
            continue
        out.append(f)
        i += 1
    if targets == 0:
        warnings.append("no -target in --slang-flag; defaulting to '-target spirv'. Pass the same flags as your shader build.")
        out = ["-target", "spirv"] + out
    elif targets > 1:
        warnings.append("several -target flags given; layouts are checked for the first one only")
    return out, warnings


def make_wrapper(shaders: Sequence[str], names: Sequence[str], buffer_kind: str, import_mode: str) -> str:
    """Generate the wrapper shader.

    slangc's -reflection-json only describes types reachable from bound
    parameters, and never reports a struct's total size. So for every requested
    struct T we bind a buffer of `struct W { T v[2]; }`: the array's
    uniformStride is sizeof(T) including tail padding, which is what C++'s
    sizeof() means too.
    """
    lines = ["// Generated by slang_layout_check. Do not edit."]
    for s in shaders:
        p = os.path.abspath(s).replace("\\", "/")
        lines.append(('import "%s";' if import_mode == "import" else '#include "%s"') % p)
    buf = "StructuredBuffer" if buffer_kind == "structured" else "ConstantBuffer"
    for i, n in enumerate(names):
        lines.append("struct __slc_wrap_%d { %s v[2]; };" % (i, n))
        lines.append("%s<__slc_wrap_%d> __slc_buf_%d;" % (buf, i, i))
    lines.append('[shader("compute")] [numthreads(1, 1, 1)] void %s() {}' % WRAPPER_ENTRY)
    return "\n".join(lines) + "\n"


@dataclass
class SlangRun:
    data: Optional[dict]
    log: str
    cmd: List[str]
    depfile: Optional[str]


def run_slangc(slangc: str, shaders: Sequence[str], names: Sequence[str], flags: Sequence[str],
               workdir: str, buffer_kind: str = "structured", import_mode: str = "import") -> SlangRun:
    """Compile a wrapper shader and return the parsed reflection JSON (or the log on failure)."""
    wrapper = os.path.join(workdir, "slang_layout_check_wrapper.slang")
    out_json = os.path.join(workdir, "reflection.json")
    out_bin = os.path.join(workdir, "wrapper.out")
    depfile = os.path.join(workdir, "wrapper.d")
    for p in (out_json, depfile):
        if os.path.exists(p):
            os.remove(p)
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write(make_wrapper(shaders, names, buffer_kind, import_mode))
    include_dirs: List[str] = []
    for s in shaders:
        d = os.path.dirname(os.path.abspath(s))
        if d not in include_dirs:
            include_dirs.append(d)
    cmd = [slangc, wrapper] + list(flags)
    for d in include_dirs:
        cmd += ["-I", d]
    cmd += ["-entry", WRAPPER_ENTRY, "-stage", "compute", "-reflection-json", out_json,
            "-o", out_bin, "-depfile", depfile]
    proc = _run(cmd, timeout=600)
    if proc is None:
        raise ToolError("could not execute slangc: %s" % slangc)
    log = (proc.stdout + proc.stderr).strip()
    # Never trust the JSON of a failed run: slangc may write it and then fail
    # (observed: "undefined identifier" followed by a crash in Slang 2026.8).
    if proc.returncode != 0 or not os.path.isfile(out_json):
        return SlangRun(None, log or "slangc exited with code %d" % proc.returncode, cmd, None)
    try:
        with open(out_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as e:
        raise ToolError("slangc wrote invalid reflection JSON (%s): %s" % (out_json, e))
    return SlangRun(data, log, cmd, depfile if os.path.isfile(depfile) else None)


_UNDEFINED_RE = re.compile(r"undefined identifier '([^']+)'")


def _first_error(log: str) -> str:
    for line in log.splitlines():
        if "error" in line:
            m = _UNDEFINED_RE.search(log)
            return "undefined identifier '%s'" % m.group(1) if m else line.strip()
    return log.strip().splitlines()[0] if log.strip() else "unknown slangc failure"


def resolve_slang_structs(slangc, shaders, names, flags, workdir, buffer_kind, import_mode, verbose=False):
    """Look up every requested Slang struct.

    Returns (layouts by name, failures by name -> reason, last successful SlangRun).
    Structs that cannot be resolved are reported individually rather than failing
    the whole run.
    """
    remaining = list(dict.fromkeys(names))
    failed: Dict[str, str] = {}
    run = None
    for _ in range(len(remaining) + 2):
        if not remaining:
            break
        run = run_slangc(slangc, shaders, remaining, flags, workdir, buffer_kind, import_mode)
        if verbose:
            sys.stderr.write("%s: slangc: %s\n" % (TOOL, " ".join(run.cmd)))
        if run.data is not None:
            break
        if import_mode == "import" and "declaration not accessible" in run.log:
            # Files with a `module X;` declaration keep non-`public` structs
            # internal to the module, so an importing wrapper cannot name them.
            # #include-ing the files instead makes them part of the wrapper.
            if verbose:
                sys.stderr.write("%s: non-public Slang declarations; retrying with #include\n" % TOOL)
            import_mode = "include"
            continue
        undefined = set(_UNDEFINED_RE.findall(run.log))
        hit = [n for n in remaining if n in undefined or n.split("::")[-1] in undefined]
        if hit:
            for n in hit:
                failed[n] = "not found"
            remaining = [n for n in remaining if n not in hit]
            continue
        # Could not attribute the failure. Do the shaders compile on their own?
        base = run_slangc(slangc, shaders, [], flags, workdir, buffer_kind, import_mode)
        if base.data is None:
            raise ToolError("slangc failed to compile the shaders:\n%s\n  command: %s" % (base.log, " ".join(base.cmd)))
        ok = []
        for n in remaining:
            single = run_slangc(slangc, shaders, [n], flags, workdir, buffer_kind, import_mode)
            if single.data is None:
                failed[n] = _first_error(single.log)
            else:
                ok.append(n)
        remaining = ok
    if not remaining:
        return {}, failed, None
    if run is None or run.data is None:
        raise ToolError("slangc failed:\n%s" % (run.log if run else ""))
    return parse_reflection(run.data, remaining), failed, run


# =============================================================================
# Slang side: parse_reflection
# =============================================================================
#
# Schema observed with slangc 2026.8 (-reflection-json). Relevant parts:
#
#   {"parameters": [
#     {"name": "__slc_buf_0",
#      "type": {"kind": "resource", "baseShape": "structuredBuffer",      # StructuredBuffer<W>
#               "resultType": <W>}}                                      #   or
#     {"name": ..., "type": {"kind": "constantBuffer", "elementType": <W>}}  # ConstantBuffer<W>
#   ]}
#   <W>      = {"kind": "struct", "name": "__slc_wrap_0", "fields": [{"name": "v", "type": <array>}]}
#   <array>  = {"kind": "array", "elementCount": 2, "uniformStride": 32, "elementType": <T>}
#   <T>      = {"kind": "struct", "name": "ShaderParticle", "fields": [<field>...]}
#   <field>  = {"name": "pos", "type": <type>,
#               "binding": {"kind": "uniform", "offset": 0, "size": 12, "elementStride": 4}}
#   <type>   = {"kind": "scalar", "scalarType": "float32"|"uint32"|"bool"|...}
#            | {"kind": "vector", "elementCount": 3, "elementType": <scalar>}
#            | {"kind": "matrix", "rowCount": 4, "columnCount": 4, "elementType": <scalar>}
#            | {"kind": "array", "elementCount": N, "uniformStride": S, "elementType": <type>}
#            | {"kind": "struct", "name": ..., "fields": [...]}
#
# Offsets of nested struct fields are relative to the nested struct.


class ReflectionFormatError(ToolError):
    pass


_SLANG_SCALAR_NAMES = {
    "float16": "half", "float32": "float", "float64": "double",
    "int8": "int8_t", "int16": "int16_t", "int32": "int", "int64": "int64_t",
    "uint8": "uint8_t", "uint16": "uint16_t", "uint32": "uint", "uint64": "uint64_t",
    "bool": "bool",
}


def _req(d: dict, key: str, where: str):
    if not isinstance(d, dict) or key not in d:
        raise ReflectionFormatError(
            "unexpected slangc reflection JSON: missing '%s' in %s (this tool was tested with Slang %s)"
            % (key, where, TESTED_SLANG_VERSION)
        )
    return d[key]


def _uniform_binding(f: dict) -> Optional[dict]:
    b = f.get("binding")
    if isinstance(b, dict) and b.get("kind") == "uniform":
        return b
    for b in f.get("bindings", []) or []:
        if isinstance(b, dict) and b.get("kind") == "uniform":
            return b
    return None


def _scalar_name(t: dict) -> str:
    st = t.get("scalarType", "?")
    return _SLANG_SCALAR_NAMES.get(st, st)


def slang_type_layout(t: dict, size: int, where: str) -> TypeLayout:
    kind = _req(t, "kind", where)
    if kind == "struct":
        layout = TypeLayout("struct", t.get("name", "struct"), size)
        for f in t.get("fields", []):
            fname = _req(f, "name", where)
            b = _uniform_binding(f)
            if b is None:
                layout.problems.append(
                    "Slang field '%s' has no byte layout (resources / opaque types cannot be mirrored in C++)" % fname
                )
                continue
            ft = slang_type_layout(_req(f, "type", where + "." + fname), int(b.get("size", 0)), where + "." + fname)
            layout.fields.append(FieldLayout(fname, int(_req(b, "offset", where + "." + fname)), ft))
        return layout
    if kind == "array":
        stride = int(t.get("uniformStride", 0))
        count = int(t.get("elementCount", 0))
        el = slang_type_layout(_req(t, "elementType", where), stride, where + "[]")
        return TypeLayout("array", "%s[%d]" % (el.name, count), size, element=el, count=count, stride=stride)
    if kind == "scalar":
        name = _scalar_name(t)
        return TypeLayout("scalar", name, size, scalar=name)
    if kind == "vector":
        el = t.get("elementType", {})
        n = int(t.get("elementCount", 0))
        return TypeLayout("vector", "%s%d" % (_scalar_name(el), n), size, count=n)
    if kind == "matrix":
        el = t.get("elementType", {})
        return TypeLayout("matrix", "%s%dx%d" % (_scalar_name(el), t.get("rowCount", 0), t.get("columnCount", 0)), size)
    return TypeLayout("other", kind, size)


def parse_reflection(data: dict, names: Sequence[str]) -> Dict[str, TypeLayout]:
    """Extract the layout of each wrapped struct from slangc's reflection JSON."""
    params = {p.get("name"): p for p in _req(data, "parameters", "reflection root") if isinstance(p, dict)}
    layouts: Dict[str, TypeLayout] = {}
    for i, name in enumerate(names):
        pname = "__slc_buf_%d" % i
        if pname not in params:
            raise ReflectionFormatError(
                "slangc reflection JSON has no parameter '%s' (expected one per checked struct; tested with Slang %s)"
                % (pname, TESTED_SLANG_VERSION)
            )
        t = _req(params[pname], "type", pname)
        wrapper = t.get("resultType") or t.get("elementType")
        if wrapper is None:
            raise ReflectionFormatError(
                "unexpected reflection JSON for '%s': buffer type of kind '%s' has neither resultType nor elementType"
                % (pname, t.get("kind"))
            )
        fields = _req(wrapper, "fields", pname)
        if not fields:
            raise ReflectionFormatError("unexpected reflection JSON: wrapper struct for '%s' has no fields" % name)
        arr = _req(fields[0], "type", pname + ".v")
        stride = int(_req(arr, "uniformStride", pname + ".v"))
        layouts[name] = slang_type_layout(_req(arr, "elementType", pname + ".v"), stride, name)
    return layouts


# =============================================================================
# compare
# =============================================================================


DEFAULT_PADDING_REGEX = r"^_*(pad|padding|reserved|unused)[_0-9]*$"


def _is_16_aligned_kind(t: TypeLayout) -> bool:
    return (t.kind == "vector" and t.count == 3) or t.kind in ("struct", "array", "matrix")


class _Collector:
    def __init__(self, cs: CppStruct, padding_regex: str = DEFAULT_PADDING_REGEX):
        self.cs = cs
        self.head = "%s vs %s" % (cs.cpp_name, cs.slang_name)
        self.diags: List[Diagnostic] = []
        self.align_hint_given = False
        self.padding = re.compile(padding_regex, re.IGNORECASE) if padding_regex else None

    def is_padding(self, name: str) -> bool:
        return bool(self.padding and self.padding.match(name))

    def error(self, msg: str):
        self.diags.append(Diagnostic("error", "%s: %s" % (self.head, msg), self.cs.file, self.cs.line))

    def note(self, msg: str):
        self.diags.append(Diagnostic("note", msg, self.cs.file, self.cs.line))


def _overlaps(f: FieldLayout, others: Sequence[FieldLayout]) -> Optional[FieldLayout]:
    lo, hi = f.offset, f.offset + f.layout.size
    for o in others:
        if o.offset < hi and lo < o.offset + o.layout.size:
            return o
    return None


def _compare_fields(c: TypeLayout, s: TypeLayout, prefix: str, cbase: int, sbase: int, out: _Collector):
    """Compare the fields of two struct layouts.

    cbase/sbase are the absolute offsets of this struct on each side. Nested
    offsets are compared relative to their own struct, so a misplaced nested
    struct is reported once rather than once per nested field. They are
    printed as absolute offsets when both sides agree on where the nested
    struct starts.
    """
    s_by = {f.name: f for f in s.fields}
    c_by = {f.name: f for f in c.fields}
    for f in c.fields:
        if f.name in s_by:
            continue
        if out.is_padding(f.name):
            hit = _overlaps(f, s.fields)
            if hit is not None:
                out.error("C++ padding field '%s%s' (bytes %d..%d) overlaps Slang field '%s%s'"
                          % (prefix, f.name, cbase + f.offset, cbase + f.offset + f.layout.size, prefix, hit.name))
            continue
        out.error("field '%s%s' exists only in C++" % (prefix, f.name))
    for f in s.fields:
        if f.name in c_by:
            continue
        if out.is_padding(f.name):
            hit = _overlaps(f, c.fields)
            if hit is not None:
                out.error("Slang padding field '%s%s' (bytes %d..%d) overlaps C++ field '%s%s'"
                          % (prefix, f.name, sbase + f.offset, sbase + f.offset + f.layout.size, prefix, hit.name))
            continue
        out.error("field '%s%s' exists only in Slang" % (prefix, f.name))
    common_c = [f.name for f in c.fields if f.name in s_by]
    common_s = [f.name for f in s.fields if f.name in c_by]
    if common_c != common_s:
        out.error("field order differs (cpp: %s; slang: %s)" % (", ".join(prefix + n for n in common_c),
                                                               ", ".join(prefix + n for n in common_s)))
    same_base = cbase == sbase
    owner = prefix.rstrip(".")
    for cf in c.fields:
        sf = s_by.get(cf.name)
        if sf is None:
            continue
        path = prefix + cf.name
        if cf.offset != sf.offset:
            if same_base:
                out.error("field '%s' offset mismatch (cpp=%d, slang=%d)" % (path, cbase + cf.offset, sbase + sf.offset))
            else:
                out.error("field '%s' offset within '%s' mismatch (cpp=%d, slang=%d)"
                          % (path, owner, cf.offset, sf.offset))
            if (not out.align_hint_given and sf.offset > cf.offset and _is_16_aligned_kind(sf.layout)
                    and common_c == common_s):
                out.align_hint_given = True
                out.note(
                    "Slang's default buffer layouts (std140/std430) align %s '%s' to 16 bytes; add explicit "
                    "padding in C++ (or alignas(16)), or compile shaders with -fvk-use-scalar-layout"
                    % (sf.layout.name, path)
                )
        _compare_types(cf.layout, sf.layout, path, cbase + cf.offset, sbase + sf.offset, out)


def _compare_types(ct: TypeLayout, st: TypeLayout, path: str, co: int, so: int, out: _Collector):
    if ct.kind == "array" and st.kind == "array":
        if ct.count != st.count:
            out.error("field '%s' array length mismatch (cpp=%s, slang=%s)" % (path, ct.count, st.count))
        if ct.stride != st.stride:
            out.error("field '%s' array stride mismatch (cpp=%s, slang=%s)" % (path, ct.stride, st.stride))
            if st.stride and st.stride % 16 == 0 and (ct.stride or 0) % 16 != 0:
                out.note("std140 (constant buffer) layout rounds array strides up to 16 bytes; pad each C++ "
                         "element or use a StructuredBuffer / -fvk-use-scalar-layout")
            return
        if ct.count == st.count and ct.size != st.size:
            out.error("field '%s' size mismatch (cpp=%d, slang=%d)" % (path, ct.size, st.size))
        if ct.element and st.element and ct.element.kind == "struct" and st.element.kind == "struct":
            if ct.element.problems or st.element.problems:
                for p in ct.element.problems + st.element.problems:
                    out.error(p)
                return
            _compare_fields(ct.element, st.element, path + "[0].", co, so, out)
        return
    if ct.size != st.size:
        out.error("field '%s' size mismatch (cpp=%d, slang=%d)" % (path, ct.size, st.size))
        if ct.scalar == "bool" or st.scalar == "bool":
            out.note("Slang 'bool' occupies 4 bytes in buffers but C++ 'bool' is 1 byte; "
                     "use uint32_t in C++ and uint in Slang for '%s'" % path)
    if ct.kind == "struct" and st.kind == "struct":
        if ct.problems or st.problems:
            for p in ct.problems + st.problems:
                out.error(p)
            return
        _compare_fields(ct, st, path + ".", co, so, out)


def compare_struct(cs: CppStruct, slang: TypeLayout, padding_regex: str = DEFAULT_PADDING_REGEX) -> List[Diagnostic]:
    """Compare one annotated C++ struct against its Slang counterpart."""
    out = _Collector(cs, padding_regex)
    if cs.layout is None:
        return out.diags
    for p in slang.problems:
        out.error(p)
    _compare_fields(cs.layout, slang, "", 0, 0, out)
    if cs.layout.size != slang.size:
        fields_ok = not any(d.severity == "error" for d in out.diags)
        out.error("size mismatch (cpp=%d, slang=%d)" % (cs.layout.size, slang.size))
        if fields_ok and slang.size > cs.layout.size:
            out.note("all fields match; the C++ struct lacks %d byte(s) of tail padding that Slang adds to round "
                     "the size up to the struct's alignment" % (slang.size - cs.layout.size))
    return out.diags


def check_structs(structs: Sequence[CppStruct], slang_layouts: Dict[str, TypeLayout],
                  slang_failures: Dict[str, str], shaders: Sequence[str],
                  padding_regex: str = DEFAULT_PADDING_REGEX) -> List[StructResult]:
    """Run all per-struct checks, including duplicate-name and not-found checks."""
    by_slang: Dict[str, List[CppStruct]] = {}
    for cs in structs:
        by_slang.setdefault(cs.slang_name, []).append(cs)

    results: List[StructResult] = []
    shader_list = ", ".join(os.path.basename(s) for s in shaders) or "<no shaders>"
    for cs in structs:
        head = "%s vs %s" % (cs.cpp_name, cs.slang_name)
        diags: List[Diagnostic] = []
        for p in cs.problems:
            diags.append(Diagnostic("error", "%s: %s" % (cs.cpp_name, p), cs.file, cs.line))
        others = [o for o in by_slang[cs.slang_name] if o is not cs]
        if others:
            diags.append(Diagnostic(
                "error",
                "%s: Slang struct '%s' is also claimed by %s; each Slang struct may be mirrored by one C++ struct"
                % (cs.cpp_name, cs.slang_name,
                   ", ".join("%s (%s:%d)" % (o.cpp_name, os.path.basename(o.file), o.line) for o in others)),
                cs.file, cs.line))
        slang = slang_layouts.get(cs.slang_name)
        if cs.slang_name in slang_failures:
            reason = slang_failures[cs.slang_name]
            if reason == "not found":
                diags.append(Diagnostic("error", "%s: Slang struct '%s' not found in %s"
                                        % (head, cs.slang_name, shader_list), cs.file, cs.line))
            else:
                diags.append(Diagnostic("error", "%s: slangc could not resolve '%s': %s"
                                        % (head, cs.slang_name, reason), cs.file, cs.line))
        elif slang is not None and cs.layout is not None and not cs.problems:
            # Unsupported constructs were already reported; comparing the
            # remaining fields would only add follow-on noise.
            diags.extend(compare_struct(cs, slang, padding_regex))
        results.append(StructResult(cs, slang, diags))
    return results


# =============================================================================
# report
# =============================================================================


def format_diagnostic(d: Diagnostic, style: str = "gcc") -> str:
    if d.file:
        loc = "%s(%d)" % (d.file, d.line or 0) if style == "msvc" else "%s:%d" % (d.file, d.line or 0)
        return "%s: %s: %s" % (loc, d.severity, d.message)
    return "%s: %s: %s" % (TOOL, d.severity, d.message)


def _layout_json(t: Optional[TypeLayout]):
    if t is None:
        return None
    return {
        "size": t.size,
        "fields": [{"name": f.name, "offset": f.offset, "size": f.layout.size, "type": f.layout.name,
                    **({"line": f.line} if f.line is not None else {})} for f in t.fields],
    }


def _diag_json(d: Diagnostic) -> dict:
    return {"severity": d.severity, "message": d.message, "file": d.file, "line": d.line}


def report(results: Sequence[StructResult], global_diags: Sequence[Diagnostic], fmt: str, style: str,
           stream=None) -> int:
    """Print results and return the exit code (0 ok, 1 mismatch)."""
    stream = stream or sys.stdout
    failed = [r for r in results if not r.ok]
    global_errors = [d for d in global_diags if d.severity == "error"]
    code = EXIT_MISMATCH if (failed or global_errors) else EXIT_OK
    if fmt == "json":
        doc = {
            "tool": TOOL,
            "version": __version__,
            "ok": code == EXIT_OK,
            "diagnostics": [_diag_json(d) for d in global_diags],
            "structs": [{
                "cpp_name": r.cpp.cpp_name,
                "slang_name": r.cpp.slang_name,
                "file": r.cpp.file,
                "line": r.cpp.line,
                "ok": r.ok,
                "cpp": _layout_json(r.cpp.layout),
                "slang": _layout_json(r.slang),
                "diagnostics": [_diag_json(d) for d in r.diagnostics],
            } for r in results],
        }
        stream.write(json.dumps(doc, indent=2) + "\n")
        return code
    for d in global_diags:
        stream.write(format_diagnostic(d, style) + "\n")
    for r in results:
        for d in r.diagnostics:
            stream.write(format_diagnostic(d, style) + "\n")
    stream.write("%s: %d struct(s) checked, %d ok, %d failed\n"
                 % (TOOL, len(results), len(results) - len(failed), len(failed)))
    return code


def _make_escape(path: str) -> str:
    return path.replace("\\", "/").replace(" ", "\\ ").replace("#", "\\#").replace("$", "$$")


def _split_make_words(text: str) -> List[str]:
    """Split Make-style words, decoding escapes (slangc writes `C\\:\\\\Users\\\\...`)."""
    words: List[str] = []
    cur: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch == "\\" and nxt in (" ", "\\", ":", "#"):
            cur.append(nxt)
            i += 2
            continue
        if ch == "$" and nxt == "$":
            cur.append("$")
            i += 2
            continue
        if ch.isspace():
            if cur:
                words.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        words.append("".join(cur))
    return words


def _read_make_depfile(path: Optional[str]) -> List[str]:
    """Return the prerequisites listed in a Makefile-style depfile."""
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read().replace("\\\r\n", " ").replace("\\\n", " ")
    deps: List[str] = []
    for line in text.splitlines():
        # "target: deps" - the separator is the first unescaped ':' followed by
        # whitespace (so drive letters like C\: or C:/ are not mistaken for it).
        m = re.match(r"^((?:\\.|[^\\])*?):(?:\s|$)(.*)$", line)
        if m:
            deps += _split_make_words(m.group(2))
    return deps


def write_depfile(path: str, target: str, deps: Iterable[str]):
    # Only list files that exist: Makefile generators turn every dependency into
    # an empty rule, so a missing one (e.g. a virtual path from slangc) would be
    # "always out of date" and re-run the check on every build.
    uniq = list(dict.fromkeys(os.path.normpath(os.path.abspath(d)) for d in deps if d and os.path.isfile(d)))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_make_escape(os.path.abspath(target)) + ":")
        for d in uniq:
            fh.write(" \\\n  " + _make_escape(d))
        fh.write("\n")


# =============================================================================
# CLI
# =============================================================================


def _split_flag_args(argv: Sequence[str]) -> List[str]:
    """Allow `--cxx-flag -std=c++20` (argparse would treat the value as an option)."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--cxx-flag", "--slang-flag") and i + 1 < len(argv):
            out.append("%s=%s" % (a, argv[i + 1]))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slang_layout_check.py",
        description="Verify that [[slang_check(\"Name\")]] C++ structs match their Slang structs byte for byte.",
        epilog="Exit codes: 0 = layouts match, 1 = mismatch, 2 = tool or usage error.",
    )
    p.add_argument("--header", action="append", nargs="+", default=[], metavar="PATH",
                   help="C++ header containing annotated structs (repeatable)")
    p.add_argument("--shader", action="append", nargs="+", default=[], metavar="PATH",
                   help="Slang source file defining the Slang structs (repeatable)")
    p.add_argument("--cxx-flag", action="append", default=[], metavar="FLAG",
                   help="extra C++ flag for libclang, e.g. --cxx-flag=-std=c++20 (repeatable)")
    p.add_argument("-I", dest="include_dirs", action="append", default=[], metavar="DIR", help="C++ include dir")
    p.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME[=VALUE]", help="C++ define")
    p.add_argument("--std", default=None, help="C++ standard, e.g. 17, 20 or c++20 (default: 17)")
    p.add_argument("--compile-features", default=None, help=argparse.SUPPRESS)  # from CMake COMPILE_FEATURES
    p.add_argument("--compile-commands", default=None, metavar="PATH",
                   help="compile_commands.json (or its directory) to take C++ flags from")
    p.add_argument("--target", default=None, metavar="TRIPLE",
                   help="target triple for C++ layout, e.g. x86_64-pc-windows-msvc, aarch64-linux-gnu")
    p.add_argument("--cxx", default=None, metavar="PATH",
                   help="host C++ compiler; used for its target triple and system include dirs")
    p.add_argument("--resource-dir", default=None, metavar="DIR",
                   help="clang resource dir for builtin headers ('none' disables auto-detection)")
    p.add_argument("--libclang", default=None, metavar="PATH", help="explicit libclang shared library")
    p.add_argument("--ignore-system-errors", action="store_true",
                   help="do not fail on libclang errors inside system headers")
    p.add_argument("--slangc", default=None, metavar="PATH", help="slangc executable (default: PATH, then $VULKAN_SDK)")
    p.add_argument("--slang-flag", action="append", default=[], metavar="FLAG",
                   help="slangc flag, e.g. --slang-flag=-target --slang-flag=spirv (repeatable). "
                        "Must match your real shader build.")
    p.add_argument("--slang-buffer", choices=("structured", "constant"), default="structured",
                   help="buffer kind used to reflect structs: structured (std430 on Vulkan) or constant (std140)")
    p.add_argument("--slang-import", choices=("import", "include"), default="import",
                   help="how the wrapper pulls in shaders: Slang 'import' (default) or '#include'")
    p.add_argument("--padding-regex", default=DEFAULT_PADDING_REGEX, metavar="REGEX",
                   help="fields matching this may exist on one side only if they overlap no field on the "
                        "other side (default: %(default)s; pass '' to disable)")
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.add_argument("--diag-style", choices=("gcc", "msvc"), default="gcc",
                   help="location style: file:line (gcc) or file(line) (msvc, for Visual Studio)")
    p.add_argument("--stamp", default=None, metavar="PATH", help="touch this file when all checks pass")
    p.add_argument("--depfile", default=None, metavar="PATH", help="write a Makefile-style depfile for the stamp")
    p.add_argument("--keep-temp", action="store_true", help="keep the generated wrapper shader and JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version",
                   version="%s %s (tested with libclang %s, Slang %s)"
                           % (TOOL, __version__, TESTED_LIBCLANG_VERSION, TESTED_SLANG_VERSION))
    return p


def parse_args(argv: Optional[Sequence[str]] = None):
    p = build_arg_parser()
    opts = p.parse_args(_split_flag_args(list(sys.argv[1:] if argv is None else argv)))
    opts.headers = [h for group in opts.header for h in group]
    opts.shaders = [s for group in opts.shader for s in group]
    opts.cxx_flags = opts.cxx_flag
    opts.slang_flags = opts.slang_flag
    if not opts.headers:
        p.error("at least one --header is required")
    return opts


def _eprint(msg: str):
    sys.stderr.write(msg + "\n")


def run(opts) -> int:
    for h in opts.headers:
        if not os.path.isfile(h):
            raise ToolError("header not found: %s" % h)
    for s in opts.shaders:
        if not os.path.isfile(s):
            raise ToolError("shader not found: %s" % s)

    ci = load_cindex(opts.libclang)
    lc_text, lc_major = libclang_version(ci)
    cfg = build_clang_args(opts, ci, lc_major)
    for w in cfg.warnings:
        _eprint("%s: warning: %s" % (TOOL, w))
    if opts.verbose:
        _eprint("%s: libclang: %s" % (TOOL, lc_text))
        _eprint("%s: target: %s%s" % (TOOL, cfg.target or "(libclang default)", " [MSVC ABI]" if cfg.msvc else ""))
        _eprint("%s: builtin headers: %s" % (TOOL, cfg.builtin_headers))
        _eprint("%s: clang args: %s" % (TOOL, " ".join(cfg.args)))

    try:
        structs, _, cpp_deps = parse_cpp(ci, opts.headers, cfg.args, opts.ignore_system_errors)
    except CppParseError as e:
        e.diagnostics += [Diagnostic("note", h) for h in cfg.hints]
        e.diagnostics.append(Diagnostic("note", "C++ target: %s; builtin headers: %s; rerun with -v for the full "
                                                "libclang command line" % (cfg.target or "libclang default",
                                                                           cfg.builtin_headers)))
        raise
    global_diags: List[Diagnostic] = []
    deps = list(cpp_deps) + [os.path.abspath(s) for s in opts.shaders]

    if not structs:
        global_diags.append(Diagnostic(
            "warning", "no structs annotated with [[slang_check(\"...\")]] found in: %s" % ", ".join(opts.headers)))
        results: List[StructResult] = []
    else:
        if not opts.shaders:
            raise ToolError("found %d annotated struct(s) but no --shader was given" % len(structs))
        slangc = find_slangc(opts.slangc)
        flags, slang_warnings = sanitize_slang_flags(opts.slang_flags)
        for w in slang_warnings:
            _eprint("%s: warning: %s" % (TOOL, w))
        if opts.verbose:
            _eprint("%s: slangc: %s (version %s)" % (TOOL, slangc, slangc_version(slangc)))
        workdir = tempfile.mkdtemp(prefix="slang_layout_check_")
        try:
            names = [cs.slang_name for cs in structs if cs.layout is not None]
            layouts, failures, srun = resolve_slang_structs(
                slangc, opts.shaders, names, flags, workdir, opts.slang_buffer, opts.slang_import, opts.verbose)
            if srun is not None:
                deps += [d for d in _read_make_depfile(srun.depfile) if "slang_layout_check_" not in d]
        finally:
            if opts.keep_temp:
                _eprint("%s: kept temporary files in %s" % (TOOL, workdir))
            else:
                shutil.rmtree(workdir, ignore_errors=True)
        results = check_structs(structs, layouts, failures, opts.shaders, opts.padding_regex)

    code = report(results, global_diags, opts.format, opts.diag_style)
    if opts.depfile and opts.stamp:
        write_depfile(opts.depfile, opts.stamp, deps)
    if opts.stamp:
        if code == EXIT_OK:
            os.makedirs(os.path.dirname(os.path.abspath(opts.stamp)), exist_ok=True)
            with open(opts.stamp, "w", encoding="utf-8") as fh:
                fh.write("ok\n")
        elif os.path.exists(opts.stamp):
            os.remove(opts.stamp)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        opts = parse_args(argv)
    except SystemExit as e:  # argparse usage errors exit with 2 already
        return int(e.code or 0)
    try:
        return run(opts)
    except CppParseError as e:
        for d in e.diagnostics:
            _eprint(format_diagnostic(d, getattr(opts, "diag_style", "gcc")))
        _eprint("%s: error: C++ parsing failed; layouts were not checked" % TOOL)
        return EXIT_ERROR
    except ToolError as e:
        _eprint("%s: error: %s" % (TOOL, e))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

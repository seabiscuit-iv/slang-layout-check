"""C++-side tests: libclang parsing of annotated structs (needs the libclang wheel)."""
import types

import pytest

from conftest import needs_libclang, slc

pytestmark = needs_libclang


def parse(cindex, path, *extra):
    opts = types.SimpleNamespace(
        headers=[path], include_dirs=[], defines=[], cxx_flags=list(extra), compile_commands=None,
        target=None, cxx=None, std=None, compile_features=None, resource_dir=None)
    _, major = slc.libclang_version(cindex)
    cfg = slc.build_clang_args(opts, cindex, major)
    structs, _, deps = slc.parse_cpp(cindex, [path], cfg.args)
    return {s.cpp_name: s for s in structs}, deps


def test_attribute_and_macro_forms(cindex, tmp_header):
    h = tmp_header("""
#include <slang_check.h>
namespace a { namespace b {
struct [[slang_check("ShaderParticle")]] Particle { float pos[3]; float speed; };
SLANG_STRUCT("ShaderLight", Light) { float color[4]; };
} }
struct Plain { int x; };                                  // not annotated -> ignored
struct [[clang::annotate("other-tool")]] Other { int y; }; // foreign annotation -> ignored
""")
    structs, deps = parse(cindex, h)
    assert set(structs) == {"a::b::Particle", "a::b::Light"}
    p = structs["a::b::Particle"]
    assert (p.slang_name, p.line, p.layout.size) == ("ShaderParticle", 4, 16)
    assert [(f.name, f.offset, f.layout.size) for f in p.layout.fields] == [("pos", 0, 12), ("speed", 12, 4)]
    assert p.layout.fields[0].layout.kind == "array"
    assert structs["a::b::Light"].slang_name == "ShaderLight"
    assert any(d.endswith("slang_check.h") for d in deps)


def test_nested_struct_and_arrays(cindex, tmp_header):
    h = tmp_header("""
#include <slang_check.h>
#include <stdint.h>
struct Inner { float a; uint32_t b; };
struct [[slang_check("Outer")]] Outer { Inner inner; Inner list[3]; bool flag; };
""")
    structs, _ = parse(cindex, h)
    o = structs["Outer"].layout
    inner, lst, flag = o.fields
    assert inner.layout.kind == "struct" and [f.name for f in inner.layout.fields] == ["a", "b"]
    assert (lst.offset, lst.layout.count, lst.layout.stride) == (8, 3, 8)
    assert flag.layout.scalar == "bool" and flag.layout.size == 1


def test_unsupported_constructs_are_reported(cindex, tmp_header):
    h = tmp_header("""
#include <slang_check.h>
struct Base { float x; };
struct [[slang_check("A")]] Bits { int a : 3; int b; };
struct [[slang_check("B")]] Anon { union { int u; float f; }; };
struct [[slang_check("C")]] Derived : Base { float y; };
struct [[slang_check("D")]] Virt { virtual ~Virt(); float y; };
template <class T> struct [[slang_check("E")]] Tmpl { T v; };
""")
    structs, _ = parse(cindex, h)
    problems = {name: " ".join(s.problems) for name, s in structs.items()}
    assert "bitfield" in problems["Bits"]
    assert "anonymous union member" in problems["Anon"]
    assert "inheritance is not supported" in problems["Derived"]
    assert "virtual functions" in problems["Virt"]
    assert "templates are not supported" in problems["Tmpl"]


def test_attribute_before_struct_is_an_error_with_hint(cindex, tmp_header):
    h = tmp_header('#include <slang_check.h>\n[[slang_check("X")]] struct Before { float a; };\n')
    with pytest.raises(slc.CppParseError) as e:
        parse(cindex, h)
    msgs = [d.message for d in e.value.diagnostics]
    assert any("misplaced attributes" in m for m in msgs)
    assert any("after the struct keyword" in m for m in msgs)


def test_missing_include_is_an_error(cindex, tmp_header):
    h = tmp_header('struct [[slang_check("X")]] NoInclude { float a; };\n')
    with pytest.raises(slc.CppParseError) as e:
        parse(cindex, h)
    assert any("include <slang_check.h>" in d.message for d in e.value.diagnostics)


def test_msvc_and_linux_abi_differ(cindex, tmp_header):
    h = tmp_header("#include <slang_check.h>\nstruct [[slang_check(\"L\")]] L { long a; wchar_t w; };\n")
    msvc, _ = parse(cindex, h, "-target", "x86_64-pc-windows-msvc", "-fms-compatibility", "-fms-extensions")
    assert [f.layout.size for f in msvc["L"].layout.fields] == [4, 2]
    try:
        linux, _ = parse(cindex, h, "-target", "x86_64-unknown-linux-gnu")
    except slc.CppParseError:
        pytest.skip("no builtin headers for a linux target on this host")
    assert [f.layout.size for f in linux["L"].layout.fields] == [8, 4]

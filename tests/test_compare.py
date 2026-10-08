"""Unit tests for the comparison logic, using hand-built layouts.

These need neither libclang nor slangc.
"""
from conftest import slc

TL, FL = slc.TypeLayout, slc.FieldLayout


def scalar(name, size=4):
    return TL("scalar", name, size, scalar=name)


def vec(n, size=None):
    return TL("vector", "float%d" % n, size if size is not None else 4 * n, count=n)


def struct(name, size, *fields, problems=None):
    return TL("struct", name, size, fields=[FL(n, o, t) for n, o, t in fields], problems=list(problems or []))


def farr(n):
    return TL("array", "float[%d]" % n, 4 * n, element=scalar("float"), count=n, stride=4)


def cpp(layout, name="Particle", slang="ShaderParticle", problems=None):
    return slc.CppStruct(name, slang, "particle.h", 12, layout, list(problems or []))


def errors(diags):
    return [d.message for d in diags if d.severity == "error"]


def notes(diags):
    return [d.message for d in diags if d.severity == "note"]


def test_identical_layouts_pass():
    c = struct("Particle", 16, ("pos", 0, farr(3)), ("speed", 12, scalar("float")))
    s = struct("ShaderParticle", 16, ("pos", 0, vec(3)), ("speed", 12, scalar("float")))
    assert slc.compare_struct(cpp(c), s) == []


def test_offset_and_size_mismatch_messages_match_spec():
    c = struct("Particle", 16, ("pos", 0, farr(3)), ("speed", 12, scalar("float")))
    s = struct("ShaderParticle", 32, ("pos", 0, vec(3)), ("speed", 16, scalar("float")))
    diags = slc.compare_struct(cpp(c), s)
    lines = [slc.format_diagnostic(d) for d in diags if d.severity == "error"]
    assert lines == [
        "particle.h:12: error: Particle vs ShaderParticle: field 'speed' offset mismatch (cpp=12, slang=16)",
        "particle.h:12: error: Particle vs ShaderParticle: size mismatch (cpp=16, slang=32)",
    ]


def test_msvc_diag_style():
    d = slc.Diagnostic("error", "boom", "particle.h", 12)
    assert slc.format_diagnostic(d, "msvc") == "particle.h(12): error: boom"
    assert slc.format_diagnostic(slc.Diagnostic("warning", "x")) == "slang_layout_check: warning: x"


def test_field_only_on_one_side():
    c = struct("A", 8, ("a", 0, scalar("float")), ("b", 4, scalar("float")))
    s = struct("A", 8, ("a", 0, scalar("float")), ("c", 4, scalar("float")))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert any("field 'b' exists only in C++" in e for e in errs)
    assert any("field 'c' exists only in Slang" in e for e in errs)


def test_field_order_difference_reported():
    c = struct("P", 8, ("mass", 0, scalar("float")), ("id", 4, scalar("uint")))
    s = struct("P", 8, ("id", 0, scalar("uint")), ("mass", 4, scalar("float")))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert any("field order differs (cpp: mass, id; slang: id, mass)" in e for e in errs)
    assert any("field 'mass' offset mismatch (cpp=0, slang=4)" in e for e in errs)


def test_bool_size_mismatch_has_hint():
    c = struct("M", 12, ("r", 0, scalar("float")), ("metallic", 4, scalar("bool", 1)), ("o", 8, scalar("float")))
    s = struct("M", 12, ("r", 0, scalar("float")), ("metallic", 4, scalar("uint")), ("o", 8, scalar("float")))
    diags = slc.compare_struct(cpp(c), s)
    assert any("field 'metallic' size mismatch (cpp=1, slang=4)" in e for e in errors(diags))
    assert any("Slang 'bool' occupies 4 bytes" in n for n in notes(diags))


def test_float3_alignment_hint():
    c = struct("S", 20, ("i", 0, scalar("float")), ("dir", 4, farr(3)), ("r", 16, scalar("float")))
    s = struct("S", 32, ("i", 0, scalar("float")), ("dir", 16, vec(3)), ("r", 28, scalar("float")))
    diags = slc.compare_struct(cpp(c), s)
    assert any("field 'dir' offset mismatch (cpp=4, slang=16)" in e for e in errors(diags))
    assert any("align float3 'dir' to 16 bytes" in n for n in notes(diags))


def test_tail_padding_hint():
    c = struct("S", 28, ("c", 0, farr(3)), ("r", 12, scalar("float")), ("col", 16, farr(3)))
    s = struct("S", 32, ("c", 0, vec(3)), ("r", 12, scalar("float")), ("col", 16, vec(3)))
    diags = slc.compare_struct(cpp(c), s)
    assert errors(diags) == ["Particle vs ShaderParticle: size mismatch (cpp=28, slang=32)"]
    assert any("lacks 4 byte(s) of tail padding" in n for n in notes(diags))


def test_padding_fields_allowed_on_one_side():
    c = struct("S", 32, ("a", 0, scalar("float")), ("_pad0", 4, farr(3)), ("v", 16, farr(3)), ("pad1", 28, scalar("float")))
    s = struct("S", 32, ("a", 0, scalar("float")), ("v", 16, vec(3)))
    assert errors(slc.compare_struct(cpp(c), s)) == []


def test_padding_field_overlapping_real_data_is_an_error():
    c = struct("S", 8, ("a", 0, scalar("float")), ("pad", 4, scalar("float")))
    s = struct("S", 8, ("a", 0, scalar("float")), ("b", 4, scalar("float")))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert any("C++ padding field 'pad' (bytes 4..8) overlaps Slang field 'b'" in e for e in errs)


def test_padding_regex_can_be_disabled():
    c = struct("S", 8, ("a", 0, scalar("float")), ("pad", 4, scalar("float")))
    s = struct("S", 8, ("a", 0, scalar("float")))
    errs = errors(slc.compare_struct(cpp(c), s, padding_regex=""))
    assert any("field 'pad' exists only in C++" in e for e in errs)


def test_nested_struct_compared_recursively():
    inner_c = struct("Inner", 32, ("a", 0, scalar("float")), ("b", 4, farr(3)))
    inner_s = struct("Inner", 32, ("a", 0, scalar("float")), ("b", 16, vec(3)))
    c = struct("Outer", 48, ("id", 0, scalar("uint")), ("inner", 16, inner_c))
    s = struct("Outer", 48, ("id", 0, scalar("uint")), ("inner", 16, inner_s))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert errs == ["Particle vs ShaderParticle: field 'inner.b' offset mismatch (cpp=20, slang=32)"]


def test_misplaced_nested_struct_reported_once():
    inner = struct("Inner", 16, ("a", 0, scalar("float")), ("b", 4, scalar("float")))
    c = struct("Outer", 20, ("id", 0, scalar("uint")), ("inner", 4, inner))
    s = struct("Outer", 32, ("id", 0, scalar("uint")), ("inner", 16, inner))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert errs == [
        "Particle vs ShaderParticle: field 'inner' offset mismatch (cpp=4, slang=16)",
        "Particle vs ShaderParticle: size mismatch (cpp=20, slang=32)",
    ]


def test_array_stride_mismatch():
    c_el = struct("L", 28, ("p", 0, farr(3)), ("r", 12, scalar("float")), ("c", 16, farr(3)))
    s_el = struct("L", 32, ("p", 0, vec(3)), ("r", 12, scalar("float")), ("c", 16, vec(3)))
    c = struct("S", 56, ("lights", 0, TL("array", "L[2]", 56, element=c_el, count=2, stride=28)))
    s = struct("S", 64, ("lights", 0, TL("array", "L[2]", 64, element=s_el, count=2, stride=32)))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert "Particle vs ShaderParticle: field 'lights' array stride mismatch (cpp=28, slang=32)" in errs


def test_array_of_structs_recurses_into_element():
    c_el = struct("L", 16, ("a", 0, scalar("float")), ("b", 4, scalar("uint")))
    s_el = struct("L", 16, ("a", 0, scalar("float")), ("b", 8, scalar("uint")))
    c = struct("S", 32, ("ls", 0, TL("array", "L[2]", 32, element=c_el, count=2, stride=16)))
    s = struct("S", 32, ("ls", 0, TL("array", "L[2]", 32, element=s_el, count=2, stride=16)))
    errs = errors(slc.compare_struct(cpp(c), s))
    assert errs == ["Particle vs ShaderParticle: field 'ls[0].b' offset mismatch (cpp=4, slang=8)"]


def test_nested_problems_only_reported_when_descending():
    # A C++ struct type with an anonymous union (like glm::vec3) mirrored by a
    # Slang vector: kinds differ, so only size/offset are compared.
    glm_like = struct("glm::vec3", 12, problems=["anonymous union member at line 3 is not supported"])
    c = struct("S", 16, ("v", 0, glm_like), ("w", 12, scalar("float")))
    s = struct("S", 16, ("v", 0, vec(3)), ("w", 12, scalar("float")))
    assert errors(slc.compare_struct(cpp(c), s)) == []


def test_check_structs_not_found_and_duplicates():
    a = cpp(struct("A", 4, ("x", 0, scalar("float"))), name="A", slang="Shared")
    b = cpp(struct("B", 4, ("x", 0, scalar("float"))), name="B", slang="Shared")
    m = cpp(struct("M", 4, ("x", 0, scalar("float"))), name="M", slang="Missing")
    shared = struct("Shared", 4, ("x", 0, scalar("float")))
    results = slc.check_structs([a, b, m], {"Shared": shared}, {"Missing": "not found"}, ["shaders/p.slang"])
    msgs = {r.cpp.cpp_name: errors(r.diagnostics) for r in results}
    assert any("also claimed by B" in e for e in msgs["A"])
    assert any("also claimed by A" in e for e in msgs["B"])
    assert msgs["M"] == ["M vs Missing: Slang struct 'Missing' not found in p.slang"]
    assert all(not r.ok for r in results)


def test_unsupported_struct_skips_field_comparison():
    c = cpp(struct("B", 8, ("b", 4, scalar("int"))), name="Bits", slang="SBits",
            problems=["field 'a' is a bitfield: not supported (Slang has no bitfields)"])
    s = struct("SBits", 8, ("a", 0, scalar("int")), ("b", 4, scalar("int")))
    [r] = slc.check_structs([c], {"SBits": s}, {}, ["x.slang"])
    assert errors(r.diagnostics) == ["Bits: field 'a' is a bitfield: not supported (Slang has no bitfields)"]


def test_report_exit_codes_and_json(capsys):
    ok = slc.StructResult(cpp(struct("A", 4)), struct("A", 4), [])
    bad = slc.StructResult(cpp(struct("A", 4)), struct("A", 8), [slc.Diagnostic("error", "x", "f.h", 1)])
    assert slc.report([ok], [], "human", "gcc") == slc.EXIT_OK
    assert slc.report([ok, bad], [], "human", "gcc") == slc.EXIT_MISMATCH
    capsys.readouterr()
    import json
    assert slc.report([bad], [], "json", "gcc") == slc.EXIT_MISMATCH
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["structs"][0]["diagnostics"][0]["message"] == "x"

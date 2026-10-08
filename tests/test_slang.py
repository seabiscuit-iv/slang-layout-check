"""Slang-side tests: reflection JSON parsing (recorded fixtures), wrapper
generation, flag handling and depfile parsing. None of these need slangc."""
import json
import os

import pytest

from conftest import DATA, slc


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


def fields(layout):
    return [(f.name, f.offset, f.layout.size) for f in layout.fields]


def test_parse_structured_buffer_fixture():
    # Recorded from slangc 2026.8: wrapper for examples/pass (Particle, PointLight, SceneConstants).
    data = load("reflection_slang_2026.8_spirv.json")
    layouts = slc.parse_reflection(data, ["Particle", "PointLight", "SceneConstants"])
    assert layouts["Particle"].size == 48
    assert fields(layouts["Particle"]) == [
        ("position", 0, 12), ("mass", 12, 4), ("velocity", 16, 12), ("flags", 28, 4), ("color", 32, 16)]
    assert layouts["PointLight"].size == 32
    scene = layouts["SceneConstants"]
    assert scene.size == 208
    lights = {f.name: f for f in scene.fields}["lights"]
    assert (lights.offset, lights.layout.kind, lights.layout.count, lights.layout.stride) == (80, "array", 4, 32)
    assert lights.layout.element.kind == "struct"
    assert [f.name for f in lights.layout.element.fields] == ["position", "radius", "color", "enabled"]
    assert {f.name: f for f in scene.fields}["view_proj"].layout.kind == "matrix"


def test_parse_constant_buffer_fixture():
    # Same wrapper shape via ConstantBuffer<W> (std140): JSON uses elementType, sizes round to 16.
    data = load("reflection_slang_2026.8_spirv_constant.json")
    layouts = slc.parse_reflection(data, ["SpotLight", "Material", "Sphere", "Particle"])
    assert fields(layouts["SpotLight"]) == [("intensity", 0, 4), ("direction", 16, 12), ("range", 28, 4)]
    assert layouts["Material"].size == 16
    assert layouts["Material"].fields[1].layout.scalar == "uint"


def test_missing_parameter_is_a_clear_schema_error():
    data = load("reflection_slang_2026.8_spirv.json")
    with pytest.raises(slc.ReflectionFormatError, match="__slc_buf_3"):
        slc.parse_reflection(data, ["Particle", "PointLight", "SceneConstants", "Extra"])


def test_unexpected_schema_names_the_missing_key():
    data = {"parameters": [{"name": "__slc_buf_0", "type": {"kind": "resource", "resultType": {"kind": "struct"}}}]}
    with pytest.raises(slc.ReflectionFormatError, match="missing 'fields'"):
        slc.parse_reflection(data, ["X"])


def test_resource_field_without_uniform_binding_is_a_problem():
    t = {"kind": "struct", "name": "S", "fields": [
        {"name": "tex", "type": {"kind": "resource"}, "binding": {"kind": "descriptorTableSlot", "index": 0}}]}
    layout = slc.slang_type_layout(t, 0, "S")
    assert layout.fields == [] and "no byte layout" in layout.problems[0]


def test_make_wrapper():
    a = os.path.join(DATA, "a.slang")
    text = slc.make_wrapper([a], ["Foo", "ns::Bar"], "structured", "import")
    assert 'import "%s";' % os.path.abspath(a).replace("\\", "/") in text
    assert "struct __slc_wrap_1 { ns::Bar v[2]; };" in text
    assert "StructuredBuffer<__slc_wrap_0> __slc_buf_0;" in text
    assert "void __slc_entry()" in text
    assert "ConstantBuffer<__slc_wrap_0>" in slc.make_wrapper(["a.slang"], ["Foo"], "constant", "include")
    assert '#include "' in slc.make_wrapper(["a.slang"], ["Foo"], "constant", "include")


def test_sanitize_slang_flags_strips_checker_owned_flags():
    flags, warns = slc.sanitize_slang_flags(
        ["-target", "spirv", "-entry", "main", "-stage", "compute", "-o", "x.spv", "-fvk-use-scalar-layout",
         "shader.slang", "-DFOO=1"])
    assert flags == ["-target", "spirv", "-fvk-use-scalar-layout", "-DFOO=1"]
    assert warns == []


def test_sanitize_slang_flags_defaults_target_with_warning():
    flags, warns = slc.sanitize_slang_flags(["-O2"])
    assert flags[:2] == ["-target", "spirv"] and "defaulting" in warns[0]


def test_sanitize_slang_flags_multiple_targets():
    flags, warns = slc.sanitize_slang_flags(["-target", "spirv", "-target", "hlsl"])
    assert flags == ["-target", "spirv"] and "first one only" in warns[0]


def test_read_slangc_depfile(tmp_path):
    # slangc 2026.8 escapes ':' and '\' in its Make-style depfile.
    p = tmp_path / "w.d"
    p.write_text("C\\:\\\\t\\\\w.out: C\\:\\\\t\\\\w.slang C\\:\\\\My\\ Proj\\\\p.slang\n")
    assert slc._read_make_depfile(str(p)) == ["C:\\t\\w.slang", "C:\\My Proj\\p.slang"]


def test_depfile_roundtrip(tmp_path):
    p = tmp_path / "x.d"
    (tmp_path / "with space").mkdir()
    deps = [str(tmp_path / "with space" / "a.h"), str(tmp_path / "b.slang")]
    for d in deps:
        open(d, "w").close()
    # Missing files are dropped: Make would treat them as always out of date.
    slc.write_depfile(str(p), str(tmp_path / "stamp"), deps + [str(tmp_path / "virtual.slang")])
    read = [os.path.normcase(os.path.normpath(d)) for d in slc._read_make_depfile(str(p))]
    assert read == [os.path.normcase(os.path.normpath(d)) for d in deps]


def test_split_flag_args():
    assert slc._split_flag_args(["--cxx-flag", "-std=c++20", "--slang-flag", "-O0", "-I", "x"]) == [
        "--cxx-flag=-std=c++20", "--slang-flag=-O0", "-I", "x"]

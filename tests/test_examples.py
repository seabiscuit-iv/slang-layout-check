"""End-to-end CLI tests against examples/ (need libclang and slangc)."""
import json
import os

from conftest import EXAMPLES, ROOT, needs_libclang, needs_slangc, run_cli

PASS_H = os.path.join(EXAMPLES, "pass", "gpu_types.h")
PASS_S = os.path.join(EXAMPLES, "pass", "particles.slang")
FAIL_H = os.path.join(EXAMPLES, "fail", "gpu_types.h")
FAIL_S = os.path.join(EXAMPLES, "fail", "particles.slang")
SPIRV = ["--slang-flag", "-target", "--slang-flag", "spirv"]


@needs_libclang
@needs_slangc
def test_pass_example_exit_0():
    r = run_cli("--header", PASS_H, "--shader", PASS_S, *SPIRV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "3 struct(s) checked, 3 ok, 0 failed" in r.stdout


@needs_libclang
@needs_slangc
def test_fail_example_exit_1_with_expected_messages():
    r = run_cli("--header", FAIL_H, "--shader", FAIL_S, *SPIRV)
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout.replace(os.path.dirname(FAIL_H) + os.sep, "")
    expected = [
        # float3 vs 16-byte alignment
        "gpu_types.h:10: error: SpotLight vs SpotLight: field 'direction' offset mismatch (cpp=4, slang=16)",
        "gpu_types.h:10: error: SpotLight vs SpotLight: size mismatch (cpp=20, slang=32)",
        # bool vs uint
        "gpu_types.h:17: error: Material vs Material: field 'metallic' size mismatch (cpp=1, slang=4)",
        # missing (tail) padding
        "gpu_types.h:25: error: Sphere vs Sphere: size mismatch (cpp=28, slang=32)",
        # field reorder
        "gpu_types.h:32: error: Particle vs Particle: field order differs (cpp: mass, id; slang: id, mass)",
    ]
    for line in expected:
        assert line in out, "missing: %s\n--- output ---\n%s" % (line, out)
    assert "4 struct(s) checked, 0 ok, 4 failed" in out


@needs_libclang
@needs_slangc
def test_scalar_layout_flag_changes_result():
    # With scalar layout float3 is 4-byte aligned, so SpotLight's C++ layout becomes correct.
    r = run_cli("--header", FAIL_H, "--shader", FAIL_S, *SPIRV, "--slang-flag", "-fvk-use-scalar-layout",
                "--format", "json")
    doc = json.loads(r.stdout)
    status = {s["cpp_name"]: s["ok"] for s in doc["structs"]}
    assert status["SpotLight"] is True and status["Sphere"] is True
    assert status["Material"] is False and status["Particle"] is False


@needs_libclang
@needs_slangc
def test_json_format():
    r = run_cli("--header", PASS_H, "--shader", PASS_S, *SPIRV, "--format", "json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert doc["ok"] is True
    names = {s["cpp_name"]: s for s in doc["structs"]}
    assert set(names) == {"demo::Particle", "demo::PointLight", "demo::SceneConstants"}
    assert names["demo::SceneConstants"]["cpp"]["size"] == names["demo::SceneConstants"]["slang"]["size"] == 208


@needs_libclang
@needs_slangc
def test_missing_slang_struct(tmp_path):
    h = tmp_path / "m.h"
    h.write_text('#include <slang_check.h>\nstruct [[slang_check("NoSuchStruct")]] M { float a; };\n')
    r = run_cli("--header", str(h), "--shader", PASS_S, *SPIRV)
    assert r.returncode == 1
    assert "m.h:2: error: M vs NoSuchStruct: Slang struct 'NoSuchStruct' not found in particles.slang" in r.stdout


@needs_libclang
@needs_slangc
def test_stamp_written_only_on_success(tmp_path):
    stamp, dep = tmp_path / "ok.stamp", tmp_path / "ok.d"
    r = run_cli("--header", PASS_H, "--shader", PASS_S, *SPIRV, "--stamp", str(stamp), "--depfile", str(dep))
    assert r.returncode == 0 and stamp.exists()
    deps = dep.read_text()
    assert "gpu_types.h" in deps and "particles.slang" in deps and "slang_check.h" in deps
    r = run_cli("--header", FAIL_H, "--shader", FAIL_S, *SPIRV, "--stamp", str(stamp))
    assert r.returncode == 1 and not stamp.exists()


def test_usage_error_no_header():
    r = run_cli("--shader", PASS_S)
    assert r.returncode == 2
    assert "--header" in r.stderr


def test_missing_header_file():
    r = run_cli("--header", os.path.join(ROOT, "does_not_exist.h"), "--shader", PASS_S)
    assert r.returncode == 2
    assert "header not found" in r.stderr


@needs_libclang
def test_bad_slangc_path():
    r = run_cli("--header", PASS_H, "--shader", PASS_S, "--slangc", os.path.join(ROOT, "nope", "slangc"))
    assert r.returncode == 2
    assert "does not exist" in r.stderr


@needs_libclang
def test_parse_error_exit_2(tmp_path):
    h = tmp_path / "bad.h"
    h.write_text('#include <slang_check.h>\nstruct [[slang_check("X")]] Bad { undeclared_t a; };\n')
    r = run_cli("--header", str(h), "--shader", PASS_S)
    assert r.returncode == 2
    assert "bad.h:2: error: unknown type name 'undeclared_t'" in r.stderr


@needs_libclang
def test_no_annotated_structs_is_ok_with_warning(tmp_path):
    h = tmp_path / "plain.h"
    h.write_text("struct Plain { int x; };\n")
    r = run_cli("--header", str(h))
    assert r.returncode == 0
    assert "no structs annotated" in r.stdout


@needs_libclang
@needs_slangc
def test_same_struct_name_in_several_shaders(tmp_path):
    (tmp_path / "mesh").mkdir()
    (tmp_path / "sky").mkdir()
    entry = '[shader("vertex")] float4 main(uint id : SV_VertexID) : SV_Position { return 0; }\n'
    (tmp_path / "mesh" / "mesh.slang").write_text("struct VertexOutput { float4 pos; float4 uv; };\n" + entry)
    (tmp_path / "sky" / "sky.slang").write_text("struct VertexOutput { float4 pos; float3 dir; float pad; };\n" + entry)
    h = tmp_path / "vs.h"
    h.write_text(
        '#include <slang_check.h>\n'
        'struct [[slang_check("mesh/mesh.slang:VertexOutput")]] MeshOut { float pos[4]; float uv[4]; };\n'
        'struct [[slang_check("sky.slang:VertexOutput")]] SkyOut { float pos[4]; float dir[3]; float pad; };\n')
    shaders = [str(tmp_path / "mesh" / "mesh.slang"), str(tmp_path / "sky" / "sky.slang")]
    r = run_cli("--header", str(h), "--shader", *shaders, *SPIRV)
    assert r.returncode == 0, r.stdout + r.stderr

    h.write_text('#include <slang_check.h>\nstruct [[slang_check("VertexOutput")]] Out { float pos[4]; };\n')
    r = run_cli("--header", str(h), "--shader", *shaders, *SPIRV)
    assert r.returncode == 1
    assert ("Slang struct 'VertexOutput' is defined in several shaders (mesh.slang, sky.slang); "
            "qualify it, e.g. [[slang_check(\"mesh.slang:VertexOutput\")]]") in r.stdout

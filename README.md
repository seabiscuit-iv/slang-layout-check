# slang-layout-check

Build-time verification that your **C++ structs and Slang structs have the same
memory layout**: total size plus every field's byte offset and size. If they
drift apart, `cmake --build` fails with compiler-style errors:

```
particle.h:12: error: Particle vs ShaderParticle: field 'speed' offset mismatch (cpp=12, slang=16)
particle.h:12: note: Slang's default buffer layouts (std140/std430) align float3 'pos' to 16 bytes; ...
particle.h:12: error: Particle vs ShaderParticle: size mismatch (cpp=16, slang=32)
```

* One Python file (`slang_layout_check.py`, Python 3.8+). Its only dependency
  is the [`libclang`](https://pypi.org/project/libclang/) wheel, which the CMake
  module installs into a private venv for you.
* Nothing to compile and no per-platform binaries. Add it to your project with CMake `FetchContent`.
* C++ layout comes from **libclang** with your target's real include dirs,
  defines, C++ standard and target ABI (including MSVC). Slang layout comes from
  **`slangc -reflection-json`**, with the same flags your shader build uses.

## Quick start

```cmake
include(FetchContent)
FetchContent_Declare(slang_layout_check
  GIT_REPOSITORY https://github.com/seabiscuit-iv/slang-layout-check.git
  GIT_TAG        v0.1.0)
FetchContent_MakeAvailable(slang_layout_check)

slang_layout_check(my_app
  HEADERS     src/gpu_types.h
  SHADERS     shaders/particles.slang
  SLANG_FLAGS ${MY_SLANG_FLAGS})   # the SAME flags you compile the shaders with
```

Then annotate the structs you share with the GPU:

```cpp
#include <slang_check.h>   // the include dir is added to my_app automatically

struct [[slang_check("ShaderParticle")]] Particle {
    float    pos[3];
    float    speed;
};
```

```hlsl
// shaders/particles.slang
struct ShaderParticle { float3 pos; float speed; };
```

That's it. `my_app` now depends on a `my_app_slang_layout_check` target. It
re-runs only when a checked header, a header it includes, a shader or an
imported shader module changes.

**Requirements:** CMake 3.20+, Python 3.8+, and `slangc` (from the Vulkan SDK,
your `PATH`, an explicit `SLANGC <path>`, or downloaded for you with
`-DSLANG_LAYOUT_CHECK_DOWNLOAD_SLANG=ON`). If Python is missing, configuration
stops with an error explaining how to install it, point CMake at it
(`-DPython3_EXECUTABLE=...`), or skip the check (`-DSLANG_LAYOUT_CHECK_ENABLE=OFF`).

## Keep the slangc flags in sync (important)

Slang's layout depends on its flags: `-target spirv` uses std430 for structured
buffers, `-fvk-use-scalar-layout` packs `float3` to 4-byte alignment, and so on.
**The check is only meaningful if it sees the same flags as your real shader
build.** Define them once and use the variable in both places:

```cmake
set(MY_SLANG_FLAGS -target spirv -fvk-use-scalar-layout)

add_custom_command(OUTPUT particles.spv
  COMMAND ${SLANGC} ${CMAKE_CURRENT_SOURCE_DIR}/shaders/particles.slang ${MY_SLANG_FLAGS} -o particles.spv ...)

slang_layout_check(my_app HEADERS ... SHADERS ... SLANG_FLAGS ${MY_SLANG_FLAGS})
```

`slang_layout_check_find_slangc(<var>)` gives you the same `slangc` the
checker uses. The checker drops flags it controls itself (`-o`, `-entry`,
`-stage`, `-reflection-json`, `-depfile`, and source file names), so a flag list
shared with a real compile command is fine. `examples/pass/CMakeLists.txt`
shows the full pattern.

## Annotation syntax

`include/slang_check.h` provides two forms:

```cpp
struct [[slang_check("ShaderName")]] CppName { ... };   // attribute form
SLANG_STRUCT("ShaderName", CppName) { ... };            // macro form
```

* The attribute goes **after** `struct`. Placed before it
  (`[[slang_check("X")]] struct S`), clang rejects it, and the checker
  reports that error with a hint.
* In normal builds `[[slang_check("X")]]` expands to the empty attribute list
  `[[]]`, which is valid C++11 and warning-free on MSVC (`/W4 /WX`), GCC and
  Clang (`-Wall -Wextra -Werror`). The examples build with these flags.
* Only annotated structs are checked. The name is the Slang struct to compare
  against; namespaces on the C++ side are fine (`gfx::Particle`).
* **Padding fields:** C++ often needs explicit padding that Slang adds
  implicitly. A field that exists on only one side is allowed if its name
  matches `^_*(pad|padding|reserved|unused)[_0-9]*$` (case-insensitive, e.g.
  `_pad0`, `padding`, `reserved1`) **and** it overlaps no field on the other
  side. Change the pattern with `--padding-regex`.

## What is checked

For every annotated struct, against the Slang struct of the same name:

| Check | Example message |
|---|---|
| total size (`sizeof` vs. Slang's padded array stride) | `size mismatch (cpp=28, slang=32)` |
| each field's offset | `field 'speed' offset mismatch (cpp=12, slang=16)` |
| each field's size | `field 'metallic' size mismatch (cpp=1, slang=4)` |
| fields present on one side only | `field 'mass' exists only in C++` |
| field order | `field order differs (cpp: mass, id; slang: id, mass)` |
| nested structs, recursively | `field 'inner.b' offset mismatch (cpp=20, slang=32)` |
| arrays: length and stride; arrays of structs recurse | `field 'lights' array stride mismatch (cpp=28, slang=32)` |
| Slang struct exists | `Slang struct 'Foo' not found in particles.slang` |
| one C++ struct per Slang struct | `Slang struct 'X' is also claimed by B (b.h:9)` |

Common mistakes come with a `note:` that suggests the fix: a 1-byte C++ `bool`
vs. Slang's 4-byte `bool`/`uint`, a `float3` aligned to 16 bytes, missing tail
padding, or std140 array strides.

Fields are matched **by name**, so C++ `float pos[3]` vs. Slang `float3 pos`,
or `float m[16]` vs. `float4x4 m`, are compared by offset and size.

## CMake reference

```cmake
slang_layout_check(<target>
    HEADERS <header>...                 # headers with annotated structs (required)
    SHADERS <file.slang>...             # shaders defining the Slang structs (required)
    [SLANG_FLAGS <flag>...]             # same flags as your shader build
    [SLANGC <path>]                     # explicit slangc
    [TARGET_TRIPLE <triple>]            # C++ ABI; default derived from the compiler
    [CXX_FLAGS <flag>...]               # extra libclang flags
    [SLANG_BUFFER structured|constant]  # reflect via StructuredBuffer (default) or ConstantBuffer (std140)
    [NAME <check-target>])              # default: <target>_slang_layout_check
```

* **Python.** `find_package(Python3 3.8)`, then a venv is created once in
  `<build>/_slang_layout_check/venv` with a pinned `libclang==18.1.1`. A stamp
  file means this happens once per build tree.
  `-DSLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON=ON` uses your Python directly if
  `import clang.cindex` works there.
* **slangc.** Looked up in this order: `SLANGC`, the
  `SLANG_LAYOUT_CHECK_SLANGC` cache variable, `find_program(slangc)` (PATH and
  `$VULKAN_SDK/Bin`), then, if `SLANG_LAYOUT_CHECK_DOWNLOAD_SLANG=ON` (default
  OFF), a pinned Slang 2026.8 release zip for the host, downloaded with
  `file(DOWNLOAD ... EXPECTED_HASH SHA256=...)`.
* **C++ flags** come from the target via generator expressions:
  `INCLUDE_DIRECTORIES` and `COMPILE_DEFINITIONS` (including those inherited
  from linked libraries), `CXX_STANDARD` / `COMPILE_FEATURES`, plus `-I` for
  `slang_check.h`. `COMPILE_OPTIONS` are deliberately not forwarded, because
  MSVC options mean nothing to libclang. Use `CXX_FLAGS` if you need something
  extra.
* **Target ABI.** `TARGET_TRIPLE`, else `CMAKE_CXX_COMPILER_TARGET`, else
  `<arch>-pc-windows-msvc` for MSVC and clang-cl, else the compiler's
  `-dumpmachine`.
* **Incremental builds.** The custom command `DEPENDS` on the headers, shaders
  and the script. A depfile adds every header libclang included and every
  module slangc imported.
* With a Visual Studio generator, errors use the `file(line):` form so they
  show up in the Error List.
* `SLANG_LAYOUT_CHECK_ENABLE=OFF` makes `slang_layout_check()` only add the
  include dir. This is useful for machines without Python.

## Command line

```
python slang_layout_check.py --header src/gpu_types.h --shader shaders/particles.slang \
    --slang-flag -target --slang-flag spirv \
    -I src -DMY_DEFINE=1 --std 20 [--target x86_64-pc-windows-msvc] [--format json]
```

| Option | |
|---|---|
| `--header PATH...`, `--shader PATH...` | repeatable |
| `--cxx-flag FLAG`, `-I DIR`, `-D DEF`, `--std N` | C++ flags (`--cxx-flag -std=c++20` works as-is) |
| `--compile-commands PATH` | take C++ flags from `compile_commands.json` (via `clang.cindex.CompilationDatabase`) |
| `--target TRIPLE` | C++ ABI (`x86_64-pc-windows-msvc`, `aarch64-linux-gnu`, ...) |
| `--cxx PATH` | host compiler, used for `-dumpmachine` and its system include dirs |
| `--resource-dir DIR\|none` | clang builtin headers (see below) |
| `--slangc PATH`, `--slang-flag FLAG` | slangc and its flags |
| `--slang-buffer structured\|constant` | std430-style vs. std140-style reflection |
| `--format human\|json`, `--diag-style gcc\|msvc` | output |
| `--padding-regex REGEX` | see *Padding fields* |
| `--stamp`, `--depfile` | used by the CMake module |
| `-v`, `--keep-temp` | show the libclang/slangc command lines; keep the generated wrapper shader |

Exit codes: **0** layouts match, **1** mismatch or unsupported construct,
**2** tool, environment, usage or C++ parse error.

## How it works

1. **C++ (libclang).** The tool generates a translation unit that `#include`s
   every header, defines `SLANG_LAYOUT_CHECK` (which turns the annotation into
   `clang::annotate("slang_check:Name")`), and parses it with
   `clang.cindex`. Every struct definition outside system headers that carries
   such an annotation is collected. Its qualified name comes from the semantic
   parents. `type.get_size()` gives the size, and `type.get_offset(field)`
   gives offsets (bits, divided by 8). Negative libclang layout error codes
   are reported as errors. Any libclang diagnostic of severity Error or worse
   stops the run with exit code 2, because layouts from a broken parse cannot
   be trusted.
2. **Slang (slangc).** `-reflection-json` only describes types reachable from
   bound shader parameters, and it never reports a struct's total size. So the
   tool generates a small wrapper shader:

   ```hlsl
   import "path/to/particles.slang";
   struct __slc_wrap_0 { ShaderParticle v[2]; };
   StructuredBuffer<__slc_wrap_0> __slc_buf_0;
   [shader("compute")] [numthreads(1,1,1)] void __slc_entry() {}
   ```

   The array's `uniformStride` is the struct size including tail padding,
   which is exactly what C++ `sizeof` means. Fields come from
   `fields[].binding.offset/size` (`"kind": "uniform"`). The wrapper is compiled
   with `-entry __slc_entry -stage compute` plus your flags, so your own entry
   points are not compiled. Each shader is `import`ed as its own module, so two
   shaders that each define `main` don't clash (`--slang-import include` switches
   to `#include`).
3. **Compare** and report as above.

### Clang builtin headers

The `libclang` wheel ships the shared library but **not** clang's builtin
headers (`stddef.h`, `stdint.h`, `stdarg.h`, ...). The tool resolves them in this order:

1. `--resource-dir DIR` if given (`none` disables auto-detection).
2. A clang whose **major version matches libclang (18)**: `clang-18`, `clang`,
   `/usr/lib/llvm-18/bin/clang`, Homebrew's `llvm@18`, ...; its
   `-print-resource-dir` is passed as `-resource-dir`. A clang of a
   *different* major version is deliberately not used for this. For example,
   clang 22's headers rely on predefined macros (`__INT32_C`) that clang 18
   doesn't define.
3. For MSVC targets, nothing extra is needed: MSVC and the Windows SDK provide
   these headers.
4. Otherwise, the host compiler's system include dirs (`--cxx`, default `$CXX`
   or `c++` on Linux/macOS, queried with `-E -x c++ - -v`) are passed as
   `-isystem`. On macOS, `-isysroot $(xcrun --show-sdk-path)` is added too.

If parsing still fails, the error says how the builtin headers were found and
how to fix it. `-v` prints the full libclang command line.

### MSVC

For `*-windows-msvc` targets (and by default on Windows), libclang runs with
`-fms-compatibility -fms-extensions`, so `long` is 4 bytes, `wchar_t` is 2, and
MSVC record layout rules apply. Recent MSVC STLs `static_assert` on Clang 19 or
newer, so the tool also defines `_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH`.
That's MSVC's own escape hatch, and it doesn't affect layout.

## Known limitations

* **Bitfields:** not supported (reported). Slang has no bitfields.
* **Anonymous struct/union members:** not supported (reported). Give the
  member a name. A *nested* type containing one (like `glm::vec3`'s unions) is
  fine as long as the Slang side isn't a struct (e.g. `float3`), because only
  its size and offset are compared.
* **Templates:** annotated class templates are not supported (reported).
  Annotate a concrete struct instead.
* **Inheritance and virtual functions:** not supported (reported). Copy base
  fields into the struct.
* **Unions:** an annotated union is not supported. A union *field* is compared
  by size and offset only.
* **Macro-generated fields:** checked as libclang expands them for the checked
  configuration (the target's defines, plus `SLANG_LAYOUT_CHECK`). Errors point
  at the struct, not the macro definition. `#ifdef _MSC_VER`-style branches
  follow the checked target.
* **Nested structs and arrays of structs:** supported and compared
  recursively. Matrices are compared by size only: row/column-major packing
  inside the matrix isn't checked.
* **std140 / constant buffers:** the default reflection uses a
  `StructuredBuffer` (std430 on Vulkan). For a struct used in a
  `ConstantBuffer`/`cbuffer`, use `SLANG_BUFFER constant` (std140 rounds struct
  sizes and array strides up to 16). Check a struct used both ways twice, with
  two calls and different `NAME`s.
* **Fields without byte layout** (textures, samplers in a struct) are reported;
  they cannot be mirrored in C++.
* **Several `-target`s:** layouts are checked for the first one only.
* **Reflection JSON stability:** the JSON schema is not a versioned contract.
  The parser is tested against **Slang 2026.8** (recorded fixtures in
  `tests/data/`). If a future Slang changes it, the tool fails with an
  `unexpected slangc reflection JSON: missing '...'` error rather than guessing.
* **slangc quirk:** on an unknown struct name, slangc 2026.8 prints
  `undefined identifier 'X'` and then crashes. The tool parses that message,
  reports the struct as not found, and retries without it.

## Tested with

| | |
|---|---|
| libclang (PyPI) | 18.1.1 (newest on PyPI) |
| Slang / slangc | 2026.8 (Vulkan SDK 1.4.350.0 and the GitHub release) |
| Python | 3.10 and 3.14 on Windows; the CI matrix adds 3.8 and 3.12 |
| CMake | 3.20+ (developed with 4.3); Visual Studio 2026 and Ninja generators |
| Compilers | MSVC 19.50 and clang 22 on Windows; the CI matrix adds GCC (Linux) and Apple clang (macOS) |

## Repository layout

```
slang_layout_check.py        the tool (parse_cpp / run_slangc / parse_reflection / compare / report)
include/slang_check.h        the annotation macros
cmake/SlangLayoutCheck.cmake the CMake module (also included by the top-level CMakeLists.txt)
examples/pass, examples/fail CMake projects: one passes, one has four deliberate layout bugs
test/                        a scratch .cpp + .slang pair for trying the CLI by hand
tests/                       pytest suite (unit tests need neither slangc nor libclang)
```

## Development

```
python -m pip install libclang==18.1.1 pytest
python -m pytest tests           # libclang/slangc tests are skipped if those are missing
python slang_layout_check.py --header test/sample.cpp --shader test/sample.slang \
    --slang-flag -target --slang-flag spirv
```

## License

MIT, see [LICENSE](LICENSE).

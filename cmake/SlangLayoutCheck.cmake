# SlangLayoutCheck.cmake - build-time C++ <-> Slang struct layout verification.
#
#   slang_layout_check(<target>
#       HEADERS <header>...
#       SHADERS <shader.slang>...
#       [SLANG_FLAGS <flag>...]        # same flags as your real slangc build!
#       [SLANGC <path>]
#       [TARGET_TRIPLE <triple>]       # C++ ABI to check; derived from the compiler by default
#       [CXX_FLAGS <flag>...]          # extra flags for libclang
#       [SLANG_BUFFER structured|constant]
#       [NAME <check-target-name>])
#
# Adds <target>_slang_layout_check (built by ALL) that runs the checker, and
# makes <target> depend on it, so a layout mismatch fails `cmake --build`.
# Also adds include/ (slang_check.h) to <target>'s include directories.
#
# Options / cache variables:
#   SLANG_LAYOUT_CHECK_ENABLE            (ON)  OFF turns slang_layout_check() into a no-op
#                                              (apart from adding the include dir).
#   SLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON (OFF) Use the found Python as-is if
#                                              `import clang.cindex` works there.
#   SLANG_LAYOUT_CHECK_DOWNLOAD_SLANG    (OFF) Download a pinned Slang release when
#                                              slangc is not found.
#   SLANG_LAYOUT_CHECK_SLANGC            ("")  Explicit slangc for every check.

include_guard(GLOBAL)

if(CMAKE_VERSION VERSION_LESS 3.20)
  message(FATAL_ERROR "slang_layout_check requires CMake 3.20 or newer (found ${CMAKE_VERSION}).")
endif()
# Functions record the policies in effect where they are defined.
cmake_policy(VERSION 3.20)

option(SLANG_LAYOUT_CHECK_ENABLE "Verify C++/Slang struct layouts at build time" ON)
option(SLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON
  "Skip the private venv if 'import clang.cindex' already works with the found Python" OFF)
option(SLANG_LAYOUT_CHECK_DOWNLOAD_SLANG
  "Download a pinned Slang release if slangc is not found" OFF)
set(SLANG_LAYOUT_CHECK_SLANGC "" CACHE FILEPATH "slangc executable used by slang_layout_check (optional)")

# Pinned versions. Variables set here are not visible when the module is
# included from a subdirectory (FetchContent), so functions call this macro.
macro(_slang_layout_check_constants)
  set(_SLC_LIBCLANG_VERSION "18.1.1")
  set(_SLC_SLANG_VERSION "2026.8")
  set(_SLC_SLANG_URL_BASE "https://github.com/shader-slang/slang/releases/download/v${_SLC_SLANG_VERSION}")
  # SHA-256 of the release zips (from the GitHub release asset digests).
  set(_SLC_SLANG_windows-x86_64 "slang-2026.8-windows-x86_64.zip;7a3ffcbd267dbddb4818f68ade13da37e2d394dd305c7276fef964933388f184")
  set(_SLC_SLANG_windows-aarch64 "slang-2026.8-windows-aarch64.zip;ea843617c822031796899aa921be9b631f53234e2a8aad6f564cc8d4d89535b5")
  set(_SLC_SLANG_linux-x86_64 "slang-2026.8-linux-x86_64-glibc-2.27.zip;df1caf7ba7f3f435a17d82a7919839b054b1ce87c83a90fe62e3962c5cfb73c4")
  set(_SLC_SLANG_linux-aarch64 "slang-2026.8-linux-aarch64-glibc-2.28.zip;daeb9528191834457e4e38cdd4b4aed2688924a0994c71e6860b375a8b83dfc7")
  set(_SLC_SLANG_macos-x86_64 "slang-2026.8-macos-x86_64.zip;b2ae6d712134c7d1c841261f1606f51fa3474895598672887a1a951a58759245")
  set(_SLC_SLANG_macos-aarch64 "slang-2026.8-macos-aarch64.zip;e182d44b403f3e5d78d66e7608cf6db1b81119883426fa9cc30f6af70c7ed554")
endmacro()

# -----------------------------------------------------------------------------
# Python: find an interpreter, then create a private venv with libclang (once).
# -----------------------------------------------------------------------------
function(_slang_layout_check_python out_var)
  get_property(_cached GLOBAL PROPERTY _SLANG_LAYOUT_CHECK_PYTHON)
  if(_cached)
    set(${out_var} "${_cached}" PARENT_SCOPE)
    return()
  endif()
  _slang_layout_check_constants()

  find_package(Python3 3.8 QUIET COMPONENTS Interpreter)
  if(NOT Python3_Interpreter_FOUND)
    message(FATAL_ERROR
      "slang_layout_check: Python 3.8 or newer was not found.\n"
      "  The C++/Slang struct layout check is a Python script, so it needs a Python 3 interpreter.\n"
      "  Fix it with one of:\n"
      "    * install Python 3.8+ (https://www.python.org/downloads/) and re-run CMake\n"
      "    * point CMake at an existing interpreter: -DPython3_EXECUTABLE=/path/to/python3\n"
      "    * skip the check for this build: -DSLANG_LAYOUT_CHECK_ENABLE=OFF")
  endif()

  set(_probe "import clang.cindex as c; c.Index.create()")

  if(SLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON)
    execute_process(COMMAND "${Python3_EXECUTABLE}" -c "${_probe}"
      RESULT_VARIABLE _rc OUTPUT_QUIET ERROR_QUIET)
    if(_rc EQUAL 0)
      message(STATUS "slang_layout_check: using ${Python3_EXECUTABLE} (clang.cindex is importable)")
      set_property(GLOBAL PROPERTY _SLANG_LAYOUT_CHECK_PYTHON "${Python3_EXECUTABLE}")
      set(${out_var} "${Python3_EXECUTABLE}" PARENT_SCOPE)
      return()
    endif()
    message(STATUS "slang_layout_check: 'import clang.cindex' failed with ${Python3_EXECUTABLE}; "
                   "falling back to a private venv")
  endif()

  set(_venv "${CMAKE_BINARY_DIR}/_slang_layout_check/venv")
  if(CMAKE_HOST_WIN32)
    set(_vpy "${_venv}/Scripts/python.exe")
  else()
    set(_vpy "${_venv}/bin/python")
  endif()
  set(_stamp "${_venv}/.libclang-${_SLC_LIBCLANG_VERSION}.stamp")

  if(NOT EXISTS "${_stamp}" OR NOT EXISTS "${_vpy}")
    message(STATUS "slang_layout_check: creating Python venv with libclang==${_SLC_LIBCLANG_VERSION} "
                   "in ${_venv} (one-time)")
    execute_process(COMMAND "${Python3_EXECUTABLE}" -m venv "${_venv}"
      RESULT_VARIABLE _rc OUTPUT_VARIABLE _out ERROR_VARIABLE _err)
    if(NOT _rc EQUAL 0 OR NOT EXISTS "${_vpy}")
      message(FATAL_ERROR
        "slang_layout_check: could not create a venv with ${Python3_EXECUTABLE}:\n${_out}${_err}\n"
        "  On Debian/Ubuntu install the venv module: sudo apt install python3-venv\n"
        "  Or install the bindings yourself (pip install libclang==${_SLC_LIBCLANG_VERSION}) and configure "
        "with -DSLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON=ON")
    endif()
    execute_process(
      COMMAND "${_vpy}" -m pip install --disable-pip-version-check --no-input --quiet
              "libclang==${_SLC_LIBCLANG_VERSION}"
      RESULT_VARIABLE _rc OUTPUT_VARIABLE _out ERROR_VARIABLE _err)
    if(NOT _rc EQUAL 0)
      message(FATAL_ERROR
        "slang_layout_check: 'pip install libclang==${_SLC_LIBCLANG_VERSION}' failed:\n${_out}${_err}\n"
        "  Check your network / proxy settings, or install it into your own Python and configure with "
        "-DSLANG_LAYOUT_CHECK_USE_SYSTEM_PYTHON=ON")
    endif()
    execute_process(COMMAND "${_vpy}" -c "${_probe}"
      RESULT_VARIABLE _rc OUTPUT_VARIABLE _out ERROR_VARIABLE _err)
    if(NOT _rc EQUAL 0)
      message(FATAL_ERROR "slang_layout_check: libclang was installed but cannot be loaded:\n${_out}${_err}")
    endif()
    file(WRITE "${_stamp}" "libclang==${_SLC_LIBCLANG_VERSION}\n")
  endif()

  set_property(GLOBAL PROPERTY _SLANG_LAYOUT_CHECK_PYTHON "${_vpy}")
  set(${out_var} "${_vpy}" PARENT_SCOPE)
endfunction()

# -----------------------------------------------------------------------------
# slangc: explicit argument > SLANG_LAYOUT_CHECK_SLANGC > PATH / Vulkan SDK >
# optional pinned download.
# -----------------------------------------------------------------------------
function(_slang_layout_check_download_slang out_var)
  _slang_layout_check_constants()
  if(CMAKE_HOST_WIN32)
    set(_os windows)
  elseif(CMAKE_HOST_APPLE)
    set(_os macos)
  elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
    set(_os linux)
  else()
    message(FATAL_ERROR "slang_layout_check: no pinned Slang download for host ${CMAKE_HOST_SYSTEM_NAME}")
  endif()
  string(TOLOWER "${CMAKE_HOST_SYSTEM_PROCESSOR}" _cpu)
  if(_cpu MATCHES "^(arm64|aarch64)$")
    set(_arch aarch64)
  elseif(_cpu MATCHES "^(x86_64|amd64|x64)$")
    set(_arch x86_64)
  else()
    message(FATAL_ERROR "slang_layout_check: no pinned Slang download for CPU ${CMAKE_HOST_SYSTEM_PROCESSOR}")
  endif()
  list(GET _SLC_SLANG_${_os}-${_arch} 0 _file)
  list(GET _SLC_SLANG_${_os}-${_arch} 1 _sha)

  set(_root "${CMAKE_BINARY_DIR}/_slang_layout_check/slang-${_SLC_SLANG_VERSION}")
  set(_exe "${_root}/bin/slangc${CMAKE_HOST_EXECUTABLE_SUFFIX}")
  if(NOT EXISTS "${_exe}")
    set(_zip "${CMAKE_BINARY_DIR}/_slang_layout_check/${_file}")
    message(STATUS "slang_layout_check: downloading ${_file}")
    file(DOWNLOAD "${_SLC_SLANG_URL_BASE}/${_file}" "${_zip}"
      EXPECTED_HASH SHA256=${_sha} STATUS _status TLS_VERIFY ON)
    list(GET _status 0 _code)
    if(NOT _code EQUAL 0)
      list(GET _status 1 _msg)
      message(FATAL_ERROR "slang_layout_check: downloading ${_file} failed: ${_msg}")
    endif()
    file(ARCHIVE_EXTRACT INPUT "${_zip}" DESTINATION "${_root}")
    file(REMOVE "${_zip}")
    if(NOT EXISTS "${_exe}")
      message(FATAL_ERROR "slang_layout_check: ${_file} did not contain bin/slangc")
    endif()
    if(NOT CMAKE_HOST_WIN32)
      file(CHMOD "${_exe}" PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE
                                        WORLD_READ WORLD_EXECUTE)
    endif()
  endif()
  set(${out_var} "${_exe}" PARENT_SCOPE)
endfunction()

# Public helper: find the slangc the checker uses, so the real shader build can
# use the very same compiler.
function(slang_layout_check_find_slangc out_var)
  set(_explicit "${ARGV1}")
  if(_explicit)
    if(NOT EXISTS "${_explicit}")
      message(FATAL_ERROR "slang_layout_check: SLANGC '${_explicit}' does not exist")
    endif()
    set(${out_var} "${_explicit}" PARENT_SCOPE)
    return()
  endif()
  if(SLANG_LAYOUT_CHECK_SLANGC)
    set(${out_var} "${SLANG_LAYOUT_CHECK_SLANGC}" PARENT_SCOPE)
    return()
  endif()
  find_program(SLANG_LAYOUT_CHECK_SLANGC_PROGRAM NAMES slangc
    HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin"
    DOC "slangc found by slang_layout_check")
  if(SLANG_LAYOUT_CHECK_SLANGC_PROGRAM)
    set(${out_var} "${SLANG_LAYOUT_CHECK_SLANGC_PROGRAM}" PARENT_SCOPE)
    return()
  endif()
  if(SLANG_LAYOUT_CHECK_DOWNLOAD_SLANG)
    _slang_layout_check_download_slang(_exe)
    set(${out_var} "${_exe}" PARENT_SCOPE)
    return()
  endif()
  message(FATAL_ERROR
    "slang_layout_check: slangc was not found.\n"
    "  Fix it with one of:\n"
    "    * install the Vulkan SDK (it ships slangc and sets VULKAN_SDK) or put slangc on PATH\n"
    "    * pass SLANGC <path> to slang_layout_check() or set -DSLANG_LAYOUT_CHECK_SLANGC=/path/to/slangc\n"
    "    * let CMake download a pinned Slang release: -DSLANG_LAYOUT_CHECK_DOWNLOAD_SLANG=ON")
endfunction()

# -----------------------------------------------------------------------------
# Default C++ target triple, so layout follows the real compiler's ABI.
# -----------------------------------------------------------------------------
function(_slang_layout_check_default_triple out_var)
  set(_triple "")
  if(CMAKE_CXX_COMPILER_TARGET)
    set(_triple "${CMAKE_CXX_COMPILER_TARGET}")
  elseif(MSVC OR CMAKE_CXX_SIMULATE_ID STREQUAL "MSVC")
    set(_arch "${CMAKE_CXX_COMPILER_ARCHITECTURE_ID}")
    if(NOT _arch)
      set(_arch "${CMAKE_SYSTEM_PROCESSOR}")
    endif()
    string(TOLOWER "${_arch}" _arch)
    if(_arch MATCHES "^(x64|amd64|x86_64)$")
      set(_triple "x86_64-pc-windows-msvc")
    elseif(_arch MATCHES "^(x86|i[3-6]86)$")
      set(_triple "i686-pc-windows-msvc")
    elseif(_arch MATCHES "^(arm64|aarch64)$")
      set(_triple "aarch64-pc-windows-msvc")
    elseif(_arch MATCHES "^arm")
      set(_triple "thumbv7-pc-windows-msvc")
    endif()
  endif()
  set(${out_var} "${_triple}" PARENT_SCOPE)
endfunction()

# -----------------------------------------------------------------------------
# slang_layout_check(<target> HEADERS ... SHADERS ... ...)
# -----------------------------------------------------------------------------
function(slang_layout_check target)
  cmake_parse_arguments(PARSE_ARGV 1 SLC ""
    "SLANGC;TARGET_TRIPLE;NAME;SLANG_BUFFER"
    "HEADERS;SHADERS;SLANG_FLAGS;CXX_FLAGS")

  if(NOT TARGET ${target})
    message(FATAL_ERROR "slang_layout_check: '${target}' is not a target (call it after add_executable/add_library)")
  endif()
  if(SLC_UNPARSED_ARGUMENTS)
    message(FATAL_ERROR "slang_layout_check: unknown arguments: ${SLC_UNPARSED_ARGUMENTS}")
  endif()
  if(NOT SLC_HEADERS)
    message(FATAL_ERROR "slang_layout_check(${target}): HEADERS is required")
  endif()
  if(NOT SLC_SHADERS)
    message(FATAL_ERROR "slang_layout_check(${target}): SHADERS is required")
  endif()

  set(_root "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/..")
  get_filename_component(_root "${_root}" ABSOLUTE)
  set(_script "${_root}/slang_layout_check.py")
  set(_include "${_root}/include")

  # Make <slang_check.h> available to the target (and to its consumers, since
  # annotated headers are usually shared).
  get_target_property(_type ${target} TYPE)
  if(_type STREQUAL "INTERFACE_LIBRARY")
    target_include_directories(${target} INTERFACE "$<BUILD_INTERFACE:${_include}>")
  else()
    target_include_directories(${target} PUBLIC "$<BUILD_INTERFACE:${_include}>")
  endif()

  if(NOT SLANG_LAYOUT_CHECK_ENABLE)
    return()
  endif()

  _slang_layout_check_python(_python)
  slang_layout_check_find_slangc(_slangc "${SLC_SLANGC}")

  set(_headers "")
  foreach(_h IN LISTS SLC_HEADERS)
    get_filename_component(_abs "${_h}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
    if(NOT EXISTS "${_abs}")
      message(FATAL_ERROR "slang_layout_check(${target}): header not found: ${_abs}")
    endif()
    list(APPEND _headers "${_abs}")
  endforeach()
  set(_shaders "")
  foreach(_s IN LISTS SLC_SHADERS)
    get_filename_component(_abs "${_s}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
    if(NOT EXISTS "${_abs}")
      message(FATAL_ERROR "slang_layout_check(${target}): shader not found: ${_abs}")
    endif()
    list(APPEND _shaders "${_abs}")
  endforeach()

  if(SLC_NAME)
    set(_check "${SLC_NAME}")
  else()
    set(_check "${target}_slang_layout_check")
  endif()
  set(_work "${CMAKE_CURRENT_BINARY_DIR}/slang_layout_check")
  file(MAKE_DIRECTORY "${_work}")  # Makefile generators don't create it for the depfile
  set(_stamp "${_work}/${_check}.stamp")
  set(_depfile "${_work}/${_check}.d")

  # The target's real include dirs / defines / standard, evaluated at generate
  # time (including usage requirements of linked targets).
  if(_type STREQUAL "INTERFACE_LIBRARY")
    set(_inc "$<TARGET_PROPERTY:${target},INTERFACE_INCLUDE_DIRECTORIES>")
    set(_def "$<TARGET_PROPERTY:${target},INTERFACE_COMPILE_DEFINITIONS>")
    set(_std "")
    set(_features "")
  else()
    set(_inc "$<TARGET_PROPERTY:${target},INCLUDE_DIRECTORIES>")
    set(_def "$<TARGET_PROPERTY:${target},COMPILE_DEFINITIONS>")
    set(_std "$<TARGET_PROPERTY:${target},CXX_STANDARD>")
    set(_features "$<JOIN:$<TARGET_PROPERTY:${target},COMPILE_FEATURES>,$<COMMA>>")
  endif()

  # $<SEMICOLON> (not a literal ';') so the genex survives being stored in a
  # CMake list; COMMAND_EXPAND_LISTS splits the evaluated result into args.
  # File lists can be a whole codebase: pass them in a response file (one
  # argument per line) to stay under command-line length limits.
  set(_rsp "${_work}/${_check}.files")
  list(JOIN _headers "\n" _hdr_lines)
  list(JOIN _shaders "\n" _shd_lines)
  file(WRITE "${_rsp}" "--header\n${_hdr_lines}\n--shader\n${_shd_lines}\n")

  set(_args
    "@${_rsp}"
    "$<$<BOOL:${_inc}>:-I$<JOIN:${_inc},$<SEMICOLON>-I>>"
    "$<$<BOOL:${_def}>:-D$<JOIN:${_def},$<SEMICOLON>-D>>"
    "--std=${_std}"
    "--compile-features=${_features}"
    --slangc "${_slangc}"
    --stamp "${_stamp}")

  if(SLC_TARGET_TRIPLE)
    set(_triple "${SLC_TARGET_TRIPLE}")
  else()
    _slang_layout_check_default_triple(_triple)
  endif()
  if(_triple)
    list(APPEND _args --target "${_triple}")
  endif()
  if(CMAKE_CXX_COMPILER)
    list(APPEND _args --cxx "${CMAKE_CXX_COMPILER}")
  endif()
  foreach(_f IN LISTS SLC_CXX_FLAGS)
    list(APPEND _args "--cxx-flag=${_f}")
  endforeach()
  foreach(_f IN LISTS SLC_SLANG_FLAGS)
    list(APPEND _args "--slang-flag=${_f}")
  endforeach()
  if(SLC_SLANG_BUFFER)
    list(APPEND _args --slang-buffer "${SLC_SLANG_BUFFER}")
  endif()
  if(CMAKE_GENERATOR MATCHES "Visual Studio")
    list(APPEND _args --diag-style msvc)  # clickable file(line) errors in VS
  endif()

  set(_depfile_args "")
  if(CMAKE_GENERATOR MATCHES "Ninja|Makefiles|Visual Studio|Xcode")
    list(APPEND _args --depfile "${_depfile}")
    set(_depfile_args DEPFILE "${_depfile}")
  endif()

  add_custom_command(
    OUTPUT "${_stamp}"
    COMMAND "${_python}" "${_script}" ${_args}
    DEPENDS ${_headers} ${_shaders} "${_script}" "${_include}/slang_check.h"
    ${_depfile_args}
    COMMENT "Checking C++/Slang struct layouts for ${target}"
    COMMAND_EXPAND_LISTS
    VERBATIM)
  add_custom_target(${_check} ALL DEPENDS "${_stamp}")
  add_dependencies(${target} ${_check})
endfunction()

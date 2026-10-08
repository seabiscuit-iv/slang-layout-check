cmake_minimum_required(VERSION 3.20)
project(slang_layout_check_example_fail LANGUAGES CXX)

# Same setup as examples/pass, but gpu_types.h has deliberate layout bugs:
# `cmake --build` must FAIL with one error per bug.
include(FetchContent)
FetchContent_Declare(slang_layout_check SOURCE_DIR "${CMAKE_CURRENT_LIST_DIR}/../..")
FetchContent_MakeAvailable(slang_layout_check)

set(CMAKE_CXX_STANDARD 17)

add_executable(broken_app main.cpp gpu_types.h)

set(SHADER_SLANG_FLAGS -target spirv)

slang_layout_check(broken_app
  HEADERS     gpu_types.h
  SHADERS     particles.slang
  SLANG_FLAGS ${SHADER_SLANG_FLAGS})

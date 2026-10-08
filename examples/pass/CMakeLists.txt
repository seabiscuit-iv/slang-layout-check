cmake_minimum_required(VERSION 3.20)
project(slang_layout_check_example_pass LANGUAGES CXX)

# In your own project, fetch the tool from GitHub instead:
#   FetchContent_Declare(slang_layout_check
#     GIT_REPOSITORY https://github.com/<you>/slang-layout-check.git
#     GIT_TAG        v0.1.0)
# Here we point FetchContent at this checkout so the example tests the same path.
include(FetchContent)
FetchContent_Declare(slang_layout_check SOURCE_DIR "${CMAKE_CURRENT_LIST_DIR}/../..")
FetchContent_MakeAvailable(slang_layout_check)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(particles_app main.cpp gpu_types.h)
# [[slang_check("...")]] expands to the empty attribute list [[]] in normal
# builds; prove that it is warning-free.
if(MSVC)
  target_compile_options(particles_app PRIVATE /W4 /WX)
else()
  target_compile_options(particles_app PRIVATE -Wall -Wextra -Werror)
endif()

# ONE list of slangc flags, shared by the real shader build and the check, so
# the check validates the layout your shaders are actually compiled with.
set(SHADER_SLANG_FLAGS -target spirv)

slang_layout_check(particles_app
  HEADERS     gpu_types.h
  SHADERS     particles.slang
  SLANG_FLAGS ${SHADER_SLANG_FLAGS})

# The real shader build, using the same slangc and the same flags.
slang_layout_check_find_slangc(SLANGC)
add_custom_command(
  OUTPUT  "${CMAKE_CURRENT_BINARY_DIR}/particles.spv"
  COMMAND "${SLANGC}" "${CMAKE_CURRENT_SOURCE_DIR}/particles.slang" ${SHADER_SLANG_FLAGS}
          -o "${CMAKE_CURRENT_BINARY_DIR}/particles.spv"
  DEPENDS particles.slang
  COMMENT "Compiling particles.slang"
  VERBATIM)
add_custom_target(particles_shaders ALL DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/particles.spv")

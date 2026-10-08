cmake_minimum_required(VERSION 3.20)
project(slang_layout_check VERSION 0.1.0 LANGUAGES NONE)

# Consumed via FetchContent_MakeAvailable(slang_layout_check) (or
# add_subdirectory): this defines the slang_layout_check() function, which is
# global, so it is callable from the parent project afterwards.
include("${CMAKE_CURRENT_LIST_DIR}/cmake/SlangLayoutCheck.cmake")

// slang_check.h - annotations for slang-layout-check.
//
// Mark a C++ struct with the name of the Slang struct it mirrors:
//
//     struct [[slang_check("ShaderParticle")]] Particle { float pos[3]; float speed; };
//
// or, equivalently, with the macro form:
//
//     SLANG_STRUCT("ShaderParticle", Particle) { float pos[3]; float speed; };
//
// The attribute must come AFTER the `struct` keyword. Placed before it, clang
// rejects it ("misplaced attributes"), and the checker reports that error.
//
// In normal builds, [[slang_check("X")]] expands to the empty attribute list
// [[]], which is valid C++11 and compiles without warnings on GCC, Clang and
// MSVC. The checker defines SLANG_LAYOUT_CHECK, which turns the annotation into
// clang::annotate("slang_check:X") so it can be found in the libclang AST. The
// "slang_check:" prefix keeps it apart from other tools' clang::annotate uses.
#ifndef SLANG_CHECK_H
#define SLANG_CHECK_H

#ifdef SLANG_LAYOUT_CHECK
  #define slang_check(name) clang::annotate("slang_check:" name)
  #define SLANG_STRUCT(shader, name) struct [[clang::annotate("slang_check:" shader)]] name
#else
  #define slang_check(name)
  #define SLANG_STRUCT(shader, name) struct name
#endif

#endif // SLANG_CHECK_H

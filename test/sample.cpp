// Scratch sample for trying slang_layout_check by hand. Not built by anything.
//
//   python slang_layout_check.py --header test/sample.cpp --shader test/sample.slang --slang-flag=-target --slang-flag=spirv
//
// Expected: Particle and gfx::Light pass, Mismatched fails with offset/size errors.
#include <cstdint>
#include <slang_check.h>

struct [[slang_check("ShaderParticle")]] Particle {
    float    pos[3];   // float3 pos      (offset 0, 12 bytes)
    float    speed;    // float speed     (offset 12: packs after the float3)
    float    vel[3];   // float3 vel      (offset 16)
    uint32_t flags;    // uint flags      (offset 28)
};

namespace gfx {
SLANG_STRUCT("ShaderLight", Light) {
    float color[4];      // float4 color
    float direction[3];  // float3 direction
    float intensity;
};
} // namespace gfx

// Deliberately wrong: bool is 1 byte in C++, 4 in Slang; Slang aligns
// `normal` (a float3 after a scalar) to 16 bytes.
struct [[slang_check("ShaderMismatched")]] Mismatched {
    bool  enabled;
    float normal[3];
};

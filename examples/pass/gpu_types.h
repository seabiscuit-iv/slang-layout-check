// GPU-shared structs for the passing example. Each one mirrors a struct in
// particles.slang under Slang's default SPIR-V buffer layout (std430).
#pragma once

#include <cstdint>
#include <slang_check.h>

namespace demo {

// float[3] + float packs into 16 bytes, exactly like float3 + float in std430.
struct [[slang_check("Particle")]] Particle {
    float    position[3];
    float    mass;
    float    velocity[3];
    uint32_t flags;
    float    color[4];
};

// Macro form. Note `enabled` is uint32_t: Slang's bool is 4 bytes.
SLANG_STRUCT("PointLight", PointLight) {
    float    position[3];
    float    radius;
    float    color[3];
    uint32_t enabled;
};

// Nested struct array: std430 aligns `lights` to 16 bytes, so C++ pads
// explicitly. Fields named pad*/_pad* may exist on one side only.
struct [[slang_check("SceneConstants")]] SceneConstants {
    float      view_proj[16];  // float4x4
    float      time;
    uint32_t   particle_count;
    float      _pad0[2];
    PointLight lights[4];
};

} // namespace demo

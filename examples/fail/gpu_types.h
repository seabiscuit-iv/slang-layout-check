// Deliberately WRONG mirrors of the structs in particles.slang.
// Building this example must fail; each struct shows one classic bug.
#pragma once

#include <cstdint>
#include <slang_check.h>

// 1) float3 alignment: C++ packs `direction` right after `intensity`
//    (offset 4), but std430 aligns a float3 that follows a scalar to 16.
struct [[slang_check("SpotLight")]] SpotLight {
    float intensity;
    float direction[3];
    float range;
};

// 2) bool vs uint: C++ bool is 1 byte, the Slang field is a 4-byte uint.
struct [[slang_check("Material")]] Material {
    float roughness;
    bool  metallic;
    float opacity;
};

// 3) Missing tail padding: every field matches, but Slang rounds the struct
//    size up to its 16-byte alignment (28 -> 32). C++ needs `float _pad;`.
struct [[slang_check("Sphere")]] Sphere {
    float center[3];
    float radius;
    float color[3];
};

// 4) Field reorder: same fields, different order.
struct [[slang_check("Particle")]] Particle {
    float    mass;
    uint32_t id;
};

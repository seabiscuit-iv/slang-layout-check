// Shader side of examples/fail/gpu_types.h (these are the "correct" layouts).

struct SpotLight {
    float  intensity;
    float3 direction;
    float  range;
};

struct Material {
    float roughness;
    uint  metallic;
    float opacity;
};

struct Sphere {
    float3 center;
    float  radius;
    float3 color;
};

struct Particle {
    uint  id;
    float mass;
};

RWStructuredBuffer<Particle> particles;

[shader("compute")]
[numthreads(64, 1, 1)]
void simulate(uint3 id : SV_DispatchThreadID)
{
    particles[id.x].mass += 1.0;
}

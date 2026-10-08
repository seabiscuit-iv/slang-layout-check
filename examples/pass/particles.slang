// Shader side of examples/pass/gpu_types.h.

struct Particle {
    float3 position;
    float  mass;
    float3 velocity;
    uint   flags;
    float4 color;
};

struct PointLight {
    float3 position;
    float  radius;
    float3 color;
    uint   enabled;
};

struct SceneConstants {
    float4x4   view_proj;
    float      time;
    uint       particle_count;
    PointLight lights[4];
};

RWStructuredBuffer<Particle>     particles;
StructuredBuffer<SceneConstants> scene;

[shader("compute")]
[numthreads(64, 1, 1)]
void simulate(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= scene[0].particle_count)
        return;
    Particle p = particles[id.x];
    p.position += p.velocity * scene[0].time;
    particles[id.x] = p;
}

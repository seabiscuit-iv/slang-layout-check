// Slang counterpart of test/sample.cpp.

struct ShaderParticle {
    float3 pos;
    float  speed;
    float3 vel;
    uint   flags;
};

struct ShaderLight {
    float4 color;
    float3 direction;
    float  intensity;
};

struct ShaderMismatched {
    bool   enabled;
    float3 normal;
};

RWStructuredBuffer<ShaderParticle> particles;

[shader("compute")]
[numthreads(64, 1, 1)]
void simulate(uint3 id : SV_DispatchThreadID)
{
    ShaderParticle p = particles[id.x];
    p.pos += p.vel * p.speed;
    particles[id.x] = p;
}

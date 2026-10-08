#include <cstdio>

#include "gpu_types.h"

int main()
{
    std::printf("sizeof(Particle)       = %zu\n", sizeof(demo::Particle));
    std::printf("sizeof(PointLight)     = %zu\n", sizeof(demo::PointLight));
    std::printf("sizeof(SceneConstants) = %zu\n", sizeof(demo::SceneConstants));
    return 0;
}

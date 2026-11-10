"""The CPU architecture of the task containers and of every agent build.

The task images build from multi-architecture base images. On an arm64 Podman host, such as
an Apple Silicon Mac, they run natively. On another host they run under emulation, and `run`
refuses that unless `--allow-emulation` is given. `ctb fetch` builds every agent for ARCH.
"""

ARCH = "arm64"
#: The platform of `podman run --platform` and of DOCKER_DEFAULT_PLATFORM for the task builds.
PLATFORM = f"linux/{ARCH}"
#: The CPU name of GNU target triples, such as the python-build-standalone assets.
GNU = {"arm64": "aarch64", "amd64": "x86_64"}[ARCH]
#: The platform name of `bb dist` in kmet-agent.
KMET = {"arm64": "linux-aarch64", "amd64": "linux-amd64"}[ARCH]

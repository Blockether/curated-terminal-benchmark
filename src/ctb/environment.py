"""Podman boundary fixes for the curated benchmark."""

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path

import yaml
from harbor.environments.base import HealthcheckError
from harbor.environments.podman import PodmanEnvironment
from harbor.models.task.config import HealthcheckConfig

from ctb import arch

#: A sidecar image that Harbor published from a directory of the task environment.
SIDECAR = re.compile(
    r"harborframework/terminal-bench:.+-sidecar-([a-z0-9-]+)-[0-9a-f]{16}(@sha256:[0-9a-f]{64})?"
)


def sidecar_builds(compose: Path, cpu: str) -> dict:
    """Compose services that build the task's published sidecars from their source for CPU.

    The published sidecar images are amd64 only. For another CPU, each sidecar with a
    Dockerfile in its directory of the environment is built from that directory."""
    if cpu == "amd64" or not compose.is_file():
        return {}
    services = {}
    for name, service in (
        (yaml.safe_load(compose.read_text()) or {}).get("services") or {}
    ).items():
        match = SIDECAR.fullmatch(str((service or {}).get("image", "")))
        source = compose.parent / match.group(1) if match else None
        if source and (source / "Dockerfile").is_file():
            services[name] = {
                "build": {"context": str(source.resolve())},
                "image": f"ctb-sidecar-{compose.parent.parent.name}-{name}:{cpu}",
                "pull_policy": "build",
            }
    return services


class Podman(PodmanEnvironment):
    """Build each task image for ctb.arch; count a timed-out healthcheck as a failed probe."""

    async def start(self, force_build: bool) -> None:
        # The published task images are amd64 only. A build from the task Dockerfile gets
        # DOCKER_DEFAULT_PLATFORM, so it has the CPU of ctb.arch. Harbor starts the
        # separate verifier with force_build=False, so this applies to both environments.
        await super().start(force_build=True)

    @property
    def _docker_compose_paths(self) -> list[Path]:
        paths = super()._docker_compose_paths
        services = sidecar_builds(self._environment_docker_compose_path, arch.ARCH)
        if not services:
            return paths
        if not hasattr(self, "_sidecar_dir"):
            self._sidecar_dir = tempfile.TemporaryDirectory()
        path = Path(self._sidecar_dir.name) / "docker-compose-sidecars.json"
        path.write_text(json.dumps({"services": services}))
        at = len(paths) - paths[::-1].index(self._environment_docker_compose_path)
        return [*paths[:at], path, *paths[at:]]

    async def _run_docker_compose_command(self, command: list[str], *args, **kwargs):
        # Without --pull, the Compose build takes a cached base image of another CPU and
        # makes an image that `up` then does not find for DOCKER_DEFAULT_PLATFORM.
        if command == ["build"]:
            command = ["build", "--pull"]
        return await super()._run_docker_compose_command(command, *args, **kwargs)

    async def run_healthcheck(self, healthcheck: HealthcheckConfig | None = None) -> None:
        hc = healthcheck if healthcheck is not None else self.task_env_config.healthcheck
        if hc is None:
            return
        start_period_end = time.monotonic() + hc.start_period_sec
        failures = 0
        while True:
            in_start_period = time.monotonic() < start_period_end
            try:
                result = await self.exec(hc.command, timeout_sec=int(hc.timeout_sec))
            except RuntimeError as error:
                # Harbor 0.24 reports compose timeouts as RuntimeError, without a subtype.
                if str(error) != f"Command timed out after {int(hc.timeout_sec)} seconds":
                    raise
                self.logger.warning("Healthcheck command timed out")
            else:
                if result.return_code == 0:
                    return
                self.logger.debug("Healthcheck failed (rc=%s)", result.return_code)
            if in_start_period:
                await asyncio.sleep(hc.start_interval_sec)
            else:
                failures += 1
                if failures >= hc.retries:
                    raise HealthcheckError(
                        f"Healthcheck failed after {hc.retries} consecutive retries: {hc.command}"
                    )
                await asyncio.sleep(hc.interval_sec)

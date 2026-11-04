"""Podman boundary fixes for the curated benchmark."""

import asyncio
import time

from harbor.environments.base import HealthcheckError
from harbor.environments.podman import PodmanEnvironment
from harbor.models.task.config import HealthcheckConfig


class Podman(PodmanEnvironment):
    """Count a timed-out healthcheck as a failed probe, not a failed trial."""

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

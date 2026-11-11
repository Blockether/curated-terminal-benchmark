"""Healthcheck recovery must remain bounded and must not hide other errors."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.environments.base import HealthcheckError
from harbor.models.task.config import HealthcheckConfig

from ctb.environment import Podman, sidecar_builds


def environment(outcomes, **kwargs):
    env = object.__new__(Podman)
    env.logger = logging.getLogger(__name__)
    env.task_env_config = SimpleNamespace(
        healthcheck=HealthcheckConfig(
            command="probe",
            timeout_sec=10,
            interval_sec=0,
            start_interval_sec=0,
            retries=2,
            **kwargs,
        )
    )
    env.exec = AsyncMock(side_effect=outcomes)
    return env


def test_healthcheck_recovers_after_compose_timeout():
    # User report: Dirge kv-live-surgery failed before agent setup on a 10-second timeout.
    env = environment(
        [RuntimeError("Command timed out after 10 seconds"), SimpleNamespace(return_code=0)]
    )
    asyncio.run(env.run_healthcheck())
    assert env.exec.await_count == 2


def test_healthcheck_stops_after_timeout_retry_budget():
    env = environment([RuntimeError("Command timed out after 10 seconds")] * 3)
    with pytest.raises(HealthcheckError):
        asyncio.run(env.run_healthcheck())
    assert env.exec.await_count == 2


def test_healthcheck_preserves_unrelated_runtime_errors():
    env = environment([RuntimeError("container disappeared")])
    with pytest.raises(RuntimeError, match="container disappeared"):
        asyncio.run(env.run_healthcheck())


def test_healthcheck_nonzero_exit_still_exhausts_budget():
    env = environment([SimpleNamespace(return_code=1)] * 3)
    with pytest.raises(HealthcheckError):
        asyncio.run(env.run_healthcheck())
    assert env.exec.await_count == 2


def test_healthcheck_without_configuration_does_not_probe():
    env = environment([])
    env.task_env_config.healthcheck = None
    asyncio.run(env.run_healthcheck())
    env.exec.assert_not_awaited()


def test_start_period_timeouts_do_not_consume_retry_budget(monkeypatch):
    from ctb import environment as module

    clock = iter([0, 0, 1, 2, 3])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    env = environment(
        [RuntimeError("Command timed out after 10 seconds")] * 3 + [SimpleNamespace(return_code=0)],
        start_period_sec=2,
    )
    asyncio.run(env.run_healthcheck())
    assert env.exec.await_count == 4


def test_override_healthcheck_replaces_default_configuration():
    env = environment([SimpleNamespace(return_code=0)])
    override = HealthcheckConfig(command="override", timeout_sec=4)
    asyncio.run(env.run_healthcheck(override))
    assert env.exec.await_args.args == ("override",)
    assert env.exec.await_args.kwargs["timeout_sec"] == 4


def test_sidecars_build_from_source_off_amd64(tmp_path):
    # payments-pipeline-fix and kv-live-surgery failed on arm64: "image not known" for
    # the amd64-only sidecar images that the task compose file pins.
    env = tmp_path / "payments-pipeline-fix" / "environment"
    for name in ("seeder", "customer"):
        (env / name).mkdir(parents=True)
        (env / name / "Dockerfile").write_text("FROM scratch\n")
    pinned = (
        "harborframework/terminal-bench:payments-pipeline-fix-sidecar-{}-dfa2b554b10ec940@sha256:"
        + "d" * 64
    )
    compose = env / "docker-compose.yaml"
    compose.write_text(
        "services:\n"
        f"  seeder:\n    image: {pinned.format('seeder')}\n"
        f"  customer:\n    image: {pinned.format('customer')}\n"
        "  kafka:\n    image: apache/kafka-native:4.3.1\n"
        "  main:\n    depends_on: [seeder]\n"
        f"  other:\n    image: {pinned.format('other')}\n"
    )
    services = sidecar_builds(compose, "arm64")
    assert services == {
        name: {
            "build": {"context": str((env / name).resolve())},
            "image": f"ctb-sidecar-payments-pipeline-fix-{name}:arm64",
            "pull_policy": "build",
        }
        for name in ("seeder", "customer")
    }
    assert sidecar_builds(compose, "amd64") == {}
    assert sidecar_builds(env / "missing.yaml", "arm64") == {}

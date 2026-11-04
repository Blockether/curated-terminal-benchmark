"""Run verifier scripts at the shell boundary without a container."""

import os
import subprocess

import pytest

from ctb import cli


@pytest.mark.parametrize("status", [0, 1, 2])
def test_photonic_reward_does_not_wait_for_optional_rendering(tmp_path, status):
    # Report: Codex passed all 14 tests, but rendering caused Harbor to discard the reward.
    task = tmp_path / "photonic-waveguide-routing"
    task.mkdir()
    cli.apply_task_fixes(tmp_path)
    logs = tmp_path / "verifier"
    logs.mkdir()
    (logs / "reward.txt").write_text("stale")
    (logs / "ctrf.json").write_text("stale")
    tests = task / "tests"
    home = tmp_path / "home"
    script = (tests / "test.sh").read_text()
    script = script.replace("/logs/verifier", str(logs))
    script = script.replace("/tmp/verifier-home", str(home)).replace("/tests", str(tests))
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python3"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "-m" ]; then exit "$TEST_STATUS"; fi\n'
        'touch "$RENDER_MARKER"\n'
        "exec sleep 10\n"
    )
    python.chmod(0o755)
    marker = tmp_path / "rendered"
    env = dict(
        os.environ,
        PATH=f"{binary}:{os.environ['PATH']}",
        TEST_STATUS=str(status),
        RENDER_MARKER=str(marker),
    )
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, timeout=2, check=False
    )
    assert result.returncode == status
    assert (logs / "reward.txt").read_text().strip() == ("1" if status == 0 else "0")
    assert not marker.exists()
    assert not (logs / "ctrf.json").exists()


@pytest.mark.parametrize("bundled", [False, True])
def test_tracked_fix_reaches_verifier_even_with_bundled_tests(tmp_path, bundled):
    # Report: the pinned photonic image kept its old renderer despite apply_task_fixes.
    from types import SimpleNamespace

    from harbor.models.task.task import Task
    from harbor.models.trial.config import VerifierConfig
    from harbor.models.trial.paths import TrialPaths
    from harbor.verifier.factory import VerifierFactory

    from ctb.matrix import AGENTS, Trial

    task = tmp_path / "photonic-waveguide-routing"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Solve the task.")
    (task / "task.toml").write_text('version = "1.0"\n')
    cli.apply_task_fixes(tmp_path)
    command = cli.harbor_command(Trial(AGENTS["codex"], "openai", task.name), tmp_path, 0.25)
    config = VerifierConfig(import_path=command[command.index("--verifier") + 1])
    from harbor.models.task.config import TaskOS

    environment = SimpleNamespace(os=TaskOS.LINUX)
    verifier = VerifierFactory.create_verifier_from_config(
        config,
        task=Task(task),
        trial_paths=TrialPaths(tmp_path / "trial"),
        environment=environment,
        skip_tests_upload=bundled,
    )
    source_dirs, _, script = verifier._resolve_tests()
    assert task / "tests" in source_dirs
    assert "render_result.py" not in script.read_text()

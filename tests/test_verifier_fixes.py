"""Run verifier scripts at the shell boundary without a container."""

import json
import os
import subprocess
from types import SimpleNamespace

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


class VerifierEnvironment:
    """A mounted Linux verifier environment that records commands and writes the reward."""

    def __init__(self, trial, install_code=0):
        from harbor.models.task.config import TaskOS

        self.os, self.capabilities = TaskOS.LINUX, SimpleNamespace(mounted=True)
        self.trial, self.install_code, self.calls = trial, install_code, []

    async def upload_file(self, source, target):
        self.calls.append(("upload", str(source), target))

    async def upload_dir(self, source_dir, target_dir):
        self.calls.append(("upload", str(source_dir), target_dir))

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append(("exec", command, user))
        if "toolkit-inventory.json" in command:
            return SimpleNamespace(return_code=self.install_code, stdout="", stderr="no tar")
        if "test.sh" in command and "chmod" not in command:
            self.trial.verifier_dir.mkdir(parents=True, exist_ok=True)
            self.trial.reward_text_path.write_text("1")
        return SimpleNamespace(return_code=0, stdout="", stderr="")


def one_verifier(tmp_path, environment_factory):
    from harbor.models.task.task import Task
    from harbor.models.trial.config import VerifierConfig
    from harbor.models.trial.paths import TrialPaths
    from harbor.verifier.factory import VerifierFactory

    from ctb.matrix import AGENTS, Trial

    task = tmp_path / "any-task"
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "test.sh").write_text("#!/bin/bash\n")
    (task / "instruction.md").write_text("Solve the task.")
    (task / "task.toml").write_text('version = "1.0"\n')
    command = cli.harbor_command(Trial(AGENTS["pi"], "openai", task.name), tmp_path, 0.25)
    trial = TrialPaths(tmp_path / "trial")
    environment = environment_factory(trial)
    verifier = VerifierFactory.create_verifier_from_config(
        VerifierConfig(import_path=command[command.index("--verifier") + 1]),
        task=Task(task),
        trial_paths=trial,
        environment=environment,
        skip_tests_upload=True,
    )
    return verifier, environment, trial


def test_every_task_verifier_has_the_agent_toolkit_before_its_tests(tmp_path):
    # Report: an agent could use the shared Python packages, but the verifier did not have them.
    import asyncio

    from ctb import toolkit

    verifier, environment, trial = one_verifier(tmp_path, VerifierEnvironment)
    assert asyncio.run(verifier.verify()).rewards == {"reward": 1.0}
    assert environment.calls[0] == ("upload", str(toolkit.archive()), toolkit.UPLOAD)
    _, install, user = environment.calls[1]
    assert user == "root" and "/logs/verifier/toolkit-inventory.json" in install
    test = next(c for c in environment.calls if c[0] == "exec" and "test-stdout" in c[1])
    assert test[1].startswith(toolkit.PATH_SETUP)
    assert json.loads((trial.verifier_dir / "toolkit-install.json").read_text())["seconds"] >= 0


def test_a_verifier_without_the_toolkit_does_not_run_the_tests(tmp_path):
    import asyncio

    verifier, environment, _ = one_verifier(
        tmp_path, lambda trial: VerifierEnvironment(trial, install_code=1)
    )
    with pytest.raises(RuntimeError, match="could not install the Python toolkit: no tar"):
        asyncio.run(verifier.verify())
    assert not any("test-stdout" in c[1] for c in environment.calls if c[0] == "exec")

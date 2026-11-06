"""The one verifier of every trial: the task's tests, with the Python toolkit of every agent."""

import json
import time

from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import Verifier as HarborVerifier

from ctb import toolkit


class ToolkitPath:
    """The verifier environment, with the PATH of each agent command in each command."""

    def __init__(self, environment):
        self._environment = environment

    def __getattr__(self, name):
        return getattr(self._environment, name)

    async def exec(self, command: str, *args, **kwargs):
        return await self._environment.exec(toolkit.PATH_SETUP + command, *args, **kwargs)


class Verifier(HarborVerifier):
    """Run the task's tests where the agent's Python toolkit is at the same path and PATH.

    Harbor gives a verifier from an import path the tests of the task directory, so the tracked
    task fixes replace the tests of a bundled verifier image for every task.
    """

    async def verify(self) -> VerifierResult:
        logs = EnvironmentPaths.for_os(self.environment.os).verifier_dir.as_posix()
        start = time.monotonic()
        await self.environment.upload_file(toolkit.archive(), toolkit.UPLOAD)
        result = await self.environment.exec(toolkit.install_command(logs), user="root")
        if result.return_code != 0:
            output = (result.stderr or result.stdout or "")[-2000:]
            raise RuntimeError(f"The verifier could not install the Python toolkit: {output}")
        seconds = round(time.monotonic() - start, 3)
        environment, self.environment = self.environment, ToolkitPath(self.environment)
        try:
            verdict = await super().verify()
        finally:
            self.environment = environment
        # The install counts against the verifier timeout, so the trial keeps its duration.
        (self.trial_paths.verifier_dir / "toolkit-install.json").write_text(
            json.dumps({"seconds": seconds}) + "\n"
        )
        return verdict

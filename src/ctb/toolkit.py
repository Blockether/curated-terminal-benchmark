"""The Python toolkit of every agent: the CPython of the Vis runtime with analysis packages.

`ctb fetch` builds it once for linux-amd64. Each agent setup unpacks it into TOOLKIT. Each agent
command finds it at the end of PATH, so a Python of the task container still comes first. Vis
also imports these packages in its sandbox, through VIS_PYTHON_PACKAGES.
"""

import hashlib
import shlex
import subprocess
from pathlib import Path

from harbor.environments.base import BaseEnvironment

from ctb import resources
from ctb.paths import ARTIFACTS

# The CPython build that vis-python-runtime embeds (.cpython-version), so Vis and the toolkit
# load the same wheels.
CPYTHON_RELEASE = "20260825"
CPYTHON_VERSION = "3.14.7"
CPYTHON_ASSET = f"cpython-{CPYTHON_VERSION}+{CPYTHON_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
CPYTHON_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{CPYTHON_RELEASE}/{CPYTHON_ASSET.replace('+', '%2B')}"
)
CPYTHON_SHA256 = "a0f39e822fd3b96a2605f30f954acf9a2cdf91faa1385a13e24bc407a41e05ea"
REQUIREMENTS = Path(__file__).with_name("toolkit-requirements.txt")
TOOLKIT = "/installed-agent/ctb-python"
PACKAGES = f"{TOOLKIT}/lib/python{CPYTHON_VERSION.rpartition('.')[0]}/site-packages"
#: The shell prefix of each agent command. It appends the toolkit, so it never hides a task tool.
PATH_SETUP = f'export PATH="$PATH:{TOOLKIT}/bin"; '
UPLOAD = "/tmp/ctb-python.tar.gz"
SETUP_NOTE = """Shared benchmark tools: ctb-python and ctb-pytest use the same pinned Python
and packages for every agent. They are in /installed-agent/ctb-python/bin.
The task's own python3 stays first on PATH. Use ctb-python for analysis packages;
use the task's interpreter for its application when required. Do not change the verifier."""


def shared_instruction(instruction: str) -> str:
    return instruction if instruction.endswith(SETUP_NOTE) else instruction + "\n\n" + SETUP_NOTE


def inventory_script() -> str:
    expected = dict(line.split("==", 1) for line in REQUIREMENTS.read_text().splitlines() if line)
    return f"""import importlib.metadata as metadata
import json, platform, sys
expected = {expected!r}
actual = {{name: metadata.version(name) for name in expected}}
if actual != expected or platform.python_version() != {CPYTHON_VERSION!r}:
    raise SystemExit('Shared Python package inventory does not match the benchmark pin')
print(json.dumps({{'schema': 1, 'python': platform.python_version(),
    'executable': sys.executable, 'packages': actual,
    'requirements_sha256': {requirements_sha256()!r}}}, sort_keys=True))
"""


# Build in a Debian image without Python, so the archive cannot depend on a system Python.
BUILD = """set -euo pipefail
mkdir -p /installed-agent
tar -xzf "/x/$CPYTHON" -C /installed-agent
mv /installed-agent/python /installed-agent/ctb-python
py=/installed-agent/ctb-python/bin/python3
"$py" -m pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
  -r /s/requirements.txt
"$py" -c 'import numpy, scipy, pandas, networkx, pytest, matplotlib, sympy, lxml, PIL'
tar -czf "/x/$OUT" -C /installed-agent ctb-python
"""


def requirements_sha256() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def archive() -> Path:
    """The local toolkit archive. Its name changes with the CPython pin and the requirements."""
    return ARTIFACTS / f"ctb-python-{CPYTHON_VERSION}-{requirements_sha256()[:12]}.tar.gz"


def build(cpython: Path, target: Path | None = None) -> Path:
    """Install the requirements into the CPython archive in a linux/amd64 container."""
    target = target or archive()
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    subprocess.run(
        ["podman", "run", "--rm", "--platform", "linux/amd64"]
        + ["-v", f"{target.parent}:/x", "-v", f"{REQUIREMENTS}:/s/requirements.txt:ro"]
        + ["-e", f"CPYTHON={cpython.relative_to(target.parent)}", "-e", f"OUT={partial.name}"]
        + ["docker.io/library/debian:bookworm-slim", "bash", "-c", BUILD],
        check=True,
    )
    # A running round uploads the archive for each trial, so replace it in one step.
    partial.replace(target)
    return target


def install_command(logs: str) -> str:
    """Unpack the uploaded toolkit into TOOLKIT, readable by every user. A repeat replaces it.

    The checked inventory goes to logs/toolkit-inventory.json. Agents and verifiers use it.
    """
    return (
        f"mkdir -p /installed-agent && rm -rf {TOOLKIT} && "
        f"tar -xzf {UPLOAD} -C /installed-agent && chmod -R a+rX {TOOLKIT} && rm -f {UPLOAD} && "
        f"ln -sfn python3 {TOOLKIT}/bin/ctb-python && "
        f"printf '%s\\n' '#!/bin/sh' 'exec {TOOLKIT}/bin/python3 -m pytest \"$@\"' > {TOOLKIT}/bin/ctb-pytest && "
        f"chmod a+rx {TOOLKIT}/bin/ctb-pytest && mkdir -p {logs} && "
        f"{TOOLKIT}/bin/python3 -I -c {shlex.quote(inventory_script())} > {logs}/toolkit-inventory.json"
    )


async def install(agent, environment: BaseEnvironment) -> None:
    """Unpack the toolkit into TOOLKIT for the agent. A repeat replaces it."""
    await environment.upload_file(archive(), UPLOAD)
    await agent.exec_as_root(environment, command=install_command("/logs/agent"))


class Toolkit:
    """Agent mixin: install the toolkit after the agent and give each agent command its PATH.

    Put it before the Harbor agent class in the bases, so it wraps that class's methods.
    """

    async def setup(self, environment: BaseEnvironment) -> None:
        if not archive().is_file():
            raise FileNotFoundError(f"Missing Python toolkit {archive()}; run `uv run ctb fetch`.")
        await super().setup(environment)
        await install(self, environment)
        await self.exec_as_agent(
            environment,
            command="{ command -v python3 || true; python3 --version 2>&1 || true; "
            "command -v ctb-python; ctb-python --version; } > /logs/agent/python-resolution.txt",
        )
        await resources.footprint(self, environment)

    async def run(self, instruction, environment, context):
        async with resources.measured(self, environment):
            return await super().run(shared_instruction(instruction), environment, context)

    async def exec_as_agent(self, environment: BaseEnvironment, command: str, *args, **kwargs):
        return await super().exec_as_agent(environment, PATH_SETUP + command, *args, **kwargs)

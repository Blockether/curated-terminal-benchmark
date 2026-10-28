"""Paths of the repository checkout that every module shares."""

from pathlib import Path

#: The repository root: profiles.toml, tasks.toml, artifacts/, jobs/, runs/ and results/.
ROOT = Path(__file__).resolve().parents[2]
#: The package sources. Harbor imports the agents and sitecustomize.py from here.
SOURCE = ROOT / "src"
#: Git-ignored downloads and builds: agent archives, the dataset and the Python toolkit.
ARTIFACTS = ROOT / "artifacts"

"""Harbor's Codex agent with the Python toolkit of every curated agent."""

from harbor.agents.installed.codex import Codex as HarborCodex

from ctb.resources import npm_footprint
from ctb.toolkit import Toolkit


class Codex(Toolkit, HarborCodex):
    """Harbor's Codex, unchanged except for the toolkit. Its name stays `codex`."""

    FOOTPRINT = npm_footprint("@openai/codex*")

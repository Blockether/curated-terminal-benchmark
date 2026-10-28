"""The full-suite queue: Vis alone on every CPU task of Terminal-Bench 4.0, on an API-key model.

`ctb.cli` compares agents on the curated tasks. This queue scores Vis on the whole dataset.
Its default round keeps Harbor jobs in SUITE/jobs and logs in SUITE/runs.
"""

from ctb.paths import ROOT

SUITE = ROOT / "jobs" / "suite"

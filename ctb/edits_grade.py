"""Grade the files of one vis-edits trial, as the Vis grader does.

Each `want` text must be in its file. No `wantnot` text may be in its file.
The verifier writes reward 1 only when every check holds.
"""

import json
import sys
from pathlib import Path


def _text(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def failures(checks: dict, work: Path) -> list[str]:
    """Every failed file check, as one line of text each."""
    found = []
    for name, texts in (checks.get("want") or {}).items():
        text = _text(work / name)
        found += [f"missing {name}: {item!r}" for item in texts if item not in text]
    for name, texts in (checks.get("wantnot") or {}).items():
        text = _text(work / name)
        found += [f"still present {name}: {item!r}" for item in texts if item in text]
    return found


def main(argv: list[str]) -> int:
    checks, work, logs = json.loads(Path(argv[0]).read_text()), Path(argv[1]), Path(argv[2])
    found = failures(checks, work)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "reward.txt").write_text("0" if found else "1")
    print("\n".join(found) or "all file checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

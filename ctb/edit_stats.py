"""Edit statistics from ATIF trajectories of any agent.

Each agent edits files with other tools. This module puts every tool call into one
of four kinds: edit, read, shell or other. Then it counts, for each trial, the edit
calls for each file, the edits of a file that was already edited and the reads
of a file after its edit.
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\]+)")
SHELL_EDIT = re.compile(
    r"sed\s+-i|perl\s+-\w*i|\bcat\s*>|>\s*[\w./-]+\.\w+\b|write_text\(|\.write\(|"
    r"open\([^)]*['\"][wa]['\"]|\btee\s|\bmv\s|\bgit\s+mv\b|\brm\s"
)
SHELL_READ = re.compile(r"\b(cat|sed\s+-n|head|tail|nl|less|rg|grep|awk|wc|diff|git\s+diff)\b")
VIS_CALL = re.compile(r"\b(patch|cat|grep|ls|shell)\(\s*([^,)]*)")
CODEX_TOOL = re.compile(r"tools\.(apply_patch|exec_command)\(")
EDIT_TOOLS = {"edit", "multiedit", "write", "notebookedit", "apply_patch", "str_replace"}
READ_TOOLS = {"read", "view", "glob", "grep", "list", "ls"}
PATH_KEYS = ("file_path", "filePath", "path", "notebook_path")


@dataclass(frozen=True)
class Call:
    """One tool action: its step, kind, files and number of edit operations."""

    step: int
    tool: str
    kind: str
    files: tuple[str, ...]
    units: int = 1


def norm(path: str) -> str:
    path = path.strip().strip("\"'`")
    for prefix in ("/app/", "./"):
        path = path.removeprefix(prefix)
    return path


def named_files(text: str, files: list[str]) -> tuple[str, ...]:
    """The workspace files that a command or a code block names."""
    found = [name for name in files if name in text or Path(name).name in text]
    return tuple(sorted(set(found)))


def shell_call(step: int, tool: str, command: str, files: list[str]) -> Call:
    names = named_files(command, files)
    if SHELL_EDIT.search(command):
        return Call(step, tool, "edit", names)
    if SHELL_READ.search(command):
        return Call(step, tool, "read", names)
    return Call(step, tool, "shell", names)


def patch_files(text: str) -> tuple[str, ...]:
    return tuple(sorted({norm(name) for name in PATCH_FILE.findall(text)}))


ASSIGN = re.compile(r"^\s*(\w+)\s*=\s*(.+)$", re.MULTILINE)


def vis_calls(step: int, code: str, files: list[str]) -> list[Call]:
    """The patch, cat and shell calls in one Vis code block.

    A call names its file in a literal or in a variable. A variable gets the files of
    its assignment. Else the call gets the files that its own code part names.
    """
    statements = "\n".join(part for line in code.splitlines() for part in line.split(";"))
    names_of = {var: named_files(value, files) for var, value in ASSIGN.findall(statements)}
    marks = list(VIS_CALL.finditer(code))
    out = []
    for index, match in enumerate(marks):
        name, argument = match.group(1), match.group(2).strip()
        end = marks[index + 1].start() if index + 1 < len(marks) else len(code)
        root = re.split(r"[\s/.\[]", argument, maxsplit=1)[0]
        names = (
            named_files(argument, files)
            or names_of.get(root, ())
            or named_files(code[match.start() : end], files)
        )
        kind = {"patch": "edit", "cat": "read", "grep": "read", "ls": "read"}.get(name, "shell")
        out.append(Call(step, f"vis.{name}", kind, names))
    if "write_text(" in code or ".write(" in code:
        out.append(Call(step, "vis.write", "edit", named_files(code, files)))
    return out or [Call(step, "python_execution", "other", ())]


def codex_calls(step: int, script: str, files: list[str]) -> list[Call]:
    """The apply_patch and exec_command calls in one Codex `exec` script."""
    out = []
    marks = [(m.start(), m.group(1)) for m in CODEX_TOOL.finditer(script)]
    for index, (start, name) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(script)
        part = script[start:end]
        if name == "apply_patch":
            out.append(Call(step, "codex.apply_patch", "edit", patch_files(part)))
        else:
            out.append(shell_call(step, "codex.exec_command", part, files))
    return out or [shell_call(step, "exec", script, files)]


def tool_calls(step: int, name: str, arguments: dict, files: list[str]) -> list[Call]:
    low = name.lower()
    if name == "python_execution":
        return vis_calls(step, arguments.get("code") or "", files)
    if name == "exec" and "input" in arguments:
        return codex_calls(step, arguments["input"], files)
    if low in {"bash", "shell", "exec_command"}:
        return [
            shell_call(step, name, arguments.get("command") or arguments.get("cmd") or "", files)
        ]
    path = next((arguments[key] for key in PATH_KEYS if arguments.get(key)), "")
    if low == "apply_patch":
        text = arguments.get("patchText") or arguments.get("input") or ""
        return [Call(step, name, "edit", patch_files(text))]
    if low in EDIT_TOOLS:
        edits = arguments.get("edits")
        units = len(edits) if isinstance(edits, list) else 1
        return [Call(step, name, "edit", (norm(path),) if path else (), units)]
    if low in READ_TOOLS:
        return [Call(step, name, "read", (norm(path),) if path else ())]
    return [Call(step, name, "other", ())]


def calls(trajectory: dict, files: list[str]) -> list[Call]:
    """Every tool action of the agent steps, in order."""
    out = []
    agent_steps = [s for s in trajectory.get("steps", []) if s.get("source") == "agent"]
    for index, step in enumerate(agent_steps):
        for call in step.get("tool_calls") or []:
            arguments = call.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            out.extend(tool_calls(index, call.get("function_name") or "", arguments, files))
    return out


def edit_profile(actions: list[Call]) -> dict:
    """Edit counts of one trial."""
    per_file: Counter = Counter()
    re_edits = reads_after_edit = 0
    edited: set[str] = set()
    for call in actions:
        if call.kind == "edit":
            re_edits += sum(1 for name in call.files if name in edited)
            per_file.update(call.files)
            edited.update(call.files)
        elif call.kind == "read":
            reads_after_edit += sum(1 for name in call.files if name in edited)
    return {
        "edit_calls": sum(1 for call in actions if call.kind == "edit"),
        "edit_units": sum(call.units for call in actions if call.kind == "edit"),
        "files_edited": len(per_file),
        "max_edits_per_file": max(per_file.values(), default=0),
        "re_edits": re_edits,
        "reads_after_edit": reads_after_edit,
        "reads": sum(1 for call in actions if call.kind == "read"),
        "shell": sum(1 for call in actions if call.kind == "shell"),
    }


def workspace_files(dataset: Path, task: str) -> list[str]:
    root = dataset / task / "environment" / "workspace"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def trial_rows(jobs: Path, dataset: Path) -> list[dict]:
    """One row for each finished trial of a round: agent, model, task, reward and counts."""
    rows = []
    for path in sorted(jobs.glob("*/*/agent/trajectory.json")):
        trial = path.parent.parent
        agent, family, task = trial.parent.name.split("__", 2)
        reward_file = trial / "verifier" / "reward.txt"
        reward = float(reward_file.read_text().strip() or 0) if reward_file.is_file() else 0.0
        trajectory = json.loads(path.read_text())
        metrics = trajectory.get("final_metrics") or {}
        steps = [s for s in trajectory.get("steps", []) if s.get("source") == "agent"]
        row = {"agent": agent, "family": family, "task": task, "reward": reward}
        row |= {
            "steps": len(steps),
            "input": metrics.get("total_prompt_tokens") or 0,
            "cached": metrics.get("total_cached_tokens") or 0,
            "output": metrics.get("total_completion_tokens") or 0,
            "cost_usd": metrics.get("total_cost_usd") or 0.0,
        }
        rows.append(row | edit_profile(calls(trajectory, workspace_files(dataset, task))))
    return rows


def summary(rows: list[dict]) -> list[dict]:
    """Means for each agent and model family."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["agent"], row["family"])].append(row)
    keys = ["steps", "edit_calls", "max_edits_per_file", "re_edits", "reads_after_edit"]
    keys += ["input", "output"]
    out = []
    for (agent, family), items in sorted(groups.items()):
        line = {"agent": agent, "family": family, "trials": len(items)}
        line["pass"] = sum(1 for item in items if item["reward"] >= 1)
        for key in keys:
            line[key] = round(sum(item[key] for item in items) / len(items), 1)
        out.append(line)
    return out

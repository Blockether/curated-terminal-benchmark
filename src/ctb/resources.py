"""Measure the RAM, the CPU time and the install size of each agent in its task container."""

import contextlib
import json
import os
import shlex
from pathlib import Path

SAMPLES = "/logs/agent/resources.jsonl"
FOOTPRINT = "/logs/agent/footprint.json"
PIDFILE = "/tmp/ctb-sampler.pid"
INTERVAL = 1.0
SHELLS = frozenset({"ash", "bash", "busybox", "dash", "sh", "zsh"})

#: Runs in the container as root while the agent runs. It records each process that started after
#: it: one `proc` line per process, then its RSS bytes and CPU ticks in each sample, with the
#: memory and CPU counters of the container cgroup. The host classifies the processes later.
SAMPLER = r"""
import json, os, signal, sys, time
out, interval = sys.argv[1], float(sys.argv[2])
tick, page, me = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE"), os.getpid()
stop = []
signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
def pids():
    return {int(p) for p in os.listdir("/proc") if p.isdigit()}
def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None
def cgroup():
    cpu, mem, stat = (read("/sys/fs/cgroup/" + n) for n in ("cpu.stat", "memory.current", "memory.stat"))
    usec = file = None
    for line in (cpu or b"").splitlines():
        if line.startswith(b"usage_usec "):
            usec = int(line.split()[1])
    for line in (stat or b"").splitlines():
        if line.startswith(b"file "):
            file = int(line.split()[1])
    used = int(mem) - file if mem and file is not None else None
    return usec, used
def image(pid):
    # Under Rosetta, exe names the translator; the first mapped file is the program.
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = None
    if exe and not exe.endswith("/rosetta"):
        return exe
    for line in (read(f"/proc/{pid}/maps") or b"").splitlines():
        path = line.split(None, 5)[5:] if line.count(b" ") >= 5 else []
        if path and path[0].startswith(b"/") and not path[0].endswith(b"/rosetta"):
            return path[0].decode(errors="replace")
    return exe
base, known = pids(), {}
with open(out, "w") as log:
    with open(sys.argv[3], "w") as f:
        f.write(str(me))
    log.write(json.dumps({"kind": "start", "epoch": time.time(), "interval": interval, "tick": tick}) + "\n")
    t0 = time.monotonic()
    while True:
        rows = []
        for pid in sorted(pids() - base - {me}):
            stat, statm = read(f"/proc/{pid}/stat"), read(f"/proc/{pid}/statm")
            if not stat or not statm:
                continue
            rest = stat[stat.rindex(b")") + 2 :].split()
            start = int(rest[19])
            # A new start time is a new process; a new name in the same process is an exec.
            identity = (start, stat[stat.index(b"(") + 1 : stat.rindex(b")")])
            if known.get(pid) != identity:
                known[pid] = identity
                exe = image(pid)
                argv = [a.decode(errors="replace")[:200] for a in (read(f"/proc/{pid}/cmdline") or b"").split(b"\0")[:3]]
                log.write(json.dumps({"kind": "proc", "pid": pid, "ppid": int(rest[1]), "start": start, "exe": exe, "argv": argv}) + "\n")
            rows.append([pid, int(statm.split()[1]) * page, int(rest[11]) + int(rest[12])])
        usec, used = cgroup()
        log.write(json.dumps({"kind": "sample", "t": round(time.monotonic() - t0, 3), "cpu_usec": usec, "mem": used, "procs": rows}) + "\n")
        log.flush()
        if stop:
            break
        time.sleep(interval)
"""

#: Reads `kind<TAB>path` lines and writes the files and bytes of each kind. Hard links count once.
MEASURE = r"""
import json, os, sys
kinds = {}
for line in sys.stdin:
    kind, _, path = line.rstrip("\n").partition("\t")
    if path and os.path.exists(path):
        kinds.setdefault(kind, set()).add(os.path.realpath(path))
seen, out = set(), {}
for kind, paths in sorted(kinds.items()):
    total = files = 0
    largest = []
    for top in sorted(paths):
        walk = os.walk(top) if os.path.isdir(top) else [(os.path.dirname(top), [], [os.path.basename(top)])]
        for root, _, names in walk:
            for name in names:
                st = os.lstat(os.path.join(root, name))
                if (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total, files = total + st.st_size, files + 1
                    largest = sorted(largest + [[st.st_size, os.path.join(root, name)]], reverse=True)[:5]
    out[kind] = {"paths": sorted(paths), "bytes": total, "files": files, "largest": largest[:5]}
json.dump(out, sys.stdout, indent=2, sort_keys=True)
"""


def npm_footprint(pattern: str, runtime: bool = True) -> str:
    """Shell lines for the global npm packages that match pattern and, if it needs it, Node.

    The pattern also matches a platform package beside the main one, such as `@scope/cli-linux-x64`.
    """
    lines = (
        "[ -s ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; "
        f'for p in "$(npm root -g)"/{pattern}; do printf "package\\t%s\\n" "$p"; done; '
    )
    return lines + ('printf "runtime\\t%s\\n" "$(command -v node)"; ' if runtime else "")


def files_footprint(**kinds: tuple[str, ...]) -> str:
    """Shell lines for fixed paths, grouped by kind."""
    return "".join(
        f"printf '{kind}\\t%s\\n' {shlex.quote(p)}; "
        for kind, paths in kinds.items()
        for p in paths
    )


def footprint_command(lines: str) -> str:
    """Write FOOTPRINT from the shell lines of one agent."""
    from ctb.toolkit import TOOLKIT

    return f"{{ {lines}}} | {TOOLKIT}/bin/python3 -I -c {shlex.quote(MEASURE)} > {FOOTPRINT}; true"


def start_command() -> str:
    """Start the sampler in the background. It writes its own process id to PIDFILE."""
    from ctb.toolkit import TOOLKIT

    return (
        f"rm -f {PIDFILE}; setsid nohup {TOOLKIT}/bin/python3 -I -c {shlex.quote(SAMPLER)} "
        f"{SAMPLES} {INTERVAL} {PIDFILE} > /dev/null 2>&1 < /dev/null & "
        f"for _ in $(seq 50); do [ -s {PIDFILE} ] && break; sleep 0.1; done; true"
    )


#: Stop the sampler after one last sample, and wait until its file is complete.
STOP = (
    f"p=$(cat {PIDFILE} 2>/dev/null) || exit 0; kill -TERM $p 2>/dev/null; "
    "for _ in $(seq 100); do kill -0 $p 2>/dev/null || break; sleep 0.1; done; "
    f"rm -f {PIDFILE}"
)


@contextlib.asynccontextmanager
async def measured(agent, environment):
    """Sample the container while the body runs. A failed sampler never fails the trial."""
    await agent.exec_as_root(environment, command=start_command())
    try:
        yield
    finally:
        await agent.exec_as_root(environment, command=STOP)


async def footprint(agent, environment) -> None:
    """Write FOOTPRINT for an agent that declares its install paths."""
    lines = getattr(agent, "FOOTPRINT", None)
    if lines:
        await agent.exec_as_agent(environment, command=footprint_command(lines))


def owned(exe: str | None, paths: list[str]) -> bool:
    return bool(exe) and any(exe == p or exe.startswith(p.rstrip("/") + "/") for p in paths)


def summary(agent_dir: Path) -> dict:
    """RAM, CPU and install size of one trial, from its sampler and footprint files.

    Agent processes run from the declared install paths and are not under a command of the agent.
    A shell that an agent process starts, and every process below it, is a tool command.
    RSS sums count the pages that processes share once for each process.
    """
    out = {}
    foot_path = agent_dir / "footprint.json"
    foot = json.loads(foot_path.read_text()) if foot_path.is_file() else {}
    for kind in ("package", "runtime", "extensions"):
        out[f"{kind}_bytes"] = (foot.get(kind) or {}).get("bytes")
    path = agent_dir / "resources.jsonl"
    if not path.is_file():
        return out
    paths = [p for kind in ("package", "runtime") for p in (foot.get(kind) or {}).get("paths", [])]
    records, kinds, ticks, last, samples = [], {}, {}, {}, []
    current: dict[int, int] = {}
    tick = 100

    def kind_of(index):
        if index in kinds:
            return kinds[index]
        proc = records[index]
        parent = current.get(proc["ppid"])
        above = kind_of(parent) if parent is not None and parent != index else None
        exe = proc["exe"]
        if above == "tool":
            kinds[index] = "tool"
        elif above == "agent":
            # An agent process runs a tool through a shell or another program. A worker from the
            # install paths, such as the Python worker of Vis, stays part of the agent.
            name = os.path.basename(exe or "")
            kinds[index] = "agent" if owned(exe, paths) and name not in SHELLS else "tool"
        else:
            kinds[index] = "agent" if owned(exe, paths) else "wrapper"
        return kinds[index]

    for line in path.read_text().split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # The sampler stopped while it wrote a line.
            continue
        if row["kind"] == "start":
            tick = row["tick"]
        elif row["kind"] == "proc":
            records.append(row)
            current[row["pid"]] = len(records) - 1
            last[(row["pid"], row["start"])] = len(records) - 1
        elif row["kind"] == "sample":
            rss = {"agent": 0, "tree": 0}
            for pid, size, used in row["procs"]:
                index = current.get(pid)
                if index is None:
                    continue
                process = (pid, records[index]["start"])
                ticks[process] = max(ticks.get(process, 0), used)
                rss["tree"] += size
                if kind_of(index) == "agent":
                    rss["agent"] += size
            samples.append((row["t"], row["cpu_usec"], row["mem"], rss["agent"], rss["tree"]))
    if len(samples) < 2:
        return out
    span = samples[-1][0] - samples[0][0]
    agent_rss = [s[3] for s in samples if s[3]]
    # An exec keeps the process and its CPU time; its last program decides its kind.
    agent_cpu = sum(t for p, t in ticks.items() if kind_of(last[p]) == "agent") / tick
    usec = [s[1] for s in samples if s[1] is not None]
    mem = [s[2] for s in samples if s[2] is not None]
    container_cpu = (usec[-1] - usec[0]) / 1e6 if len(usec) > 1 else None
    out.update(
        {
            "seconds_sampled": round(span, 1),
            "agent_rss_min": min(agent_rss) if agent_rss else None,
            "agent_rss_max": max(agent_rss) if agent_rss else None,
            "agent_cpu_s": round(agent_cpu, 2),
            "agent_cpu_pct": round(100 * agent_cpu / span, 1) if span else None,
            "tree_rss_max": max(s[4] for s in samples),
            "container_mem_min": min(mem) if mem else None,
            "container_mem_max": max(mem) if mem else None,
            "container_cpu_s": None if container_cpu is None else round(container_cpu, 2),
            "container_cpu_pct": round(100 * container_cpu / span, 1)
            if container_cpu is not None and span
            else None,
        }
    )
    return out

import json
import os
import subprocess
import sys

from ctb import resources


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def proc(pid, ppid, exe, start=1):
    return {"kind": "proc", "pid": pid, "ppid": ppid, "start": start, "exe": exe, "argv": []}


def test_summary_splits_agent_from_its_tools_and_reads_the_container(tmp_path):
    (tmp_path / "footprint.json").write_text(
        json.dumps(
            {
                "package": {"paths": ["/opt/agent"], "bytes": 5, "files": 1},
                "runtime": {"paths": ["/usr/bin/node", "/opt/py"], "bytes": 7, "files": 1},
            }
        )
    )
    mib = 2**20
    write(
        tmp_path / "resources.jsonl",
        [
            {"kind": "start", "epoch": 0, "interval": 1, "tick": 100},
            proc(10, 0, "/usr/bin/bash"),  # the exec wrapper
            proc(11, 10, "/usr/bin/node"),  # the agent on its runtime
            proc(12, 11, "/opt/agent/bin/helper"),  # a package binary of the agent
            proc(13, 11, "/usr/bin/bash"),  # a tool shell
            proc(14, 13, "/usr/bin/node"),  # a tool on the same runtime
            proc(15, 11, "/opt/py/worker"),  # a runtime worker of the agent, like the one of Vis
            {
                "kind": "sample",
                "t": 0,
                "cpu_usec": 1_000_000,
                "mem": 50 * mib,
                "procs": [[10, mib, 0], [11, 100 * mib, 50], [12, 10 * mib, 10]],
            },
            {
                "kind": "sample",
                "t": 10,
                "cpu_usec": 9_000_000,
                "mem": 400 * mib,
                "procs": [
                    [10, mib, 0],
                    [11, 150 * mib, 150],
                    [12, 10 * mib, 50],
                    [13, 2 * mib, 1],
                    [14, 200 * mib, 400],
                    [15, 20 * mib, 30],
                ],
            },
            # The tool shell runs another program in the same process.
            proc(13, 11, "/usr/bin/python3"),
            {
                "kind": "sample",
                "t": 20,
                "cpu_usec": 13_000_000,
                "mem": 120 * mib,
                "procs": [
                    [10, mib, 0],
                    [11, 120 * mib, 200],
                    [12, 10 * mib, 60],
                    [13, 30 * mib, 100],
                ],
            },
        ],
    )
    # The sampler stopped while it wrote a line.
    with (tmp_path / "resources.jsonl").open("a") as f:
        f.write('{"kind": "sample", "t": 21')
    out = resources.summary(tmp_path)
    assert out["package_bytes"] == 5 and out["runtime_bytes"] == 7
    assert out["seconds_sampled"] == 20
    assert out["agent_rss_min"] == 110 * mib
    assert out["agent_rss_max"] == 180 * mib
    # Agent CPU: node 200, helper 60 and worker 30 ticks; the tools are not in it.
    assert out["agent_cpu_s"] == 2.9
    assert out["agent_cpu_pct"] == 14.5
    assert out["tree_rss_max"] == 383 * mib
    assert out["container_cpu_s"] == 12.0
    assert out["container_cpu_pct"] == 60.0
    assert (out["container_mem_min"], out["container_mem_max"]) == (50 * mib, 400 * mib)


def test_summary_without_samples_keeps_only_the_sizes(tmp_path):
    assert resources.summary(tmp_path) == {
        "package_bytes": None,
        "runtime_bytes": None,
        "extensions_bytes": None,
    }


def test_footprint_counts_each_kind_and_a_hard_link_once(tmp_path):
    package = tmp_path / "pkg"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "agent").write_bytes(b"x" * 1000)
    os.link(package / "bin" / "agent", package / "agent-link")
    (package / "lib.js").write_bytes(b"y" * 24)
    runtime = tmp_path / "node"
    runtime.write_bytes(b"z" * 300)
    lines = resources.files_footprint(
        package=(str(package), str(tmp_path / "missing")), runtime=(str(runtime),)
    )
    listed = subprocess.run(["bash", "-c", lines], capture_output=True, text=True, check=True)
    measured = subprocess.run(
        [sys.executable, "-I", "-c", resources.MEASURE],
        input=listed.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(measured.stdout)
    assert out["package"]["bytes"] == 1024 and out["package"]["files"] == 2
    assert out["runtime"]["bytes"] == 300
    assert out["package"]["paths"] == [os.path.realpath(package)]

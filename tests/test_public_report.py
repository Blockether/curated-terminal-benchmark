import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctb import public_report as p


class PublicReportTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "schema": 2,
            "sources": {
                a: [
                    {
                        "round": r,
                        "seal_sha256": "a" * 64,
                        "harness_commit": "b" * 40,
                        "agent_version": "1.0.0",
                    }
                    for r in rounds
                ]
                for a, rounds in p.SELECTION.items()
            },
            "rows": [
                {
                    "agent": a,
                    "run": n,
                    "task": t,
                    "passed": True,
                    "error": None,
                    "result_sha256": "c" * 64,
                }
                for a, n, _ in p.selected()
                for t in p.TASKS
            ],
        }

        root = Path(__file__).resolve().parents[1]
        first = next(iter(p.read(root / "public/report-metrics.json").values()))
        self.metrics = {"c" * 64: {**first, "reward": 1}}

    def test_sealed_agent_versions_are_published_and_checked(self):
        root = Path(__file__).resolve().parents[1]
        sources = p.from_seals(root)["sources"]
        seal = p.read(root / "results" / (p.SELECTION["codex"][0] + ".json"))
        self.assertEqual(
            sources["codex"][0]["agent_version"], seal["runs"][0]["pins"]["harnesses"]["codex"]
        )
        self.data["sources"]["codex"][0]["agent_version"] = "<b>1</b>"
        with self.assertRaises(ValueError):
            p.validate(self.data)

    def test_render_has_real_results_and_no_external_requests(self):
        text = p.render(self.data, self.metrics).decode()
        self.assertIn(f"{p.trial_count()} trials", text)
        self.assertIn("100%", text)
        # One round for each agent: no mean and no round tabs.
        self.assertNotIn('data-param="view"', text)
        self.assertIn("photonic-waveguide-routing", text)
        # A canonical link names the page address; the browser does not load it.
        loads = re.findall(
            r'<(?:link(?![^>]*rel="canonical")|script|img|iframe)\b[^>]*\b(?:src|href)="([^"]*)"',
            text,
        )
        self.assertTrue(loads)
        self.assertFalse([u for u in loads if "//" in u or ":" in u])
        self.assertIn('src="report.js?v=', text)

    def test_repeated_rounds_show_the_mean_first_then_each_round(self):
        rounds = ("gpt-bench-b2a4d4a", "second-round")
        with mock.patch.dict(p.SELECTION, {"codex": rounds}):
            self.data["sources"]["codex"].append(
                {**self.data["sources"]["codex"][0], "round": rounds[1]}
            )
            # Round 2 of Codex (the first row) passes only the first half of the tasks.
            half = len(p.TASKS) // 2
            self.data["rows"] += [
                {**self.data["rows"][0], "run": 2, "task": t, "passed": i < half}
                | ({} if i < half else {"result_sha256": "d" * 64})
                for i, t in enumerate(p.TASKS)
            ]
            self.metrics["d" * 64] = {**self.metrics["c" * 64], "reward": 0}
            text = p.render(self.data, self.metrics).decode()
        tabs = re.findall(r'data-param="view">(.*?)</div>', text)[0]
        self.assertEqual(re.findall(r'data-tab="([^"]+)"', tabs), ["mean", "round-1", "round-2"])
        # 12 and 6 passes: the mean and the sample standard deviation per round.
        self.assertIn("9.0 ± 4.2", text)
        last = p.TASKS[-1]
        row = re.search(rf'title="{last}">[^<]*</th>(.*?)</tr>', text.split("Passes per task")[1])
        self.assertIn(">1/2<", row.group(1))
        round_2 = text.split('data-tab-panel="round-2"')[1]
        self.assertIn("r2-general", round_2)
        self.assertNotIn(">kmet</a>", round_2.split('data-tab-panel="r2-normalized"')[0])

    def test_metrics_reject_unknown_text_and_invalid_numbers(self):
        root = Path(__file__).resolve().parents[1]
        data = p.read(root / "public/comparison.json")
        metrics = p.read(root / "public/report-metrics.json")
        p.validate_metrics(metrics, data)
        key = next(iter(metrics))
        for field, value in (
            ("run_ms", -1),
            ("cost", float("nan")),
            ("tool_calls", True),
            ("secret", "private"),
        ):
            bad = copy.deepcopy(metrics)
            bad[key][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.validate_metrics(bad, data)
        with self.assertRaises(ValueError):
            p.validate_metrics({}, data)

    def test_unknown_or_secret_values_fail(self):
        changes = [
            ("agent", "SECRET-canary"),
            ("task", "SECRET-canary"),
            ("error", "SECRET-canary"),
            ("result_sha256", "SECRET-canary"),
            ("passed", "SECRET-canary"),
            ("transcript", "SECRET-canary"),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["rows"][0][key] = value
                with self.assertRaises(ValueError):
                    p.render(data, self.metrics)
        for key in ("logs", "token", "prompt"):
            data = copy.deepcopy(self.data)
            data[key] = "SECRET-canary"
            with self.assertRaises(ValueError):
                p.validate(data)

    def test_empty_duplicate_and_boundary_inputs_fail(self):
        for rows in (
            [],
            self.data["rows"][:-1],
            self.data["rows"] + self.data["rows"][:1],
            self.data["rows"][:-1] + self.data["rows"][:1],
        ):
            data = {**self.data, "rows": rows}
            with self.assertRaises(ValueError):
                p.validate(data)
        for value in (0, 1, None, float("nan")):
            data = copy.deepcopy(self.data)
            data["rows"][0]["passed"] = value
            with self.assertRaises(ValueError):
                p.validate(data)

    def test_output_extra_hidden_symlink_or_tampered_file_fails(self):
        for kind in ("extra", "hidden", "nested", "symlink", "tamper"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp).resolve()
                for name, raw in p.expected(self.data, self.metrics).items():
                    (out / name).write_bytes(raw)
                p.check_output(out, self.data, self.metrics)
                if kind == "extra":
                    (out / "transcript.txt").write_text("SECRET-canary")
                elif kind == "hidden":
                    (out / ".env").write_text("SECRET-canary")
                elif kind == "nested":
                    (out / "nested").mkdir()
                elif kind == "symlink":
                    (out / "index.html").unlink()
                    (out / "index.html").symlink_to(out / "comparison.json")
                else:
                    (out / "index.html").write_text("SECRET-canary")
                with self.assertRaises(ValueError):
                    p.check_output(out, self.data, self.metrics)

    def test_duplicate_json_keys_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "data.json"
            path.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(ValueError):
                p.read(path)


class AssetRevisionTests(unittest.TestCase):
    def test_changed_asset_changes_browser_url_and_nested_css_urls(self):
        from unittest.mock import patch

        raw = p.source_assets()
        first = p.public_page('<meta charset="utf-8"><script src="report.js"></script>')
        with patch.object(
            p, "source_assets", return_value={**raw, "report.js": raw["report.js"] + b"\n// next"}
        ):
            second = p.public_page('<meta charset="utf-8"><script src="report.js"></script>')
            self.assertNotEqual(first, second)
            revision = p.asset_revision()
            self.assertIn("report.js?v=" + revision, second)
            linked = p.public_page(
                '<meta charset="utf-8"><link rel="stylesheet" href="tokens.css">'
            )
            self.assertIn("tokens.css?v=" + revision, linked)
            self.assertIn(("inter-latin.woff2?v=" + revision).encode(), p.assets()["fonts.css"])
            nested = p.version_assets('<script src="../viewer.js"></script>')
            self.assertIn("../viewer.js?v=" + revision, nested)
            self.assertEqual(p.version_assets(nested), nested)


if __name__ == "__main__":
    unittest.main()

import copy
import tempfile
import unittest
from pathlib import Path

from ctb import public_report as p


class PublicReportTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "schema": 1,
            "sources": {
                a: {"round": r, "seal_sha256": "a" * 64, "harness_commit": "b" * 40}
                for a, r in p.SELECTION.items()
            },
            "rows": [
                {"agent": a, "task": t, "passed": True, "error": None, "result_sha256": "c" * 64}
                for a in p.SELECTION
                for t in p.TASKS
            ],
        }

        root = Path(__file__).resolve().parents[1]
        first = next(iter(p.read(root / "public/report-metrics.json").values()))
        self.metrics = {"c" * 64: {**first, "reward": 1}}

    def test_render_has_real_results_and_no_external_requests(self):
        text = p.render(self.data, self.metrics).decode()
        self.assertIn("72 trials", text)
        self.assertIn("100%", text)
        self.assertIn("photonic-waveguide-routing", text)
        self.assertNotIn("https://", text)
        self.assertIn('src="report.js?v=', text)

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

    def test_real_projection_matches_seals_and_build_is_checked(self):
        root = Path(__file__).resolve().parents[1]
        data = p.from_seals(root)
        self.assertEqual(data, p.read(root / "public/comparison.json"))
        self.assertEqual(sum(r["passed"] for r in data["rows"] if r["agent"] == "vis-solo"), 7)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / "site"
            p.build(root, out)
            p.build(root, out, check=True)
            with self.assertRaises(ValueError):
                p.build(root, out)
            (out / ".credential").write_text("SECRET-canary")
            with self.assertRaises(ValueError):
                p.build(root, out, check=True)


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
            self.assertIn(("tokens.css?v=" + revision).encode(), p.assets()["report.css"])
            self.assertIn(("inter-latin.woff2?v=" + revision).encode(), p.assets()["fonts.css"])
            nested = p.version_assets('<script src="../viewer.js"></script>')
            self.assertIn("../viewer.js?v=" + revision, nested)
            self.assertEqual(p.version_assets(nested), nested)


if __name__ == "__main__":
    unittest.main()

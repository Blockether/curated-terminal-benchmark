import tempfile
import unittest
from pathlib import Path

from ctb import public_report as report
from ctb import public_transcripts as pub


class TranscriptTests(unittest.TestCase):
    def payload(self, text="A harmless tool result"):
        return {
            "schema": 1,
            "result_sha256": "a" * 64,
            "events": [
                {"kind": "tool", "name": "shell", "text": text, "input": None, "output": None}
            ],
        }

    def test_scrub_known_and_encoded_credentials(self):
        values = pub.secret_variants(["synthetic-private-value-123456"])
        for value in values:
            clean = pub.scrub("before " + value + " after", values)
            self.assertNotIn(value, clean)
            self.assertIn(pub.REDACTED, clean)
            self.assertEqual(clean, pub.scrub(clean, values))

    def test_patterns_and_paths_are_removed(self):
        for text in (
            "password='sample-password'",
            "Authorization: Bearer abcdefghijklmnop",
            "sk-" + "Z" * 40,
            "ghp_" + "A" * 40,
            "eyJabcdef.abcdefghi.signature",
            "/Users/example/task",
            "-----BEGIN " + "PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----",
            '"encrypted_content": "opaque-data"',
        ):
            clean = pub.scrub(text)
            self.assertNotEqual(clean, text)
            self.assertEqual(clean, pub.scrub(clean))
            with self.assertRaises(ValueError):
                pub.validate_payload(self.payload(text), "a" * 64)

    def test_generic_credentials_and_all_cookies_are_removed(self):
        for text in (
            "AWS_SECRET_ACCESS_KEY=" + "Q" * 40,
            "GITHUB_TOKEN=" + "Q" * 40,
            "NPM_TOKEN=" + "Q" * 40,
            "Cookie: session=firstprivatevalue; other=secondprivatevalue",
        ):
            clean = pub.scrub(text)
            self.assertNotIn("Q" * 40, clean)
            self.assertNotIn("secondprivatevalue", clean)
            self.assertEqual(clean, pub.scrub(clean))
            with self.assertRaises(ValueError):
                pub.validate_payload(self.payload(text), "a" * 64)

    def test_roundtrip_nonempty_and_unicode(self):
        data = self.payload("Zażółć 日本語 <script>alert(1)</script>")
        raw = report.encoded(data)
        self.assertEqual(pub.unpack(pub.pack(raw)), raw)
        self.assertEqual(pub.validate_payload(pub.decode(raw), "a" * 64), data)

    def test_extra_fields_and_wrong_types_fail(self):
        for key, value in (("kind", "unknown"), ("name", {}), ("text", 1), ("secret", "x")):
            data = self.payload()
            data["events"][0][key] = value
            with self.assertRaises(ValueError):
                pub.validate_payload(data, "a" * 64)
        for events in ([], None, "bad", [{}] * 20_001):
            data = self.payload()
            data["events"] = events
            with self.assertRaises(ValueError):
                pub.validate_payload(data, "a" * 64)

    def test_gzip_trailing_data_extra_members_and_bomb_fail(self):
        raw = pub.pack(report.encoded(self.payload()))
        for value in (
            raw + b"hidden",
            raw + raw,
            raw[:10],
            b"",
            pub.pack(b"x" * (pub.MAX_EXPANDED + 1)),
        ):
            with self.assertRaises((ValueError, pub.zlib.error)):
                pub.unpack(value)
        damaged = bytearray(raw)
        damaged[3] = 8  # A filename field could hide data outside the JSON payload.
        with self.assertRaises(ValueError):
            pub.unpack(bytes(damaged))

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            pub.decode('{"schema":1,"schema":1}')

    def test_real_checked_site_mutations_fail(self):
        root = Path(__file__).resolve().parents[1]
        comparison, manifest, metrics = pub.checked_inputs(root)
        self.assertEqual(len(manifest["transcripts"]), len(comparison["rows"]))
        # Reuse a small synthetic site with an approved synthetic manifest.
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / "site"
            (out / "transcripts").mkdir(parents=True)
            fake = {"schema": 1, "transcripts": {}}
            blobs = {}
            for row in comparison["rows"]:
                data = self.payload()
                data["result_sha256"] = row["result_sha256"]
                expanded = report.encoded(data)
                raw = pub.pack(expanded)
                fake["transcripts"][row["result_sha256"]] = {
                    "sha256": pub.content_hash(raw),
                    "bytes": len(raw),
                    "expanded_bytes": len(expanded),
                    "events": 1,
                }
                blobs[row["result_sha256"]] = raw
            for name, raw in pub.static_files(comparison, fake, metrics).items():
                (out / name).parent.mkdir(parents=True, exist_ok=True)
                (out / name).write_bytes(raw)
            for key, raw in blobs.items():
                (out / "transcripts" / (key + ".json.gz")).write_bytes(raw)
            with patch.object(pub, "checked_inputs", return_value=(comparison, fake, metrics)):
                pub.check(root, out)
                (out / ".env").write_text("synthetic secret")
                with self.assertRaises(ValueError):
                    pub.check(root, out)
                (out / ".env").unlink()
                path = out / "transcripts" / (next(iter(blobs)) + ".json.gz")
                path.write_bytes(path.read_bytes() + b"hidden")
                with self.assertRaises(ValueError):
                    pub.check(root, out)


if __name__ == "__main__":
    unittest.main()

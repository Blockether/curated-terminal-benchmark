from ctb.report import chart, digest, encode


def test_canonical_json_ignores_dictionary_insertion_order():
    assert encode({"b": 2, "a": 1}) == encode({"a": 1, "b": 2})
    assert digest(encode({"a": 1}).encode()) == digest(encode({"a": 1}).encode())


def test_chart_is_deterministic_and_escapes_labels():
    groups = [{"agent": "A&B", "model": "x/<model>", "passed": 0}]
    first = chart(groups, "passed", "Test & chart", "")
    assert first == chart(groups, "passed", "Test & chart", "")
    assert "A&amp;B" in first and "&lt;model&gt;" in first
    assert 'width="0.00"' in first


def test_dirge_report_keeps_sealed_primary_trials_and_separate_retries():
    import json

    from ctb.report import build

    outputs = build(include_dirge=True)
    data = json.loads(outputs["data.json"])
    assert len(data["rows"]) == 84
    rows = [row for row in data["rows"] if row["agent"] == "dirge"]
    assert len(rows) == 12
    assert sum(row["reward"] == 1 for row in rows) == 3
    assert all(row["provenance"].startswith("sealed-dirge") for row in rows)
    assert len(data["supplements"]) == 2
    assert all("excluded from primary scores" in row["provenance"] for row in data["supplements"])
    manifest = json.loads(outputs["manifest.json"])
    assert any(name.endswith("dirge.jsonl") for name in manifest["evidence"])

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

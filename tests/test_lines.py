from ctb import lines as lines_module
from ctb.lines import lines


def test_lines_cross_read_parts_and_keep_only_the_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(lines_module, "CHUNK", 5)
    log = tmp_path / "agent.edn"
    log.write_text(
        '{:type :message-end, :a 1}\n{:tool-results "'
        + "x" * 50
        + '"}\nż\u2028ł\n{:type :message-end 2}'
    )
    assert list(lines(log)) == [
        "{:type :message-end, :a 1}",
        '{:tool-results "' + "x" * 50 + '"}',
        "ż\u2028ł",
        "{:type :message-end 2}",
    ]
    assert list(lines(log, prefix="{:type :message-end")) == [
        "{:type :message-end, :a 1}",
        "{:type :message-end 2}",
    ]

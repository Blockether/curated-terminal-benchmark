import json

import pytest

from ctb.agents.pi import validate_completion


def stream(*reasons):
    return "\n".join(
        json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": reason}})
        for reason in reasons
    )


def test_pi_json_terminal_errors_do_not_count_as_success():
    for text in ("", "not json", stream("error"), stream("aborted"), stream("toolUse")):
        with pytest.raises(RuntimeError):
            validate_completion(text)
    assert validate_completion(stream("toolUse", "stop")) == "stop"
    assert validate_completion(stream("error", "stop")) == "stop"
    with pytest.raises(RuntimeError):
        validate_completion(stream("stop", "error"))

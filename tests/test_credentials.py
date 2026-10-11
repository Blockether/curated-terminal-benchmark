import base64
import json
import time

import pytest

from ctb import credentials


def jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_staged_files_never_carry_a_refresh_token(tmp_path):
    path = tmp_path / "anthropic.json"
    future = credentials.now_ms() + 10 * 3600 * 1000
    path.write_text(
        json.dumps({"access_token": "A", "refresh_token": "R", "expires_at_ms": future})
    )
    access = credentials.anthropic(3600, path)
    staged = json.loads(credentials.vis_anthropic_file(access))
    assert staged["access_token"] == "A" and staged["refresh_token"] == ""
    assert '"R"' not in credentials.codex_auth_json(access)
    assert ':refresh ""' in credentials.kmet_auth_edn("anthropic", access)


def test_a_token_that_expires_during_the_trial_is_refused(tmp_path):
    path = tmp_path / "codex.json"
    path.write_text(json.dumps({"tokens": {"access_token": jwt(int(time.time()) + 60)}}))
    vis = tmp_path / "vis.json"
    vis.write_text(json.dumps({"access_token": "V", "expires_at_ms": credentials.now_ms()}))
    with pytest.raises(credentials.CredentialError, match="expires in"):
        credentials.codex_cli(3600, path, vis)


def test_an_expired_codex_login_falls_back_to_the_vis_token(tmp_path):
    path = tmp_path / "codex.json"
    path.write_text(json.dumps({"tokens": {"access_token": jwt(1), "id_token": "I"}}))
    vis = tmp_path / "vis.json"
    future = credentials.now_ms() + 10 * 3600 * 1000
    vis.write_text(json.dumps({"access_token": "V", "expires_at_ms": future, "account_id": "acc"}))
    access = credentials.codex_cli(3600, path, vis)
    assert (access.token, access.account_id, access.id_token) == ("V", "acc", "I")


def test_jwt_expiry_is_read_and_opaque_tokens_give_none():
    assert credentials.jwt_exp_ms(jwt(100)) == 100_000
    assert credentials.jwt_exp_ms("opaque") is None


def write_claude(path, left_s):
    path.write_text(
        json.dumps({"access_token": "A", "expires_at_ms": credentials.now_ms() + left_s * 1000})
    )


def test_refresh_anthropic_keeps_a_long_token(tmp_path):
    path = tmp_path / "anthropic.json"
    write_claude(path, 9000)
    calls = []
    credentials.refresh_anthropic(7500, path, sleep=calls.append, run=calls.append)
    assert calls == []


def test_refresh_anthropic_waits_for_the_vis_window_then_runs_one_turn(tmp_path):
    path = tmp_path / "anthropic.json"
    write_claude(path, 1000)
    slept, turns = [], []

    def turn(argv, **_):
        turns.append(argv)
        write_claude(path, 28000)

    access = credentials.refresh_anthropic(7500, path, sleep=slept.append, run=turn)
    assert access.token == "A" and len(turns) == 1 and turns[0][0] == "vis-agent"
    assert 700 < slept[0] <= 1000 - credentials.VIS_REFRESH_AT_S + 30


def test_refresh_anthropic_fails_when_vis_did_not_refresh(tmp_path):
    path = tmp_path / "anthropic.json"
    write_claude(path, 100)
    with pytest.raises(credentials.ExpiringToken):
        credentials.refresh_anthropic(7500, path, sleep=lambda _: None, run=lambda *a, **k: None)

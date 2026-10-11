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

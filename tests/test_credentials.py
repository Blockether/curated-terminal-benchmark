import base64
import json
import time

import pytest

from ctb import credentials


def jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_staged_files_never_carry_a_refresh_token(tmp_path):
    path = tmp_path / "codex.json"
    future = credentials.now_ms() + 10 * 3600 * 1000
    path.write_text(
        json.dumps({"access_token": "A", "refresh_token": "R", "expires_at_ms": future})
    )
    access = credentials.codex_vis(3600, path)
    staged = json.loads(credentials.vis_codex_file(access))
    assert staged["access_token"] == "A" and staged["refresh_token"] == ""
    assert '"R"' not in credentials.codex_auth_json(access)
    assert ':refresh ""' in credentials.kmet_auth_edn("openai-codex", access)


def test_a_token_that_expires_during_the_trial_is_refused(tmp_path):
    path = tmp_path / "codex.json"
    path.write_text(json.dumps({"tokens": {"access_token": jwt(int(time.time()) + 60)}}))
    vis = tmp_path / "vis.json"
    vis.write_text(json.dumps({"access_token": "V", "expires_at_ms": credentials.now_ms()}))
    with pytest.raises(credentials.CredentialError, match="expires in"):
        credentials.codex_cli(3600, path, vis)


def test_vis_refreshes_a_token_that_is_too_short_for_a_trial(tmp_path):
    path = tmp_path / "vis.json"
    left_ms = 2000 * 1000
    path.write_text(
        json.dumps({"access_token": "OLD", "expires_at_ms": credentials.now_ms() + left_ms})
    )
    waits, commands = [], []

    def vis(command, **_):
        # Vis refreshes in its last minutes and saves a new 10-day token.
        commands.append(tuple(command))
        ten_days = credentials.now_ms() + 240 * 3600 * 1000
        path.write_text(json.dumps({"access_token": "NEW", "expires_at_ms": ten_days}))

    access = credentials.fresh_codex_vis(7500, path, waits.append, vis)
    assert access.token == "NEW" and commands == [credentials.VIS_REFRESH]
    assert left_ms / 1000 - credentials.VIS_REFRESH_MARGIN_S - 5 < waits[0] <= 1760


def test_a_token_that_vis_does_not_refresh_still_blocks(tmp_path):
    path = tmp_path / "vis.json"
    path.write_text(json.dumps({"access_token": "OLD", "expires_at_ms": credentials.now_ms()}))
    waits = []
    with pytest.raises(credentials.ExpiringToken):
        credentials.fresh_codex_vis(7500, path, waits.append, lambda *_a, **_k: None)
    assert waits == []


def test_codex_uses_the_vis_token_and_keeps_a_same_account_id_token(tmp_path):
    path = tmp_path / "codex.json"
    path.write_text(
        json.dumps({"tokens": {"access_token": jwt(1), "id_token": "I", "account_id": "acc"}})
    )
    vis = tmp_path / "vis.json"
    future = credentials.now_ms() + 10 * 3600 * 1000
    vis.write_text(json.dumps({"access_token": "V", "expires_at_ms": future, "account_id": "acc"}))
    access = credentials.codex_cli(3600, path, vis)
    assert (access.token, access.account_id, access.id_token) == ("V", "acc", "I")


def test_codex_never_uses_another_valid_codex_login(tmp_path):
    # A free host Codex login refused gpt-6.1-sol, while the Vis login could use it.
    claims = {
        "exp": int(time.time()) + 36000,
        credentials.AUTH_CLAIM: {"chatgpt_account_id": "acc", "chatgpt_plan_type": "pro"},
        credentials.PROFILE_CLAIM: {"email": "dev@example.com"},
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    vis_token = f"h.{body}.s"
    path = tmp_path / "codex.json"
    other = jwt(int(time.time()) + 36000)
    path.write_text(
        json.dumps({"tokens": {"access_token": other, "id_token": "I", "account_id": "free"}})
    )
    vis = tmp_path / "vis.json"
    future = credentials.now_ms() + 10 * 3600 * 1000
    vis.write_text(
        json.dumps({"access_token": vis_token, "expires_at_ms": future, "account_id": "acc"})
    )
    access = credentials.codex_cli(3600, path, vis)
    assert (access.token, access.account_id) == (vis_token, "acc")
    id_claims = credentials.jwt_claims(access.id_token)
    assert id_claims[credentials.AUTH_CLAIM]["chatgpt_plan_type"] == "pro"
    assert id_claims["email"] == "dev@example.com"


def test_jwt_expiry_is_read_and_opaque_tokens_give_none():
    assert credentials.jwt_exp_ms(jwt(100)) == 100_000
    assert credentials.jwt_exp_ms("opaque") is None

"""Stage subscription access tokens for disposable task containers.

OAuth refresh tokens rotate. If a container refreshes a copied token, the host
login can stop working. So a container gets only a current access token, and
every refresh token that it sees is empty.
"""

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
VIS_ANTHROPIC = HOME / ".vis" / "anthropic-auth.json"
VIS_CODEX = HOME / ".vis" / "openai-codex-auth.json"
CODEX_CLI = HOME / ".codex" / "auth.json"


class CredentialError(RuntimeError):
    """A host credential is missing or expires before the trial can end."""


@dataclass(frozen=True)
class Access:
    """One access token without its refresh token."""

    token: str
    expires_ms: int
    account_id: str | None = None
    id_token: str | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def jwt_exp_ms(token: str) -> int | None:
    """Read the `exp` claim of a JWT, or None for an opaque token."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return int(exp * 1000) if isinstance(exp, (int, float)) else None


def _require(access: Access, min_valid_s: int, source: Path, refresh_hint: str) -> Access:
    left_s = (access.expires_ms - now_ms()) // 1000
    if left_s < min_valid_s:
        raise CredentialError(
            f"{source} expires in {left_s} s; a trial needs {min_valid_s} s. {refresh_hint}"
        )
    return access


def _read(path: Path) -> dict:
    if not path.is_file():
        raise CredentialError(f"Missing credential file: {path}")
    return json.loads(path.read_text())


def anthropic(min_valid_s: int, path: Path = VIS_ANTHROPIC) -> Access:
    """The Claude subscription access token that Vis keeps."""
    data = _read(path)
    access = Access(data["access_token"], int(data["expires_at_ms"]))
    return _require(
        access, min_valid_s, path, "Run one Vis turn on anthropic-coding-plan to refresh it."
    )


def codex_vis(min_valid_s: int, path: Path = VIS_CODEX) -> Access:
    """The ChatGPT subscription access token that Vis keeps."""
    data = _read(path)
    access = Access(data["access_token"], int(data["expires_at_ms"]), data.get("account_id"))
    return _require(access, min_valid_s, path, "Run one Vis turn on openai-codex to refresh it.")


def codex_cli(min_valid_s: int, path: Path = CODEX_CLI, vis_path: Path = VIS_CODEX) -> Access:
    """A ChatGPT access token in Codex CLI form.

    Use the Codex CLI token while it is valid. Else use the Vis token with the
    Codex CLI `id_token`, which carries only account claims.
    """
    tokens = (_read(path).get("tokens") or {}) if path.is_file() else {}
    token = tokens.get("access_token") or ""
    access = Access(token, jwt_exp_ms(token) or 0, tokens.get("account_id"), tokens.get("id_token"))
    if access.expires_ms - now_ms() >= min_valid_s * 1000:
        return access
    vis = codex_vis(min_valid_s, vis_path)
    return Access(vis.token, vis.expires_ms, vis.account_id or access.account_id, access.id_token)


def vis_anthropic_file(access: Access) -> str:
    return json.dumps(
        {
            "access_token": access.token,
            "refresh_token": "",
            "expires_at_ms": access.expires_ms,
            "saved_at_ms": now_ms(),
        }
    )


def vis_codex_file(access: Access) -> str:
    return json.dumps(
        {
            "access_token": access.token,
            "refresh_token": "",
            "expires_at_ms": access.expires_ms,
            "account_id": access.account_id,
            "saved_at": now_ms(),
        }
    )


def codex_auth_json(access: Access) -> str:
    """A Codex CLI `auth.json` that cannot rotate the host login."""
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": access.id_token,
                "access_token": access.token,
                "refresh_token": "",
                "account_id": access.account_id,
            },
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


def kmet_auth_edn(provider: str, access: Access) -> str:
    """A kmet `auth.edn` with one OAuth entry and an empty refresh token."""
    return (
        "{:"
        + provider
        + " {:type :oauth :access "
        + json.dumps(access.token)
        + ' :refresh "" :expires '
        + str(access.expires_ms)
        + "}}\n"
    )

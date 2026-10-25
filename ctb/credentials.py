"""Stage subscription access tokens for disposable task containers.

OAuth refresh tokens rotate. If a container refreshes a copied token, the host
login can stop working. So a container gets only a current access token, and
every refresh token that it sees is empty.
"""

import base64
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
VIS_ANTHROPIC = HOME / ".vis" / "anthropic-auth.json"
VIS_CODEX = HOME / ".vis" / "openai-codex-auth.json"
CODEX_CLI = HOME / ".codex" / "auth.json"
# Vis refreshes a stored Claude token only when less than this time is left.
VIS_REFRESH_AT_S = 300
REFRESH_TURN = (
    "vis-agent",
    "--db",
    ":memory",
    "--model",
    "anthropic-coding-plan/claude-opus-5-5",
    "--",
    "Reply with OK.",
)
_refresh_lock = threading.Lock()


class CredentialError(RuntimeError):
    """A host credential is missing or expires before the trial can end."""


class ExpiringToken(CredentialError):
    """A host token is valid, but not for the time that a trial needs."""


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
        raise ExpiringToken(
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


def anthropic_left_s(path: Path = VIS_ANTHROPIC) -> int:
    """Seconds until the stored Claude token expires, as Vis counts them."""
    return (int(_read(path)["expires_at_ms"]) - now_ms()) // 1000


def refresh_anthropic(
    min_valid_s: int, path: Path = VIS_ANTHROPIC, sleep=time.sleep, run=subprocess.run
) -> Access:
    """Return a Claude token valid for min_valid_s; wait for the Vis refresh window if needed.

    Vis owns the rotating refresh token and refreshes it under its own file lock.
    So the harness never calls the token endpoint: it runs one short host Vis turn.
    """
    left_s = anthropic_left_s(path)
    if left_s >= min_valid_s:
        return anthropic(min_valid_s, path)
    if left_s >= VIS_REFRESH_AT_S:
        sleep(left_s - VIS_REFRESH_AT_S + 30)
    with _refresh_lock:
        if anthropic_left_s(path) < min_valid_s:
            run(
                list(REFRESH_TURN),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
    return anthropic(min_valid_s, path)


def codex_vis(min_valid_s: int, path: Path = VIS_CODEX) -> Access:
    """The ChatGPT subscription access token that Vis keeps."""
    data = _read(path)
    access = Access(data["access_token"], int(data["expires_at_ms"]), data.get("account_id"))
    return _require(access, min_valid_s, path, "Run one Vis turn on openai-codex to refresh it.")


def codex_cli(min_valid_s: int, path: Path = CODEX_CLI, vis_path: Path = VIS_CODEX) -> Access:
    """A ChatGPT access token in Codex CLI form.

    Use the Vis token with the Codex CLI `id_token`, which carries only account claims.
    Codex then uses the same ChatGPT account as the Vis arms. Use the Codex CLI token
    only when the Vis token is not valid.
    """
    tokens = (_read(path).get("tokens") or {}) if path.is_file() else {}
    token = tokens.get("access_token") or ""
    access = Access(token, jwt_exp_ms(token) or 0, tokens.get("account_id"), tokens.get("id_token"))
    try:
        vis = codex_vis(min_valid_s, vis_path)
    except (CredentialError, KeyError, ValueError) as error:
        hint = (
            f"The Vis token is not usable ({error}). Log in to Codex CLI or refresh the Vis token."
        )
        return _require(access, min_valid_s, path, hint)
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

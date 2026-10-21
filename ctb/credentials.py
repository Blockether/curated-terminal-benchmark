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
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"
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


def jwt_claims(token: str) -> dict:
    """The payload of a JWT, or an empty dict for an opaque token."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def jwt_exp_ms(token: str) -> int | None:
    """Read the `exp` claim of a JWT, or None for an opaque token."""
    exp = jwt_claims(token).get("exp")
    return int(exp * 1000) if isinstance(exp, (int, float)) else None


def _segment(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def account_id_token(access_token: str) -> str:
    """An unsigned `id_token` with the account claims of a ChatGPT access token.

    Codex CLI reads the plan and the account from these claims. It does not check a signature.
    """
    claims = jwt_claims(access_token)
    body = {key: claims[key] for key in ("exp", "iat", "sub") if key in claims}
    body["email"] = (claims.get(PROFILE_CLAIM) or {}).get("email")
    body[AUTH_CLAIM] = claims.get(AUTH_CLAIM) or {}
    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(body)}.unsigned"


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
    """The Vis ChatGPT token in Codex CLI form.

    Codex then uses the same account and plan as Vis. The host Codex CLI login can be
    another account, and its plan can refuse the model. Keep its `id_token` only for
    the same account. Else build the `id_token` from the claims of the Vis token.
    """
    vis = codex_vis(min_valid_s, vis_path)
    tokens = (_read(path).get("tokens") or {}) if path.is_file() else {}
    id_token = tokens.get("id_token")
    if not id_token or tokens.get("account_id") != vis.account_id:
        id_token = account_id_token(vis.token)
    return Access(vis.token, vis.expires_ms, vis.account_id, id_token)


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

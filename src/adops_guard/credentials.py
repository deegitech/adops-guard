"""Load credentials from the places they belong, and nowhere else.

Google: the standard ``google-ads.yaml`` keys, from ``GOOGLE_ADS_*`` environment
variables or from a YAML file that only you can read (chmod 600). The OAuth
client id, client secret and refresh token are required. A developer token is
optional: Google sunset developer tokens on 9 September 2026 (API access now
belongs to the Google Cloud project), ignores them in requests, and has said it
will reject them in a future major API version. One is sent only if you set it.

Meta: an access token from an environment variable, a chmod 600 file, the
macOS Keychain or AWS SSM Parameter Store (through the ``aws`` CLI).

Values are wrapped in :class:`~adops_guard.redact.Secret` immediately. They
are never accepted as command-line arguments, never put in URLs, and never
written to the journal or the state file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adops_guard.config import GoogleSettings, MetaSettings
from adops_guard.errors import CredentialError
from adops_guard.redact import Secret, redact

GOOGLE_ENV = {
    "client_id": "GOOGLE_ADS_CLIENT_ID",
    "client_secret": "GOOGLE_ADS_CLIENT_SECRET",
    "refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
}
GOOGLE_DEVELOPER_TOKEN_ENV = "GOOGLE_ADS_DEVELOPER_TOKEN"  # optional, see the module docstring
GOOGLE_LOGIN_ENV = "GOOGLE_ADS_LOGIN_CUSTOMER_ID"
GOOGLE_FILE_ENV = "GOOGLE_ADS_CONFIGURATION_FILE_PATH"
GOOGLE_DEFAULT_FILE = "~/google-ads.yaml"
META_FILE_ENV = "META_ACCESS_TOKEN_FILE"
GOOGLE_SETUP_HINT = (
    "set up the Cloud project, the OAuth client and a refresh token, then write ~/google-ads.yaml "
    "(docs/setup.md, Google steps 1 to 5); or set GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET and "
    "GOOGLE_ADS_REFRESH_TOKEN"
)

Runner = Callable[..., Any]


@dataclass(frozen=True)
class GoogleCredentials:
    developer_token: Secret | None
    client_id: Secret
    client_secret: Secret
    refresh_token: Secret
    login_customer_id: str | None
    source: str


def check_private_file(path: Path) -> None:
    """Refuse a credential file that other users could read, or that someone else owns."""
    if os.name == "nt":  # POSIX permission bits do not describe Windows ACLs
        return
    info = path.stat()
    if info.st_mode & 0o077:
        raise CredentialError(
            f"{path} can be read by other users (mode {oct(info.st_mode & 0o777)})",
            hint=f"run: chmod 600 {path}",
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CredentialError(f"{path} belongs to another user", hint="credential files must be owned by you")


def _is_placeholder(value: str) -> bool:
    upper = value.upper()
    return upper.startswith(("INSERT_", "YOUR_", "REPLACE_")) or upper in {"", "NONE", "NULL", "~"}


def parse_flat_yaml(text: str) -> dict[str, str]:
    """Read top-level ``key: value`` pairs, which is all ``google-ads.yaml`` needs."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw[:1] in (" ", "\t"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def _login_id(value: str | None) -> str | None:
    if not value or _is_placeholder(value):
        return None
    return value


def _developer_token(value: str | None) -> Secret | None:
    return Secret(value) if value and not _is_placeholder(value) else None


def load_google_credentials(settings: GoogleSettings, env: Mapping[str, str] | None = None) -> GoogleCredentials:
    env = os.environ if env is None else env
    present = {key: env[name] for key, name in GOOGLE_ENV.items() if env.get(name)}
    if present:
        missing = [name for key, name in GOOGLE_ENV.items() if key not in present]
        if missing:
            raise CredentialError(
                "some GOOGLE_ADS_* variables are set but these are missing: " + ", ".join(missing),
                hint="set the client id, client secret and refresh token variables, or unset them all to use a "
                "google-ads.yaml file",
            )
        return GoogleCredentials(
            developer_token=_developer_token(env.get(GOOGLE_DEVELOPER_TOKEN_ENV)),
            client_id=Secret(present["client_id"]),
            client_secret=Secret(present["client_secret"]),
            refresh_token=Secret(present["refresh_token"]),
            login_customer_id=_login_id(env.get(GOOGLE_LOGIN_ENV)),
            source="environment",
        )

    path = Path(settings.credentials_file or env.get(GOOGLE_FILE_ENV) or GOOGLE_DEFAULT_FILE).expanduser()
    if not path.is_file():
        raise CredentialError(
            f"no Google Ads credentials: {path} does not exist and GOOGLE_ADS_* variables are not set",
            hint=GOOGLE_SETUP_HINT,
        )
    check_private_file(path)
    data = parse_flat_yaml(path.read_text(encoding="utf-8"))
    if data.get("json_key_file_path") and not data.get("refresh_token"):
        raise CredentialError(
            "service-account credentials are not supported yet", hint="use an OAuth refresh token (see README)"
        )
    missing = [key for key in GOOGLE_ENV if _is_placeholder(data.get(key, ""))]
    if missing:
        raise CredentialError(
            f"{path} is missing: " + ", ".join(missing),
            hint="replace the REPLACE_ME values with your OAuth client and refresh token "
            "(docs/setup.md, Google steps 3 to 5)",
        )
    return GoogleCredentials(
        developer_token=_developer_token(data.get("developer_token")),
        client_id=Secret(data["client_id"]),
        client_secret=Secret(data["client_secret"]),
        refresh_token=Secret(data["refresh_token"]),
        login_customer_id=_login_id(data.get("login_customer_id")),
        source=str(path),
    )


STORE_AGAIN = "store the token again (docs/setup.md, Meta step 6)"


def no_meta_token_hint(token_env: str) -> str:
    """The fix when no Meta token is set: how to get one, or how to hand over the one you have."""
    return (
        f"no token yet? docs/setup.md, Meta steps 1 to 6; have one? run: read -rs {token_env} && export "
        f"{token_env} (paste it, press Enter), or set [meta] token_source to file, keychain or ssm"
    )


def _clean_token(value: str, where: str) -> Secret:
    token = value.strip()
    if not token:
        raise CredentialError(f"the Meta access token from {where} is empty", hint=STORE_AGAIN)
    if any(ch.isspace() for ch in token):
        raise CredentialError(
            f"the Meta access token from {where} contains whitespace",
            hint="a token has no spaces: the command text (two lines pasted at once) or two values were stored; "
            + STORE_AGAIN,
        )
    return Secret(token)


def _helper_hint(what: str, returncode: int, detail: str) -> str:
    """A fix for the usual failures of the ``security`` and ``aws`` helpers."""
    if what == "the macOS Keychain":
        if returncode == 44:  # errSecItemNotFound
            return "no such Keychain item: check [meta] keychain_service and keychain_account, or " + STORE_AGAIN
        return "check [meta] keychain_service and keychain_account, and that the Keychain is unlocked"
    if "ParameterNotFound" in detail:
        return "check [meta] ssm_parameter (the full name, with its leading /) and ssm_region"
    if "AccessDenied" in detail:
        return "the AWS identity needs ssm:GetParameter on the parameter and kms:Decrypt on its key"
    if "credentials" in detail.lower():
        return "the aws CLI found no AWS credentials: set AWS_PROFILE, or run on a host with an instance role"
    return "check [meta] ssm_parameter and ssm_region, and which AWS identity runs it: aws sts get-caller-identity"


def _run_secret_command(cmd: list[str], what: str, runner: Runner) -> str:
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        hint = (
            "install the AWS CLI v2, or set another [meta] token_source (env, file or keychain)"
            if cmd[0] == "aws"
            else f"{cmd[0]} comes with the system: check PATH, or set another [meta] token_source"
        )
        raise CredentialError(
            f"{cmd[0]!r} was not found; it is needed to read the token from {what}", hint=hint
        ) from None
    except subprocess.TimeoutExpired:
        raise CredentialError(
            f"reading the token from {what} timed out after 30 s",
            hint="a locked Keychain or a pending permission prompt can cause this",
        ) from None
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        detail = redact(lines[-1] if lines else "no error output")
        raise CredentialError(
            f"could not read the token from {what} (exit {result.returncode}): {detail}",
            hint=_helper_hint(what, result.returncode, detail),
        )
    return result.stdout or ""


def load_meta_token(
    settings: MetaSettings, env: Mapping[str, str] | None = None, runner: Runner = subprocess.run
) -> Secret:
    env = os.environ if env is None else env
    source = settings.token_source
    if source is None:
        if env.get(settings.token_env):
            source = "env"
        elif settings.token_file or env.get(META_FILE_ENV):
            source = "file"
        else:
            raise CredentialError(
                f"no Meta access token: {settings.token_env} is not set and no token source is configured",
                hint=no_meta_token_hint(settings.token_env),
            )

    if source == "env":
        value = env.get(settings.token_env)
        if not value:
            raise CredentialError(
                f"{settings.token_env} is not set ([meta] token_source = env)",
                hint=no_meta_token_hint(settings.token_env),
            )
        return _clean_token(value, settings.token_env)

    if source == "file":
        name = settings.token_file or env.get(META_FILE_ENV)
        if not name:
            raise CredentialError(
                "[meta] token_source = file needs [meta] token_file or META_ACCESS_TOKEN_FILE",
                hint="add token_file = ~/.config/adops-guard/meta-token under [meta] (docs/setup.md, Meta step 6)",
            )
        path = Path(name).expanduser()
        if not path.is_file():
            raise CredentialError(
                f"token file not found: {path}",
                hint="check [meta] token_file (or $META_ACCESS_TOKEN_FILE), or create the file: docs/setup.md, "
                "Meta step 6",
            )
        check_private_file(path)
        return _clean_token(path.read_text(encoding="utf-8"), str(path))

    if source == "keychain":
        if sys.platform != "darwin":
            raise CredentialError(
                "token_source = keychain works only on macOS",
                hint="set [meta] token_source to file, env or ssm on this system (docs/setup.md, Meta step 6)",
            )
        if not settings.keychain_service:
            raise CredentialError(
                "token_source = keychain needs [meta] keychain_service",
                hint="add keychain_service = adops-guard-meta under [meta]: the -s name the token was stored with",
            )
        cmd = ["security", "find-generic-password", "-s", settings.keychain_service]
        if settings.keychain_account:
            cmd += ["-a", settings.keychain_account]
        cmd.append("-w")
        return _clean_token(_run_secret_command(cmd, "the macOS Keychain", runner), "the macOS Keychain")

    if source == "ssm":
        if not settings.ssm_parameter:
            raise CredentialError(
                "token_source = ssm needs [meta] ssm_parameter",
                hint="add ssm_parameter = /adops-guard/meta-token under [meta] (the full name, with its leading /)",
            )
        cmd = [
            "aws", "ssm", "get-parameter", "--name", settings.ssm_parameter, "--with-decryption",
            "--query", "Parameter.Value", "--output", "text",
        ]  # fmt: skip
        if settings.ssm_region:
            cmd += ["--region", settings.ssm_region]
        return _clean_token(_run_secret_command(cmd, "AWS SSM", runner), "AWS SSM")

    raise CredentialError(f"unknown token source {source!r}", hint="use env, file, keychain or ssm")

"""Configuration from an INI file (stdlib ``configparser``). Never secrets.

Lookup order: ``--config PATH``, then ``$ADOPS_GUARD_CONFIG``, then
``./adops-guard.ini``. No file is fine: every setting has a default or a
command-line flag. Unknown keys are errors (a typo in a ceiling must not pass
silently), and keys that look like secrets are refused outright.

A config file that other users can change, or that belongs to another user,
is refused. The keys that point the API clients at a local mock server
(``api_base_url``, ``oauth_token_url``, ``graph_base_url``) are refused unless
``ADOPS_GUARD_TEST_ENDPOINTS=1`` is set, so a config file planted in a
directory you run the tool from cannot redirect your credentials.
"""

from __future__ import annotations

import configparser
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from adops_guard.errors import ConfigError
from adops_guard.money import parse_amount

CONFIG_ENV = "ADOPS_GUARD_CONFIG"
STATE_DIR_ENV = "ADOPS_GUARD_STATE_DIR"
TEST_ENDPOINTS_ENV = "ADOPS_GUARD_TEST_ENDPOINTS"
DEFAULT_CONFIG_NAME = "adops-guard.ini"
DEFAULT_STATE_DIR = ".adops-guard"
DEFAULT_GOOGLE_API_VERSION = "v25"
DEFAULT_META_API_VERSION = "v26.0"

_ALLOWED: dict[str, frozenset[str]] = {
    "general": frozenset({"state_dir"}),
    "google": frozenset(
        {
            "customer_id",
            "login_customer_id",
            "credentials_file",
            "api_version",
            "max_daily_budget",
            "api_base_url",
            "oauth_token_url",
        }
    ),
    "meta": frozenset(
        {
            "ad_account_id",
            "api_version",
            "token_source",
            "token_env",
            "token_file",
            "keychain_service",
            "keychain_account",
            "ssm_parameter",
            "ssm_region",
            "usage_stop_percent",
            "max_daily_budget",
            "max_lifetime_budget",
            "max_spend_cap",
            "currency_offset",
            "graph_base_url",
        }
    ),
    "audit": frozenset(
        {
            "min_clicks",
            "min_impressions",
            "instream_ctr_ratio",
            "night_hours",
            "night_click_share",
            "night_ctr_ratio",
            "flagged_placement_share",
            "ctr_rise",
            "cpc_fall",
            "min_days",
            "age_undetermined_share",
            "extra_kids_keywords",
            "extra_drama_keywords",
        }
    ),
}
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "credential")
_NOT_SECRETS = frozenset({"token_source", "token_env", "token_file", "credentials_file", "oauth_token_url"})
# Endpoint overrides for the offline test suite. Credentials are sent to these URLs.
TEST_ONLY_KEYS = (("google", "api_base_url"), ("google", "oauth_token_url"), ("meta", "graph_base_url"))
TOKEN_SOURCES = ("env", "file", "keychain", "ssm")
DUPLICATE_HINT = (
    "each [section] and each key may appear only once: merge the two blocks, i.e. edit the lines that are already "
    "in the file instead of pasting a second [google] or [meta] block"
)


@dataclass(frozen=True)
class GoogleSettings:
    customer_id: str | None = None
    login_customer_id: str | None = None
    credentials_file: str | None = None
    api_version: str = DEFAULT_GOOGLE_API_VERSION
    max_daily_budget: Decimal | None = None
    api_base_url: str = "https://googleads.googleapis.com"
    oauth_token_url: str = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class MetaSettings:
    ad_account_id: str | None = None
    api_version: str = DEFAULT_META_API_VERSION
    token_source: str | None = None
    token_env: str = "META_ACCESS_TOKEN"
    token_file: str | None = None
    keychain_service: str | None = None
    keychain_account: str | None = None
    ssm_parameter: str | None = None
    ssm_region: str | None = None
    usage_stop_percent: int = 85
    max_daily_budget: Decimal | None = None
    max_lifetime_budget: Decimal | None = None
    max_spend_cap: Decimal | None = None
    currency_offset: int | None = None
    graph_base_url: str = "https://graph.facebook.com"


@dataclass(frozen=True)
class AuditSettings:
    min_clicks: int = 50
    min_impressions: int = 1000
    instream_ctr_ratio: float = 3.0
    night_hours: tuple[int, int] = (0, 6)
    night_click_share: float = 0.40
    night_ctr_ratio: float = 1.5
    flagged_placement_share: float = 0.25
    ctr_rise: float = 0.30
    cpc_fall: float = 0.20
    min_days: int = 6
    age_undetermined_share: float = 0.40
    extra_kids_keywords: tuple[str, ...] = ()
    extra_drama_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    google: GoogleSettings = field(default_factory=GoogleSettings)
    meta: MetaSettings = field(default_factory=MetaSettings)
    audit: AuditSettings = field(default_factory=AuditSettings)
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    source: Path | None = None
    discovered: bool = False  # True when the file was found in the working directory, not named explicitly
    test_endpoints: bool = False  # ADOPS_GUARD_TEST_ENDPOINTS=1: loopback endpoints are allowed

    def endpoint_overrides(self) -> list[tuple[str, str]]:
        """The endpoint settings that differ from the official API hosts (test setups only)."""
        pairs = (
            ("[google] api_base_url", self.google.api_base_url, GoogleSettings.api_base_url),
            ("[google] oauth_token_url", self.google.oauth_token_url, GoogleSettings.oauth_token_url),
            ("[meta] graph_base_url", self.meta.graph_base_url, MetaSettings.graph_base_url),
        )
        return [(name, value) for name, value, default in pairs if value.rstrip("/") != default]


class _Reader:
    def __init__(self, parser: configparser.ConfigParser, source: Path | None) -> None:
        self.parser = parser
        self.where = str(source) if source else "config"

    def text(self, section: str, key: str) -> str | None:
        if not self.parser.has_option(section, key):
            return None
        value = self.parser.get(section, key).strip()
        return value or None

    def decimal(self, section: str, key: str) -> Decimal | None:
        value = self.text(section, key)
        return parse_amount(value, f"[{section}] {key}") if value is not None else None

    def integer(self, section: str, key: str, default: int, low: int, high: int) -> int:
        value = self.text(section, key)
        if value is None:
            return default
        if not re.fullmatch(r"\d+", value) or not low <= int(value) <= high:
            raise ConfigError(f"[{section}] {key} must be a whole number from {low} to {high} (in {self.where})")
        return int(value)

    def ratio(self, section: str, key: str, default: float, high: float = 1000.0) -> float:
        value = self.text(section, key)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            number = -1.0
        if not 0 < number <= high:
            raise ConfigError(f"[{section}] {key} must be a positive number (in {self.where})")
        return number

    def words(self, section: str, key: str) -> tuple[str, ...]:
        value = self.text(section, key)
        if not value:
            return ()
        return tuple(word.strip() for word in value.split(",") if word.strip())


def _check_keys(parser: configparser.ConfigParser, where: str) -> None:
    for section in parser.sections():
        if section not in _ALLOWED:
            raise ConfigError(f"unknown section [{section}] in {where}", hint="sections: " + ", ".join(_ALLOWED))
        for key in parser.options(section):
            lowered = key.lower()
            if lowered not in _NOT_SECRETS and any(word in lowered for word in _SECRET_HINTS):
                raise ConfigError(
                    f"[{section}] {key} looks like a secret; secrets never go in the config file ({where})",
                    hint="use environment variables, a chmod 600 file, the macOS Keychain or AWS SSM (see README)",
                )
            if lowered not in _ALLOWED[section]:
                raise ConfigError(
                    f"unknown key [{section}] {key} in {where}",
                    hint="known keys: " + ", ".join(sorted(_ALLOWED[section])),
                )


def _night_hours(value: str | None) -> tuple[int, int]:
    if value is None:
        return (0, 6)
    match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", value)
    if not match:
        raise ConfigError("[audit] night_hours must look like 0-6 or 22-5")
    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise ConfigError("[audit] night_hours must be two different hours between 0 and 24")
    return (start, end)


def check_config_file(path: Path) -> None:
    """Refuse a config file that other users could change, or that belongs to someone else.

    The config decides ceilings, the ad account and where tokens come from, so it
    must be as trustworthy as your own shell profile. Files owned by root are
    accepted (only root can change them).
    """
    if os.name == "nt":  # POSIX permission bits do not describe Windows ACLs
        return
    info = path.stat()
    if info.st_mode & 0o022:
        raise ConfigError(
            f"config file {path} can be changed by other users (mode {oct(info.st_mode & 0o777)})",
            hint=f"run: chmod go-w {path}",
        )
    if hasattr(os, "getuid") and info.st_uid not in (os.getuid(), 0):
        raise ConfigError(f"config file {path} belongs to another user", hint="use a config file that you own")


def load_settings(path: str | None = None, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Settings:
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    chosen = path or env.get(CONFIG_ENV)
    source: Path | None = None
    discovered = False
    if chosen:
        source = Path(chosen).expanduser()
        if not source.is_file():
            raise ConfigError(
                f"config file not found: {source}",
                hint="check the path, or create the file from examples/adops-guard.example.ini (docs/setup.md, step 0)",
            )
    elif (cwd / DEFAULT_CONFIG_NAME).is_file():
        source = cwd / DEFAULT_CONFIG_NAME
        discovered = True
    test_endpoints = env.get(TEST_ENDPOINTS_ENV) == "1"

    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    if source is not None:
        check_config_file(source)
        try:
            parser.read_string(source.read_text(encoding="utf-8"), source=str(source))
        except (configparser.DuplicateSectionError, configparser.DuplicateOptionError) as exc:
            raise ConfigError(f"cannot read {source}: {exc}", hint=DUPLICATE_HINT) from None
        except (configparser.Error, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"cannot read {source}: {exc}",
                hint="fix the line it names: the file is INI, [section] headers and key = value lines "
                "(examples/adops-guard.example.ini shows every key)",
            ) from None
    _check_keys(parser, str(source) if source else "config")
    for section, key in TEST_ONLY_KEYS:
        if parser.has_option(section, key) and not test_endpoints:
            raise ConfigError(
                f"[{section}] {key} in {source} is only for the offline test suite: credentials would be sent there",
                hint=f"remove it; it is accepted only when {TEST_ENDPOINTS_ENV}=1 is set",
            )
    read = _Reader(parser, source)

    token_source = read.text("meta", "token_source")
    if token_source is not None and token_source not in TOKEN_SOURCES:
        raise ConfigError(
            f"[meta] token_source must be one of {', '.join(TOKEN_SOURCES)}",
            hint="pick the place the token is stored (docs/setup.md, Meta step 6)",
        )
    offset = read.text("meta", "currency_offset")
    if offset is not None and offset not in ("1", "100", "1000"):
        raise ConfigError(
            "[meta] currency_offset must be 1, 100 or 1000",
            hint="minor units per unit of the currency: 100 for most currencies, 1 for JPY (Meta's currency table)",
        )

    google = GoogleSettings(
        customer_id=read.text("google", "customer_id"),
        login_customer_id=read.text("google", "login_customer_id"),
        credentials_file=read.text("google", "credentials_file"),
        api_version=read.text("google", "api_version") or DEFAULT_GOOGLE_API_VERSION,
        max_daily_budget=read.decimal("google", "max_daily_budget"),
        api_base_url=read.text("google", "api_base_url") or GoogleSettings.api_base_url,
        oauth_token_url=read.text("google", "oauth_token_url") or GoogleSettings.oauth_token_url,
    )
    meta = MetaSettings(
        ad_account_id=read.text("meta", "ad_account_id"),
        api_version=read.text("meta", "api_version") or DEFAULT_META_API_VERSION,
        token_source=token_source,
        token_env=read.text("meta", "token_env") or MetaSettings.token_env,
        token_file=read.text("meta", "token_file"),
        keychain_service=read.text("meta", "keychain_service"),
        keychain_account=read.text("meta", "keychain_account"),
        ssm_parameter=read.text("meta", "ssm_parameter"),
        ssm_region=read.text("meta", "ssm_region"),
        usage_stop_percent=read.integer("meta", "usage_stop_percent", 85, 1, 100),
        max_daily_budget=read.decimal("meta", "max_daily_budget"),
        max_lifetime_budget=read.decimal("meta", "max_lifetime_budget"),
        max_spend_cap=read.decimal("meta", "max_spend_cap"),
        currency_offset=int(offset) if offset else None,
        graph_base_url=read.text("meta", "graph_base_url") or MetaSettings.graph_base_url,
    )
    defaults = AuditSettings()
    audit = AuditSettings(
        min_clicks=read.integer("audit", "min_clicks", defaults.min_clicks, 1, 10**9),
        min_impressions=read.integer("audit", "min_impressions", defaults.min_impressions, 1, 10**12),
        instream_ctr_ratio=read.ratio("audit", "instream_ctr_ratio", defaults.instream_ctr_ratio),
        night_hours=_night_hours(read.text("audit", "night_hours")),
        night_click_share=read.ratio("audit", "night_click_share", defaults.night_click_share, 1.0),
        night_ctr_ratio=read.ratio("audit", "night_ctr_ratio", defaults.night_ctr_ratio),
        flagged_placement_share=read.ratio("audit", "flagged_placement_share", defaults.flagged_placement_share, 1.0),
        ctr_rise=read.ratio("audit", "ctr_rise", defaults.ctr_rise),
        cpc_fall=read.ratio("audit", "cpc_fall", defaults.cpc_fall, 1.0),
        min_days=read.integer("audit", "min_days", defaults.min_days, 3, 366),
        age_undetermined_share=read.ratio("audit", "age_undetermined_share", defaults.age_undetermined_share, 1.0),
        extra_kids_keywords=read.words("audit", "extra_kids_keywords"),
        extra_drama_keywords=read.words("audit", "extra_drama_keywords"),
    )

    if env.get(STATE_DIR_ENV):
        state_dir = Path(env[STATE_DIR_ENV]).expanduser()
        if not state_dir.is_absolute():
            state_dir = cwd / state_dir
    else:
        configured = Path(read.text("general", "state_dir") or DEFAULT_STATE_DIR).expanduser()
        base = source.parent if source is not None else cwd
        state_dir = configured if configured.is_absolute() else base / configured
    return Settings(
        google=google,
        meta=meta,
        audit=audit,
        state_dir=state_dir,
        source=source,
        discovered=discovered,
        test_endpoints=test_endpoints,
    )

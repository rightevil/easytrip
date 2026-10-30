"""Backend-only configuration. Never serialize Settings to a browser or a report."""

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = ROOT_DIR / "data"
if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys._MEIPASS)
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EasyTrip"
EXPORT_DIR = ROOT_DIR / "reports" / "p0"
ALIASES = {
    "rail_mcp": "12306_MCP",
    "model_base_url": "AGNES_URL",
    "model_api_key": "AGNES_KEY",
    "model_id": "AGNES_MODEL",
}


def settings_overrides(path: Path):
    return normalize_settings(dotenv_values(path, interpolate=False, encoding="utf-8-sig"))


def normalize_settings(values):
    values = {key.upper(): value for key, value in values.items()}
    return {
        name: (values.get(name.upper(), values.get(ALIASES.get(name))) or "").strip()
        for name in Settings.__dataclass_fields__
        if name != "secrets" and (name.upper() in values or ALIASES.get(name) in values)
    }


@dataclass(repr=False)
class Settings:
    xhs_mcp: str = ""
    rail_mcp: str = ""
    amap_web_key: str = ""
    amap_js_key: str = ""
    amap_js_secure_key: str = ""
    amap_mcp_url: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_id: str = ""
    secrets: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def load(cls, overrides_path: Path | None = None, *, isolated=False):
        values = {} if isolated else {
            **dotenv_values(ROOT_DIR / ".env"),
            **dotenv_values(BACKEND_DIR / ".env"),
            **os.environ,
        }
        settings = cls(
            **{
                name: (values.get(name.upper()) or "").strip()
                for name in cls.__dataclass_fields__
                if name != "secrets"
            }
        )
        folded = {key.upper(): value for key, value in values.items()}
        for name, alias in ALIASES.items():
            if not getattr(settings, name):
                setattr(settings, name, (folded.get(alias) or "").strip())
        # Saved desktop choices, including explicit clears, override legacy aliases.
        path = overrides_path if overrides_path is not None else DATA_DIR / ".env"
        for name, value in settings_overrides(path).items():
            setattr(settings, name, value)
        settings.secrets.update(
            value
            for name, value in vars(settings).items()
            if isinstance(value, str) and value and name != "model_id"
        )
        return settings

    def redact(self, value):
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if re.fullmatch(
                        r"xsec_?token|cookies?|authorization|(?:x[-_]?)?api[-_]?key|"
                        r"(?:access|refresh|id)[-_]?token|password|passwd|client[-_]?secret|"
                        r"secret|secure|avatar|user_id|nickname",
                        key,
                        re.I,
                    )
                    else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"https?://[^\s\"<>]+", "[URL]", value)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", value)
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)


def direct_client(**kwargs):
    return httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(40, read=60),
        follow_redirects=False,
        **kwargs,
    )


def configure_transport_logging():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_easytrip_safe", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == "mcp" or record.name.startswith(
            ("mcp.", "httpx", "httpcore")
        ):
            # Provider logs may contain raw responses and credential URLs; usage stays in SQLite.
            error = record.exc_info[0].__name__ if record.exc_info else "TransportEvent"
            record.msg = f"{record.name} {record.levelname}: {error}"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    factory._easytrip_safe = True
    logging.setLogRecordFactory(factory)


def validate_url(url: str):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
        or parsed.port == 0
    ):
        raise ValueError("配置 URL 必须是无内嵌账号密码的 HTTP(S) 地址")


URL_FIELDS = {"xhs_mcp", "rail_mcp", "amap_mcp_url", "model_base_url"}


def validate_setting(name: str, value: str):
    if len(value) > 4096 or any(ord(c) < 32 or c == "\ufffd" for c in value):
        raise ValueError("配置包含无效字符")
    if name in URL_FIELDS and value.strip():
        validate_url(value.strip())


def configuration_checks(values):
    groups = {
        "xhs_mcp": ("xhs_mcp",),
        "model": ("model_base_url", "model_api_key", "model_id"),
        "amap_web": ("amap_web_key",),
        "amap_js": ("amap_js_key", "amap_js_secure_key"),
        "rail_mcp": ("rail_mcp",),
        "amap_mcp": ("amap_mcp_url",),
    }
    checks = {}
    for service, names in groups.items():
        missing = [name for name in names if not values[name].strip()]
        invalid = []
        for name in names:
            try:
                validate_setting(name, values[name])
            except ValueError:
                invalid.append(name)
        checks[service] = {
            "state": "invalid" if invalid else "unconfigured" if len(missing) == len(names)
            else "incomplete" if missing else "configured_not_probed",
            "missing": missing,
            "invalid": invalid,
        }
    return checks

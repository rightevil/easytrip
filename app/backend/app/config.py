"""Backend-only configuration. Never serialize Settings to a browser or a report."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

APP_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = APP_DIR.parent
DATA_DIR = APP_DIR / "data"
EXPORT_DIR = APP_DIR / "reports" / "p0"


@dataclass(repr=False)
class Settings:
    xhs_mcp: str = ""
    amap_web_key: str = ""
    amap_js_key: str = ""
    amap_js_secure_key: str = ""
    amap_mcp_url: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_id: str = ""
    secrets: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def load(cls):
        values = {
            **dotenv_values(ROOT_DIR / ".env"),
            **dotenv_values(APP_DIR / ".env"),
            **os.environ,
        }
        settings = cls(
            **{
                name: (values.get(name.upper()) or "").strip()
                for name in cls.__dataclass_fields__
                if name != "secrets"
            }
        )
        aliases = {
            "model_base_url": "AGNES_URL",
            "model_api_key": "AGNES_KEY",
            "model_id": "AGNES_MODEL",
        }
        folded = {key.upper(): value for key, value in values.items()}
        for name, alias in aliases.items():
            if not getattr(settings, name):
                setattr(settings, name, (folded.get(alias) or "").strip())
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
                    if isinstance(item, str)
                    and re.fullmatch(
                        r"xsec_?token|cookie|authorization|secret|secure|avatar|user_id|nickname",
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


def validate_url(url: str):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("配置 URL 必须是无内嵌账号密码的 HTTP(S) 地址")

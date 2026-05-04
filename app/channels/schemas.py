import ipaddress
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ChannelParseError(Exception):
    """Raised when a channel inbound payload cannot be normalized into ChannelMessage."""


def _ssrf_guard_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"url scheme must be http or https: {url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"url has no host: {url}")
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise ValueError(f"url host is localhost: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(f"url host is non-routable: {host}")
    return url


class ChannelMessage(BaseModel):
    """Normalized inbound from any messenger backend."""

    chat_id: int
    from_user_id: int | None = None
    from_username: str | None = None
    text: str | None = None
    photo_file_id: str | None = None
    urls: list[HttpUrl] = Field(default_factory=list)
    received_at: datetime

    model_config = {"strict": False}

    @field_validator("urls", mode="before")
    @classmethod
    def _ssrf_guard(cls, v: list[str] | None) -> list[str]:
        if not v:
            return []
        out: list[str] = []
        for u in v:
            s = str(u)
            try:
                _ssrf_guard_url(s)
            except ValueError:
                continue
            out.append(s)
        return out


class BotCard(BaseModel):
    image_url: HttpUrl
    caption: str
    button_text: str = "View"
    button_url: HttpUrl


class BotReply(BaseModel):
    text: str | None = None
    cards: list[BotCard] = Field(default_factory=list)
    closing_text: str | None = None

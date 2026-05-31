"""First-class proxy configuration for the browser MCP.

Accepts a handful of common spellings and normalizes them into a single
:class:`ProxyConfig`. The only proxy "modes" we support are the ones Chromium
itself supports:

* ``http`` / ``https`` upstream proxies, with optional username/password.
  Authentication is satisfied at runtime through the CDP ``Fetch.authRequired``
  flow (Chromium prompts with an HTTP 407 challenge, we answer it).
* ``socks5`` / ``socks4`` upstream proxies, **without** authentication.

Chromium's ``--proxy-server`` flag (how this MCP wires every proxy) cannot
authenticate SOCKS proxies. nodriver *can* via per-context
``browser.create_context(proxy_server="socks5://user:pass@host:port")``, but that
path is not wired into this launch flow yet, so a SOCKS spec carrying credentials
is rejected up front with an actionable message rather than failing at nav time.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# Maps the user-facing scheme spellings to the canonical Chromium scheme used
# in ``--proxy-server``.
_SCHEME_ALIASES: dict[str, str] = {
    "http": "http",
    "https": "https",
    "socks": "socks5",
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
}

_SOCKS_SCHEMES = {"socks4", "socks5"}


@dataclass(frozen=True)
class ProxyConfig:
    """Normalized upstream proxy description."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def has_auth(self) -> bool:
        return bool(self.username) or bool(self.password)

    @property
    def is_socks(self) -> bool:
        return self.scheme in _SOCKS_SCHEMES

    @property
    def server_url(self) -> str:
        """Value chrome expects after ``--proxy-server=`` (no credentials)."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def proxy_server_arg(self) -> str:
        return f"--proxy-server={self.server_url}"

    def redacted(self) -> str:
        """Safe-to-log/persist representation that never leaks the password."""
        if self.username:
            return f"{self.scheme}://{self.username}:***@{self.host}:{self.port}"
        return self.server_url

    def to_metadata(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "has_auth": self.has_auth,
            "redacted": self.redacted(),
        }


def _normalize_scheme(raw: str | None) -> str:
    scheme = (raw or "http").strip().lower()
    if scheme not in _SCHEME_ALIASES:
        raise ValueError(
            f"Unsupported proxy scheme '{raw}'. "
            f"Expected one of: {', '.join(sorted(_SCHEME_ALIASES))}."
        )
    return _SCHEME_ALIASES[scheme]


def _coerce_port(raw: object) -> int:
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Proxy port must be an integer, got {raw!r}.") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"Proxy port out of range (1-65535): {port}.")
    return port


def _build(
    *,
    scheme: str | None,
    host: str | None,
    port: object,
    username: str | None,
    password: str | None,
) -> ProxyConfig:
    normalized_scheme = _normalize_scheme(scheme)
    clean_host = (host or "").strip()
    if not clean_host:
        raise ValueError("Proxy host is required.")
    config = ProxyConfig(
        scheme=normalized_scheme,
        host=clean_host,
        port=_coerce_port(port),
        username=(username or None),
        password=(password or None),
    )
    if config.is_socks and config.has_auth:
        raise ValueError(
            "Authenticated SOCKS proxies are not wired into this launch flow yet "
            "(Chromium's --proxy-server cannot authenticate SOCKS; nodriver supports "
            "it via create_context). Use the provider's HTTP/HTTPS endpoint with the "
            "same credentials, or an IP-whitelisted SOCKS endpoint without a password."
        )
    return config


def _parse_url_form(spec: str) -> ProxyConfig:
    parsed = urlsplit(spec)
    return _build(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port if parsed.port is not None else "",
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def _parse_colon_form(spec: str) -> ProxyConfig:
    """Parse ``scheme:host:port[:user:pass]`` (and the no-scheme variant)."""
    parts = spec.split(":")
    scheme: str | None = None
    if parts and parts[0].strip().lower() in _SCHEME_ALIASES:
        scheme = parts.pop(0)
    if len(parts) < 2:
        raise ValueError(
            f"Could not parse proxy {spec!r}. "
            "Expected 'scheme:host:port' or 'scheme:host:port:user:pass'."
        )
    host = parts[0]
    port = parts[1]
    username: str | None = None
    password: str | None = None
    if len(parts) >= 3:
        username = parts[2] or None
    if len(parts) >= 4:
        # Re-join any trailing fragments so passwords containing ':' survive.
        password = ":".join(parts[3:]) or None
    return _build(
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def parse_proxy(spec: object) -> ProxyConfig | None:
    """Normalize a proxy spec into a :class:`ProxyConfig` (or ``None``).

    Accepts:

    * ``None`` / empty string -> ``None`` (no proxy).
    * an existing :class:`ProxyConfig` (returned unchanged).
    * a mapping with ``scheme``/``host``/``port``/``username``/``password``
      (or a single ``server`` URL string under the ``server`` key).
    * a URL string: ``http://user:pass@host:port``, ``socks5://host:port``.
    * a colon string: ``http:host:port:user:pass`` (the provider format).
    """
    if spec is None:
        return None
    if isinstance(spec, ProxyConfig):
        return spec
    if isinstance(spec, dict):
        server = spec.get("server")
        if server and not spec.get("host"):
            base = parse_proxy(str(server))
            if base is None:
                return None
            return _build(
                scheme=spec.get("scheme") or base.scheme,
                host=base.host,
                port=base.port,
                username=spec.get("username", base.username),
                password=spec.get("password", base.password),
            )
        return _build(
            scheme=spec.get("scheme"),
            host=spec.get("host"),
            port=spec.get("port", ""),
            username=spec.get("username"),
            password=spec.get("password"),
        )
    text = str(spec).strip()
    if not text:
        return None
    if "://" in text:
        return _parse_url_form(text)
    return _parse_colon_form(text)

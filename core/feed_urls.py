"""Build and redact RSSHub-relative feed request URLs."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_decoded_path(path: str) -> None:
    decoded = path
    for _ in range(10):
        if "\\" in decoded or _has_control(decoded):
            raise ValueError("路径不能包含反斜线或控制字符")
        if decoded.startswith("//"):
            raise ValueError("不允许 network-path 地址")
        if any(segment in {".", ".."} for segment in decoded.split("/")):
            raise ValueError("路径不能包含 . 或 .. 段")
        try:
            next_decoded = unquote(decoded, errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("路径百分号编码无效") from exc
        if next_decoded == decoded:
            return
        decoded = next_decoded
    raise ValueError("路径编码层数过多")


def _normal_host(host: str) -> tuple[str, bool]:
    host = host.rstrip(".")
    if not host or "%" in host:
        raise ValueError("主机名无效")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").lower(), False
        except UnicodeError as exc:
            raise ValueError("主机名无效") from exc
    return address.compressed.lower(), isinstance(address, ipaddress.IPv6Address)


def validate_default_url_prefix(value: str) -> str:
    """Validate and normalize the configured RSSHub prefix without resolving DNS.

    Actual request URLs still go through the normal public-address and redirect
    validation in the fetch path.
    """
    if not isinstance(value, str):
        raise ValueError("必须是文本")
    if not value:
        return ""
    if _has_control(value) or "\\" in value:
        raise ValueError("不能包含控制字符或反斜线")
    if "?" in value or "#" in value:
        raise ValueError("不能包含查询参数或片段")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise ValueError("URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("必须是完整 HTTP(S) URL")
    if username is not None or password is not None:
        raise ValueError("不能包含用户名或密码")
    if port is not None and port not in {80, 443}:
        raise ValueError("仅允许标准 HTTP(S) 端口 80 或 443")
    if parsed.netloc.endswith(":"):
        raise ValueError("端口无效")
    normalized_host, is_ipv6 = _normal_host(host)
    try:
        ipaddress.ip_address(normalized_host)
        is_ip_address = True
    except ValueError:
        is_ip_address = False
    if not is_ip_address and (
        len(normalized_host) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in normalized_host.split("."))
    ):
        raise ValueError("主机名无效")
    normalized_path = parsed.path.rstrip("/")
    _validate_decoded_path(normalized_path)
    netloc_host = f"[{normalized_host}]" if is_ipv6 else normalized_host
    netloc = netloc_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, normalized_path, "", ""))


def is_relative_feed_reference(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith("/")


def normalize_relative_feed_reference(ref: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise ValueError("请提供订阅地址")
    if _has_control(ref) or "\\" in ref:
        raise ValueError("相对订阅地址不能包含反斜线或控制字符")
    if not ref.startswith("/") or ref.startswith("//"):
        raise ValueError("相对订阅地址必须以单个 / 开头")
    try:
        parsed = urlsplit(ref)
    except ValueError as exc:
        raise ValueError("相对订阅地址格式无效") from exc
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("不允许 network-path 地址")
    _validate_decoded_path(parsed.path)
    decoded_query = unquote(parsed.query)
    if _has_control(decoded_query) or "\\" in decoded_query:
        raise ValueError("查询参数不能包含反斜线或控制字符")
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise ValueError("查询参数格式无效") from exc
    if any(name.lower() == "key" for name, _ in query_pairs):
        raise ValueError("请在 RSSHub 配置中填写 Access Key，不要把 key 放入订阅地址")
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _config_value(config, name: str, default: str = "") -> str:
    if isinstance(config, dict):
        value = config.get(name, default)
    else:
        value = getattr(config, name, default)
    if not isinstance(value, str):
        raise ValueError(f"配置项 {name} 必须是文本")
    return value


def _strip_configured_key(url: str, access_key: str) -> str:
    if not access_key:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    kept = []
    for part in parsed.query.split("&") if parsed.query else []:
        name, separator, value = part.partition("=")
        try:
            decoded_name = unquote(name.replace("+", " "), errors="strict")
            decoded_value = unquote(value.replace("+", " "), errors="strict") if separator else ""
        except UnicodeDecodeError:
            kept.append(part)
            continue
        if decoded_name.lower() == "key" and decoded_value == access_key:
            continue
        kept.append(part)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(kept), parsed.fragment))


def _origin(parsed) -> tuple[str, str, int | None]:
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        return scheme, host, None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port


@dataclass(frozen=True, slots=True)
class FeedRequest:
    request_url: str = field(repr=False)
    public_url: str
    fingerprint: str | None
    access_key: str = field(repr=False)
    scope_prefix: str | None

    def redact_url(self, url: str) -> str:
        """Remove the configured automatic key from a URL while preserving other parts."""
        return _strip_configured_key(url, self.access_key)

    def redirect_url(self, url: str) -> str:
        """Attach the configured key only to redirects within the RSSHub scope."""
        absolute = urljoin(self.public_url, url)
        if not self.scope_prefix or not self.access_key:
            return _strip_configured_key(absolute, self.access_key)
        try:
            parsed = urlsplit(absolute)
            scope = urlsplit(self.scope_prefix)
        except ValueError:
            return _strip_configured_key(absolute, self.access_key)
        if parsed.username is not None or parsed.password is not None:
            return _strip_configured_key(absolute, self.access_key)
        try:
            _validate_decoded_path(parsed.path)
        except ValueError:
            return _strip_configured_key(absolute, self.access_key)
        base_path = scope.path.rstrip("/")
        within_scope = parsed.path == base_path or not base_path or parsed.path.startswith(base_path + "/")
        if _origin(parsed) != _origin(scope) or not within_scope:
            return _strip_configured_key(absolute, self.access_key)
        # Replace a reflected key so exactly one current configured key is sent.
        pairs = [
            (name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True) if name.lower() != "key"
        ]
        pairs.append(("key", self.access_key))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), ""))


def build_feed_request(reference: str, config) -> FeedRequest:
    """Build a request from the current config, adding RSSHub auth only to routes."""
    if not isinstance(reference, str) or not reference:
        raise ValueError("请提供订阅地址")
    if not is_relative_feed_reference(reference):
        try:
            parsed = urlsplit(reference)
        except ValueError as exc:
            raise ValueError("订阅地址格式无效") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("订阅地址必须是 HTTP(S) URL 或以 / 开头的相对地址")
        return FeedRequest(reference, reference, None, "", None)

    route = normalize_relative_feed_reference(reference)
    prefix = validate_default_url_prefix(_config_value(config, "default_url_prefix"))
    access_key = _config_value(config, "rsshub_access_key")
    if not prefix:
        raise ValueError("请先配置默认订阅地址前缀")
    prefix_parts = urlsplit(prefix)
    route_parts = urlsplit(route)
    base = prefix.rstrip("/")
    public_url = base + route_parts.path
    if route_parts.query:
        public_url += "?" + route_parts.query
    request_url = public_url
    if access_key:
        request_url += ("&" if route_parts.query else "?") + urlencode({"key": access_key})
    scope_prefix = urlunsplit((prefix_parts.scheme, prefix_parts.netloc, prefix_parts.path, "", ""))
    fingerprint = hashlib.sha256(request_url.encode("utf-8")).hexdigest()
    return FeedRequest(request_url, public_url, fingerprint, access_key, scope_prefix)

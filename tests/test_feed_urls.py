import hashlib
from urllib.parse import parse_qsl, urlsplit

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.feed_urls import (
    build_feed_request,
    is_relative_feed_reference,
    normalize_relative_feed_reference,
)


def _config(prefix="https://rsshub.example/base///", key="key &/?雪"):
    return PluginConfig.from_mapping({"rsshub": {"default_url_prefix": prefix, "rsshub_access_key": key}})


def test_relative_feed_request_preserves_query_encodes_key_and_fingerprints_effective_url():
    config = _config()
    request = build_feed_request("/popular/week?lang=zh%20cn&limit=5#discarded", config)

    assert request.public_url == "https://rsshub.example/base/popular/week?lang=zh%20cn&limit=5"
    assert request.request_url.startswith(request.public_url + "&key=")
    assert ("key", config.rsshub_access_key) in parse_qsl(urlsplit(request.request_url).query)
    assert request.fingerprint == hashlib.sha256(request.request_url.encode()).hexdigest()
    assert request.scope_prefix == "https://rsshub.example/base"
    assert config.rsshub_access_key not in repr(request)


def test_absolute_url_is_unchanged_and_does_not_receive_rsshub_key():
    absolute = "https://feeds.example/rss?key=user-key#fragment"
    request = build_feed_request(absolute, _config())

    assert request.request_url == absolute
    assert request.public_url == absolute
    assert request.fingerprint is None
    assert request.scope_prefix is None
    assert request.access_key == ""


def test_each_build_uses_current_prefix_and_key_values():
    config = _config()
    first = build_feed_request("/route", config)
    config.default_url_prefix = "https://other.example/new/path////"
    config.rsshub_access_key = "changed"
    second = build_feed_request("/route", config)

    assert first.public_url == "https://rsshub.example/base/route"
    assert second.public_url == "https://other.example/new/path/route"
    assert second.request_url == "https://other.example/new/path/route?key=changed"
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("https://RSSHUB.example/base////", "https://rsshub.example/base"),
        ("http://RSSHUB.example////", "http://rsshub.example"),
        ("https://bücher.example/rsshub/", "https://xn--bcher-kva.example/rsshub"),
    ],
)
def test_default_prefix_normalizes_host_path_and_trailing_slashes(prefix, expected):
    config = _config(prefix=prefix, key="")
    assert config.default_url_prefix == expected


def test_relative_reference_normalization_drops_fragment_and_preserves_query():
    assert is_relative_feed_reference("/route?q=one#fragment")
    assert not is_relative_feed_reference("https://rsshub.example/route")
    assert normalize_relative_feed_reference("/route?q=one%20two&n=1#fragment") == "/route?q=one%20two&n=1"


@pytest.mark.parametrize(
    "reference",
    [
        "//attacker.example/route",
        "/../route",
        "/%2e%2e/route",
        "/%2e%2e%2froute",
        "/%252e%252e/route",
        "/route\\tail",
        "/route%5Ctail",
        "/route\nnext",
        "/route%00next",
        "/route?key=manual",
        "/route?%6bey=manual",
    ],
)
def test_relative_reference_rejects_network_path_traversal_controls_and_explicit_key(reference):
    with pytest.raises(ValueError):
        normalize_relative_feed_reference(reference)


@pytest.mark.parametrize(
    "prefix",
    [
        "ftp://rsshub.example",
        "https://user:password@rsshub.example",
        "https://rsshub.example/path?query=1",
        "https://rsshub.example/path#fragment",
        "https://rsshub.example:8443",
        "https://rsshub.example\\path",
        "https://rsshub.example/../path",
    ],
)
def test_default_prefix_rejects_unsafe_syntax(prefix):
    with pytest.raises(ValueError):
        PluginConfig.from_mapping({"rsshub": {"default_url_prefix": prefix}})


def test_redaction_and_redirect_key_scoping():
    request = build_feed_request("/feed?x=1", _config())
    assert request.redact_url(request.request_url) == request.public_url

    scoped = request.redirect_url("https://rsshub.example/base/next?x=2&key=reflected")
    assert parse_qsl(urlsplit(scoped).query).count(("key", "key &/?雪")) == 1
    assert "key=" not in request.redact_url(scoped)

    cross_origin = request.redirect_url("https://other.example/path?key=key%20%26%2F%3F%E9%9B%AA")
    assert cross_origin == "https://other.example/path"
    outside_prefix = request.redirect_url("https://rsshub.example/baseball/path?key=key+%26%2F%3F%E9%9B%AA")
    assert outside_prefix == "https://rsshub.example/baseball/path"


def test_relative_reference_requires_a_configured_prefix():
    config = PluginConfig.from_mapping({})
    with pytest.raises(ValueError, match="配置默认订阅地址前缀"):
        build_feed_request("/route", config)

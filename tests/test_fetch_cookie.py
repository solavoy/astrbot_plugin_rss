import time

from astrbot_plugin_rss.fetchers.cookies import CookieManager


def test_cookie_domain_path_expiry_and_client_binding(tmp_path):
    manager = CookieManager(tmp_path)
    manager.save(
        "https://example.com",
        [
            {"name": "root", "value": "a", "domain": ".example.com", "path": "/", "expires": -1, "secure": True},
            {"name": "path", "value": "b", "domain": "example.com", "path": "/foo", "expires": -1},
            {"name": "bad", "value": "c", "domain": "badexample.com", "path": "/"},
        ],
        "UA",
        "proxy-a",
        "146.0",
        "chrome146",
    )
    identity = ("UA", "proxy-a", "146.0", "chrome146")
    assert [c["name"] for c in manager.get_for_url("https://example.com/foo/x", *identity)] == ["root", "path"]
    assert [c["name"] for c in manager.get_for_url("https://sub.example.com/", *identity)] == ["root"]
    assert [c["name"] for c in manager.get_for_url("https://example.com/foobar", *identity)] == ["root"]
    assert manager.get_for_url("https://example.com/", "different-UA", "proxy-a", "146.0", "chrome146") == []
    assert manager.status("https://example.com")["validation_status"] == "browser_obtained"
    session_id = manager.status("https://example.com")["session_id"]
    assert session_id and session_id == manager.list_statuses()[0]["session_id"]
    manager.mark_invalid("https://example.com")
    assert manager.get_for_url("https://example.com/", *identity) == []
    profile = manager.root.parent / "example.com"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "profile-marker").write_text("test", encoding="utf-8")
    manager.clear("https://example.com")
    assert manager.status("https://example.com")["validation_status"] == "missing"
    assert not profile.exists()


def test_host_only_cookie_does_not_extend_to_subdomains(tmp_path):
    manager = CookieManager(tmp_path)
    manager.save(
        "https://example.com",
        [{"name": "host", "value": "secret", "domain": "example.com", "path": "/", "expires": -1}],
        "UA",
        "",
        "146.0",
        "chrome146",
    )
    identity = ("UA", "", "146.0", "chrome146")
    assert [cookie["name"] for cookie in manager.get_for_url("https://example.com/", *identity)] == ["host"]
    assert manager.get_for_url("https://sub.example.com/", *identity) == []
    assert manager.origin_for_session(manager.list_statuses()[0]["session_id"]) == "https://example.com"


def test_expired_cookie_cleanup_removes_browser_profile(tmp_path):
    manager = CookieManager(tmp_path)
    origin = "https://expired.example"
    manager.save(
        origin,
        [{"name": "session", "value": "secret", "domain": "expired.example", "path": "/", "expires": time.time() + 60}],
        "UA",
        "",
        "146.0.1",
        "chrome146",
    )
    data = manager._read_json(manager._path(origin))
    data["cookies"][0]["expires"] = time.time() - 1
    manager._write_json(manager._path(origin), data)
    profile = manager.root.parent / "expired.example"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "profile-marker").write_text("test", encoding="utf-8")
    assert manager.clear_expired() == 1
    assert manager.status(origin)["validation_status"] == "missing"
    assert not profile.exists()

import json
import math
from pathlib import Path

import pytest
from astrbot_plugin_rss.core.config import (
    DEFAULT_TRANSLATION_PROMPT,
    PluginConfig,
    migrate_legacy_config,
)
from astrbot_plugin_rss.core.errors import ConfigurationError


def test_task_config_names_and_legacy_aliases():
    config = PluginConfig.from_mapping(
        {
            "request_timeout": 12,
            "max_fetch_concurrency": 7,
            "enable_etag_cache": False,
            "browser_allowed_domains": ["example.org"],
            "max_concurrent_fetches": 4,
        }
    )
    assert config.request_timeout == 12
    assert config.max_fetch_concurrency == 7
    assert config.enable_etag_cache is False
    assert config.browser_allowed_domains == ("example.org",)


@pytest.mark.parametrize(
    "mapping",
    [
        {"request_timeout": math.inf},
        {"global_poll_interval": 0},
        {"global_poll_interval": 1.5},
        {"global_poll_interval": True},
        {"request_retry_limit": True},
        {"request_retry_limit": -1},
        {"enable_cloakbrowser": 1},
        {"browser_allowed_domains": "example.org"},
        {"fetch_user_agent": "bad\nheader"},
        {"feishu_max_card_bytes": 30721},
        {"max_delivery_retries": -1},
        {"rsshub": {"default_url_prefix": "https://user:pass@rsshub.example"}},
        {"rsshub": {"default_url_prefix": "https://rsshub.example/path?x=1"}},
        {"rsshub": {"default_url_prefix": "https://rsshub.example/path#fragment"}},
        {"rsshub": {"default_url_prefix": "https://rsshub.example:8443"}},
        {"rsshub": {"default_url_prefix": "https://rsshub.example\\route"}},
        {"rsshub": {"default_url_prefix": "https://rsshub.example/../route"}},
        {"rsshub": {"rsshub_access_key": 123}},
        {"general": {"timezone": ""}},
        {"general": {"timezone": "Not/A_Real_Zone"}},
        {"general": {"timezone": 123}},
        {"general": {"group_admin_only": "false"}},
    ],
)
def test_unsafe_or_invalid_config_is_rejected(mapping):
    with pytest.raises(ConfigurationError):
        PluginConfig.from_mapping(mapping)


def test_retry_zero_is_valid_and_secrets_are_redacted():
    config = PluginConfig.from_mapping(
        {"request_retry_limit": 0, "max_delivery_retries": 0, "telegraph_access_token": "secret"}
    )
    assert config.request_retry_limit == 0
    assert config.telegraph_access_token == "secret"
    public = config.public_dict()
    assert public["telegram"]["telegraph_access_token"] == "已配置"
    assert "secret" not in repr(public)
    assert set(public) == {"fetch", "network", "rsshub", "telegram", "media", "translation", "general"}
    assert public["rsshub"]["rsshub_access_key"] == ""
    assert public["media"]["send_as_media"] is True
    assert public["translation"] == {
        "provider_id": "",
        "include_original": True,
        "prompt": DEFAULT_TRANSLATION_PROMPT,
    }
    assert public["general"] == {"timezone": "Asia/Shanghai", "group_admin_only": False}


def test_rsshub_config_prefix_is_normalized_and_access_key_is_masked():
    config = PluginConfig.from_mapping(
        {
            "rsshub": {
                "default_url_prefix": "HTTPS://RSSHUB.Example./base/path///",
                "rsshub_access_key": "private-access-key",
            }
        }
    )
    assert config.default_url_prefix == "https://rsshub.example/base/path"
    assert config.rsshub_access_key == "private-access-key"
    public = config.public_dict()
    assert public["rsshub"] == {
        "default_url_prefix": "https://rsshub.example/base/path",
        "rsshub_access_key": "已配置",
    }
    assert "private-access-key" not in repr(config)
    assert "private-access-key" not in repr(public)


def test_existing_config_without_rsshub_fields_loads_with_empty_defaults():
    # Configurations saved before the RSSHub group was added have no such keys.
    config = PluginConfig.from_mapping(
        {
            "fetch": {"global_poll_interval": 300, "request_timeout": 15},
            "network": {"proxy": ""},
            "telegram": {"telegraph_access_token": ""},
        }
    )
    assert config.default_url_prefix == ""
    assert config.rsshub_access_key == ""
    assert "rsshub_access_key" not in repr(config)
    assert config.public_dict()["rsshub"] == {"default_url_prefix": "", "rsshub_access_key": ""}


def test_plugin_config_schema_has_groups_and_legacy_keys():
    path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    groups = {key: value for key, value in schema.items() if value.get("type") == "object"}
    assert set(groups) == {"general", "fetch", "network", "rsshub", "telegram", "media", "translation"}
    visible = {group: set(value["items"]) for group, value in groups.items()}
    assert visible == {
        "general": {"timezone", "group_admin_only"},
        "fetch": {"global_poll_interval", "request_timeout"},
        "network": {"proxy"},
        "rsshub": {"default_url_prefix", "rsshub_access_key"},
        "telegram": {"telegraph_access_token"},
        "media": {"send_as_media"},
        "translation": {"provider_id", "include_original", "prompt"},
    }
    defaults = {
        group: {key: item["default"] for key, item in definition["items"].items()}
        for group, definition in groups.items()
    }
    for key in ("global_poll_interval", "request_timeout", "proxy", "telegraph_access_token"):
        assert schema[key]["invisible"] is True
    assert schema["global_poll_interval"]["default"] == 0
    assert schema["request_timeout"]["default"] == 0.0
    assert schema["proxy"]["default"] == ""
    assert schema["telegraph_access_token"]["default"] == ""
    assert schema["rsshub"]["items"]["default_url_prefix"]["default"] == ""
    assert schema["rsshub"]["items"]["rsshub_access_key"]["default"] == ""
    assert schema["rsshub"]["items"]["rsshub_access_key"]["secret"] is True
    assert schema["general"]["items"]["timezone"]["default"] == "Asia/Shanghai"
    assert schema["general"]["items"]["group_admin_only"]["default"] is False
    assert schema["general"]["items"]["timezone"]["description"] == "文章时区"
    assert schema["general"]["items"]["group_admin_only"]["description"] == "群订阅权限"
    assert schema["translation"]["items"]["provider_id"]["default"] == ""
    assert schema["translation"]["items"]["provider_id"]["_special"] == "select_provider"
    assert schema["translation"]["items"]["include_original"]["default"] is True
    assert schema["translation"]["items"]["prompt"]["default"] == DEFAULT_TRANSLATION_PROMPT
    assert schema["translation"]["items"]["prompt"]["type"] == "text"
    config = PluginConfig.from_mapping(defaults)
    assert config == PluginConfig()


def test_translation_config_is_grouped_strict_and_defaults_to_disabled():
    config = PluginConfig.from_mapping({})
    assert config.translation_provider_id == ""
    assert config.translation_include_original is True
    assert config.translation_prompt == DEFAULT_TRANSLATION_PROMPT
    selected = PluginConfig.from_mapping(
        {
            "translation": {
                "provider_id": "  provider-one  ",
                "include_original": False,
                "prompt": "  Translate to French.  ",
            }
        }
    )
    assert selected.translation_provider_id == "provider-one"
    assert selected.translation_include_original is False
    assert selected.translation_prompt == "Translate to French."
    blank = PluginConfig.from_mapping({"translation": {"prompt": "  \n  "}})
    assert blank.translation_prompt == DEFAULT_TRANSLATION_PROMPT


@pytest.mark.parametrize(
    "mapping",
    [
        {"translation": []},
        {"translation": {"provider_id": 7}},
        {"translation": {"include_original": "false"}},
        {"translation": {"prompt": 7}},
        {"translation": {"prompt": None}},
        {"translation_provider_id": None},
        {"translation_include_original": 1},
    ],
)
def test_translation_config_rejects_invalid_values(mapping):
    with pytest.raises(ConfigurationError):
        PluginConfig.from_mapping(mapping)


def test_general_settings_validate_timezone_and_admin_policy():
    config = PluginConfig.from_mapping({"general": {"timezone": "  America/New_York  ", "group_admin_only": True}})
    assert config.timezone == "America/New_York"
    assert config.group_admin_only is True
    assert config.public_dict()["general"] == {"timezone": "America/New_York", "group_admin_only": True}


def test_missing_timezone_database_has_installation_guidance(monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError

    import astrbot_plugin_rss.core.config as config_module

    def missing_zone_data(_name):
        raise ZoneInfoNotFoundError("test")

    monkeypatch.setattr(config_module, "ZoneInfo", missing_zone_data)
    monkeypatch.setattr(config_module, "_timezone_database_available", lambda: False)
    with pytest.raises(ConfigurationError, match="requirements.txt"):
        PluginConfig.from_mapping({"general": {"timezone": "Asia/Shanghai"}})


def test_legacy_config_without_general_settings_keeps_defaults_and_migrates():
    legacy = {"global_poll_interval": 120, "request_timeout": 10}
    migrate_legacy_config(legacy)
    config = PluginConfig.from_mapping(legacy)
    assert config.timezone == "Asia/Shanghai"
    assert config.group_admin_only is False
    assert legacy["fetch"] == {"global_poll_interval": 120, "request_timeout": 10.0}
    assert "general" not in legacy


def test_grouped_settings_take_priority_and_ignore_legacy_minimum_interval():
    config = PluginConfig.from_mapping(
        {
            "fetch": {"global_poll_interval": 120, "request_timeout": 9},
            "network": {"proxy": "http://new-proxy.example:8080"},
            "telegram": {"telegraph_access_token": "new-token"},
            "global_poll_interval": 600,
            "request_timeout": 30,
            "proxy": "http://old-proxy.example:8080",
            "telegraph_access_token": "old-token",
            "min_poll_interval": 600,
        }
    )
    assert config.global_poll_interval == 120
    assert config.min_poll_interval == 1
    assert config.request_timeout == 9
    assert config.proxy == "http://new-proxy.example:8080"
    assert config.telegraph_access_token == "new-token"


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ({"global_poll_interval": 1, "min_poll_interval": 60}, 1),
        ({"global_poll_interval": 300, "min_poll_interval": 600}, 300),
    ],
)
def test_legacy_minimum_is_ignored_and_global_interval_is_at_least_one(mapping, expected):
    config = PluginConfig.from_mapping(mapping)
    assert config.global_poll_interval == expected
    assert config.min_poll_interval == 1
    assert config.scheduler_tick_seconds == 1


def test_legacy_values_migrate_once_and_group_nondefaults_win():
    class Config(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved = 0

        def save_config(self):
            self.saved += 1

    legacy = Config(
        fetch={"global_poll_interval": 300, "request_timeout": 15},
        network={"proxy": ""},
        telegram={"telegraph_access_token": ""},
        global_poll_interval=90,
        request_timeout=20,
        proxy="http://user:pass@proxy.example:8080",
        telegraph_access_token="legacy-token",
    )
    migrate_legacy_config(legacy)
    assert legacy["fetch"] == {"global_poll_interval": 90, "request_timeout": 20}
    assert legacy["network"]["proxy"] == "http://user:pass@proxy.example:8080"
    assert legacy["telegram"]["telegraph_access_token"] == "legacy-token"
    assert legacy["global_poll_interval"] == 0
    assert legacy["request_timeout"] == 0.0
    assert legacy["proxy"] == ""
    assert legacy["telegraph_access_token"] == ""
    assert legacy.saved == 1
    migrate_legacy_config(legacy)
    assert legacy.saved == 1

    grouped = Config(
        fetch={"global_poll_interval": 180, "request_timeout": 12},
        network={"proxy": "http://new.example:8080"},
        telegram={"telegraph_access_token": "new-token"},
        global_poll_interval=90,
        request_timeout=20,
        proxy="http://old.example:8080",
        telegraph_access_token="old-token",
    )
    migrate_legacy_config(grouped)
    assert grouped["fetch"] == {"global_poll_interval": 180, "request_timeout": 12}
    assert grouped["network"]["proxy"] == "http://new.example:8080"
    assert grouped["telegram"]["telegraph_access_token"] == "new-token"
    assert grouped.saved == 1


def test_host_null_defaults_cannot_overwrite_grouped_settings():
    # AstrBot 4.29.1 config-service converts None to the schema type's zero value.
    config = {
        "fetch": {"global_poll_interval": 300, "request_timeout": 15.0},
        "network": {"proxy": ""},
        "telegram": {"telegraph_access_token": ""},
        "global_poll_interval": None,
        "request_timeout": None,
        "proxy": None,
        "telegraph_access_token": None,
    }
    host_default = {"global_poll_interval": 0, "request_timeout": 0.0, "proxy": "", "telegraph_access_token": ""}
    for key, default in host_default.items():
        if config[key] is None:
            config[key] = default
    migrate_legacy_config(config)
    assert config["fetch"] == {"global_poll_interval": 300, "request_timeout": 15.0}
    assert PluginConfig.from_mapping(config).request_timeout == 15.0


def test_startup_repairs_legacy_zero_timeout_but_direct_mapping_stays_strict(caplog):
    config = {"fetch": {"global_poll_interval": 0, "request_timeout": 0.0}}
    migrate_legacy_config(config)
    assert config["fetch"] == {"global_poll_interval": 300, "request_timeout": 15.0}
    assert "fetch.request_timeout" in caplog.text
    with pytest.raises(ConfigurationError):
        PluginConfig.from_mapping({"fetch": {"request_timeout": 0}})
    with pytest.raises(ConfigurationError):
        PluginConfig.from_mapping({"fetch": {"global_poll_interval": 0}})


@pytest.mark.parametrize("value", [True, -1, 0, math.inf, math.nan])
def test_invalid_legacy_values_do_not_override_valid_group_defaults(value, caplog):
    config = {
        "fetch": {"global_poll_interval": 300, "request_timeout": 15.0},
        "global_poll_interval": value,
        "request_timeout": value,
    }
    migrate_legacy_config(config)
    assert config["fetch"] == {"global_poll_interval": 300, "request_timeout": 15.0}
    if value != 0:
        assert "global_poll_interval" in caplog.text
        assert "request_timeout" in caplog.text


def test_grouped_numeric_strings_are_normalized_and_invalid_values_rejected():
    config = PluginConfig.from_mapping({"fetch": {"global_poll_interval": "1", "request_timeout": "15.5"}})
    assert config.global_poll_interval == 1
    assert config.request_timeout == 15.5
    for value in (True, -1, 0, math.inf, math.nan):
        with pytest.raises(ConfigurationError):
            PluginConfig.from_mapping({"fetch": {"request_timeout": value}})
    for value in (True, -1, 0, "--1", "1.0", "１"):
        with pytest.raises(ConfigurationError):
            PluginConfig.from_mapping({"fetch": {"global_poll_interval": value}})


def test_send_as_media_is_strict_boolean_and_defaults_on():
    assert PluginConfig.from_mapping({}).send_as_media is True
    assert PluginConfig.from_mapping({"media": {"send_as_media": False}}).send_as_media is False
    for value in (0, 1, "false", None):
        with pytest.raises(ConfigurationError):
            PluginConfig.from_mapping({"media": {"send_as_media": value}})

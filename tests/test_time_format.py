from datetime import UTC, datetime

from astrbot_plugin_rss.renderers.time_format import format_published


def test_format_published_uses_shanghai_time_without_suffix_and_keeps_epoch_zero():
    timestamp = datetime(2026, 10, 20, 4, 34, tzinfo=UTC).timestamp()
    assert format_published(timestamp) == "2026-10-20 12:34"
    assert format_published(0) == "1970-01-01 08:00"


def test_format_published_omits_missing_timestamp():
    assert format_published(None) == ""


def test_format_published_uses_requested_iana_timezone_for_seasons_and_day_rollover():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).timestamp()
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).timestamp()
    shanghai_rollover = datetime(2026, 10, 20, 16, 34, tzinfo=UTC).timestamp()

    assert format_published(winter, "America/New_York") == "2026-01-15 07:00"
    assert format_published(summer, "America/New_York") == "2026-07-15 08:00"
    assert format_published(shanghai_rollover, "Asia/Shanghai") == "2026-10-21 00:34"
    assert format_published(0, "America/New_York") == "1969-12-31 19:00"

import math

import pytest
from astrbot_plugin_rss.core.intervals import parse_interval_seconds


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (0, 0),
        (300, 300),
        ("300", 300),
        ("1d", 86400),
        ("2h", 7200),
        ("30m", 1800),
        ("45s", 45),
        (" 2 H ", 7200),
        ("01M", 60),
        ("0d", 0),
        ("0", 0),
    ],
)
def test_intervals_convert_to_seconds(value, seconds):
    assert parse_interval_seconds(value) == seconds


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        -1,
        1.0,
        1.5,
        math.inf,
        math.nan,
        [],
        {},
        "",
        " ",
        "-1h",
        "+1",
        "1.5h",
        "1.0",
        "1h30m",
        "1 h extra",
        "1w",
        "h",
        "１h",
        "1e3",
        "1_000",
        "NaN",
        "Infinity",
    ],
)
def test_invalid_intervals_are_rejected(value):
    with pytest.raises(ValueError, match="d/h/m/s"):
        parse_interval_seconds(value)

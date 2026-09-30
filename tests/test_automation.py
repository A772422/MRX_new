import datetime as dt
import time

import pytest

from mrx.tools import execute
from mrx.tools.automation import next_run, parse_schedule
from mrx.tools.registry import ToolError

NOW = dt.datetime(2026, 9, 30, 10, 0).timestamp()


def test_parse_schedules():
    assert parse_schedule("in 15m", NOW)[0] == NOW + 900
    assert parse_schedule("every 2h", NOW) == (NOW + 7200, "every 2h")
    t, canon = parse_schedule("daily 08:30", NOW)
    assert canon == "daily 08:30" and dt.datetime.fromtimestamp(t) == dt.datetime(2026, 10, 1, 8, 30)   # already past today -> tomorrow
    assert dt.datetime.fromtimestamp(parse_schedule("daily 23:00", NOW)[0]) == dt.datetime(2026, 9, 30, 23, 0)
    assert parse_schedule("at 2026-10-05 09:15", NOW)[1] == "at 2026-10-05 09:15"


@pytest.mark.parametrize("bad", ["every 10s", "sometimes", "at 2020-01-01 10:00", "daily 25"])
def test_bad_schedules(bad):
    with pytest.raises(ToolError):
        parse_schedule(bad, NOW)


def test_next_run():
    assert next_run("at 2026-10-05 09:15", NOW) is None
    assert next_run("every 1h", NOW) == NOW + 3600


async def test_task_tools(ctx):
    r = await execute("task_schedule", {"name": "brief", "prompt": "Summarise the news", "schedule": "every 1h"}, ctx)
    assert not r.is_error
    assert "brief" in (await execute("task_list", {}, ctx)).content
    assert not (await execute("task_delete", {"id": 1}, ctx)).is_error

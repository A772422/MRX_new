"""Automation: schedule the agent to run prompts later or on a recurring basis, unattended."""
from __future__ import annotations

import datetime as dt
import re
import time

from .registry import Context, ToolError, i, s, tool


def parse_schedule(spec: str, now: float | None = None) -> tuple[float, str]:
    """Return (first run epoch, canonical schedule). Accepts:
    'every 30m' / 'every 2h' / 'every 1d', 'daily 08:30', 'at 2026-10-01 09:00', 'in 15m'."""
    now = now or time.time()
    spec = spec.strip().lower()
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if m := re.fullmatch(r"in\s+(\d+)\s*([smhd])", spec):
        return now + int(m[1]) * unit[m[2]], f"at {dt.datetime.fromtimestamp(now + int(m[1]) * unit[m[2]]):%Y-%m-%d %H:%M}"
    if m := re.fullmatch(r"every\s+(\d+)\s*([smhd])", spec):
        secs = int(m[1]) * unit[m[2]]
        if secs < 60:
            raise ToolError("Minimum interval is 1 minute.")
        return now + secs, f"every {m[1]}{m[2]}"
    if m := re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", spec):
        t = dt.datetime.fromtimestamp(now).replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        if t.timestamp() <= now:
            t += dt.timedelta(days=1)
        return t.timestamp(), f"daily {int(m[1]):02d}:{m[2]}"
    if m := re.fullmatch(r"at\s+(\d{4}-\d{2}-\d{2})[ t](\d{1,2}):(\d{2})", spec):
        t = dt.datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M")
        if t.timestamp() <= now:
            raise ToolError("That time is in the past.")
        return t.timestamp(), f"at {t:%Y-%m-%d %H:%M}"
    raise ToolError("Unrecognised schedule. Use: 'in 15m', 'every 2h', 'daily 08:30', or 'at 2026-10-01 09:00'.")


def next_run(schedule: str, after: float) -> float | None:
    """Next epoch after a run, or None for one-shot schedules."""
    if schedule.startswith("at "):
        return None
    return parse_schedule(schedule, after)[0]


@tool("task_schedule", "Schedule a prompt for M.R.X. to run by itself later or repeatedly (e.g. a morning news briefing, a network scan every hour). "
                       "Unattended runs cannot perform actions that need approval (deleting, sending, posting, shell) — they are denied. Schedule formats: 'in 15m', 'every 2h', 'daily 08:30', 'at 2026-10-01 09:00'.",
      {"name": s("Short name"), "prompt": s("What M.R.X. should do, written as a complete instruction"), "schedule": s("When/how often")},
      ["name", "prompt", "schedule"], risk="modify")
def task_schedule(ctx: Context, name: str, prompt: str, schedule: str) -> dict:
    first, canonical = parse_schedule(schedule)
    tid = ctx.memory.add_task(name, prompt, canonical, first)
    return {"task_id": tid, "schedule": canonical, "first_run": time.strftime("%Y-%m-%d %H:%M", time.localtime(first))}


@tool("task_list", "List scheduled tasks and their last results.", {})
def task_list(ctx: Context) -> list[dict]:
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None  # noqa: E731
    return [{"id": t["id"], "name": t["name"], "schedule": t["schedule"], "enabled": bool(t["enabled"]), "next_run": fmt(t["next_run"]),
             "last_run": fmt(t["last_run"]), "last_result": (t["last_result"] or "")[:300]} for t in ctx.memory.tasks()]


@tool("task_delete", "Delete a scheduled task.", {"id": i("Task id")}, ["id"], risk="modify")
def task_delete(ctx: Context, id: int) -> str:
    if not ctx.memory.delete_task(id):
        raise ToolError(f"No task #{id}")
    return "Task deleted"

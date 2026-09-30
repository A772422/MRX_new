"""Radar tools: find and identify every device on the local network."""
from __future__ import annotations

import json
import time
from typing import Any

from .. import radar
from ..memory import Memory
from .registry import Context, ToolError, b, s, tool


async def run_scan(memory: Memory, emit, cidr: str | None = None, deep: bool = True) -> dict[str, Any]:
    """Scan, persist, flag new devices, and push the result to the UI radar."""
    try:
        result = await radar.scan(cidr, deep, on_progress=lambda p: emit({"type": "radar_progress", **p}))
    except ValueError as e:
        raise ToolError(str(e))
    baseline = not memory.devices()   # very first scan: record what's there without raising intruder alarms
    for dev in result["devices"]:
        real = not dev["mac"].startswith("unknown-")
        dev["new"] = (memory.upsert_device(dev) if real else False) and not baseline and not dev.get("is_self")
        stored = memory.device(dev["mac"]) if real else None
        dev["label"] = stored["label"] if stored else ""
        dev["trusted"] = bool(stored["trusted"]) or bool(dev.get("is_self"))
        dev["first_seen"] = stored["first_seen"] if stored else time.time()
    result["baseline"] = baseline
    result["intruder_alerts"] = [d for d in result["devices"] if d["new"] and not d["trusted"] and not d.get("is_self")]
    await emit({"type": "radar", **result})
    return result


def _brief(d: dict) -> dict:
    row = {"ip": d["ip"], "mac": d["mac"], "name": d.get("label") or d.get("hostname") or "", "vendor": d.get("vendor") or ("randomized MAC" if d.get("randomized_mac") else "unknown"),
           "type": d["kind"], "confidence": d["confidence"], "ports": d.get("open_services") or []}
    for flag in ("new", "trusted", "is_gateway", "is_self"):
        if d.get(flag):
            row[flag] = True
    return row


@tool("radar_scan", "Scan the local network and identify connected devices (IP, MAC, vendor, name, device type, open services). "
                    "Shows the result on the UI radar and flags devices never seen before. Only your own private network is scanned. Takes 10-40 seconds.",
      {"network": s("Optional CIDR, e.g. 192.168.1.0/24 (default: this machine's network)"), "deep": b("Also probe names/ports for better identification (default true)")},
      untrusted=True)
async def radar_scan(ctx: Context, network: str = "", deep: bool = True) -> dict:
    res = await run_scan(ctx.memory, ctx.emit, network or None, deep)
    return {"network": res["network"], "gateway": res["gateway"], "count": len(res["devices"]), "took_s": res["took_s"],
            "first_scan_baseline": res["baseline"], "new_untrusted_devices": [_brief(d) for d in res["intruder_alerts"]], "devices": [_brief(d) for d in res["devices"]],
            "note": "Names/types are best-effort guesses from MAC vendor, hostnames and open ports. Phones using private Wi-Fi addresses show no vendor."}


@tool("radar_history", "Devices ever seen on the network (from earlier scans), with first/last seen times, labels and trust status.", {})
def radar_history(ctx: Context) -> list[dict]:
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    return [{"mac": d["mac"], "ip": d["ip"], "name": d["label"] or d["hostname"], "vendor": d["vendor"], "type": d["kind"], "trusted": bool(d["trusted"]),
             "first_seen": fmt(d["first_seen"]), "last_seen": fmt(d["last_seen"])} for d in ctx.memory.devices()]


@tool("radar_label", "Name a device on the radar and/or mark it trusted (trusted devices don't raise new-device alerts).",
      {"mac": s("Device MAC address"), "label": s("Friendly name, e.g. 'Dad's iPhone'"), "trusted": b("Mark trusted (true) or untrusted (false)")}, ["mac"], risk="modify", sandboxed=True)
async def radar_label(ctx: Context, mac: str, label: str | None = None, trusted: bool | None = None) -> str:
    mac = radar.normalize_mac(mac)
    if not ctx.memory.label_device(mac, label, trusted):
        raise ToolError(f"Unknown device {mac}. Run radar_scan first.")
    await ctx.emit({"type": "radar_device_update", "mac": mac, "label": label, "trusted": trusted})
    return f"Updated {mac}"


@tool("radar_networks", "Show the local networks this machine is attached to (the ones the radar may scan).", {})
def radar_networks(ctx: Context) -> Any:
    return {"networks": radar.local_networks(), "gateway": radar.default_gateway()}

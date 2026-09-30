"""Live world map data & control: geocoding, quakes, natural events, geolocated news, flights, weather, routes."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .registry import Context, ToolError, b, enum, i, n, s, tool

UA = {"User-Agent": "MRX-agent/0.1 (personal assistant; contact via repo)"}
LAYERS = ("quakes", "events", "news", "flights", "rain")

_cache: dict[str, tuple[float, Any]] = {}
_last_nominatim = 0.0


async def _get(url: str, params: dict | None = None, ttl: int = 60, timeout: float = 25) -> Any:
    key = url + repr(sorted((params or {}).items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url, params=params, headers=UA)
    if r.status_code == 429:
        raise ToolError(f"Rate limited by {httpx.URL(url).host}; try again shortly.")
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


async def geocode(place: str) -> dict:
    global _last_nominatim
    wait = 1.1 - (time.time() - _last_nominatim)  # Nominatim usage policy: max 1 request/second
    if wait > 0:
        import asyncio  # noqa: PLC0415
        await asyncio.sleep(wait)
    _last_nominatim = time.time()
    res = await _get("https://nominatim.openstreetmap.org/search", {"q": place, "format": "jsonv2", "limit": 1, "addressdetails": 0}, ttl=86400)
    if not res:
        raise ToolError(f"Could not find a place called {place!r}")
    r = res[0]
    return {"lat": float(r["lat"]), "lon": float(r["lon"]), "name": r["display_name"], "bbox": [float(x) for x in r["boundingbox"]]}


# -- GeoJSON feeds used both by tools and by the UI's /api/geo/* endpoints ---------
async def quakes(period: str = "day", min_mag: str = "2.5") -> dict:
    period = period if period in ("hour", "day", "week") else "day"
    mag = min_mag if min_mag in ("significant", "4.5", "2.5", "1.0", "all") else "2.5"
    return await _get(f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{mag}_{period}.geojson", ttl=120)


async def natural_events(limit: int = 60) -> dict:
    data = await _get("https://eonet.gsfc.nasa.gov/api/v3/events", {"status": "open", "limit": limit}, ttl=600)
    feats = []
    for ev in data.get("events", []):
        geo = ev["geometry"][-1]
        if geo["type"] != "Point":
            continue
        feats.append({"type": "Feature", "geometry": geo, "properties": {"title": ev["title"], "category": ev["categories"][0]["title"], "date": geo["date"], "link": ev.get("link")}})
    return {"type": "FeatureCollection", "features": feats}


async def news_geo(query: str = "", timespan: str = "1d") -> dict:
    q = query or "sourcelang:english"
    return await _get("https://api.gdeltproject.org/api/v2/geo/geo", {"query": q, "format": "GeoJSON", "timespan": timespan, "maxpoints": 250}, ttl=300)


async def flights(lamin: float, lomin: float, lamax: float, lomax: float) -> dict:
    span = (lamax - lamin) * (lomax - lomin)
    if span > 2500:
        raise ToolError("Zoom in further to show flights (area too large for the free OpenSky API).")
    data = await _get("https://opensky-network.org/api/states/all", {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}, ttl=15)
    feats = []
    for st in (data.get("states") or [])[:400]:
        if st[5] is None or st[6] is None:
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [st[5], st[6]]},
                      "properties": {"callsign": (st[1] or "").strip(), "country": st[2], "alt_m": st[7], "speed_ms": st[9], "heading": st[10]}})
    return {"type": "FeatureCollection", "features": feats}


async def rain_radar_tiles() -> dict:
    data = await _get("https://api.rainviewer.com/public/weather-maps.json", ttl=120)
    frame = (data.get("radar", {}).get("past") or [{}])[-1]
    return {"host": data.get("host"), "path": frame.get("path")}


# -- tools -------------------------------------------------------------------
@tool("map_show", "Fly the UI's live world map to a place or coordinates and drop a marker. Use a place name in any language, or lat/lon.",
      {"place": s("Place name/address, any language"), "lat": n("Latitude"), "lon": n("Longitude"), "zoom": i("Zoom 1-18 (default 10)"), "label": s("Marker label")},
      sandboxed=True)
async def map_show(ctx: Context, place: str = "", lat: float | None = None, lon: float | None = None, zoom: int = 10, label: str = "") -> dict:
    if place and (lat is None or lon is None):
        g = await geocode(place)
        lat, lon, label = g["lat"], g["lon"], label or g["name"]
    if lat is None or lon is None:
        raise ToolError("Give a place or both lat and lon.")
    await ctx.emit({"type": "map", "action": "fly", "lat": lat, "lon": lon, "zoom": max(1, min(zoom, 18)), "label": label or place})
    return {"lat": lat, "lon": lon, "label": label or place}


@tool("map_layers", "Turn live data layers on the UI map on/off: quakes (USGS earthquakes), events (NASA natural events: storms, fires, volcanoes), "
                    "news (GDELT geolocated news; optional query), flights (live aircraft in view), rain (weather radar).",
      {"layer": enum("Layer", *LAYERS), "enabled": b("On or off (default on)"), "query": s("For news: topic filter")}, ["layer"], sandboxed=True)
async def map_layers(ctx: Context, layer: str, enabled: bool = True, query: str = "") -> str:
    await ctx.emit({"type": "map", "action": "layer", "layer": layer, "enabled": enabled, "query": query})
    return f"Layer {layer} {'on' if enabled else 'off'}"


@tool("map_markers", "Show custom markers on the map (e.g. search results). Replaces earlier custom markers.",
      {"markers": {"type": "array", "description": "List of {lat, lon, label}", "items": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "label": {"type": "string"}}, "required": ["lat", "lon"]}}},
      ["markers"], sandboxed=True)
async def map_markers(ctx: Context, markers: list[dict]) -> str:
    await ctx.emit({"type": "map", "action": "markers", "markers": markers[:200]})
    return f"Showing {len(markers[:200])} markers"


@tool("map_route", "Compute a driving route between two places, draw it on the map, and return distance and duration.",
      {"origin": s("Start place"), "destination": s("End place")}, ["origin", "destination"], sandboxed=True)
async def map_route(ctx: Context, origin: str, destination: str) -> dict:
    a, d = await geocode(origin), await geocode(destination)
    data = await _get(f"https://router.project-osrm.org/route/v1/driving/{a['lon']},{a['lat']};{d['lon']},{d['lat']}", {"overview": "full", "geometries": "geojson"}, ttl=300)
    if data.get("code") != "Ok":
        raise ToolError(f"No driving route found ({data.get('code')}).")
    r = data["routes"][0]
    await ctx.emit({"type": "map", "action": "route", "geometry": r["geometry"], "from": a, "to": d})
    return {"from": a["name"], "to": d["name"], "distance_km": round(r["distance"] / 1000, 1), "duration_min": round(r["duration"] / 60)}


@tool("world_events", "Fetch live data about the world: recent earthquakes, open natural events (wildfires, storms, volcanoes) or geolocated news counts. "
                      "Returns a summary; use map_layers to visualise on the map.",
      {"kind": enum("Data set", "quakes", "events", "news"), "period": s("For quakes: hour, day or week"), "min_magnitude": s("For quakes: 1.0, 2.5, 4.5 or significant"), "query": s("For news: topic")},
      ["kind"], untrusted=True)
async def world_events(ctx: Context, kind: str, period: str = "day", min_magnitude: str = "4.5", query: str = "") -> Any:
    if kind == "quakes":
        data = await quakes(period, min_magnitude)
        rows = [{"mag": f["properties"]["mag"], "place": f["properties"]["place"], "time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(f["properties"]["time"] / 1000)),
                 "lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0]} for f in data["features"]]
        rows.sort(key=lambda r: r["mag"] or 0, reverse=True)
        return {"count": len(rows), "top": rows[:20]}
    if kind == "events":
        data = await natural_events()
        return [f["properties"] | {"lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1]} for f in data["features"]][:40]
    data = await news_geo(query)
    return [{"place": f["properties"].get("name"), "mentions": f["properties"].get("count")} for f in data.get("features", [])[:40]]


WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}


@tool("weather", "Current weather and 3-day forecast for a place (any language).", {"place": s("Place name")}, ["place"])
async def weather(ctx: Context, place: str) -> dict:
    g = await geocode(place)
    d = await _get("https://api.open-meteo.com/v1/forecast", {"latitude": g["lat"], "longitude": g["lon"], "timezone": "auto", "forecast_days": 3,
                   "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
                   "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"}, ttl=600)
    cur, day = d["current"], d["daily"]
    return {"place": g["name"], "now": {"temp_c": cur["temperature_2m"], "feels_c": cur["apparent_temperature"], "humidity": cur["relative_humidity_2m"],
                                        "wind_kmh": cur["wind_speed_10m"], "sky": WMO.get(cur["weather_code"], cur["weather_code"])},
            "forecast": [{"date": day["time"][k], "min_c": day["temperature_2m_min"][k], "max_c": day["temperature_2m_max"][k], "rain_mm": day["precipitation_sum"][k],
                          "sky": WMO.get(day["weather_code"][k], day["weather_code"][k])} for k in range(len(day["time"]))]}

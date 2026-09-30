"""Built-in YouTube player: search, play, queue and transport control (the player lives in the UI)."""
from __future__ import annotations

import json
import re

import httpx

from .registry import Context, ToolError, b, enum, i, s, tool

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en"}
ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def extract_video_id(text: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text.strip()):
        return text.strip()
    m = ID_RE.search(text)
    return m.group(1) if m else None


async def search(query: str, limit: int = 8, api_key: str = "") -> list[dict]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        if api_key:
            r = await c.get("https://www.googleapis.com/youtube/v3/search",
                            params={"part": "snippet", "q": query, "type": "video", "maxResults": limit, "key": api_key, "videoEmbeddable": "true"})
            if r.is_success:
                return [{"id": it["id"]["videoId"], "title": it["snippet"]["title"], "channel": it["snippet"]["channelTitle"]} for it in r.json().get("items", [])]
        r = await c.get("https://www.youtube.com/results", params={"search_query": query, "hl": "en"}, headers=UA)
    m = re.search(r"var ytInitialData\s*=\s*", r.text)
    if not m:
        raise ToolError("YouTube search failed (page format changed?). Set YOUTUBE_API_KEY for reliable search, or pass a video URL.")
    data, _ = json.JSONDecoder().raw_decode(r.text[m.end():])
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                out.append({"id": v["videoId"], "title": "".join(x.get("text", "") for x in v.get("title", {}).get("runs", [])),
                            "channel": "".join(x.get("text", "") for x in v.get("ownerText", {}).get("runs", [])),
                            "duration": v.get("lengthText", {}).get("simpleText", "live")})
            else:
                for val in o.values():
                    walk(val)
        elif isinstance(o, list):
            for val in o:
                walk(val)

    walk(data)
    return out[:limit]


@tool("youtube_search", "Search YouTube and show results in the UI's YouTube panel. Returns video ids/titles.",
      {"query": s("Search text, any language"), "limit": i("Max results (default 8)")}, ["query"], untrusted=True, sandboxed=True)
async def youtube_search(ctx: Context, query: str, limit: int = 8) -> list[dict]:
    res = await search(query, limit, ctx.config.get("YOUTUBE_API_KEY"))
    await ctx.emit({"type": "youtube", "action": "results", "query": query, "results": res})
    return res


@tool("youtube_play", "Play a video in the built-in player. Give a search query (plays the top result), a YouTube URL, or a video id. enqueue=true adds it to the queue instead.",
      {"query": s("Search text, URL, or 11-character video id"), "enqueue": b("Add to queue instead of playing now")}, ["query"], sandboxed=True)
async def youtube_play(ctx: Context, query: str, enqueue: bool = False) -> dict:
    vid = extract_video_id(query)
    title = ""
    if not vid:
        res = await search(query, 5, ctx.config.get("YOUTUBE_API_KEY"))
        if not res:
            raise ToolError(f"No YouTube results for {query!r}")
        vid, title = res[0]["id"], res[0]["title"]
    await ctx.emit({"type": "youtube", "action": "queue" if enqueue else "load", "videoId": vid, "title": title})
    return {"video_id": vid, "title": title, "url": f"https://youtu.be/{vid}", "queued": enqueue}


@tool("youtube_control", "Control the built-in YouTube player.",
      {"action": enum("Action", "pause", "resume", "stop", "next", "previous", "seek", "volume", "mute", "unmute", "fullscreen"),
       "value": {"type": "number", "description": "Seconds for seek (relative, e.g. -10 / 30), or 0-100 for volume"}}, ["action"], sandboxed=True)
async def youtube_control(ctx: Context, action: str, value: float = 0) -> str:
    await ctx.emit({"type": "youtube", "action": "control", "command": action, "value": value})
    return f"Player: {action}"

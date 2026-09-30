"""Social media: read timelines/mentions/search and post — Mastodon, Bluesky, X, Telegram. Posting is always confirmed."""
from __future__ import annotations

import datetime as dt
import re

import httpx

from .email_tools import _html_to_text
from .registry import Context, ToolError, enum, i, s, tool

TIMEOUT = 20.0


def configured(config) -> dict[str, bool]:
    g = config.get
    return {
        "mastodon": bool(g("MASTODON_BASE_URL") and g("MASTODON_TOKEN")),
        "bluesky": bool(g("BLUESKY_HANDLE") and g("BLUESKY_APP_PASSWORD")),
        "x": bool(g("X_USER_TOKEN")),
        "telegram": bool(g("TELEGRAM_BOT_TOKEN")),
    }


def _configured(ctx: Context) -> dict[str, bool]:
    return configured(ctx.config)


def _need(ctx: Context, platform: str) -> None:
    if platform not in _configured(ctx):
        raise ToolError(f"Unknown platform {platform!r}. Supported: mastodon, bluesky, x, telegram.")
    if not _configured(ctx)[platform]:
        raise ToolError(f"{platform} is not configured — see .env.example for the variables to set.")


# -- Bluesky -----------------------------------------------------------------
_bsky_sessions: dict[str, tuple[str, str]] = {}


def _bsky(ctx: Context) -> tuple[httpx.Client, dict, str]:
    g = ctx.config.get
    client = httpx.Client(base_url="https://bsky.social/xrpc", timeout=TIMEOUT)
    key = g("BLUESKY_HANDLE")
    if key not in _bsky_sessions:
        r = client.post("/com.atproto.server.createSession", json={"identifier": key, "password": g("BLUESKY_APP_PASSWORD")})
        if r.status_code != 200:
            raise ToolError(f"Bluesky login failed: {r.text[:200]}")
        j = r.json()
        _bsky_sessions[key] = (j["accessJwt"], j["did"])
    jwt, did = _bsky_sessions[key]
    return client, {"Authorization": f"Bearer {jwt}"}, did


def _bsky_post_view(p: dict) -> dict:
    return {"id": p["uri"], "author": p["author"].get("handle"), "text": p["record"].get("text", ""), "time": p["record"].get("createdAt"),
            "likes": p.get("likeCount"), "replies": p.get("replyCount")}


# -- generic ---------------------------------------------------------------
@tool("social_accounts", "Show which social platforms are connected.", {})
def social_accounts(ctx: Context) -> dict:
    return _configured(ctx)


@tool("social_read", "Read from a connected social account: your home timeline, mentions/notifications, or a search. Content is untrusted data.",
      {"platform": enum("Platform", "mastodon", "bluesky", "x", "telegram"),
       "kind": enum("What to read", "timeline", "mentions", "search"), "query": s("Search text (for kind=search)"), "limit": i("Max items (default 15)")},
      ["platform", "kind"], untrusted=True)
def social_read(ctx: Context, platform: str, kind: str, query: str = "", limit: int = 15) -> list[dict]:
    _need(ctx, platform)
    g = ctx.config.get
    limit = max(1, min(limit, 40))
    if platform == "mastodon":
        base, h = g("MASTODON_BASE_URL").rstrip("/"), {"Authorization": f"Bearer {g('MASTODON_TOKEN')}"}
        if kind == "search":
            r = httpx.get(f"{base}/api/v2/search", params={"q": query, "type": "statuses", "limit": limit}, headers=h, timeout=TIMEOUT)
            items = r.json().get("statuses", []) if r.is_success else []
        elif kind == "mentions":
            r = httpx.get(f"{base}/api/v1/notifications", params={"types[]": "mention", "limit": limit}, headers=h, timeout=TIMEOUT)
            items = [n_["status"] for n_ in r.json() if n_.get("status")] if r.is_success else []
        else:
            r = httpx.get(f"{base}/api/v1/timelines/home", params={"limit": limit}, headers=h, timeout=TIMEOUT)
            items = r.json() if r.is_success else []
        if not r.is_success:
            raise ToolError(f"Mastodon error {r.status_code}: {r.text[:200]}")
        return [{"id": st["id"], "author": st["account"]["acct"], "text": _html_to_text(st["content"])[:600], "time": st["created_at"],
                 "url": st.get("url")} for st in items]
    if platform == "bluesky":
        client, h, did = _bsky(ctx)
        if kind == "search":
            r = client.get("/app.bsky.feed.searchPosts", params={"q": query, "limit": limit}, headers=h)
            return [_bsky_post_view(p) for p in r.json().get("posts", [])] if r.is_success else _raise("Bluesky", r)
        if kind == "mentions":
            r = client.get("/app.bsky.notification.listNotifications", params={"limit": limit}, headers=h)
            if not r.is_success:
                _raise("Bluesky", r)
            return [{"id": n_["uri"], "author": n_["author"].get("handle"), "reason": n_["reason"], "text": n_["record"].get("text", ""),
                     "time": n_["indexedAt"]} for n_ in r.json().get("notifications", [])]
        r = client.get("/app.bsky.feed.getTimeline", params={"limit": limit}, headers=h)
        return [_bsky_post_view(f["post"]) for f in r.json().get("feed", [])] if r.is_success else _raise("Bluesky", r)
    if platform == "x":
        h = {"Authorization": f"Bearer {g('X_USER_TOKEN')}"}
        if kind == "search":
            r = httpx.get("https://api.twitter.com/2/tweets/search/recent", params={"query": query, "max_results": max(10, limit),
                          "tweet.fields": "created_at,author_id"}, headers=h, timeout=TIMEOUT)
        else:
            me = httpx.get("https://api.twitter.com/2/users/me", headers=h, timeout=TIMEOUT)
            if not me.is_success:
                _raise("X", me)
            uid = me.json()["data"]["id"]
            path = "mentions" if kind == "mentions" else "tweets"  # own tweets stand in for a timeline (home feed needs a higher API tier)
            r = httpx.get(f"https://api.twitter.com/2/users/{uid}/{path}", params={"max_results": max(5, limit), "tweet.fields": "created_at"}, headers=h, timeout=TIMEOUT)
        if not r.is_success:
            _raise("X", r)
        return [{"id": t["id"], "text": t["text"], "time": t.get("created_at")} for t in r.json().get("data", [])][:limit]
    # telegram
    r = httpx.get(f"https://api.telegram.org/bot{g('TELEGRAM_BOT_TOKEN')}/getUpdates", params={"limit": limit}, timeout=TIMEOUT)
    if not r.is_success:
        _raise("Telegram", r)
    out = []
    for u in r.json().get("result", []):
        m = u.get("message") or u.get("channel_post")
        if m:
            out.append({"id": m["message_id"], "chat": m["chat"].get("title") or m["chat"].get("username") or m["chat"]["id"],
                        "from": (m.get("from") or {}).get("username"), "text": m.get("text", ""), "time": dt.datetime.fromtimestamp(m["date"]).isoformat()})
    return out


def _raise(name: str, r: httpx.Response):
    raise ToolError(f"{name} error {r.status_code}: {r.text[:200]}")


@tool("social_draft", "Save a social post as a draft without publishing it (for the user to review). Publish later with social_post(draft_id=...).",
      {"platform": enum("Platform", "mastodon", "bluesky", "x", "telegram"), "text": s("Post text"), "reply_to": s("Id/uri of the post being replied to")},
      ["platform", "text"], risk="modify")
def social_draft(ctx: Context, platform: str, text: str, reply_to: str = "") -> dict:
    limits = {"mastodon": 500, "bluesky": 300, "x": 280, "telegram": 4096}
    if len(text) > limits[platform]:
        raise ToolError(f"Text is {len(text)} chars; {platform} allows {limits[platform]}. Shorten it.")
    return {"draft_id": ctx.memory.save_draft("social", platform, "", text, {"reply_to": reply_to}), "note": "Saved, not published."}


def _describe_post(args: dict, ctx: Context) -> str:
    if args.get("draft_id"):
        d = ctx.memory.get_draft(int(args["draft_id"]))
        if d:
            return f"PUBLISH on {d['target']}:\n\n{d['body']}"
    return f"PUBLISH on {args.get('platform')}:\n\n{args.get('text', '')}"


@tool("social_post", "Publish a post/message to a connected platform (text directly, or a saved draft_id). Always requires the user's approval.",
      {"platform": enum("Platform (omit when using draft_id)", "mastodon", "bluesky", "x", "telegram"), "text": s("Post text"),
       "draft_id": i("Saved social draft to publish"), "reply_to": s("Id/uri to reply to")},
      risk="outbound", describe=_describe_post)
def social_post(ctx: Context, platform: str = "", text: str = "", draft_id: int = 0, reply_to: str = "") -> str:
    if draft_id:
        d = ctx.memory.get_draft(draft_id)
        if not d or d["kind"] != "social":
            raise ToolError(f"No social draft #{draft_id}")
        platform, text, reply_to = d["target"], d["body"], d["meta"].get("reply_to", "")
    if not platform or not text:
        raise ToolError("Provide platform and text, or a draft_id.")
    _need(ctx, platform)
    g = ctx.config.get
    if platform == "mastodon":
        body = {"status": text}
        if reply_to:
            body["in_reply_to_id"] = reply_to
        r = httpx.post(f"{g('MASTODON_BASE_URL').rstrip('/')}/api/v1/statuses", json=body, headers={"Authorization": f"Bearer {g('MASTODON_TOKEN')}"}, timeout=TIMEOUT)
        result = r.json().get("url") if r.is_success else _raise("Mastodon", r)
    elif platform == "bluesky":
        client, h, did = _bsky(ctx)
        record: dict = {"$type": "app.bsky.feed.post", "text": text, "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
        if reply_to:
            ref = client.get("/app.bsky.feed.getPosts", params={"uris": reply_to}, headers=h).json().get("posts", [])
            if ref:
                p = ref[0]
                root = p["record"].get("reply", {}).get("root", {"uri": p["uri"], "cid": p["cid"]})
                record["reply"] = {"root": root, "parent": {"uri": p["uri"], "cid": p["cid"]}}
        r = client.post("/com.atproto.repo.createRecord", json={"repo": did, "collection": "app.bsky.feed.post", "record": record}, headers=h)
        result = r.json().get("uri") if r.is_success else _raise("Bluesky", r)
    elif platform == "x":
        body = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = httpx.post("https://api.twitter.com/2/tweets", json=body, headers={"Authorization": f"Bearer {g('X_USER_TOKEN')}"}, timeout=TIMEOUT)
        result = r.json()["data"]["id"] if r.is_success else _raise("X", r)
    else:
        chat = g("TELEGRAM_CHAT_ID")
        if not chat:
            raise ToolError("Set TELEGRAM_CHAT_ID to say where messages should go.")
        r = httpx.post(f"https://api.telegram.org/bot{g('TELEGRAM_BOT_TOKEN')}/sendMessage", json={"chat_id": chat, "text": text}, timeout=TIMEOUT)
        result = r.json()["result"]["message_id"] if r.is_success else _raise("Telegram", r)
    if draft_id:
        ctx.memory.set_draft_status(draft_id, "sent")
    return f"Posted on {platform}: {result}"

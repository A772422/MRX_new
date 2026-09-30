import json
from email.message import EmailMessage

import pytest

from mrx.tools import email_tools, news, world, youtube


def test_youtube_id_extraction():
    ex = youtube.extract_video_id
    assert ex("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert ex("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "dQw4w9WgXcQ"
    assert ex("https://youtu.be/dQw4w9WgXcQ?si=abc") == "dQw4w9WgXcQ"
    assert ex("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert ex("lofi hip hop radio") is None


async def test_youtube_scrape_parsing(monkeypatch):
    data = {"contents": [{"videoRenderer": {"videoId": "AAAAAAAAAAA", "title": {"runs": [{"text": "Song "}, {"text": "One"}]},
                                            "ownerText": {"runs": [{"text": "Chan"}]}, "lengthText": {"simpleText": "3:21"}}},
                         {"x": [{"videoRenderer": {"videoId": "BBBBBBBBBBB", "title": {"runs": [{"text": "Two"}]}}}]}]}
    html = f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"

    class R:
        text, is_success = html, False
    class C:
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def get(s, *a, **k): return R()
    monkeypatch.setattr(youtube.httpx, "AsyncClient", lambda **k: C())
    res = await youtube.search("q", 5)
    assert [r["id"] for r in res] == ["AAAAAAAAAAA", "BBBBBBBBBBB"] and res[0]["title"] == "Song One" and res[0]["duration"] == "3:21"


async def test_youtube_tools_emit_ui_events(ctx, make_ctx, monkeypatch):
    c, rec = make_ctx()
    from mrx.tools import execute
    r = await execute("youtube_play", {"query": "https://youtu.be/dQw4w9WgXcQ"}, c)
    assert not r.is_error
    assert rec.events[-1] == {"type": "youtube", "action": "load", "videoId": "dQw4w9WgXcQ", "title": ""}
    await execute("youtube_control", {"action": "pause"}, c)
    assert rec.events[-1]["command"] == "pause"


def test_email_body_parsing_multilingual_and_html():
    m = EmailMessage()
    m["Subject"] = "=?utf-8?B?5pel5pys6Kqe44Gu5Lu25ZCN?="
    m.set_content("plain フォールバック")
    m.add_alternative("<html><style>x{}</style><body><p>Hello <b>wörld</b></p><script>bad()</script></body></html>", subtype="html")
    body, files = email_tools._body(m)
    assert "フォールバック" in body and files == []
    assert email_tools._dh(m["Subject"]) == "日本語の件名"
    only_html = EmailMessage()
    only_html.set_content("<p>Line one</p><p>Line two</p>", subtype="html")
    text, _ = email_tools._body(only_html)
    assert "Line one" in text and "Line two" in text and "<p>" not in text


def test_email_build_utf8_and_threading():
    class C: address = "me@x.com"
    msg = email_tools._build(C, "你好 <you@y.com>", "Ré: héllo", "Здравствуйте, мир", in_reply_to="<abc@x>")
    assert msg["In-Reply-To"] == "<abc@x>" and msg["References"] == "<abc@x>"
    assert msg.get_content().strip() == "Здравствуйте, мир"
    msg.as_bytes()  # serialises without error


async def test_email_send_requires_draft_and_shows_full_message_for_approval(make_ctx, config, monkeypatch):
    from mrx.tools import execute
    config.autonomy = "trusted"
    ctx, rec = make_ctx()
    did = ctx.memory.save_draft("email", "bob@example.com", "Lunch?", "Are you free at noon?", {"cc": ""})
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "x")
    sent = {}
    class FakeSMTP:
        def __init__(s, *a, **k): pass
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def login(s, *a): pass
        def send_message(s, msg, to_addrs): sent["to"], sent["subject"] = to_addrs, msg["Subject"]
    monkeypatch.setattr(email_tools.smtplib, "SMTP_SSL", FakeSMTP)
    r = await execute("email_send", {"draft_id": did}, ctx)
    assert not r.is_error and sent == {"to": ["bob@example.com"], "subject": "Lunch?"}
    tool, risk, desc = rec.confirms[0]
    assert risk == "outbound" and "bob@example.com" in desc and "Are you free at noon?" in desc
    assert (await execute("email_send", {"draft_id": did}, ctx)).is_error          # can't send twice
    assert (await execute("email_send", {"draft_id": 999}, ctx)).is_error


async def test_email_not_configured_message(ctx, monkeypatch):
    from mrx.tools import execute
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    r = await execute("email_list", {}, ctx)
    assert r.is_error and "not configured" in r.content


async def test_social_post_needs_config_and_approval(make_ctx, config):
    from mrx.tools import execute
    ctx, rec = make_ctx()
    r = await execute("social_post", {"platform": "mastodon", "text": "hi"}, ctx)
    assert r.is_error and rec.confirms and rec.confirms[0][1] == "outbound"
    assert (await execute("social_draft", {"platform": "x", "text": "a" * 300}, ctx)).is_error     # too long for X
    assert not (await execute("social_draft", {"platform": "x", "text": "short"}, ctx)).is_error


async def test_headlines_merge_sort_dedupe(monkeypatch):
    async def fake(client, source, url):
        base = {"BBC": 100, "DW": 200}[source]
        return [{"source": source, "title": "Same story", "url": "u", "summary": "", "time": base},
                {"source": source, "title": f"{source} only", "url": "u2", "summary": "", "time": base + 1}], None
    monkeypatch.setattr(news, "_fetch_feed", fake)
    monkeypatch.setitem(news.FEEDS["en"], "t_test", [("BBC", "x"), ("DW", "y")])
    news._cache.clear()
    res = await news.fetch_headlines("t_test", "en")
    titles = [i["title"] for i in res["items"]]
    assert titles[0] == "DW only" and titles.count("Same story") == 1


async def test_feed_parsing_real_rss(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>Titre \xc3\xa9l\xc3\xa9gant</title><link>http://x/1</link>
    <description>&lt;p&gt;R\xc3\xa9sum\xc3\xa9&lt;/p&gt;</description><pubDate>Wed, 30 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    class R:
        content = rss
        def raise_for_status(s): pass
    class C:
        async def get(s, *a, **k): return R()
    items, err = await news._fetch_feed(C(), "Src", "http://feed")
    assert err is None and items[0]["title"] == "Titre élégant" and items[0]["summary"] == "Résumé" and items[0]["time"] > 0


async def test_map_tools_emit_ui_events(make_ctx):
    from mrx.tools import execute
    ctx, rec = make_ctx()
    assert not (await execute("map_show", {"lat": 48.85, "lon": 2.35, "zoom": 12, "label": "Paris"}, ctx)).is_error
    assert rec.events[-1]["action"] == "fly" and rec.events[-1]["lat"] == 48.85
    await execute("map_layers", {"layer": "quakes"}, ctx)
    assert rec.events[-1] == {"type": "map", "action": "layer", "layer": "quakes", "enabled": True, "query": ""}
    assert (await execute("map_show", {}, ctx)).is_error


async def test_flights_area_guard():
    from mrx.tools.registry import ToolError
    with pytest.raises(ToolError):
        await world.flights(-80, -170, 80, 170)

import pytest

from mrx.tools import REGISTRY, api_tools, execute
from mrx.tools.registry import Image, tool


def test_all_tools_have_valid_schemas():
    names = set()
    for t in api_tools():
        assert t["name"] not in names
        names.add(t["name"])
        assert t["description"].strip()
        sch = t["input_schema"]
        assert sch["type"] == "object"
        for r in sch["required"]:
            assert r in sch["properties"], f"{t['name']} requires undeclared {r}"
        for p, spec in sch["properties"].items():
            assert "type" in spec, f"{t['name']}.{p}"
    assert len(names) >= 60


def test_api_tools_are_sorted_for_cache_stability():
    names = [t["name"] for t in api_tools()]
    assert names == sorted(names)


def test_covers_requested_capabilities():
    have = set(REGISTRY)
    for needed in ("shell_run", "app_open", "browser_open", "browser_click", "youtube_play", "fs_copy", "fs_move", "fs_rename", "fs_delete",
                   "memory_save", "email_draft", "email_send", "social_post", "news_headlines", "map_show", "radar_scan", "task_schedule"):
        assert needed in have


async def test_unknown_tool_and_bad_args(ctx):
    assert (await execute("nope", {}, ctx)).is_error
    assert "Missing" in (await execute("fs_read", {}, ctx)).content
    assert "Unknown argument" in (await execute("fs_read", {"path": "x", "bogus": 1}, ctx)).content
    assert (await execute("fs_read", [], ctx)).is_error


async def test_outbound_and_destructive_need_approval_even_when_trusted(make_ctx, config):
    config.autonomy = "trusted"
    ctx, rec = make_ctx()
    await execute("process_kill", {"pid": 1}, ctx)
    assert rec.confirms and rec.confirms[0][1] == "destructive"


async def test_unattended_runs_are_denied_risky_tools(make_ctx, config):
    config.autonomy = "trusted"
    ctx, rec = make_ctx(interactive=False)
    o = await execute("fs_delete", {"path": "x"}, ctx)
    assert o.is_error and "unattended" in o.content and not rec.confirms


async def test_taint_escalates_confirmation(make_ctx, config):
    config.autonomy = "trusted"
    ctx, rec = make_ctx()
    await execute("fs_mkdir", {"path": "a"}, ctx)
    assert not rec.confirms                       # trusted: no prompt
    ctx.tainted = True                            # e.g. a web page or email was read this turn
    await execute("fs_mkdir", {"path": "b"}, ctx)
    assert rec.confirms and "untrusted content" in rec.confirms[0][2]


async def test_untrusted_tool_sets_taint(ctx):
    @tool("t_read_untrusted", "test", untrusted=True)
    def _t(ctx_):
        return "hi"
    try:
        assert not ctx.tainted
        await execute("t_read_untrusted", {}, ctx)
        assert ctx.tainted
    finally:
        REGISTRY.pop("t_read_untrusted")


async def test_results_are_truncated_and_images_shaped(ctx, home):
    ctx.config.tool_result_chars = 100
    (home / "big.txt").write_text("a" * 5000)
    o = await execute("fs_read", {"path": "big.txt", "limit": 5000}, ctx)
    assert len(o.content) < 250 and "truncated" in o.content

    @tool("t_img", "test")
    def _i(ctx_):
        return Image("QUJD", "image/jpeg", "cap")
    try:
        o = await execute("t_img", {}, ctx)
        assert o.content[0]["type"] == "image" and o.content[1]["text"] == "cap"
    finally:
        REGISTRY.pop("t_img")


async def test_tool_exceptions_become_error_results(ctx):
    o = await execute("fs_read", {"path": "/definitely/not/here"}, ctx)
    assert o.is_error and "Not a file" in o.content

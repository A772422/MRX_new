import pytest

from mrx.tools import execute


async def run(ctx, tool_name, **args):
    return await execute(tool_name, args, ctx)


async def test_create_read_edit_append(ctx, home):
    o = await run(ctx, "fs_write", path="notes/a.txt", content="hello world")
    assert not o.is_error
    assert (home / "notes/a.txt").read_text() == "hello world"
    assert "hello" in (await run(ctx, "fs_read", path="~/notes/a.txt")).content
    assert not (await run(ctx, "fs_edit", path="notes/a.txt", old="world", new="there")).is_error
    assert (home / "notes/a.txt").read_text() == "hello there"
    await run(ctx, "fs_append", path="notes/a.txt", content="!")
    assert (home / "notes/a.txt").read_text() == "hello there!"


async def test_write_refuses_overwrite_without_flag_and_backs_up(ctx, home, config):
    await run(ctx, "fs_write", path="a.txt", content="v1")
    o = await run(ctx, "fs_write", path="a.txt", content="v2")
    assert o.is_error and "overwrite" in o.content
    assert (await run(ctx, "fs_write", path="a.txt", content="v2", overwrite=True)).is_error is False
    backups = list(config.trash_dir.glob("*/a.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "v1"


async def test_edit_ambiguous_match_is_rejected(ctx, home):
    (home / "a.txt").write_text("x x")
    o = await run(ctx, "fs_edit", path="a.txt", old="x", new="y")
    assert o.is_error and "2 places" in o.content
    assert not (await run(ctx, "fs_edit", path="a.txt", old="x", new="y", replace_all=True)).is_error
    assert (home / "a.txt").read_text() == "y y"


async def test_copy_move_rename(ctx, home):
    (home / "src").mkdir()
    (home / "src/f.txt").write_text("data")
    (home / "dst").mkdir()
    assert not (await run(ctx, "fs_copy", src="src/f.txt", dst="dst")).is_error       # copy into folder
    assert (home / "dst/f.txt").read_text() == "data" and (home / "src/f.txt").exists()
    assert (await run(ctx, "fs_copy", src="src/f.txt", dst="dst/f.txt")).is_error      # exists
    assert not (await run(ctx, "fs_move", src="src/f.txt", dst="dst/g.txt")).is_error
    assert not (home / "src/f.txt").exists() and (home / "dst/g.txt").exists()
    assert not (await run(ctx, "fs_rename", path="dst/g.txt", new_name="h.txt")).is_error
    assert (home / "dst/h.txt").exists()
    assert (await run(ctx, "fs_rename", path="dst/h.txt", new_name="../evil.txt")).is_error


async def test_copy_directory(ctx, home):
    (home / "d/sub").mkdir(parents=True)
    (home / "d/sub/x").write_text("1")
    assert not (await run(ctx, "fs_copy", src="d", dst="d2")).is_error
    assert (home / "d2/sub/x").read_text() == "1"


async def test_delete_goes_to_trash_and_restores(ctx, home):
    (home / "gone.txt").write_text("precious")
    o = await run(ctx, "fs_delete", path="gone.txt")
    assert not o.is_error and not (home / "gone.txt").exists()
    entries = (await run(ctx, "fs_trash_list")).content
    assert "gone.txt" in entries
    tid = [e for e in __import__("json").loads(entries) if e["reason"] == "deleted"][0]["id"]
    assert not (await run(ctx, "fs_trash_restore", id=tid)).is_error
    assert (home / "gone.txt").read_text() == "precious"


async def test_delete_protected_location_refused(ctx, home):
    (home / "Documents").mkdir()
    o = await run(ctx, "fs_delete", path="~/Documents")
    assert o.is_error and "protected" in o.content and (home / "Documents").exists()
    assert (await run(ctx, "fs_delete", path="/")).is_error


async def test_trash_restore_rejects_path_traversal(ctx):
    assert (await run(ctx, "fs_trash_restore", id="../..")).is_error


async def test_search_and_list(ctx, home):
    (home / "a").mkdir()
    (home / "a/one.py").write_text("needle here")
    (home / "a/two.py").write_text("nothing")
    (home / "a/three.txt").write_text("needle")
    hits = __import__("json").loads((await run(ctx, "fs_search", root="a", name="*.py", contains="NEEDLE")).content)
    assert hits == [str(home / "a/one.py")]
    listing = (await run(ctx, "fs_list", path="a")).content
    assert "one.py" in listing and "three.txt" in listing


async def test_unicode_paths_and_content(ctx, home):
    assert not (await run(ctx, "fs_write", path="文档/ملف.txt", content="こんにちは")).is_error
    assert (home / "文档/ملف.txt").read_text(encoding="utf-8") == "こんにちは"


async def test_file_writes_are_gated_in_strict_mode(make_ctx, config, home):
    config.autonomy = "strict"
    ctx, rec = make_ctx()
    await execute("fs_write", {"path": "x.txt", "content": "1"}, ctx)
    assert rec.confirms and rec.confirms[0][0] == "fs_write"
    rec2 = type(rec)(approve=False)
    ctx2, _ = make_ctx(rec2)
    o = await execute("fs_write", {"path": "y.txt", "content": "1"}, ctx2)
    assert o.is_error and "declined" in o.content and not (home / "y.txt").exists()

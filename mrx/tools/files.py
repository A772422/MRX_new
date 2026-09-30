"""File & folder management: create, read, modify, copy, move, rename, delete (to a recoverable trash)."""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from pathlib import Path

from ..safety import is_protected
from .registry import Context, ToolError, b, i, s, tool

TEXT_LIMIT = 200_000


def resolve(path: str) -> Path:
    """`~` expands to the home dir; relative paths are relative to the home dir."""
    p = Path(os.path.expandvars(path)).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p.resolve()


def _stash(ctx: Context, src: Path, reason: str) -> Path:
    """Move/copy `src` into the trash area with metadata. Returns the trash entry dir."""
    entry = ctx.config.trash_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{src.name}"
    n = 1
    while entry.exists():
        entry = entry.with_name(f"{entry.name}.{n}")
        n += 1
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text(json.dumps({"original": str(src), "reason": reason, "time": time.time()}))
    return entry


def _backup(ctx: Context, src: Path) -> None:
    if src.is_file():
        entry = _stash(ctx, src, "backup-before-overwrite")
        shutil.copy2(src, entry / src.name)


def _describe_size(p: Path) -> str:
    if p.is_dir():
        return "dir"
    size = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


@tool("fs_list", "List the contents of a directory (name, type, size, modified time).",
      {"path": s("Directory path (~ allowed)"), "show_hidden": b("Include dotfiles"), "limit": i("Max entries (default 200)")},
      ["path"])
def fs_list(ctx: Context, path: str, show_hidden: bool = False, limit: int = 200) -> list[dict]:
    p = resolve(path)
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    out = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name.startswith(".") and not show_hidden:
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        out.append({"name": child.name + ("/" if child.is_dir() else ""), "size": _describe_size(child),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        if len(out) >= limit:
            break
    return out


@tool("fs_read", "Read a text file (UTF-8, undecodable bytes replaced). Use offset/limit for large files.",
      {"path": s("File path"), "offset": i("Character offset to start at"), "limit": i("Max characters (default 20000)")},
      ["path"])
def fs_read(ctx: Context, path: str, offset: int = 0, limit: int = 20000) -> str:
    p = resolve(path)
    if not p.is_file():
        raise ToolError(f"Not a file: {p}")
    with p.open("r", encoding="utf-8", errors="replace") as f:
        text = f.read(TEXT_LIMIT + offset)
    chunk = text[offset:offset + min(limit, TEXT_LIMIT)]
    more = " [more content follows]" if len(text) > offset + len(chunk) else ""
    return chunk + more


@tool("fs_info", "Get metadata about a file or folder (size, times, type, item count).", {"path": s("Path")}, ["path"])
def fs_info(ctx: Context, path: str) -> dict:
    p = resolve(path)
    if not p.exists():
        raise ToolError(f"Does not exist: {p}")
    st = p.stat()
    info = {"path": str(p), "type": "dir" if p.is_dir() else "file", "size": _describe_size(p),
            "modified": time.ctime(st.st_mtime), "created": time.ctime(st.st_ctime)}
    if p.is_dir():
        info["items"] = sum(1 for _ in p.iterdir())
    return info


@tool("fs_search", "Find files under a folder by name pattern (glob, e.g. '*.pdf') and/or text they contain.",
      {"root": s("Folder to search"), "name": s("Glob for the file name, e.g. '*.py'"),
       "contains": s("Only files whose text contains this (case-insensitive)"), "limit": i("Max results (default 50)")},
      ["root"])
def fs_search(ctx: Context, root: str, name: str = "*", contains: str = "", limit: int = 50) -> list[str]:
    base = resolve(root)
    if not base.is_dir():
        raise ToolError(f"Not a directory: {base}")
    hits: list[str] = []
    needle = contains.lower()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for fn in filenames:
            if not fnmatch.fnmatch(fn.lower(), name.lower()):
                continue
            fp = Path(dirpath) / fn
            if needle:
                try:
                    if fp.stat().st_size > 5_000_000 or needle not in fp.read_text(errors="ignore").lower():
                        continue
                except OSError:
                    continue
            hits.append(str(fp))
            if len(hits) >= limit:
                return hits
    return hits


@tool("fs_write", "Create a text file, or overwrite it when overwrite=true (the old version is backed up). Parent folders are created.",
      {"path": s("File path"), "content": s("Full file content"), "overwrite": b("Allow replacing an existing file")},
      ["path", "content"], risk="modify",
      describe=lambda a: f"Write file {a.get('path')} ({len(a.get('content', ''))} chars, overwrite={a.get('overwrite', False)})")
def fs_write(ctx: Context, path: str, content: str, overwrite: bool = False) -> str:
    p = resolve(path)
    if p.exists():
        if not overwrite:
            raise ToolError(f"{p} already exists. Pass overwrite=true to replace it.")
        if p.is_dir():
            raise ToolError(f"{p} is a directory.")
        _backup(ctx, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


@tool("fs_append", "Append text to the end of a file (created if missing).",
      {"path": s("File path"), "content": s("Text to append")}, ["path", "content"], risk="modify",
      describe=lambda a: f"Append {len(a.get('content', ''))} chars to {a.get('path')}")
def fs_append(ctx: Context, path: str, content: str) -> str:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended to {p}"


@tool("fs_edit", "Modify a file by replacing exact text. `old` must match exactly once unless replace_all=true. The old version is backed up.",
      {"path": s("File path"), "old": s("Exact text to replace"), "new": s("Replacement text"), "replace_all": b("Replace every occurrence")},
      ["path", "old", "new"], risk="modify",
      describe=lambda a: f"Edit {a.get('path')}: replace {a.get('old', '')[:200]!r} → {a.get('new', '')[:200]!r}")
def fs_edit(ctx: Context, path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = resolve(path)
    if not p.is_file():
        raise ToolError(f"Not a file: {p}")
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ToolError("`old` text not found in file.")
    if count > 1 and not replace_all:
        raise ToolError(f"`old` matches {count} places; make it more specific or pass replace_all=true.")
    _backup(ctx, p)
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
    return f"Replaced {count if replace_all else 1} occurrence(s) in {p}"


@tool("fs_mkdir", "Create a folder (and parents).", {"path": s("Folder path")}, ["path"], risk="modify")
def fs_mkdir(ctx: Context, path: str) -> str:
    p = resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {p}"


@tool("fs_copy", "Copy a file or folder to a new location (copy/paste). Fails if the target exists unless overwrite=true.",
      {"src": s("Source path"), "dst": s("Destination path (a file path, or an existing folder to copy into)"),
       "overwrite": b("Replace an existing target (a backup is kept for files)")},
      ["src", "dst"], risk="modify", describe=lambda a: f"Copy {a.get('src')} → {a.get('dst')}")
def fs_copy(ctx: Context, src: str, dst: str, overwrite: bool = False) -> str:
    a, d = resolve(src), resolve(dst)
    if not a.exists():
        raise ToolError(f"Source does not exist: {a}")
    if d.is_dir() and not a.samefile(d):
        d = d / a.name
    if d.exists():
        if not overwrite:
            raise ToolError(f"Target exists: {d}. Pass overwrite=true to replace.")
        if d.is_file():
            _backup(ctx, d)
        elif d.is_dir():
            raise ToolError("Refusing to overwrite an existing folder; choose a new name.")
    d.parent.mkdir(parents=True, exist_ok=True)
    if a.is_dir():
        shutil.copytree(a, d)
    else:
        shutil.copy2(a, d)
    return f"Copied {a} → {d}"


@tool("fs_move", "Move a file or folder (cut/paste). Also renames when the destination has a different name.",
      {"src": s("Source path"), "dst": s("Destination path, or existing folder to move into"), "overwrite": b("Replace an existing file")},
      ["src", "dst"], risk="modify", describe=lambda a: f"Move {a.get('src')} → {a.get('dst')}")
def fs_move(ctx: Context, src: str, dst: str, overwrite: bool = False) -> str:
    a, d = resolve(src), resolve(dst)
    if not a.exists():
        raise ToolError(f"Source does not exist: {a}")
    if is_protected(a):
        raise ToolError(f"Refusing to move protected location {a}")
    if d.is_dir():
        d = d / a.name
    if d.exists():
        if not overwrite or d.is_dir():
            raise ToolError(f"Target exists: {d}")
        _backup(ctx, d)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(a), str(d))
    return f"Moved {a} → {d}"


@tool("fs_rename", "Rename a file or folder in place.", {"path": s("Current path"), "new_name": s("New name only (no directories)")},
      ["path", "new_name"], risk="modify", describe=lambda a: f"Rename {a.get('path')} → {a.get('new_name')}")
def fs_rename(ctx: Context, path: str, new_name: str) -> str:
    p = resolve(path)
    if not p.exists():
        raise ToolError(f"Does not exist: {p}")
    if is_protected(p):
        raise ToolError(f"Refusing to rename protected location {p}")
    if os.sep in new_name or (os.altsep and os.altsep in new_name) or new_name in ("", ".", ".."):
        raise ToolError("new_name must be a plain name; use fs_move to change directories.")
    target = p.with_name(new_name)
    if target.exists():
        raise ToolError(f"{target} already exists")
    p.rename(target)
    return f"Renamed to {target}"


@tool("fs_delete", "Delete a file or folder. It is moved to M.R.X.'s trash (recoverable with fs_trash_restore), not erased.",
      {"path": s("Path to delete")}, ["path"], risk="destructive",
      describe=lambda a: f"DELETE {a.get('path')} (moved to recoverable trash)")
def fs_delete(ctx: Context, path: str) -> str:
    p = resolve(path)
    if not p.exists() and not p.is_symlink():
        raise ToolError(f"Does not exist: {p}")
    if is_protected(p):
        raise ToolError(f"Refusing to delete protected location {p}")
    entry = _stash(ctx, p, "deleted")
    shutil.move(str(p), str(entry / p.name))
    return f"Moved {p} to trash as '{entry.name}'"


@tool("fs_trash_list", "List items in the recoverable trash.", {})
def fs_trash_list(ctx: Context) -> list[dict]:
    out = []
    for entry in sorted(ctx.config.trash_dir.iterdir(), reverse=True):
        meta_file = entry / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            out.append({"id": entry.name, "original": meta["original"], "reason": meta["reason"]})
    return out


@tool("fs_trash_restore", "Restore an item from the trash to its original location (or a given one).",
      {"id": s("Trash id from fs_trash_list"), "to": s("Optional alternative destination path")}, ["id"], risk="modify")
def fs_trash_restore(ctx: Context, id: str, to: str = "") -> str:
    entry = ctx.config.trash_dir / id
    if not entry.is_dir() or Path(id).name != id or not (entry / "meta.json").exists():
        raise ToolError(f"No such trash entry: {id}")
    meta = json.loads((entry / "meta.json").read_text())
    dst = resolve(to) if to else Path(meta["original"])
    item = next(c for c in entry.iterdir() if c.name != "meta.json")
    if dst.exists():
        raise ToolError(f"Destination exists: {dst}. Provide a different `to` path.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(item), str(dst))
    shutil.rmtree(entry)
    return f"Restored to {dst}"

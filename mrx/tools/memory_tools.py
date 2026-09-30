"""Long-term memory and drafts inspection."""
from __future__ import annotations

import time

from .registry import Context, ToolError, i, s, tool


def _fmt(f: dict) -> dict:
    return {"id": f["id"], "text": f["text"], "tags": f["tags"], "saved": time.strftime("%Y-%m-%d", time.localtime(f["created"]))}


@tool("memory_save", "Remember a durable fact or preference about the user, their world, or a standing instruction "
                     "(e.g. name, family, preferences, projects, accounts). Save what will matter in future conversations; skip trivia.",
      {"text": s("The fact, as a self-contained sentence"), "tags": s("Optional comma-separated tags")}, ["text"], risk="modify")
def memory_save(ctx: Context, text: str, tags: str = "") -> dict:
    return {"id": ctx.memory.remember(text, tags), "saved": text}


@tool("memory_search", "Search long-term memory for facts relevant to a topic.", {"query": s("Topic or keywords"), "limit": i("Max results")}, ["query"])
def memory_search(ctx: Context, query: str, limit: int = 8) -> list[dict]:
    return [_fmt(f) for f in ctx.memory.recall(query, limit)]


@tool("memory_list", "List everything in long-term memory (most recently updated first).", {"limit": i("Max (default 100)")})
def memory_list(ctx: Context, limit: int = 100) -> list[dict]:
    return [_fmt(f) for f in ctx.memory.all_facts(limit)]


@tool("memory_update", "Correct an existing memory by id.", {"id": i("Memory id"), "text": s("New text")}, ["id", "text"], risk="modify")
def memory_update(ctx: Context, id: int, text: str) -> str:
    if not ctx.memory.update_fact(id, text):
        raise ToolError(f"No memory #{id}")
    return "Updated"


@tool("memory_forget", "Delete a memory by id.", {"id": i("Memory id")}, ["id"], risk="modify")
def memory_forget(ctx: Context, id: int) -> str:
    if not ctx.memory.forget(id):
        raise ToolError(f"No memory #{id}")
    return "Forgotten"


@tool("conversation_search", "Search the log of past conversations (all words must appear).", {"query": s("Words to look for")}, ["query"])
def conversation_search(ctx: Context, query: str) -> list[dict]:
    return [{"when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])), "role": r["role"], "text": r["text"][:500]} for r in ctx.memory.search_log(query)]


@tool("drafts_list", "List saved drafts (email and social), newest first.", {"kind": s("Optional: email or social")})
def drafts_list(ctx: Context, kind: str = "") -> list[dict]:
    return ctx.memory.list_drafts(kind or None)


@tool("draft_get", "Show a saved draft in full.", {"id": i("Draft id")}, ["id"])
def draft_get(ctx: Context, id: int) -> dict:
    d = ctx.memory.get_draft(id)
    if not d:
        raise ToolError(f"No draft #{id}")
    return d


@tool("draft_delete", "Discard a saved draft.", {"id": i("Draft id")}, ["id"], risk="modify")
def draft_delete(ctx: Context, id: int) -> str:
    if not ctx.memory.delete_draft(id):
        raise ToolError(f"No draft #{id}")
    return "Draft deleted"

"""Tool registry: declarative tools with risk levels, gated execution, and result shaping."""
from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import Config
from ..memory import Memory
from ..safety import RISKS, needs_confirmation

Emit = Callable[[dict], Awaitable[None]]
Confirm = Callable[[str, str, str], Awaitable[bool]]  # (tool, risk, description) -> approved


@dataclass
class Image:
    """A tool result that is an image (screenshot etc.)."""
    data_b64: str
    media_type: str = "image/jpeg"
    caption: str = ""


@dataclass
class Context:
    """Everything a tool may touch. One per agent turn."""
    config: Config
    memory: Memory
    emit: Emit
    confirm: Confirm
    services: dict[str, Any] = field(default_factory=dict)
    interactive: bool = True     # False for scheduled/unattended runs
    tainted: bool = False        # set once untrusted content was read this turn

    def service(self, name: str) -> Any:
        return self.services[name]


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    required: list[str]
    risk: str
    fn: Callable[..., Any]
    untrusted: bool = False
    describe: Callable[..., str] | None = None
    sandboxed: bool = False   # effects stay inside M.R.X.'s own browser/UI; not escalated by taint

    def api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {"type": "object", "properties": self.properties, "required": self.required},
        }


@dataclass
class Outcome:
    content: Any            # str or list of content blocks
    is_error: bool = False


REGISTRY: dict[str, Tool] = {}


# -- schema helpers ----------------------------------------------------------
def s(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}


def i(desc: str, **kw: Any) -> dict:
    return {"type": "integer", "description": desc, **kw}


def n(desc: str, **kw: Any) -> dict:
    return {"type": "number", "description": desc, **kw}


def b(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def enum(desc: str, *values: str) -> dict:
    return {"type": "string", "description": desc, "enum": list(values)}


def tool(
    name: str,
    description: str,
    props: dict[str, dict] | None = None,
    required: list[str] | None = None,
    risk: str = "safe",
    untrusted: bool = False,
    describe: Callable[..., str] | None = None,
    sandboxed: bool = False,
) -> Callable:
    if risk not in RISKS:
        raise ValueError(f"unknown risk {risk!r} for tool {name}")

    def deco(fn: Callable) -> Callable:
        if name in REGISTRY:
            raise ValueError(f"duplicate tool {name}")
        REGISTRY[name] = Tool(name, description, props or {}, required or [], risk, fn, untrusted, describe, sandboxed)
        return fn

    return deco


def api_tools() -> list[dict]:
    """Tool list for the API. Sorted so the prompt-cache prefix is stable between requests."""
    return [REGISTRY[k].api_schema() for k in sorted(REGISTRY)]


def _default_describe(t: Tool, args: dict) -> str:
    shown = json.dumps(args, ensure_ascii=False, indent=2)
    if len(shown) > 1500:
        shown = shown[:1500] + "\n... (truncated)"
    return f"{t.name}\n{shown}"


def _describe(t: Tool, args: dict, ctx: Context) -> str:
    if t.describe is None:
        return _default_describe(t, args)
    try:
        # describe(args) or describe(args, ctx) - the latter can look things up (e.g. a stored draft)
        if len(inspect.signature(t.describe).parameters) >= 2:
            return t.describe(args, ctx)
        return t.describe(args)
    except Exception:  # noqa: BLE001 - never let a describe bug block the safety prompt
        return _default_describe(t, args)


def _shape(result: Any, limit: int) -> Any:
    """Turn a tool's return value into tool_result content."""
    if isinstance(result, Image):
        blocks: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": result.media_type, "data": result.data_b64}}]
        if result.caption:
            blocks.append({"type": "text", "text": result.caption})
        return blocks
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
    return text or "(ok)"


async def execute(name: str, args: dict, ctx: Context) -> Outcome:
    t = REGISTRY.get(name)
    if t is None:
        return Outcome(f"Unknown tool {name!r}. Available: {', '.join(sorted(REGISTRY))}", True)
    if not isinstance(args, dict):
        return Outcome("Tool input must be a JSON object.", True)
    missing = [r for r in t.required if r not in args]
    if missing:
        return Outcome(f"Missing required argument(s): {', '.join(missing)}", True)
    unknown = [k for k in args if k not in t.properties]
    if unknown:
        return Outcome(f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(t.properties) or 'none'}", True)

    if needs_confirmation(t.risk, ctx.config.autonomy, ctx.tainted and not t.sandboxed):
        if not ctx.interactive:
            return Outcome(f"Denied: '{name}' ({t.risk}) needs approval and this is an unattended run.", True)
        desc = _describe(t, args, ctx)
        if ctx.tainted and not t.sandboxed and t.risk in ("modify", "exec"):
            desc = "⚠ This turn read untrusted content (web/email/news). Approve only if YOU asked for this.\n" + desc
        if not await ctx.confirm(name, t.risk, desc):
            return Outcome("The user declined this action. Do not retry it; ask what they would prefer.", True)

    try:
        if inspect.iscoroutinefunction(t.fn):
            result = await t.fn(ctx, **args)
        else:
            result = await asyncio.to_thread(t.fn, ctx, **args)
    except asyncio.CancelledError:
        raise
    except ToolError as e:
        return Outcome(str(e), True)
    except Exception as e:  # noqa: BLE001 - surface any tool failure to the model
        tb = traceback.format_exc(limit=3)
        return Outcome(f"{type(e).__name__}: {e}\n{tb}", True)

    if t.untrusted:
        ctx.tainted = True
    return Outcome(_shape(result, ctx.config.tool_result_chars))


class ToolError(Exception):
    """Expected, model-readable failure (bad path, service not configured, ...)."""

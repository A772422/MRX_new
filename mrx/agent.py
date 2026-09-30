"""The agent brain: a streaming tool-use loop over the Claude API, with memory and safety gating."""
from __future__ import annotations

import asyncio
import json
import platform
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import anthropic

from .config import Config
from .memory import Memory
from .prompts import SYSTEM_PROMPT
from .tools import REGISTRY, Context, api_tools, execute

Emit = Callable[[dict], Awaitable[None]]


class ClaudeLLM:
    """One streamed model turn. Kept behind a tiny interface so the loop can be tested without the network."""

    def __init__(self, config: Config, client: anthropic.AsyncAnthropic | None = None):
        self.config = config
        self.client = client or anthropic.AsyncAnthropic()

    def _params(self, messages: list, tools: list[dict]) -> dict[str, Any]:
        return dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            cache_control={"type": "ephemeral"},  # caches tools + system + the growing conversation prefix
        )

    async def turn(self, messages: list, on_event: Callable[[str, Any], Awaitable[None]], tools_enabled: bool = True) -> Any:
        params = self._params(messages, api_tools())
        if not tools_enabled:
            params["tool_choice"] = {"type": "none"}
        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                t = event.type
                if t == "text":
                    await on_event("text", event.text)
                elif t == "content_block_start":
                    cb = event.content_block
                    await on_event("block", cb.type if cb.type != "tool_use" else f"tool:{cb.name}")
            return await stream.get_final_message()


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _block_chars(block: Any) -> int:
    """Rough size used to decide when to roll the conversation over. Images are costed as ~6k chars."""
    t = _get(block, "type")
    if t == "text":
        return len(_get(block, "text", "") or "")
    if t == "tool_use":
        return len(json.dumps(_get(block, "input", {}), default=str)) + 80
    if t == "tool_result":
        c = _get(block, "content", "")
        return len(c) if isinstance(c, str) else sum(_block_chars(x) for x in c)
    if t == "image":
        return 6000
    return 200  # thinking etc.


class Agent:
    def __init__(self, config: Config, memory: Memory, services: dict[str, Any], llm: Any | None = None):
        self.config, self.memory, self.services = config, memory, services
        self.llm = llm or ClaudeLLM(config)
        self.messages: list[Any] = []
        self._chars = 0
        self._summary: str | None = None
        self.lock = asyncio.Lock()

    # -- conversation bookkeeping ---------------------------------------------
    def _append(self, role: str, content: Any) -> None:
        self.messages.append({"role": role, "content": content})
        self._chars += len(content) if isinstance(content, str) else sum(_block_chars(b) for b in content)

    def _repair(self) -> None:
        """If an earlier turn was interrupted between a tool_use and its tool_result, close it so the API accepts the history."""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        content = self.messages[-1]["content"]
        pending = [_get(b, "id") for b in content if _get(b, "type") == "tool_use"] if isinstance(content, list) else []
        if pending:
            self._append("user", [{"type": "tool_result", "tool_use_id": i, "content": "Interrupted before this ran.", "is_error": True} for i in pending])

    def reset(self) -> None:
        self.messages, self._chars, self._summary = [], 0, None

    async def _rollover(self, emit: Emit) -> None:
        """Conversation is getting long: summarise it, start a fresh one seeded with the summary (memory persists anyway)."""
        await emit({"type": "status", "text": "Compacting conversation…"})
        self._repair()
        prompt = ("Write a compact briefing of this conversation so a fresh session can continue seamlessly: the user's goals, decisions made, "
                  "work completed and its results (file paths, ids, draft ids), anything still pending, and preferences you learned. Plain text, no preamble.")
        msgs = self.messages + [{"role": "user", "content": prompt}]
        try:
            resp = await self.llm.turn(msgs, _noop, tools_enabled=False)
            text = "".join(_get(b, "text", "") for b in resp.content if _get(b, "type") == "text").strip()
        except Exception:  # noqa: BLE001 - fall back to a plain restart rather than blocking the user
            text = ""
        self.reset()
        self._summary = text or None

    # -- context header ---------------------------------------------------------
    def _context_block(self, user_text: str) -> str:
        now = time.strftime("%A %Y-%m-%d %H:%M %Z")
        lines = [f"Now: {now} | OS: {platform.system()} | Home: {Path.home()}"]
        facts = self.memory.recall(user_text, 6)
        if facts:
            lines.append("Relevant memories:\n" + "\n".join(f"- (#{f['id']}) {f['text']}" for f in facts))
        if self._summary:
            lines.append("Earlier in this conversation (summary):\n" + self._summary)
            self._summary = None
        return "<context>\n" + "\n".join(lines) + "\n</context>"

    # -- the loop -----------------------------------------------------------------
    async def run_turn(self, user_text: str, emit: Emit, confirm: Callable[[str, str, str], Awaitable[bool]], interactive: bool = True) -> str:
        async with self.lock:
            self._repair()
            if self._chars > self.config.max_context_chars:
                await self._rollover(emit)
            ctx = Context(self.config, self.memory, emit, confirm, self.services, interactive=interactive)
            self.memory.log("user" if interactive else "scheduled", user_text)
            self._append("user", [{"type": "text", "text": self._context_block(user_text)}, {"type": "text", "text": user_text}])
            await emit({"type": "turn_start"})
            final_text = ""
            try:
                for _ in range(self.config.max_agent_steps):
                    seg: list[str] = []
                    n_text_blocks = 0

                    async def on_event(kind: str, data: Any) -> None:
                        nonlocal n_text_blocks
                        if kind == "text":
                            seg.append(data)
                            await emit({"type": "assistant_delta", "text": data})
                        elif data == "thinking":
                            await emit({"type": "status", "text": "Thinking…"})
                        elif data == "text":
                            if n_text_blocks:
                                await emit({"type": "assistant_break"})
                            n_text_blocks += 1
                        elif isinstance(data, str) and data.startswith("tool:"):
                            await emit({"type": "status", "text": f"Preparing {data[5:]}…"})

                    resp = await self.llm.turn(self.messages, on_event)
                    self._append("assistant", list(resp.content))
                    final_text = "".join(seg) or final_text
                    stop = resp.stop_reason

                    if stop == "tool_use":
                        results = []
                        for block in resp.content:
                            if _get(block, "type") != "tool_use":
                                continue
                            results.append(await self._run_tool(block, ctx, emit))
                        self._append("user", results)  # all results in ONE user message
                        continue
                    if stop == "pause_turn":
                        continue
                    if stop == "refusal":
                        await emit({"type": "error", "text": "The model declined to continue with that request."})
                    elif stop == "max_tokens":
                        await emit({"type": "notice", "text": "The reply hit the length limit and was cut short. Ask me to continue."})
                    break
                else:
                    await emit({"type": "notice", "text": f"Stopped after {self.config.max_agent_steps} steps. Say 'continue' to keep going."})
            except asyncio.CancelledError:
                self._repair()
                await emit({"type": "notice", "text": "Stopped."})
                raise
            except anthropic.AuthenticationError:
                await emit({"type": "error", "text": "Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY in .env and restart."})
            except anthropic.RateLimitError:
                await emit({"type": "error", "text": "Rate limited by the API. Try again in a moment."})
            except anthropic.APIConnectionError:
                await emit({"type": "error", "text": "Can't reach the Anthropic API. Check your internet connection."})
            except anthropic.APIStatusError as e:
                await emit({"type": "error", "text": f"API error {e.status_code}: {getattr(e, 'message', e)}"})
            finally:
                self._repair()
                if final_text:
                    self.memory.log("assistant", final_text)
                await emit({"type": "assistant_done", "text": final_text})
                await emit({"type": "turn_end"})
            return final_text

    async def _run_tool(self, block: Any, ctx: Context, emit: Emit) -> dict:
        name, args, tid = _get(block, "name"), _get(block, "input") or {}, _get(block, "id")
        tool = REGISTRY.get(name)
        await emit({"type": "tool_call", "id": tid, "name": name, "input": _short(args), "risk": tool.risk if tool else "?"})
        outcome = await execute(name, args, ctx)
        preview = outcome.content if isinstance(outcome.content, str) else "[image]"
        await emit({"type": "tool_result", "id": tid, "name": name, "ok": not outcome.is_error, "preview": preview[:400]})
        res: dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": outcome.content}
        if outcome.is_error:
            res["is_error"] = True
        return res


def _short(args: Any, limit: int = 300) -> Any:
    if isinstance(args, dict):
        return {k: (v[:limit] + "…" if isinstance(v, str) and len(v) > limit else v) for k, v in args.items()}
    return args


async def _noop(*_: Any) -> None:
    return None

"""Exercise the real ClaudeLLM + Agent code path against a mocked HTTP transport that replays a real-shaped SSE stream."""
import json

import anthropic
import httpx2

from mrx.agent import Agent, ClaudeLLM

from conftest import Recorder


def sse(*events):
    return "".join(f"event: {t}\ndata: {json.dumps({'type': t, **d})}\n\n" for t, d in events).encode()


def message_start():
    return ("message_start", {"message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                           "usage": {"input_tokens": 10, "output_tokens": 1}}})


def text_turn(*chunks):
    ev = [message_start(), ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})]
    ev += [("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": c}}) for c in chunks]
    ev += [("content_block_stop", {"index": 0}), ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}), ("message_stop", {})]
    return sse(*ev)


def tool_turn(tool_id, name, args):
    raw = json.dumps(args)
    ev = [message_start(),
          ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Checking. "}}), ("content_block_stop", {"index": 0}),
          ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}),
          ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[:8]}}),
          ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[8:]}}),
          ("content_block_stop", {"index": 1}),
          ("message_delta", {"delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 20}}), ("message_stop", {})]
    return sse(*ev)


def make_llm(config, bodies, captured):
    queue = list(bodies)

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=queue.pop(0))

    client = anthropic.AsyncAnthropic(api_key="test-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    return ClaudeLLM(config, client)


async def test_request_shape_and_streaming(config):
    captured = []
    llm = make_llm(config, [text_turn("Hel", "lo ", "world")], captured)
    got = []

    async def on_event(kind, data):
        got.append((kind, data))
    msg = await llm.turn([{"role": "user", "content": "hi"}], on_event)
    body = captured[0]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "M.R.X." in body["system"][0]["text"]
    names = [t["name"] for t in body["tools"]]
    assert names == sorted(names) and "fs_delete" in names and "radar_scan" in names
    assert "temperature" not in body and "budget_tokens" not in json.dumps(body)       # removed on these models
    assert [d for k, d in got if k == "text"] == ["Hel", "lo ", "world"]
    assert msg.stop_reason == "end_turn" and msg.content[0].text == "Hello world"


async def test_full_agent_turn_on_real_sdk_path(config, memory, home):
    (home / "note.txt").write_text("remember the milk")
    captured = []
    llm = make_llm(config, [tool_turn("toolu_1", "fs_read", {"path": "note.txt"}), text_turn("It says: remember the milk.")], captured)
    agent = Agent(config, memory, {}, llm)
    rec = Recorder()
    out = await agent.run_turn("read note.txt", rec.emit, rec.confirm)
    assert out.endswith("It says: remember the milk.")
    # second request carries the assistant tool_use and a single user message of tool_results
    msgs = captured[1]["messages"]
    assert msgs[1]["role"] == "assistant" and any(b["type"] == "tool_use" and b["id"] == "toolu_1" and b["input"] == {"path": "note.txt"} for b in msgs[1]["content"])
    assert msgs[2]["role"] == "user" and msgs[2]["content"][0]["type"] == "tool_result" and "milk" in msgs[2]["content"][0]["content"]
    assert [e["type"] for e in rec.events if e["type"] in ("tool_call", "tool_result")] == ["tool_call", "tool_result"]


async def test_auth_error_is_reported_not_raised(config, memory):
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    client = anthropic.AsyncAnthropic(api_key="bad", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    agent = Agent(config, memory, {}, ClaudeLLM(config, client))
    rec = Recorder()
    await agent.run_turn("hi", rec.emit, rec.confirm)
    assert any(e["type"] == "error" and "API key" in e["text"] for e in rec.events)
    assert rec.events[-1]["type"] == "turn_end"

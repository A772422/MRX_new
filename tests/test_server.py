import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mrx.config import Config
from mrx.server import create_app

from conftest import FakeLLM, reply, text, tool_use


@pytest.fixture
def app_and_token(config):
    llm = FakeLLM(reply(text("Hello from MRX")))
    app = create_app(config, llm)
    return app, app.state.token, llm


HOST = {"Host": "127.0.0.1:8765"}   # TestClient's websocket handshake always says "testserver" otherwise


def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_locked_without_token(app_and_token):
    app, token, _ = app_and_token
    with client(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/memory").status_code == 401
        assert c.get("/", params={"token": "wrong"}).status_code == 401


def test_token_login_sets_cookie_and_serves_ui(app_and_token):
    app, token, _ = app_and_token
    with client(app) as c:
        r = c.get("/", params={"token": token}, follow_redirects=True)
        assert r.status_code == 200 and "M.R.X." in r.text
        assert c.get("/api/status").json()["tools"] >= 60          # cookie now authenticates
        assert c.get("/static/app.js").status_code == 200
        assert c.get("/static/vendor/leaflet/leaflet.js").status_code == 200


def test_bearer_header_works(app_and_token):
    app, token, _ = app_and_token
    with client(app) as c:
        assert c.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_dns_rebinding_host_rejected(app_and_token):
    app, token, _ = app_and_token
    with TestClient(app, base_url="http://evil.example.com") as c:
        assert c.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_cross_origin_post_rejected(app_and_token):
    app, token, _ = app_and_token
    with client(app) as c:
        h = {"Authorization": f"Bearer {token}"}
        assert c.post("/api/autonomy/trusted", headers={**h, "Origin": "http://evil.example.com"}).status_code == 403
        assert c.post("/api/autonomy/trusted", headers={**h, "Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert c.post("/api/autonomy/bogus", headers=h).status_code == 400


def test_websocket_requires_token_and_same_origin(app_and_token):
    app, token, _ = app_and_token
    with client(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers=HOST):
                pass
        with pytest.raises(WebSocketDisconnect):        # right token, wrong origin (cross-site hijack attempt)
            with c.websocket_connect(f"/ws?token={token}", headers={**HOST, "Origin": "http://evil.example.com"}):
                pass
        with pytest.raises(WebSocketDisconnect):        # right token, rebinding host
            with c.websocket_connect(f"/ws?token={token}", headers={"Host": "evil.example.com"}):
                pass
        with c.websocket_connect(f"/ws?token={token}", headers={**HOST, "Origin": "http://127.0.0.1:8765"}) as ws:   # legit browser
            assert ws.receive_json()["type"] == "hello"


def test_websocket_chat_roundtrip(app_and_token):
    app, token, llm = app_and_token
    with client(app) as c:
        with c.websocket_connect(f"/ws?token={token}", headers=HOST) as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"type": "user", "text": "hi"})
            seen = []
            while True:
                e = ws.receive_json()
                seen.append(e["type"])
                if e["type"] == "turn_end":
                    break
            assert seen[0] == "user_echo" and "assistant_delta" in seen and "assistant_done" in seen


def test_confirmation_flow_over_websocket(config, home):
    (home / "victim.txt").write_text("x")
    llm = FakeLLM(reply(tool_use("d", "fs_delete", {"path": "victim.txt"}), stop="tool_use"), reply(text("deleted")))
    app = create_app(config, llm)
    with client(app) as c, c.websocket_connect(f"/ws?token={app.state.token}", headers=HOST) as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "delete it"})
        while (e := ws.receive_json())["type"] != "confirm_request":
            pass
        assert e["risk"] == "destructive" and "victim.txt" in e["description"]
        assert (home / "victim.txt").exists()               # nothing happens before approval
        ws.send_json({"type": "confirm", "id": e["id"], "approved": True})
        while ws.receive_json()["type"] != "turn_end":
            pass
        assert not (home / "victim.txt").exists()


def test_no_ui_connected_means_denied(config, home):
    """If nobody can be asked, risky actions are denied rather than silently allowed."""
    import asyncio
    from mrx.server import Hub
    hub = Hub(config)
    assert asyncio.run(hub.confirm("fs_delete", "destructive", "x")) is False


def test_autonomy_validation():
    with pytest.raises(ValueError):
        Config(autonomy="yolo")


def _slow_tool():
    import asyncio
    from mrx.tools.registry import tool

    @tool("t_srv_slow", "slow")
    async def slow(ctx):
        await asyncio.sleep(60)
    return slow


def test_stop_button_cancels_turn_and_next_message_works(config):
    from mrx.tools import REGISTRY
    _slow_tool()
    try:
        llm = FakeLLM(reply(tool_use("s", "t_srv_slow", {}), stop="tool_use"), reply(text("second answer")))
        app = create_app(config, llm)
        with client(app) as c, c.websocket_connect(f"/ws?token={app.state.token}", headers=HOST) as ws:
            ws.receive_json()
            ws.send_json({"type": "user", "text": "go slow"})
            while ws.receive_json()["type"] != "tool_call":
                pass
            ws.send_json({"type": "stop"})
            types = []
            while (e := ws.receive_json())["type"] != "turn_end":
                types.append(e["type"])
            assert "notice" in types
            ws.send_json({"type": "user", "text": "again"})
            while (e := ws.receive_json())["type"] != "assistant_done":
                pass
            assert e["text"] == "second answer"
    finally:
        REGISTRY.pop("t_srv_slow", None)


async def test_shutdown_mid_turn_does_not_hang(config):
    import asyncio
    from mrx.server import Runtime
    from mrx.tools import REGISTRY
    _slow_tool()
    try:
        rt = Runtime(config, FakeLLM(reply(tool_use("s", "t_srv_slow", {}), stop="tool_use")))
        await rt.start()
        await rt.queue.put("go")
        for _ in range(100):                   # wait until the slow tool is running
            await asyncio.sleep(0.02)
            if rt.current and rt.agent.messages and len(rt.agent.messages) >= 2:
                break
        await asyncio.wait_for(rt.stop(), 5)
    finally:
        REGISTRY.pop("t_srv_slow", None)

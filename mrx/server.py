"""HTTP + WebSocket server: the UI's link to the agent."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent import Agent
from .config import AUTONOMY_MODES, Config
from .memory import Memory
from .tools import REGISTRY, news, world
from .tools.automation import next_run
from .tools.browser import BrowserService
from .tools.radar import run_scan
from .tools.social import configured as social_configured

WEB = Path(__file__).parent / "web"
COOKIE = "mrx_token"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def load_token(config: Config) -> str:
    p = config.token_path
    if p.exists() and p.read_text().strip():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(24)
    p.write_text(tok)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return tok


class Hub:
    """Fan-out of events to every connected UI, plus the approval-prompt broker."""

    def __init__(self, config: Config):
        self.config = config
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, asyncio.Future] = {}

    async def broadcast(self, event: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(event, default=str))
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def confirm(self, tool: str, risk: str, description: str) -> bool:
        if not self.clients:
            return False  # nobody to ask -> deny
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.broadcast({"type": "confirm_request", "id": cid, "tool": tool, "risk": risk, "description": description,
                              "timeout": self.config.confirm_timeout_s})
        try:
            return await asyncio.wait_for(fut, self.config.confirm_timeout_s)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(cid, None)
            await self.broadcast({"type": "confirm_resolved", "id": cid})

    def resolve(self, cid: str, approved: bool) -> None:
        fut = self.pending.get(cid)
        if fut and not fut.done():
            fut.set_result(bool(approved))


class Runtime:
    """Everything that lives for the life of the server."""

    def __init__(self, config: Config, llm: Any | None = None):
        self.config = config
        self.memory = Memory(config.db_path)
        self.hub = Hub(config)
        self.browser = BrowserService(config)
        self.services = {"browser": self.browser}
        self.llm = llm
        self.agent = Agent(config, self.memory, self.services, llm)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.current: asyncio.Task | None = None
        self._user_stopped = False
        self.tasks: list[asyncio.Task] = []
        self.radar_live: asyncio.Task | None = None

    async def start(self) -> None:
        self.tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._scheduler())]

    async def stop(self) -> None:
        for t in [*self.tasks, self.radar_live, self.current]:
            if t:
                t.cancel()
        for t in [*self.tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.browser.close()

    # -- interactive turns, one at a time ------------------------------------
    async def _worker(self) -> None:
        while True:
            text = await self.queue.get()
            self.current = asyncio.create_task(self.agent.run_turn(text, self.hub.broadcast, self.hub.confirm))
            try:
                await self.current
            except asyncio.CancelledError:
                if not self._user_stopped:        # the worker itself is being cancelled (shutdown)
                    raise
                self._user_stopped = False        # the user pressed Stop: carry on with the next message
            except Exception as e:  # noqa: BLE001
                await self.hub.broadcast({"type": "error", "text": f"Internal error: {type(e).__name__}: {e}"})
            finally:
                self.current = None

    def stop_turn(self) -> None:
        if self.current and not self.current.done():
            self._user_stopped = True
            self.current.cancel()
        while not self.queue.empty():
            self.queue.get_nowait()

    # -- scheduled, unattended tasks -------------------------------------------
    async def _scheduler(self) -> None:
        while True:
            await asyncio.sleep(15)
            for task in self.memory.due_tasks(time.time()):
                nxt = None if task["schedule"].startswith("at ") else next_run(task["schedule"], time.time())
                self.memory.finish_task(task["id"], "(running)", nxt)  # advance/disable first so it can't fire twice
                asyncio.create_task(self._run_task(task, nxt))

    async def _run_task(self, task: dict, nxt: float | None) -> None:
        agent = Agent(self.config, self.memory, self.services, self.llm)  # fresh context; never touches the main conversation
        async def deny(*_: Any) -> bool:
            return False
        try:
            text = await agent.run_turn(task["prompt"], self._quiet_emit, deny, interactive=False)
        except Exception as e:  # noqa: BLE001
            text = f"Task failed: {type(e).__name__}: {e}"
        self.memory.finish_task(task["id"], text, nxt)
        await self.hub.broadcast({"type": "notification", "title": f"Task: {task['name']}", "text": text[:1500]})

    async def _quiet_emit(self, event: dict) -> None:
        # Unattended runs only surface UI-side effects (map/news/radar...), not chat chatter.
        if event.get("type") not in ("assistant_delta", "assistant_break", "assistant_done", "turn_start", "turn_end", "status", "tool_call", "tool_result", "notice", "error"):
            await self.hub.broadcast(event)

    # -- live radar ---------------------------------------------------------------
    def set_radar_live(self, enabled: bool) -> None:
        if enabled and not (self.radar_live and not self.radar_live.done()):
            self.radar_live = asyncio.create_task(self._radar_loop())
        elif not enabled and self.radar_live:
            self.radar_live.cancel()
            self.radar_live = None

    async def _radar_loop(self) -> None:
        while True:
            try:
                res = await run_scan(self.memory, self.hub.broadcast, deep=False)
                for d in res["intruder_alerts"]:
                    await self.hub.broadcast({"type": "alert", "text": f"New device on your network: {d.get('hostname') or d.get('vendor') or d['mac']} ({d['ip']}) — {d['kind']}"})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await self.hub.broadcast({"type": "notice", "text": f"Radar: {e}"})
            await asyncio.sleep(45)


def create_app(config: Config | None = None, llm: Any | None = None) -> FastAPI:
    config = config or Config()
    token = load_token(config)
    rt = Runtime(config, llm)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="M.R.X.", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rt, app.state.token = rt, token

    def host_ok(host_header: str) -> bool:
        if config.host not in LOOPBACK:
            return True  # deliberately exposed on the LAN: the token is the gate
        name = host_header.rsplit(":", 1)[0].strip("[]") if not host_header.startswith("[") else host_header.split("]")[0].strip("[")
        return name in LOOPBACK

    def origin_ok(origin: str | None, host_header: str) -> bool:
        return origin is None or origin.split("://", 1)[-1] == host_header

    def authed(cookies: dict, headers: Any, query: dict) -> bool:
        supplied = cookies.get(COOKIE) or query.get("token") or (headers.get("authorization", "")[7:] if headers.get("authorization", "").lower().startswith("bearer ") else "")
        return bool(supplied) and hmac.compare_digest(supplied, token)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not host_ok(host):
            return PlainTextResponse("Bad host", status_code=403)
        if request.method not in ("GET", "HEAD") and not origin_ok(request.headers.get("origin"), host):
            return PlainTextResponse("Bad origin", status_code=403)
        if request.url.path == "/" and request.query_params.get("token"):
            if hmac.compare_digest(request.query_params["token"], token):
                resp = RedirectResponse("/")
                resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 90)
                return resp
            return PlainTextResponse("Invalid token", status_code=401)
        if not authed(request.cookies, request.headers, {}):
            return PlainTextResponse("M.R.X. is locked. Open the URL with ?token=… printed in the terminal where it started.", status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/status")
    async def status():
        g = config.get
        return {"version": __version__, "model": config.model, "effort": config.effort, "autonomy": config.autonomy, "autonomy_modes": AUTONOMY_MODES,
                "tools": len(REGISTRY), "email": bool(g("EMAIL_ADDRESS") and g("EMAIL_PASSWORD")), "social": social_configured(config),
                "youtube_api": bool(g("YOUTUBE_API_KEY")), "api_key": bool(g("ANTHROPIC_API_KEY")) or bool(g("ANTHROPIC_AUTH_TOKEN"))}

    @app.get("/api/news")
    async def api_news(topic: str = "world", lang: str = "en"):
        return await news.fetch_headlines(topic, lang, 30)

    @app.get("/api/geo/{layer}")
    async def api_geo(layer: str, query: str = "", bbox: str = ""):
        try:
            if layer == "quakes":
                return await world.quakes("day", "2.5")
            if layer == "events":
                return await world.natural_events()
            if layer == "news":
                return await world.news_geo(query)
            if layer == "flights":
                a, b_, c, d = (float(x) for x in bbox.split(","))
                return await world.flights(a, b_, c, d)
            if layer == "rain":
                return await world.rain_radar_tiles()
        except (ValueError, TypeError):
            raise HTTPException(400, "bbox must be lamin,lomin,lamax,lomax")
        except Exception as e:  # noqa: BLE001 - upstream feeds fail; the UI shows the message
            return JSONResponse({"error": str(e)}, status_code=502)
        raise HTTPException(404, "unknown layer")

    @app.get("/api/memory")
    async def api_memory():
        return rt.memory.all_facts(500)

    @app.delete("/api/memory/{fact_id}")
    async def api_forget(fact_id: int):
        return {"ok": rt.memory.forget(fact_id)}

    @app.get("/api/radar/history")
    async def api_radar_history():
        return rt.memory.devices()

    @app.post("/api/autonomy/{mode}")
    async def api_autonomy(mode: str):
        if mode not in AUTONOMY_MODES:
            raise HTTPException(400, f"mode must be one of {AUTONOMY_MODES}")
        config.autonomy = mode
        return {"autonomy": mode}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        host = ws.headers.get("host", "")
        if not (host_ok(host) and origin_ok(ws.headers.get("origin"), host) and authed(ws.cookies, ws.headers, dict(ws.query_params))):
            await ws.close(code=4401)
            return
        await ws.accept()
        rt.hub.clients.add(ws)
        await ws.send_text(json.dumps({"type": "hello", "version": __version__, "model": config.model, "autonomy": config.autonomy,
                                       "busy": rt.current is not None, "history": rt.memory.recent_log(30)}))
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                t = msg.get("type")
                if t == "user" and (text := str(msg.get("text", "")).strip()):
                    await rt.hub.broadcast({"type": "user_echo", "text": text})
                    await rt.queue.put(text)
                elif t == "stop":
                    rt.stop_turn()
                elif t == "confirm":
                    rt.hub.resolve(str(msg.get("id")), bool(msg.get("approved")))
                elif t == "reset":
                    rt.agent.reset()
                    await rt.hub.broadcast({"type": "notice", "text": "Started a fresh conversation (long-term memory kept)."})
                elif t == "radar_scan":
                    asyncio.create_task(_scan_and_report(rt))
                elif t == "radar_label":
                    rt.memory.label_device(str(msg.get("mac", "")).lower(), msg.get("label"), msg.get("trusted"))
                elif t == "radar_live":
                    rt.set_radar_live(bool(msg.get("enabled")))
        except WebSocketDisconnect:
            pass
        except json.JSONDecodeError:
            await ws.close(code=1003)
        finally:
            rt.hub.clients.discard(ws)

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


async def _scan_and_report(rt: Runtime) -> None:
    try:
        await run_scan(rt.memory, rt.hub.broadcast)
    except Exception as e:  # noqa: BLE001
        await rt.hub.broadcast({"type": "notice", "text": f"Radar: {e}"})

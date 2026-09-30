from types import SimpleNamespace

import pytest

from mrx.config import Config
from mrx.memory import Memory
from mrx.tools import Context


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MRX_HOME", str(tmp_path / "mrxhome"))
    return h


@pytest.fixture
def config(home):
    return Config()


@pytest.fixture
def memory(config):
    return Memory(config.db_path)


class Recorder:
    def __init__(self, approve=True):
        self.events, self.confirms, self.approve = [], [], approve

    async def emit(self, e):
        self.events.append(e)

    async def confirm(self, tool, risk, desc):
        self.confirms.append((tool, risk, desc))
        return self.approve


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def make_ctx(config, memory):
    def make(recorder=None, **kw):
        r = recorder or Recorder()
        return Context(config, memory, r.emit, r.confirm, {}, **kw), r
    return make


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()[0]


# -- fake model helpers --------------------------------------------------------
def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class FakeLLM:
    def __init__(self, *script):
        self.script, self.seen = list(script), []

    async def turn(self, messages, on_event, tools_enabled=True):
        self.seen.append([dict(m) for m in messages])
        r = self.script.pop(0)
        r = r(messages) if callable(r) else r
        for b in r.content:
            if b.type == "text":
                await on_event("text", b.text)
        return r

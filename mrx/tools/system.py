"""System control: info, processes, apps, shell, clipboard, desktop automation."""
from __future__ import annotations

import base64
import configparser
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

from ..safety import shell_looks_catastrophic
from .files import resolve
from .registry import Context, Image, ToolError, b, enum, i, n, s, tool

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _spawn(cmd: list[str] | str, shell: bool = False) -> int:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, shell=shell, **kwargs).pid


@tool("system_info", "OS, CPU, memory, disk, battery, uptime and network addresses of this machine.", {})
def system_info(ctx: Context) -> dict:
    vm, du = psutil.virtual_memory(), psutil.disk_usage(str(Path.home()))
    info = {
        "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(), "hostname": platform.node(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME"), "python": platform.python_version(),
        "cpu_cores": psutil.cpu_count(), "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory": f"{vm.used / 2**30:.1f}/{vm.total / 2**30:.1f} GiB ({vm.percent}%)",
        "disk_home": f"{du.used / 2**30:.0f}/{du.total / 2**30:.0f} GiB ({du.percent}%)",
        "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
        "home": str(Path.home()), "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    try:
        bat = psutil.sensors_battery()
        if bat:
            info["battery"] = f"{bat.percent:.0f}% {'charging' if bat.power_plugged else 'on battery'}"
    except Exception:  # noqa: BLE001
        pass
    return info


@tool("process_list", "List running processes, sorted by memory. Optionally filter by name substring.",
      {"name": s("Substring of the process name"), "limit": i("Max rows (default 25)")})
def process_list(ctx: Context, name: str = "", limit: int = 25) -> list[dict]:
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent", "username"]):
        info = p.info
        if name and name.lower() not in (info["name"] or "").lower():
            continue
        rows.append({"pid": info["pid"], "name": info["name"], "mem%": round(info["memory_percent"] or 0, 1), "user": info["username"]})
    rows.sort(key=lambda r: r["mem%"], reverse=True)
    return rows[:limit]


@tool("process_kill", "Terminate a process by PID (graceful SIGTERM first; force=true to SIGKILL).",
      {"pid": i("Process id"), "force": b("Kill immediately")}, ["pid"], risk="destructive",
      describe=lambda a: f"{'FORCE-KILL' if a.get('force') else 'Terminate'} process {a.get('pid')} ({_pname(a.get('pid'))})")
def process_kill(ctx: Context, pid: int, force: bool = False) -> str:
    if pid in (0, 1, os.getpid(), os.getppid()):
        raise ToolError("Refusing to kill PID 0/1 or M.R.X. itself.")
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.kill() if force else p.terminate()
    except psutil.NoSuchProcess:
        raise ToolError(f"No such process: {pid}")
    except psutil.AccessDenied:
        raise ToolError(f"Access denied for PID {pid}")
    return f"Signalled {name} ({pid})"


def _pname(pid: int | None) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return "?"


def _desktop_entries() -> list[tuple[str, str]]:
    """(display name, launch id/path) of installed apps."""
    out: list[tuple[str, str]] = []
    if IS_MAC:
        for d in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
            if os.path.isdir(d):
                out += [(f[:-4], f) for f in os.listdir(d) if f.endswith(".app")]
    elif IS_WIN:
        roots = [os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"), os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs")]
        for r in roots:
            for dp, _, fns in os.walk(r):
                out += [(f[:-4], os.path.join(dp, f)) for f in fns if f.endswith(".lnk")]
    else:
        dirs = ["/usr/share/applications", "/usr/local/share/applications", str(Path.home() / ".local/share/applications"),
                "/var/lib/flatpak/exports/share/applications", "/var/lib/snapd/desktop/applications"]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".desktop"):
                    continue
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    cp.read(os.path.join(d, fn), encoding="utf-8")
                    entry = cp["Desktop Entry"]
                    if entry.get("NoDisplay", "false").lower() == "true":
                        continue
                    out.append((entry.get("Name", fn[:-8]), fn[:-8]))
                except Exception:  # noqa: BLE001
                    continue
    return sorted(set(out), key=lambda t: t[0].lower())


@tool("app_list", "List installed desktop applications (optionally filtered by name).", {"filter": s("Name substring")})
def app_list(ctx: Context, filter: str = "") -> list[str]:
    names = [n_ for n_, _ in _desktop_entries() if filter.lower() in n_.lower()]
    return names[:300]


@tool("app_open", "Launch an application by name (e.g. 'Firefox', 'Spotify', 'calculator'), optionally with a file/URL to open in it.",
      {"name": s("Application name or executable"), "target": s("Optional file path or URL to open with it")}, ["name"], risk="modify",
      describe=lambda a: f"Launch app {a.get('name')}" + (f" with {a['target']}" if a.get("target") else ""))
def app_open(ctx: Context, name: str, target: str = "") -> str:
    tgt = [str(resolve(target)) if target and "://" not in target else target] if target else []
    if IS_MAC:
        _spawn(["open", "-a", name, *tgt])
        return f"Launched {name}"
    if IS_WIN:
        _spawn(["cmd", "/c", "start", "", name, *tgt])
        return f"Launched {name}"
    exe = shutil.which(name) or shutil.which(name.lower().replace(" ", "-"))
    if exe:
        _spawn([exe, *tgt])
        return f"Launched {exe}"
    q = name.lower()
    matches = [(n_, did) for n_, did in _desktop_entries() if q == n_.lower() or q == did.lower()] or \
              [(n_, did) for n_, did in _desktop_entries() if q in n_.lower() or q in did.lower()]
    if matches and shutil.which("gtk-launch"):
        _spawn(["gtk-launch", matches[0][1], *tgt])
        return f"Launched {matches[0][0]}"
    raise ToolError(f"Could not find an application called {name!r}. Try app_list to see what is installed.")


@tool("app_close", "Ask running application(s) to quit, matched by process name substring.",
      {"name": s("Process/app name substring"), "force": b("Kill instead of asking nicely")}, ["name"], risk="destructive",
      describe=lambda a: f"{'FORCE-QUIT' if a.get('force') else 'Quit'} all processes matching '{a.get('name')}' (unsaved work may be lost)")
def app_close(ctx: Context, name: str, force: bool = False) -> str:
    hit = 0
    for p in psutil.process_iter(["pid", "name"]):
        if name.lower() in (p.info["name"] or "").lower() and p.info["pid"] not in (os.getpid(), os.getppid(), 1):
            try:
                p.kill() if force else p.terminate()
                hit += 1
            except psutil.Error:
                pass
    if not hit:
        raise ToolError(f"No running process matches {name!r}")
    return f"Signalled {hit} process(es) matching {name!r}"


@tool("open_target", "Open a file, folder or URL with the system's default application.",
      {"target": s("File path, folder path, or URL")}, ["target"], risk="modify")
def open_target(ctx: Context, target: str) -> str:
    t = target if "://" in target else str(resolve(target))
    if "://" not in t and not os.path.exists(t):
        raise ToolError(f"Does not exist: {t}")
    if IS_WIN:
        os.startfile(t)  # type: ignore[attr-defined]  # noqa: S606
    else:
        _spawn(["open" if IS_MAC else "xdg-open", t])
    return f"Opened {t}"


@tool("shell_run", "Run a shell command and return its output. Use for anything the dedicated tools don't cover. Times out (default 60s).",
      {"command": s("Command line"), "cwd": s("Working directory (default: home)"), "timeout": i("Seconds (default 60, max 600)")},
      ["command"], risk="exec", describe=lambda a: f"Run shell command:\n$ {a.get('command')}\n(in {a.get('cwd') or '~'})")
def shell_run(ctx: Context, command: str, cwd: str = "", timeout: int = 60) -> str:
    if shell_looks_catastrophic(command):
        raise ToolError("Refusing: this command looks catastrophically destructive.")
    wd = resolve(cwd) if cwd else Path.home()
    try:
        r = subprocess.run(command, shell=True, cwd=wd, capture_output=True, text=True, errors="replace",
                           timeout=max(1, min(timeout, 600)), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return f"[timed out after {timeout}s]\n{(e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')}"
    out = r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr.strip() else "")
    return f"[exit {r.returncode}]\n{out}"


# -- clipboard ---------------------------------------------------------------
def _clip_cmds() -> tuple[list[str] | None, list[str] | None]:
    if IS_MAC:
        return ["pbpaste"], ["pbcopy"]
    if IS_WIN:
        return ["powershell", "-command", "Get-Clipboard"], ["clip"]
    if shutil.which("wl-paste"):
        return ["wl-paste"], ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "-b", "-o"], ["xsel", "-b", "-i"]
    return None, None


@tool("clipboard_get", "Read the clipboard text.", {}, untrusted=True)
def clipboard_get(ctx: Context) -> str:
    get, _ = _clip_cmds()
    if not get:
        raise ToolError("No clipboard utility found (install xclip, xsel or wl-clipboard).")
    return subprocess.run(get, capture_output=True, text=True, errors="replace", timeout=5).stdout


@tool("clipboard_set", "Put text on the clipboard.", {"text": s("Text")}, ["text"], risk="modify")
def clipboard_set(ctx: Context, text: str) -> str:
    _, put = _clip_cmds()
    if not put:
        raise ToolError("No clipboard utility found (install xclip, xsel or wl-clipboard).")
    subprocess.run(put, input=text, text=True, timeout=5, check=True)
    return "Copied to clipboard"


@tool("notify", "Show a desktop notification.", {"title": s("Title"), "message": s("Body")}, ["title", "message"])
def notify(ctx: Context, title: str, message: str) -> str:
    if IS_MAC:
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], timeout=5)
    elif IS_WIN:
        return "Notifications are not supported on Windows yet; the UI toast was shown instead."
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], timeout=5)
    return "Notified"


# -- desktop automation (optional: pip install 'mrx[desktop]') ---------------
def _gui():
    try:
        import pyautogui  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 - also raises on headless X errors
        raise ToolError(f"Desktop automation unavailable ({type(e).__name__}: {e}). Install with: pip install 'mrx[desktop]' and run on a graphical session.")
    pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
    return pyautogui


@tool("desktop_screenshot", "Capture the whole screen so you can see what is on it. Returns an image.", {}, untrusted=True)
def desktop_screenshot(ctx: Context) -> Image:
    gui = _gui()
    img = gui.screenshot()
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return Image(base64.b64encode(buf.getvalue()).decode(), "image/jpeg", f"Screen {gui.size()[0]}x{gui.size()[1]} (click coordinates are in screen pixels; this image may be downscaled)")


@tool("desktop_click", "Move the mouse to screen coordinates and click.",
      {"x": i("X pixel"), "y": i("Y pixel"), "button": enum("Mouse button", "left", "right", "middle"), "double": b("Double-click")},
      ["x", "y"], risk="exec", describe=lambda a: f"Click screen at ({a.get('x')}, {a.get('y')}) {a.get('button', 'left')}")
def desktop_click(ctx: Context, x: int, y: int, button: str = "left", double: bool = False) -> str:
    gui = _gui()
    gui.click(x, y, clicks=2 if double else 1, button=button)
    return f"Clicked ({x}, {y})"


@tool("desktop_type", "Type text using the keyboard into whatever window has focus.",
      {"text": s("Text to type"), "interval": n("Seconds between keys (default 0.01)")}, ["text"], risk="exec",
      describe=lambda a: f"Type into the focused window: {a.get('text', '')[:300]!r}")
def desktop_type(ctx: Context, text: str, interval: float = 0.01) -> str:
    _gui().write(text, interval=interval)
    return f"Typed {len(text)} chars"


@tool("desktop_hotkey", "Press a key combination, e.g. keys=['ctrl','l'] or ['enter'].",
      {"keys": {"type": "array", "items": {"type": "string"}, "description": "Keys pressed together"}}, ["keys"], risk="exec",
      describe=lambda a: f"Press keys: {'+'.join(a.get('keys', []))}")
def desktop_hotkey(ctx: Context, keys: list[str]) -> str:
    _gui().hotkey(*keys)
    return f"Pressed {'+'.join(keys)}"

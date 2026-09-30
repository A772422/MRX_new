"""Command line entry point: `mrx` or `python -m mrx.main`."""
from __future__ import annotations

import argparse
import webbrowser

import uvicorn

from .config import Config
from .server import create_app, load_token


def main() -> None:
    ap = argparse.ArgumentParser(prog="mrx", description="M.R.X. real-time agent")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true", help="don't open the UI automatically")
    args = ap.parse_args()

    cfg = Config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    token = load_token(cfg)
    url = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{cfg.port}/?token={token}"

    print(f"\n  M.R.X. online — model {cfg.model}, autonomy '{cfg.autonomy}'\n  Open: {url}\n")
    if not (cfg.get("ANTHROPIC_API_KEY") or cfg.get("ANTHROPIC_AUTH_TOKEN")):
        print("  ! ANTHROPIC_API_KEY is not set (fine if you use `ant auth login`); otherwise copy .env.example to .env and add your key.\n")
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ! Listening on {cfg.host}: anyone on your network with the token can control this computer. Prefer 127.0.0.1.\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

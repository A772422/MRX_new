# M.R.X.

A real-time personal agent that runs on your own computer. Talk (or type) to it in any language; it acts through tools and shows what it is doing in a live interface — chat, world map, news, YouTube player, browser view and a network radar.

The brain is Claude (`claude-opus-5-5` by default) running a streaming tool-use loop. Everything else — files, apps, browser, mail, radar — is local code in this repo.

## What it can do

| You asked for | How it works | Tools |
|---|---|---|
| **Full system control** — manage apps and software | Launch/close apps by name, list installed apps, process list/kill, shell commands, clipboard, notifications, open files/URLs, and (optional) mouse/keyboard/screen automation | `app_open` `app_close` `app_list` `process_*` `shell_run` `clipboard_*` `desktop_*` `system_info` |
| **Complete browser control** | Real Chromium via Playwright with a *persistent profile* (log in once, stays logged in). The model gets numbered page elements, clicks/types/scrolls/navigates, takes screenshots; you watch a live view in the **Browser** tab. Can also attach to your own Chrome (`MRX_BROWSER_CDP_URL`) | `browser_*` |
| **Automation** | Schedule prompts to run later or repeatedly ("every day 08:30, brief me on the news"); results arrive as notifications | `task_schedule` `task_list` `task_delete` |
| **Built-in YouTube player** | Search (API key optional), play, queue and transport control in an embedded player | `youtube_*` |
| **File/folder management** | Create, read, edit, append, copy, move, rename, delete (to a recoverable trash), search, restore | `fs_*` |
| **Memory** | Long-term facts (SQLite full-text search, any language) injected into every conversation, plus a searchable log of past conversations; long chats are summarised and rolled over automatically | `memory_*` `conversation_search` |
| **Email** | IMAP/SMTP: list, search, read, **draft** (also saved to your mailbox's Drafts), reply with threading, **send** (always asks you first, showing the full message) | `email_*` |
| **Social media** | Mastodon, Bluesky, X, Telegram: read timeline/mentions/search, save drafts, post (always asks first) | `social_*` |
| **Every language** | Claude understands and writes all major languages; voice input in 19 languages (browser speech API), spoken replies pick a voice from the script of the reply, news from native-language outlets, place names in any language | — |
| **Live global news** | RSS from 20+ outlets in 10 languages, plus GDELT worldwide article search (100+ languages, 15-minute refresh) | `news_headlines` `news_search` |
| **Live world map** | Leaflet map you can drive by voice: fly to places, routes, custom markers, and live layers — earthquakes (USGS), natural events (NASA EONET), geolocated news (GDELT), aircraft (OpenSky), rain radar | `map_*` `world_events` `weather` |
| **Network radar** | Sweeps your LAN, reads the ARP table, resolves names (DNS, NetBIOS, mDNS, UPnP), looks up the MAC vendor (31k-entry bundled database), probes common ports and classifies each device (router, printer, Apple phone, smart TV, IoT, …). Animated radar in the UI, unknown devices flagged, name/trust devices, optional live mode | `radar_*` |

## Quick start

```bash
git clone <this repo> && cd MRX_new
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # add '.[desktop,mdns]' for mouse/keyboard automation and richer radar names
playwright install chromium      # only needed for browser control
cp .env.example .env             # put your ANTHROPIC_API_KEY in it
mrx                              # prints and opens  http://127.0.0.1:8765/?token=…
```

Use Chrome, Edge or Brave for voice input (Firefox lacks the speech-recognition API). The YouTube player, map tiles and live map data load from the internet in your browser.

## Safety model — read this, it controls your computer

M.R.X. can run commands, delete files and send email as you. The design assumes the model can be tricked (web pages, emails and news headlines are attacker-controlled text) and puts hard gates in code, not in the prompt:

* **Approval prompts.** Every tool has a risk level. `destructive` (delete, kill) and `outbound` (send email, post, message) *always* ask you, with the exact content shown. Shell commands, JS-in-page and desktop clicks/typing (`exec`) ask in `balanced` mode (default). `MRX_AUTONOMY=strict` asks for every write; `trusted` skips only `exec` prompts. Switchable live in the header. No UI open, or no answer in 3 minutes → denied.
* **Prompt-injection containment.** Once a turn has read untrusted content (web page, email, feed, news, clipboard, screenshot), all non-browser side effects in that turn require approval, whatever the autonomy mode. A hostile page saying "now write a file / run this" produces a prompt you can refuse. (Files read from disk are *not* treated as untrusted, so a booby-trapped downloaded document is a residual risk — the approval prompts for shell/delete/send still apply.)
* **Browser guard rails.** Clicking a button whose label looks consequential (buy, send, pay, delete, post, …) and typing into password fields need your explicit approval. `javascript:`/`file:` URLs are refused.
* **Recoverable deletes.** `fs_delete` moves to `~/.mrx/trash` with metadata; overwrites and edits keep a backup there; `fs_trash_restore` undoes. Home, Documents, `/`, `/etc`, … can never be deleted or moved. Catastrophic shell patterns are refused.
* **Unattended runs.** Scheduled tasks run in a fresh context with *no* ability to perform anything that needs approval.
* **Network exposure.** Listens on `127.0.0.1` only, behind a random token (`~/.mrx/token`, cookie is HttpOnly + SameSite=Strict). Host and Origin are checked on every request and WebSocket (DNS-rebinding and cross-site hijack protection). Binding elsewhere prints a warning.
* **Radar scope.** Only private IPv4 networks your machine is directly attached to, max /22. Public ranges are refused in code.
* Secrets are read from the environment/`.env` and never sent to the model unless a tool result contains them — don't `cat` your `.env` into a chat.

## Setting up integrations

* **Email** — set `EMAIL_ADDRESS` and `EMAIL_PASSWORD` (an *app password*; Gmail/Outlook/Yahoo/iCloud/AOL hosts are auto-detected, otherwise set `EMAIL_IMAP_HOST`/`EMAIL_SMTP_HOST`). See `.env.example`.
* **Social** — Mastodon (`MASTODON_BASE_URL`, `MASTODON_TOKEN`), Bluesky (handle + app password), X (an OAuth2 user token with read/write scopes; API access is paid), Telegram (bot token + chat id). Instagram, Facebook, LinkedIn and WhatsApp are **not** supported: their APIs don't allow this kind of personal read/post access. Ask the browser tools to do it in your logged-in profile instead, and M.R.X. will stop for approval on anything consequential.
* **YouTube** — works without a key by scraping search results; set `YOUTUBE_API_KEY` for reliable search. Some videos forbid embedding; the UI then links to YouTube.

## Radar notes

Detection needs you to be on the same LAN segment. A phone using a "private Wi-Fi address" has a randomised MAC, so no vendor can be looked up — the radar says so and classifies it from name and ports if possible. Names and types are best-effort guesses (shown with a confidence); label devices you recognise and mark them *trusted*. The first scan is a silent baseline; after that new untrusted devices raise an alert. No root needed. Install `mrx[mdns]` for friendly Bonjour/Chromecast names.

The vendor table `mrx/data/oui.json` is derived from the public IEEE OUI registry via Wireshark's `manuf` list (GPL-2.0-or-later).

## Layout

```
mrx/
  agent.py        streaming tool-use loop, memory injection, conversation roll-over
  safety.py       risk policy, protected paths        tools/registry.py   tool decorator + gated execution
  server.py       FastAPI + WebSocket hub, auth       memory.py           SQLite: facts, log, drafts, devices, tasks
  radar.py        network discovery engine            tools/*.py          one file per capability area
  web/            single-page UI (no build step; Leaflet vendored)
tests/            96 tests
```

Add a capability: write a function in `mrx/tools/` with `@tool(name, description, props, required, risk=…)`, import it in `tools/__init__.py`. Schemas, approval prompts and the UI activity feed come for free.

## Tests and what has (not) been verified

`pip install -e '.[dev]' && pytest` — 96 tests: safety policy, every file operation, the tool registry and approval gating, prompt-injection escalation, memory, scheduling, radar parsing/classification/guards, the agent loop (with a scripted model), the real Anthropic SDK code path against replayed streaming responses, and server auth/WebSocket behaviour.

Also exercised for real during development: the browser tools against headless Chromium on a local page (Unicode typing, denied purchase and password prompts), the radar against a live network interface (also under `uvloop`, which uvicorn uses), and the full UI driven by Playwright with real tools.

**Not verified** (no API key or open internet in the build sandbox): live conversations with the model — the loop, request shape and streaming are tested, but prompt quality with the real model needs your feedback; the RSS feed URLs, GDELT, USGS, EONET, OpenSky, Open-Meteo, OSRM and YouTube scraping endpoints (third parties change these — failures are reported per source, not fatal); real IMAP/SMTP servers; the social APIs; macOS and Windows code paths (app launching, clipboard, gateway detection); and voice input/output, which depends on your browser.

# TokenFurnace

**A zero-dependency local web console for burning, watching and quota-managing LLM tokens.**

English · [简体中文](README.md)

[![CI](https://github.com/richenxiao/tokenfurnace/actions/workflows/ci.yml/badge.svg)](https://github.com/richenxiao/tokenfurnace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)

## Get it

**Single file (fastest).** Download `tokenfurnace-0.1.0.pyz` from [Releases](https://github.com/richenxiao/tokenfurnace/releases/latest), then:

```bash
python tokenfurnace-0.1.0.pyz
```

The whole tool is that one 61 KB file. No clone, no pip, no directory layout to worry about. Your browser opens `http://127.0.0.1:8760/`.

A `.pyz` is a Python zipapp (PEP 441): a complete program packed into one file, the equivalent of a Java `.jar`. **It has to be run from a terminal with `python`.** Double-clicking usually does nothing, because Windows has no default file association for `.pyz`.

**Prefer double-clicking?** Download `Source code (zip)` from the bottom of the Releases page, extract it, and double-click `start.bat` (Windows) or `start.sh` (macOS / Linux).

**Install to a fixed location with a desktop icon** (recommended; you never have to find it again):

```bash
git clone https://github.com/richenxiao/tokenfurnace.git
cd tokenfurnace
python scripts/install.py
```

You get a `TokenFurnace` icon on your desktop. **Double-click it from then on** — no commands, no hunting for directories.
It installs to `%LOCALAPPDATA%\TokenFurnace` on Windows (`~/.local/share/tokenfurnace` elsewhere) and migrates your config and ledger, so history carries over.

Source directories usually carry a date (like `2026-09-20-09-30-59`); if one gets cleaned up the tool goes with it. This script exists so that can't happen. To remove: `python scripts/install.py --uninstall` (config and ledger are kept).

**Clone and run directly:**

```bash
git clone https://github.com/richenxiao/tokenfurnace.git
cd tokenfurnace
python run.py
```

**Install as a command** (from inside the clone):

```bash
pip install -e .
tokenfurnace
```

All four need Python 3.9+ and nothing else. Config and ledger land in whichever directory you run the command from.

---

## Why this exists

It started with a rebate rule on SenseNova TokenPlan: every 1 credit of Flash-Lite spend earns 1 general credit back, and general credits run any model enabled on the account. So the job was to burn the Flash-Lite credits down and convert them.

Then a second problem showed up. Switching providers meant editing code, because the script had the base URL, model names and request shape baked in. So it became generic.

Today: enter an endpoint, pick a protocol, tick the models, set limits, start.

---

## Quick start

Four steps in the UI:

1. Pick a protocol, enter the Base URL and key, click *Test connection*
2. Click *Fetch models* and tick the ones to consume
3. Adjust the strategy (mode, concurrency, input size)
4. Set limits and click *Start with this profile*

Don't want to install first? [`docs/preview.html`](docs/preview.html) is a static preview rendered from the real frontend files, with demo data. Open it in a browser.

### What you see on first run

A panel at the top lists what's still missing:

```
3 steps to go
 1. Fill in the Base URL, e.g. https://api.example.com
 2. Enter a key, or switch to an environment variable (never touches disk)
 3. Click "Fetch models" and tick some (you can also type a model ID)
Then click "Test connection", then "Start with this profile".
```

Config lands in `config.json` in the current directory, the ledger in `data/tokenfurnace.db`. Both are gitignored. `config.example.json` is a template.

Command-line flags:

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` exposes it to your LAN, with no authentication |
| `--port` | `8760` | Walks forward if the port is taken |
| `--no-browser` | — | Don't open a browser |
| `--config` | `./config.json` | Custom config path |

---

## Four API protocols

The four protocols are mutually incompatible. Pick wrong and you get a 400 or 404:

| | Anthropic Messages | OpenAI Chat | OpenAI Responses | Gemini Native |
|---|---|---|---|---|
| Path | `/v1/messages` | `/v1/chat/completions` | `/v1/responses` | `/v1beta/models/{m}:generateContent` |
| Default auth | `x-api-key` | `Authorization: Bearer` | same | `x-goog-api-key` |
| System prompt | top-level `system` | `role: system` in messages | top-level `instructions` | top-level `systemInstruction` |
| Input field | `messages` | `messages` | `input` | `contents[].parts[].text` |
| Output cap | `max_tokens` (required) | `max_tokens` | `max_output_tokens` | `generationConfig.maxOutputTokens` |
| Reasoning | `thinking.budget_tokens` | `reasoning_effort` | `reasoning.effort` | `thinkingConfig.thinkingBudget` |
| Usage fields | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` | `usageMetadata.promptTokenCount` / `candidatesTokenCount` |

An adapter layer handles all of it. Pick a protocol and the request body, auth header and usage-field parsing follow.

Which one? Whichever path your endpoint exposes: `/v1/messages` for Anthropic, `/v1/chat/completions` for Chat, `/v1/responses` for Responses, `:generateContent` for Gemini. A URL containing `anthropic`, `claude` or `gemini` switches automatically.

To verify the four really do differ, run `python scripts/protocol_probe.py`. It starts a local mock endpoint and prints the raw HTTP request each protocol sends.

### Auth fields

Which header carries the credential is separate from the protocol. The same protocol may want a different header behind a different gateway, so they're configured independently:

| Auth field | What gets sent | Common on |
|---|---|---|
| `Authorization: Bearer` | `Authorization: Bearer <key>` | OpenAI-family; `ANTHROPIC_AUTH_TOKEN` in the Anthropic ecosystem |
| `x-api-key` | `x-api-key: <key>` | Anthropic-family; `ANTHROPIC_API_KEY` |
| `x-goog-api-key` | `x-goog-api-key: <key>` | Google Gemini |

Switching protocol pre-selects that protocol's usual field. You can still change it. An empty key sends no credential header, so there's no separate "no auth" option.

### Base URL normalisation

```
https://api.example.com                     → https://api.example.com/v1/chat/completions
https://api.example.com/v1                  → https://api.example.com/v1/chat/completions
https://api.example.com/v1/chat/completions → https://api.example.com/v1/chat/completions
https://api.example.com/api/paas/v4         → https://api.example.com/api/paas/v4/chat/completions
https://generativelanguage.googleapis.com   → https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent
```

A root address is enough. The version segment is filled in, and pasting a full endpoint still works.

---

## Configuration

### Profiles

Save any number of profiles (different providers, endpoints, quota rules). Switch from a dropdown, export or import as JSON. Duplicating a profile goes through the server so the key and model list come along.

### Three key sources

| Source | Notes |
|---|---|
| Inline | Plaintext in the local `config.json` |
| Environment variable | Stores only the variable name; the key never touches disk |
| File path | Stores only the path; reads the file at runtime |

Keys are masked as `sk-abc********wxyz` in every API response. The frontend never sees plaintext. The three sources hold their values separately, so switching doesn't carry one over into another.

### Environment injection

Start a throwaway profile without editing config files:

```bash
export TOKENFURNACE_BASE_URL=https://api.example.com/v1
export TOKENFURNACE_API_KEY=sk-xxxx
export TOKENFURNACE_MODEL=model-a,model-b
export TOKENFURNACE_PROTOCOL=anthropic      # optional
python run.py
```

`TOKENFURNACE_CONFIG` overrides the config path.

---

## Model selection

Click *Fetch models* to pull the endpoint's full list, then tick what you want. Each model takes its own **weight** (selection probability when spreading load) and **credits per 1K** (a cost coefficient; leave it at 0 to use the global value). The panel footer shows "consuming N models" as you go.

Two details:

- The bulk buttons act on the **currently filtered** set. Lists can run to hundreds of models, and an unfiltered *select all* would sweep in models you don't want. Filter first, then click *Select visible*.
- Models missing from the list can be **typed in by ID**. Some gateways return a partial `/models` response, and typing an ID isn't limited by the list.

There is no "keep only model X" preset.

---

## Concurrent profiles

Tick several profiles under *Batch start* and launch them all at once, instead of switching back and forth.

- Each session uses its own key, models, limits and cost coefficient
- Strategy settings (mode, concurrency, input size, budgets) come from the current form and apply to all sessions
- Profiles that can't start are **disabled with the reason shown** (no Base URL / no key / no models), so you can't tick them by mistake
- Results stay on the page rather than appearing in a toast that disappears

The dashboard header shows the combined total (rates, tokens and request counts added point by point). Each running session is listed below and can be stopped on its own.

The rolling window is shared. When one session exhausts the quota, every session waits, because the platform quota is shared in the first place. There's a global worker cap of 32; exceeding it returns a refusal with the current usage.

Credits are recorded per request and the rolling window is a plain sum over the table, so concurrent sessions don't corrupt each other's accounting.

Stopping a session **can't interrupt in-flight requests** (urllib blocks in `recv`, and Python can't kill a thread). The status shows "stopping, waiting for in-flight requests". Those requests still get recorded when they land.

Profile IDs use strict lookup. An unknown ID returns an error rather than silently falling back to another profile, which would otherwise burn profile A's quota while you think you're running profile B.

---

## Consumption strategy

### Three modes

| Mode | Approach | Use when |
|---|---|---|
| Prefill (default) | Huge input, tiny output | Fastest burn |
| Decode | Small input, long output | Simulating real generation |
| Mixed | Mostly prefill, some decode | A more natural token mix |

Prefill is the default because the measured gap is large. Same model:

| Path | Measured throughput |
|---|---|
| Prefill (huge input, tiny output) | 9,000 – 18,600 tok/s |
| Decode (small input, long output) | ~111 tok/s |

About 80×. Prefill sends roughly 380k characters of random English (about 180k tokens) and asks for two characters back.

### Key parameters

| Parameter | Default | Notes |
|---|---|---|
| Concurrency | 4 | A reasonable balance; higher tends to trip rate limits |
| Input per request | 380,000 chars | ~180k tokens, close to but under the context limit |
| Max output | 8 | Kept small in prefill mode |
| Reasoning effort | `none` | Fastest |
| Cache busting | on | See below |

### Cache busting

Reuse the same prefix and many platforms serve it from a prefix cache at a discount. You think you're burning quota and barely spend any. TokenFurnace regenerates random text on every request; measured `cached_tokens` stays at 0. The dashboard shows this metric, and it should always read 0.

### A 429 doesn't always mean "out of quota"

Some platforms return this when you exceed requests per second:

```json
{"error": {"message": "rps exhausted", "type": "quota_exceeded_error"}}
```

The `type` says `quota_exceeded_error` but the `message` says rps. That's rate limiting, and it's retryable. Judging by `type` shuts the run down while quota is still available, so TokenFurnace classifies on `message`:

| Kind | Trigger | Handling |
|---|---|---|
| `rps` / `server` | Rate limit, 5xx | Every thread in that session backs off together, so concurrency doesn't keep hammering the limit window |
| `timeout` / `network` | One connection stuck | Only that thread backs off; the others keep going |
| `quota` | Genuinely exhausted | Stop, and derive the cost coefficient |
| `auth` | 401 / 403 | Stop and prompt for the key |
| `bad_request` | 400 / 404 / 422 | Log and skip; 404 also hints at a protocol mismatch |

Timeouts adapt to measured latency (3× the recent median, floor 30s, never above your configured maximum). A fixed 180s means one hung connection occupies a worker for three minutes, when normal requests finish in 25.

---

## Live rate

The headline number is the instant rate. The window switches between 5 / 10 / 20 / 60 seconds.

The window **widens on its own**. In prefill mode a single request takes 20–40 seconds, so a 5-second window often contains no completions at all and the headline sits at `0.0`. The effective window expands to cover at least the last two completions, and the UI labels it. Your setting is a lower bound: it can widen, never narrow.

Each session card also shows **measured latency per request**, which is the fastest way to tell whose problem a slowdown is:

| Symptom | Conclusion |
|---|---|
| Average rate dropped, latency unchanged | Local: concurrency too low, window throttling, or threads backing off |
| Latency clearly grew (25s → 115s) | Server-side congestion; nothing local will fix it |

### Speed checklist

1. **Run more profiles.** Biggest lever. Ticking several under *Batch start* adds their throughput.
2. **Raise concurrency.** Default 4; try 6–8. Requests spend most of their time waiting on the server, so more concurrency means more throughput until you hit a rate limit.
3. **Don't set the timeout ceiling high.** It adapts, but your setting is the ceiling.
4. **Leave cache busting on.** With it off, requests hit the prefix cache and the platform bills at a discount.
5. **Don't shrink the input blindly.** More tokens per request means less per-request overhead. Only reduce it if you suspect the platform limits long requests.

---

## Quota management

### Four budgets

Whichever trips first stops the run. `0` means unlimited: credits, total tokens, request count, duration.

### Rolling windows

The tool accounts locally for a rolling 5-hour and 7-day window. When usage approaches the cap it computes how long until the oldest record falls out of the window and sleeps exactly that long, instead of polling.

Keep-alive mode waits through the wall and resumes automatically.

> **On metering scope.** A platform's general credit pool and its Flash-Lite-only pool are metered separately, while the local ledger keeps one combined total. Burn only Flash-Lite and the two agree. Mix in a model billed against general credits and the window reading drifts from the platform's own figure, so leave headroom.

### Cost coefficient

If your platform bills in credits rather than tokens, set a coefficient (credits per 1K tokens) and the tool can show live credit spend, brake on a credit budget, and throttle on 5h/weekly credit windows.

Two ways to get the number:

1. **Manually**: from a billing delta, `coefficient = credits spent × 1000 ÷ tokens spent`
2. **Derive**: click *Derive* in the UI to work backwards from tokens used in the current window. Or wait until the platform actually returns quota-exhausted; the coefficient gets printed to the log

Deriving assumes the window really was exhausted. If it merely ran a long time without erroring, you get an upper bound, not the true value.

---

## Interface

### Controls

Segmented controls for protocol, auth field, mode, key source and chart range. Numeric inputs have steppers with per-field step sizes (input size jumps 20,000 characters at a time). Booleans are switches with a subtitle explaining the consequence.

### Dashboard

- **Rate card**: instant rate, average, peak, throughput, plus a sparkline. The number is colour-coded by tier
- **Trend chart**: green area is instantaneous rate, blue dashed line is cumulative tokens, twin Y axes, hover for exact values
- **Rolling window usage**: two bars for 5-hour and weekly, amber past 75%, red past 92%
- **Run log**: colour-coded by level, auto-scrolls
- **Session history**: click *Details* for a per-model and per-error breakdown

---

## REST API

The server exposes HTTP endpoints for scripting:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Full state: config, live metrics, logs, history |
| GET | `/api/run/detail?run=<id>` | Per-session breakdown by model and error |
| POST | `/api/profile/save` | Create or update a profile |
| POST | `/api/profile/activate` | Switch the active profile |
| POST | `/api/profile/duplicate` | Duplicate a profile, key included |
| POST | `/api/profile/delete` | Delete a profile |
| POST | `/api/profile/import` | Import profiles |
| POST | `/api/config/engine` | Update engine defaults |
| POST | `/api/config/window` | Change the instant-rate window (seconds), takes effect immediately |
| POST | `/api/test` | Connectivity self-check (list models plus one minimal call) |
| POST | `/api/models` | Fetch the model list |
| POST | `/api/guess_protocol` | Guess a protocol from a base URL |
| POST | `/api/run/start` | Start one session |
| POST | `/api/run/start_batch` | Start several profiles, reporting success and failure per profile |
| POST | `/api/run/stop` | Stop one session by `sid`, or all of them |
| POST | `/api/estimate` | Estimate consumption for a spec |
| POST | `/api/calibrate` | Compute the cost coefficient manually |
| POST | `/api/calibrate/auto` | Derive it from the current window |
| GET | `/api/export/requests.csv` | Per-request CSV |
| GET | `/api/export/runs.csv` | Per-session CSV |
| GET | `/api/export/config.json` | Config export |

---

## Files

```
tokenfurnace/
├── run.py                    launcher
├── start.bat                 double-click launcher (Windows)
├── start.sh                  double-click launcher (macOS / Linux)
├── tokenfurnace/
│   ├── cli.py                CLI entry point
│   ├── config.py             config sources, profiles, key resolution
│   ├── providers.py          four protocol adapters, error classification, URL normalisation
│   ├── engine.py             consumption engine (sessions, backoff, budgets, rolling windows)
│   ├── store.py              SQLite ledger
│   ├── server.py             REST API plus static serving
│   └── web/                  frontend (vanilla JS, no framework, no build step)
├── docs/preview.html         static UI preview, self-contained
├── scripts/
│   ├── protocol_probe.py     proves the four protocols differ, offline
│   ├── smoke_ci.py           boots the server in-process and asserts the API shape
│   ├── smoke_live.py         end-to-end check against an already-running server
│   ├── build_preview.py      regenerate docs/preview.html
│   ├── build_release.py      build the single-file .pyz release asset
│   ├── install.py            install to a fixed location with a desktop icon
│   └── check_batch_ui.js     runs the real frontend under jsdom (needs npm i jsdom)
├── tests/test_core.py        90 offline unit tests
├── config.example.json       config template
└── data/tokenfurnace.db      SQLite ledger (auto-created, gitignored)
```

SQLite rather than a log file: indexed window queries stay fast at hundreds of thousands of rows, history is one SQL statement, and WAL plus per-row commits survive a hard kill.

Details are kept for 30 days, pruned and `ANALYZE`d at startup, so the file doesn't grow without bound and slow the window queries down.

---

## Development

```bash
python -m unittest discover -s tests -v     # 90 offline tests
python -m compileall -q tokenfurnace run.py # syntax check
```

Coverage includes URL normalisation, error classification (with a regression test for the rps-masquerading-as-quota case), request building and response parsing for all four protocols, the three key sources, config migration and import, SQLite window accounting, instant rate and the adaptive window, multi-session aggregation, and budget brakes.

CI runs three jobs: Ubuntu on Python 3.9 (the declared floor), Ubuntu on 3.13, and Windows on 3.13 (the primary user platform). Those cover the two real risks: the 3.9 floor can't be tested on a machine that only has 3.13, and Windows is where path and encoding problems surface. macOS adds nothing for a pure-stdlib project, so it isn't in the matrix.

Each job runs the syntax check, the 90 unit tests, the protocol probe, and a smoke test that boots the server in-process, calls the API, asserts the shape, and shuts down.

Four scripts reproduce the key claims:

| Script | Purpose |
|---|---|
| `scripts/protocol_probe.py` | Starts a local mock endpoint and captures the raw HTTP request for each protocol |
| `scripts/smoke_ci.py` | Boots the server in-process and asserts the API shape; this is what CI runs |
| `scripts/build_preview.py` | Builds `docs/preview.html` from the real frontend files |
| `scripts/check_batch_ui.js` | Runs the real frontend under jsdom to verify interaction behaviour (needs `npm i jsdom`) |

---

## FAQ

**404 / 400 saying the path doesn't exist?**
Wrong protocol. Check whether the endpoint exposes `/chat/completions`, `/responses`, `/messages` or `:generateContent`.

**Constant 429s?**
Concurrency is too high. Drop to 4 or lower. The tool backs off automatically, but lowering concurrency is the actual fix.

**`cached_tokens` isn't 0?**
Some requests are hitting the prefix cache. Make sure cache busting is on.

**The live rate shows 0.0?**
Check for an "auto-widened to N seconds" note below it. If a run is active, the window was widened. If idle, 0 is correct.

**Can several profiles run at once?**
Yes. Tick them under *Batch start*, or switch profiles and click start each time. The rolling window is global: when one session fills the quota, all of them wait.

**Can I expose it publicly?**
It binds `127.0.0.1` by default. `--host 0.0.0.0` ships with no authentication, so put it behind a reverse proxy if you need remote access.

---

## Disclaimer

This is a generic API client and isn't tailored to any specific platform. Make sure your usage complies with your provider's terms. The authors aren't responsible for any account, quota or billing consequences arising from its use.

## License

[MIT](LICENSE)

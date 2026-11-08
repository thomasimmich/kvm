# KVM computer-using agent (`cua/`)

This service turns the JetKVM into an agent that operates the connected computer by itself.
On each step it:

1. **Sees** the screen. It fetches the current video frame from the KVM and parses it into UI
   elements with **OmniParser v2**: YOLO icon detection, OCR, and Florence-2 icon captions.
2. **Decides** what to do with **TypeSafe Jev**, a decision-only model. Jev picks the next element to
   click, a scroll direction, an input to type, Escape, *done* or *stuck*. In the same call, Jev also
   judges whether the previous action helped.
3. **Acts** like a person would, using the KVM's USB HID emulation:
   - the pointer moves on a curved, eased path, then clicks;
   - the wheel scrolls up, down, left or right;
   - typing goes through the remote machine's keyboard layout.
4. **Checks** the result. If nothing changed, it marks that element as tried. If the action led
   somewhere unhelpful, it presses Escape and tries a different element.

```
LeanScope agent server ──MCP──▶ cua serve (this service) ──HTTP──▶ JetKVM /api/cua ──USB HID──▶ remote PC
```

The device only provides "eyes and hands" (frames in, HID events out). Everything intelligent runs
here: on a Mac today, and later in the cloud next to the agent server.

## Quick start (after the setup below)

From `cua/`, in two terminals:

```bash
npm run deploy   # starts the x86 build VM if needed, deploys the firmware to KVM_URL from cua/.env
npm run cua      # starts the CUA (MCP + dashboard) and opens the dashboard
```

The debug firmware runs only while `npm run deploy` runs. Use `npm run deploy -- -i` to
install it permanently.

## Setup

**1. Firmware.** Build and deploy this repository's firmware, which includes the new `/api/cua`
endpoints:

```bash
./dev_deploy.sh -r <device-ip>
```

**2. Enable the API on the device and get a token.** Open the JetKVM web UI, open the browser
console, and run:

```js
window.__kvmTestHooks.sendJsonRpc("setCuaApiConfig", { enabled: true, regenerateToken: true }, (r) => console.log(r))
// → { result: { enabled: true, token: "…" } }
```

You can call `getCuaApiConfig` later to read the token again. Setting `enabled: false` turns the API
off.

**3. Install and configure this service.**

```bash
cd cua
uv sync --extra omniparser          # PyTorch, ultralytics, transformers, OCR
cp .env.example .env                # set KVM_URL, KVM_TOKEN, KVM_KEYBOARD_LAYOUT, TYPESAFE_API_KEY
```

The OmniParser weights (about 1 GB) download into `weights/` on first use. Without
`TYPESAFE_API_KEY`, a simple keyword matcher stands in for Jev. That is good for wiring tests,
not for real tasks.

## Try it

```bash
uv run cua state                                   # resolution, codec, viewers
uv run cua observe --save shot.png                 # parse the screen; writes shot.annotated.png
uv run cua click "Settings"                        # one human-like click on a label
uv run cua scroll down --notches 5
uv run cua type "hello world" && uv run cua press enter
uv run cua run "Open System Settings and show the Wi-Fi page" \
    --hint "Settings is the gear icon in the Dock" --expect "Wi-Fi"
uv run cua serve                                   # MCP at :8765/mcp, dashboard at :8765/dashboard
```

The **dashboard** (`http://127.0.0.1:8765/dashboard`) shows:
- the latest screen, with numbered element boxes;
- each decision, with Jev's top alternatives and their probabilities;
- the result of the last run.

You can also start goals from it. Keep the normal JetKVM browser view open next to it: the agent
doesn't take over the WebRTC session.

**Without a device:** use `--fake` to replay screenshots. The next image appears after each
click, scroll or key press.

```bash
uv run cua --fake screens/ serve        # or: --fake a.png --fake b.png
```

**Traces:** every run is saved to `runs/<time>-<goal>/`:
- each screen, as a raw and an annotated image;
- the element lists;
- `decisions.jsonl`, with Jev's probabilities;
- `result.json`.

A trace's screens and element files can be fed back into `FakeKvm` and `FixtureParser` as
regression fixtures.

## MCP surface

The server speaks Streamable HTTP at `/mcp` and takes a bearer token from `CUA_MCP_TOKEN`.
**Every tool returns plain text**, because the agent server flattens tool results to text.

| Tool | Purpose |
|---|---|
| `cua_run_goal(goal, hints?, inputs?, expect_text?, max_steps=15, time_budget_s=100)` | The main tool. Runs the whole see → decide → act → verify loop and returns a step-by-step summary. |
| `cua_observe()` | Lists the current elements as `[id] kind 'label' at position (x,y) WxH`. |
| `cua_click_element(id, button?, double?)` | Clicks one element by its id from the last observation. |
| `cua_click_text(text)` | Clicks one element by the text it contains. |
| `cua_scroll(direction, notches?)` | Scrolls up, down, left or right. |
| `cua_type_text(text)` | Types text. |
| `cua_press_keys("ctrl+l")` | Presses a key or key combination. |

The server also offers the prompt **`additional_agent_instructions`**, which carries the CUA
background knowledge from `knowledge/additional_agent_instructions.md`. The agent server adds it to
the step prompt automatically.

See [docs/agent-server-integration.md](docs/agent-server-integration.md) for connecting a choreo.

## Following and steering a run

- **Steering:** while a run is going, a human can tell the agent where to go, pause it, resume
  it or cancel it. On the dashboard that is the "Tell the agent" field and its buttons; from
  LeanScope it is the remote control (below).
  - Guidance goes into Jev's state as `HUMAN GUIDANCE` with the highest priority.
  - When Jev gives up, or after three discarded attempts in a row, the agent asks the human
    and waits up to `CUA_HUMAN_WAIT_S` seconds for an answer. It only waits when someone can
    answer: a dashboard run or a narrated session.
- **Rough screen description:** with `OPENAI_API_KEY` set, a fast vision model
  (`CUA_VISION_MODEL`) describes each new view in two or three sentences. It runs next to
  OmniParser and never delays a decision.
- **Live narration:** set the `LEANSCOPE_*` variables, and every run starts its own LeanScope
  session of the narration choreo. The CUA writes into that session's activity:
  - the screenshot and its description
  - the annotated screenshot with the element list
  - every decision and every micro step
  The dashboard shows a QR code for the remote control app. The flow, the activity tags and
  the command format are in
  [docs/agent-server-integration.md](docs/agent-server-integration.md#5-live-narration-and-remote-control).
- **Inner voice:** with `ELEVENLABS_API_KEY` and the OpenAI key set, the acting persona of the
  picked choreo (its primary actor, e.g. Heinz) thinks the activity log aloud in the dashboard.
  - An LLM (`CUA_INNER_VOICE_MODEL`) retells the newest log lines as one or two short German
    thoughts in the persona's style (profile, motives, frustrations, quote).
  - ElevenLabs speaks them with the voice attached to the persona: the nearest `voiceId` on the
    persona's resource relationships, the same walk the agent server does.
  - Lines that arrive while a thought is made or spoken are taken together, so the voice keeps up.
  - The "🔊 Inner voice" switch in the activity log turns it on and off. A click on ▶ replays a thought.
  - The LeanScope panel shows the persona (photo, name) and its voice, with "▶ Test voice".

## Driving the pointer yourself, and the physical Sphero mouse

- **🖱 Control** (above the live view) lets your own mouse drive the remote pointer, like the
  JetKVM web UI: hovering moves it, press and release click (left, right, middle), and the
  wheel scrolls.
  - Coordinates are absolute.
  - Moves go out at most 10 times a second, and the live view refreshes about every 0.4 s
    while Control is on.
  - The dashboard refuses input while a run is going (cancel or pause the run first).
- **Pointer dot:** a dot on the live view shows where the pointer is, after your moves and
  after the agent's.
- **Physical mouse (Sphero):** tick "Mirror the pointer to the Sphero mouse", enter the Sphero
  server's address (from its log, e.g. `http://192.168.1.20:8765`) and press Connect.
  - The CUA service checks `/health`, then sends every pointer target, yours and the agent's,
    as `POST /target {x, y}` in percent of the screen.
  - The latest target wins, and at most 10 targets go out per second.
  - "Stop the mouse" sends `POST /stop`. Turning the mirror off sends `/stop` too.
  - The dot turns green while the mirror is on.
  - Rejections (e.g. the Sphero web app is not calibrated) and connection errors show up once
    each in the activity log.
  - On/off and the address are kept in `CUA_SPHERO_STATE_FILE` (default `.sphero-mouse.json`,
    git-ignored). `CUA_SPHERO_URL` sets the first address.
  - The Sphero server has no authentication: use it only on trusted networks.

## Performance (M3 Pro, 1920×1280 screen)

| Stage | Time |
|---|---|
| YOLO icon detection | 0.1 s |
| Apple Vision OCR | 0.5 s |
| Florence-2 captions (off by default, `CUA_CAPTION_ICONS=true`) | about 0.2 s per *new* icon (cached by content, so repeated toolbars and taskbars are free) |
| Typical screen | about 0.4 s; with captions 8–20 s for a new screen, 0.4 s once its icons are cached |
| Model loading | about 10 s, done once on first use |

## Code map

| Path | What it does |
|---|---|
| `kvm/device.py` | `KvmDevice` interface; `HttpKvm` for the real device; `FakeKvm` for replays and tests |
| `kvm/decode.py` | Decodes the device's GOP with PyAV, keeping the last picture |
| `kvm/actuation.py` | Human-like mouse paths, clicks, scrolling and typing |
| `kvm/sphero.py` | Mirrors pointer targets to the physical Sphero mouse server (rate-limited, own thread) |
| `kvm/keymap.py` | Text and key combos to HID codes, using `data/keymaps.json`, which is generated from `ui/src/keyboardLayouts` by `scripts/export_keymaps.sh` |
| `perception/parser.py` | OmniParser v2 (also `ocr`-only and `null` parsers) |
| `perception/screen.py` | Change detection and new-element detection |
| `decision/jev.py`, `decision/decider.py` | Jev client (a port of the agent server's) and the option/state building |
| `agent/loop.py` | The loop, including discard/undo, scroll bookkeeping and the time budget |
| `agent/inbox.py` | Human commands for a running loop: guidance, answer, pause, resume, cancel |
| `narration/vision.py` | Rough screen description (OpenAI-compatible image-to-text) |
| `narration/agent_server.py`, `narration/session.py`, `narration/publisher.py` | One LeanScope session per run: run-choreo, worker dispatch, activity narration |
| `narration/persona_voice.py`, `narration/inner_voice.py` | The acting persona and its ElevenLabs voice; the inner voice (thoughts and speech) |
| `mcp_server.py` | MCP tools, the prompt, the dashboard routes, `/worker/dispatch` and bearer auth |

Tests: `uv run pytest`. On the firmware side: `go test ./internal/cuaapi/`, which runs on any host.
`cua_api_test.go` runs on the device with `./dev_deploy.sh -r <ip> --run-go-tests`.

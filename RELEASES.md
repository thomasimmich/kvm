# Release notes

## CUA 0.2.0: follow and steer a run live (2026-11-08)

**`cua/` service**
- **Steering:** guidance, pause, resume and cancel reach a running loop.
  - Guidance goes into Jev's state as `HUMAN GUIDANCE`, with the highest priority.
  - When Jev gives up, or after three discarded attempts in a row, the agent asks the human and
    waits up to `CUA_HUMAN_WAIT_S` for an answer. It only waits when someone can answer.
  - The dashboard has a "Tell the agent" field with Pause and Resume.
- **Rough screen description:** a fast vision model (OpenAI-compatible, `gpt-4.1-mini` by
  default) describes each new view, next to OmniParser and without delaying decisions.
- **Live narration into LeanScope:** with the `LEANSCOPE_*` variables set, every run starts
  its own agent-server session of the narration choreo (run-choreo plugin route). The CUA is
  that choreo's 🦾 Worker (`/worker/dispatch`). It writes into the worker step's activity:
  - screenshots
  - descriptions
  - annotated screens with element lists
  - decisions and micro steps

  Each entry starts with a tag. Answers to the choreo's guidance need come back as commands.
- **Dashboard:** a "Remote control" panel with a QR code (agent-server URL plus session token)
  for the remote control app. The log also shows descriptions, micro steps and human input.
- `cua_run_goal` returns the live session link.

## CUA 0.1.0: the KVM becomes a computer-using agent (2026-11-07)

**Firmware (device side)**
- Adds a new bearer-token API under `/api/cua`, separate from the browser login:
  - `GET /state`
  - `GET /frame`: H.264/H.265 data from the last keyframe on, enough to decode the current picture
  - `POST /mouse`, `/wheel`, `/keys`: lists of HID events, played on the device with real timing
- A browser session can stay open while the agent works. Without a browser, a 60-second lease
  keeps the video encoder (and the host display advertisement) running for the agent.
- When a browser connects with a different codec, the encoder is restarted instead of
  silently keeping the wrong codec.
- A browser tab in the background no longer blinds the agent. The web UI's background pause now
  says `reason: "hidden"`, and the device ignores that while the agent reads the screen. The
  virtual-media pause still stops video, and the agent then gets a clear error.
- The API is off by default. Turn it on with the JSON-RPC calls `setCuaApiConfig` / `getCuaApiConfig`.

**`cua/` service (off-device, Python)**
- **Screen parsing:** OmniParser v2, with YOLO icon detection, Florence-2 icon captions (cached)
  and OCR (Apple Vision on macOS, EasyOCR elsewhere).
- **Decisions:** TypeSafe Jev. Each step makes one call asking two questions: did the last action help, and what next?
- **Human-like input:** curved, eased mouse paths; wheel scrolling in all four directions; typing that uses the web UI's
  keyboard layouts.
- **Agent loop:** an undo/discard step (Escape and exclude the element), detection of clicks that had
  no effect and of scrolls that reached the end, and run traces on disk.
- **MCP server:** a Streamable-HTTP server with 7 tools and the prompt
  `additional_agent_instructions`, plus a live dashboard and a `cua` CLI.
- **Menus:** a click outside an open menu (which on macOS only closes the menu) is repeated
  once instead of being judged a failure. Jev now also sees which elements appeared after an action.
- **Clicking by text** tolerates small OCR misreads, for example when the pointer covers a letter.
- **Device-free testing:** `--fake` replays screenshots instead of talking to a real device.

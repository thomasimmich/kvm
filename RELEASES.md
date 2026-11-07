# Release notes

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
- **Clicking by text** tolerates small OCR misreads, for example when the pointer covers a letter.
- **Device-free testing:** `--fake` replays screenshots instead of talking to a real device.

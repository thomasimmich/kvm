# Handoff: `leanscope-kvm-cua` — a tool service for the LeanScope `cua` channels

Copy this file into an agent session in the **`leanscope-kvm-cua`** repository (the Python service
under `cua/`). It describes work that belongs there, not in `leanscope-mcp-server`.

## Context

A persona is to work towards a goal on a real computer, and the journey is to be stored in a
LeanScope repository as an "interaction" scenario. The architecture:

```
JetKVM device <- kvm-cua service (MCP) <- cua channels (leanscope-mcp-server) <- agent server
              <- LeanScope repository <- timeline app
```

- **The agent server is the brain.** It runs a choreo; the model of the actor leading a step calls
  this service's primitive tools (look at the screen, click, type) through the MCP server's `cua`
  channel. This service's own decision loop (`cua_run_goal`, Jev) is not used on this path.
- **The agent server is the recorder.** It stores each action as a scenario step and files the
  screenshot a tool returns. This service must therefore know nothing about LeanScope on this
  path: no session token, no upload, no narration.
- **A supervisor actor resets the computer** as the first tool call of a choreo, through a second
  channel, `cua-operator`.

`leanscope-mcp-server` 0.15.0 already proxies to this service. It calls these tools by name, with
these arguments (`src/channels/cua/cua-service-client.ts` there):

| Channel tool | Calls here | Arguments |
|---|---|---|
| `computer_get_screen` | `cua_observe` | `max_chars?` |
| `computer_click_element` | `cua_click_element` | `element_id`, `button?`, `double?` |
| `computer_click_text` | `cua_click_text` | `text`, `double?` |
| `computer_type_text` | `cua_type_text` | `text` |
| `computer_press_keys` | `cua_press_keys` | `combo` |
| `computer_scroll` | `cua_scroll` | `direction`, `notches?` |
| `computer_reset` | `cua_reset` (**missing**) | none |

It sends `Authorization: Bearer <CUA_SERVICE_TOKEN>` (one static token, as `BearerAuth` checks
today), refuses redirects, opens one MCP connection per call, and hands every content block of a
result back unchanged. Do not rename the existing tools or arguments.

## Work

1. **Return the screen as content, not only as text.** Today every tool is declared with
   `structured_output=False` and returns plain text (`cua/src/kvm_cua/mcp_server.py:63-123`). Each
   tool that yields a screen should return:
   - the text it returns today (unchanged, first block),
   - an image block with the screenshot: JPEG, at most 1280 px wide, quality 80 (the size
     `publisher.py:40-46` already produces for narration),
   - the parsed element list as structured content (the shape of `screen_NNN.elements.json`:
     `id, kind, label, bbox, interactable, score, source`).
   The agent server files the image and the element list on the recorded step.
2. **Add `cua_reset`.** There is no machine reset today; `dashboard/api/reset` only clears the
   activity log (`service.py:318-324`). Define a start-state procedure the device owns (for
   example: close every window, show the desktop) and expose it as one tool with no arguments. It
   must be safe to call twice. What "start state" means may be configured per device; it is not an
   argument, because the caller does not know the machine. Return the screen afterwards.
3. **Report progress while perceiving.** Capturing and parsing a screen takes seconds. Send MCP
   progress notifications with a short message per phase ("capturing the screen", "parsing icons").
   Note: the MCP server's HTTP transport cannot relay them yet; this is preparation.
4. **Reachability.** The hosted MCP server must reach this service. The service URL goes into a
   LeanScope Computer resource and its host must be on the MCP server's `CUA_ALLOWED_HOSTS`. Decide
   and document how the service is exposed (Tailscale, tunnel); `docs/agent-server-integration.md:12`
   already suggests Tailscale.

## Decision needed here

**Device auth.** Today: one static bearer per service, and loopback clients may skip it
(`mcp_server.py:460-528`). That is what the MCP server sends. If several computers must not share
one token, the alternative is a per-device token that the MCP server holds per Computer resource;
say so if this service should move to that, because it changes the MCP server's configuration. The
MCP server deliberately does not forward the caller's LeanScope token.

## Out of scope

The dashboard, `cua_run_goal`, the narration path (`narration/`), and the firmware.

## Verification

- An MCP client that calls `cua_observe` gets a text block, an image block and structured elements.
- `cua_reset` twice in a row leaves the same screen.
- Through the MCP server: with a Computer resource pointing here, `computer_get_screen` on
  `/mcp/<repositoryId>/cua` returns the same blocks, and `computer_reset` on
  `/mcp/<repositoryId>/cua-operator` succeeds.

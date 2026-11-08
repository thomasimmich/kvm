# Connecting the CUA to the LeanScope agent server

The CUA is a plain **Streamable-HTTP MCP server**. The agent server already knows how to use
those, so the basic connection needs **no agent-server code**, only data in LeanScope. See
`leanscope-agent-server/docs/mcp-server-resource.md`.

## 1. Run the CUA where the agent server can reach it

| Setup | CUA URL | Notes |
|---|---|---|
| Both on the Mac | `http://127.0.0.1:8765/mcp` | Plain HTTP needs `LEANSCOPE_MCP_ALLOW_INSECURE=1` on the agent server. |
| CUA in the cloud | `https://cua.<domain>/mcp` | Put it behind TLS. The CUA reaches the KVM over Tailscale, which the firmware already supports. Set `KVM_URL=http://<kvm-tailnet-ip>`. |

Always set `CUA_MCP_TOKEN`. The service refuses to listen on a non-loopback address without it.

## 2. Author the resource in LeanScope

Create the following:

1. A resource **"KVM Computer-Using Agent"**.
2. An `IS` ResourceRelationship from that resource to the **🫴 MCP Server** type
   (`8c4210d9-1cc3-43db-bdc8-b795ebc549c9`), with the parameter override `url = <CUA URL>`.
3. A relationship that attaches the resource to the scenario, or to a single step. The attaching
   relationship may override `url` again, for example so that one choreo uses a different KVM.

The engine then does three things for every step that sees the resource:
- It lists the 7 `cua_*` tools. MCP tools are always added, whatever tier the other tools come from.
- It appends the prompt `additional_agent_instructions` to the step prompt. This is the CUA
  background knowledge; edit `cua/knowledge/additional_agent_instructions.md` to change it.
- The step's LLM calls `cua_run_goal`, with the step's text as the goal and with hints and inputs
  taken from the step and persona context.

## 3. Credentials

The agent server sends its **own choreo bearer** to hosts it trusts: `leanscope.io`,
`leanscope.ai` and `localhost`. It sends nothing to any other host. In both cases, pass the CUA
token explicitly in the session request:

```json
{ "mcpTokens": { "http://127.0.0.1:8765/mcp": "<CUA_MCP_TOKEN>" } }
```

An entry for the exact URL takes precedence over the choreo bearer. For purely local
experiments, you can also run the CUA on loopback without `CUA_MCP_TOKEN`.

## 4. Limits to keep in mind

| Limit in the agent server | How the CUA handles it |
|---|---|
| 120 s per MCP tool call (`DEFAULT_CALL_TIMEOUT_MS`) | `cua_run_goal` stops starting new steps after `time_budget_s` (default 100 s) and returns `status: time_budget`. The LLM can call it again to continue. |
| 10 model calls per turn | One `cua_run_goal` call runs the whole inner loop, so the LLM usually needs just one or two calls. |
| Tool results flattened to text; images become `[image/png]` | Every tool returns text: the element list and the step summary. |
| At most 32 tools per server | The CUA exposes 7. |

## 5. Live narration and remote control

Each run (dashboard Run or `cua_run_goal`) can get its own LeanScope session. A remote control
app can follow that session and steer it. This works over REST only, with no GraphQL.

### Flow

1. **The CUA starts the session.** It calls `POST {LEANSCOPE_AGENT_SERVER_URL}/api/v1/plugins/run-choreo`
   with `{repositoryId, scenarioGuid, sessionRepositoryId?, goal, hints, inputNames}`, then
   `POST /api/v1/sessions/:token/start`.
   - Input values never leave the CUA, because they can be passwords. Only their names are sent.
2. **The engine dispatches the CUA.** The choreo's run step carries a 🦾 Worker:
   - its dispatcher is `CUA_PUBLIC_URL/worker/dispatch?key=CUA_WORKER_KEY`
   - `waitForCompletion` is `"false"` (asynchronous)

   The engine POSTs the worker body. The CUA matches it to the waiting run by `sessionToken` and
   answers `202`.
   - If no dispatch arrives within `CUA_DISPATCH_TIMEOUT_S` (20 s), the run goes ahead unnarrated.
     The dashboard shows why.
3. **The CUA narrates.** It writes into the worker step's activity with
   `PATCH {config.endpoints.activityApiUrl}`, one write at a time, in order.
   - Screenshots are uploaded first with `POST /api/v1/sessions/:token/files`. Entries show them
     as markdown images.
   - The last write carries `phase: succeeded` (status `done`) or `failed` (any other status),
     with a summary. That settles the worker step.
4. **The remote control steers.**
   - It reads the session with `GET /api/v1/sessions/:token/events` (SSE). The activity is in the
     worker step's `meanTime`.
   - It answers the choreo's guidance step with `POST /api/v1/sessions/:token/submit`.
   - A Worker on that step's need, with the same dispatcher, is dispatched with the answer as
     `config.context.input`. The CUA hands it to the running loop and settles that short need
     run with a summary such as "Passed to the CUA: guidance: …".

The dashboard's "Remote control" panel shows:
- the session status
- the dispatcher URL to put into the choreo
- a QR code for `LEANSCOPE_REMOTE_CONTROL_URL?agentServer=<url>&sessionToken=<token>` (the
  LeanScope session link, when no remote control URL is set)

`cua_run_goal` adds the session link to its summary (`live session: …`).

### Activity entries

Each entry is markdown, and its first word is a tag:

| Tag | Content |
|---|---|
| `[screen:N]` | Screen N as a raw screenshot (image) |
| `[desc:N]` | The rough description of screen N from the vision model (arrives a moment later, only for new views) |
| `[elements:N]` | The annotated screenshot and the dense element list (`[id] kind 'label' at position …`) |
| `[decision:S]` | Step S: the next action, its confidence, alternatives, the verdict on the previous action, and the chosen element highlighted |
| `[activity]` | Micro steps: looking at the screen, asking Jev, pressing Esc to undo, no visible effect, … |
| `[human]` | Guidance, an answer, pause, resume or cancel that arrived |
| `[ask]` | The CUA is stuck and waits for an answer (`phase: pending`) |
| `[result]` | The run's summary |

N counts screens and S counts steps.
- `summary` holds the current step (`Step 3/15: click …`).
- `progress` is step / max steps.
- `phase` is `working`, `pausing` (paused), `pending` (asking), `succeeded` or `failed`.

### Commands (`context.input`, or the dashboard's `/dashboard/api/guidance`)

- JSON `{"command": "guidance" | "answer" | "pause" | "resume" | "cancel", "text": "…"}`.
  `guidance` and `answer` need a text.
- Or plain text. The words `pause`/`wait`/`warte`, `resume`/`continue`/`weiter` and
  `cancel`/`stop`/`abbrechen` are commands. Anything else is guidance.
- A dict without `command` but with one text value (for example `{"message": "…"}`) is guidance.

How the loop handles them:
- Pause holds the loop before its next decision, or before it looks at the screen after an action.
- Guidance and answers end a pause.
- `time_budget_s` keeps counting while paused, so an MCP caller still gets its answer in time.

### Needed from the other repositories

- **LeanScope choreo:**
  - a run step with the Worker described above
  - a human guidance step whose need carries a Worker on the same dispatcher
- **Agent server:** callback URLs that reach the CUA (`LEANSCOPE_CALLBACK_ORIGIN`). If they
  don't, the CUA builds the activity URL from `LEANSCOPE_AGENT_SERVER_URL` itself.
- **Remote control app:** reads `/events` and submits commands via `/submit`; opening the mic
  sends `pause`.

## Possible agent-server changes later (not needed for the first version)

- Resolve Jev as a scenario resource (the existing 🎯 Decision Model) and pass the model key to the
  CUA. That would let the scenario choose the decision model, instead of `JEV_MODEL` in the CUA's
  environment.
- Pass multimodal tool results through, so a vision LLM in the choreo could also look at the screenshot.
- Raise the call timeout for this server, so long runs need fewer continuation calls.
- A worker-scoped route to append real session steps with file attachments, so screens could be
  steps with resources instead of activity entries. See the follow-up below.
- A read-only viewer token, so a QR code does not hand out a session token that can also submit.

## Follow-up: narrate as choreo steps instead of activity entries

Idea: write the narration as choreo steps into the run's disposable choreo copy, the way the
group-discussion example grows its choreo (`examples/group-discussion` in the agent server,
`docs/emergent-choreo.md`). The steps would then be real conversation turns, spoken by TTS.
We checked this on 2026-11-08 against the agent server and stayed with activity entries,
because today a Worker can't get the same effect:

| | Group discussion (director) | CUA (🦾 Worker) |
|---|---|---|
| Who writes | the director persona, with the in-engine `write_choreo_step` Function, inside its own LLM turn | an outside process, with `POST {choreoStepsUrl}` `{after, line, actorRef?}` |
| When the step plays | right after the director's turn: the engine re-reads the copy and plays the new step next | only after the Worker has finished: while it runs, the engine holds the Worker step (`holdWhileWorkerPending` in `src/engine/agent-engine.ts`) and walks into inserted steps afterwards |
| How it plays | one LLM turn of the speaking persona | the same: the line becomes a task for the persona ("Your current task now is: …"), so it is rewritten, not shown word for word |

Further limits of the insert route:
- **Text only.** The body is strictly `{after, line, actorRef?}`, so a file can't be attached to
  a step. An image link in the line only shows up if the persona's model copies it into its reply.
- **No step ID back.** It answers only `202`. Several inserts after the same step therefore end
  up in reverse order (`X → C → B → A`), and a second insert can't be chained after the first.
- **One firing per step.** A step's Worker fires only once per run, even when the step is
  revisited, and inserted steps carry no Worker. So "one Worker call per CUA iteration" can't
  be built as a loop either.
- **The loop guard.** `src/engine/infinite-loop-guard.ts` stops after 100 automatic
  transitions without human input, and every inserted step counts as one.

What the agent server would need for live, narrated choreo steps from a Worker:
1. Play steps a Worker inserts while that Worker is still running. The hold walks into
   them, then returns to the Worker step.
2. Return the new step's ID from `POST …/choreo/steps`, so inserts can be chained in order.
3. A word-for-word step: no LLM call, the line written as it is. It could also take image or
   file attachments in the body.
4. Optionally, speak activity entries instead (a follow-up already listed in the agent
   server's `docs/activity-narration.md`). This would also give spoken narration without
   choreo steps.

With 1–3 in place, the CUA side would be small:
- `NarrationPublisher` would write `[desc]`, `[elements]` and `[decision]` as choreo steps,
  using `config.context.choreoStepsUrl` and `insertAfterStepGuid` from the worker body.
- The micro `[activity]` lines would stay in the activity.

## 6. Inner voice of the acting persona

The inner voice (see the README) plays in the CUA dashboard only; its thoughts and clips do not go
into the session. It reads from LeanScope with the dashboard login, in the picked choreo's
repository: the choreo's `primaryActorRef`, the persona's profile, and the persona's resource
relationships (breadth-first, up to 3 edges). The nearest `voiceId` (parameter
`94896ee6-bf6c-46a1-b404-e872365ef034`, or the legacy `voice` parameter) is the ElevenLabs voice.
The persona's own `voiceId` field only counts when it looks like an ElevenLabs id, since it
usually names an OpenAI voice.

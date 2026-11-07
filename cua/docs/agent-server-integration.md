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

## Possible agent-server changes later (not needed for the first version)

- Resolve Jev as a scenario resource (the existing 🎯 Decision Model) and pass the model key to the
  CUA. That would let the scenario choose the decision model, instead of `JEV_MODEL` in the CUA's
  environment.
- Pass multimodal tool results through, so a vision LLM in the choreo could also look at the screenshot.
- Raise the call timeout for this server, so long runs need fewer continuation calls.

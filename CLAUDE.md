## Repository layout
- Root: JetKVM firmware (Go backend, C native layer in `internal/native/cgo`, React UI in `ui/`).
  See DEVELOPMENT.md for building and deploying to a device.
- `internal/cuaapi/` + `cua_api.go`: the device side of the computer-using agent (CUA):
  a bearer-token HTTP API under `/api/cua` for frames and HID input.
- `cua/`: the off-device CUA service (Python, uv). It parses the screen (OmniParser v2), decides
  with TypeSafe Jev, drives the mouse and keyboard, and is exposed to the LeanScope agent server
  as an MCP server. See `cua/README.md`.

## Versioning
- Change the CUA version number in `cua/pyproject.toml` after each session (only in case there was a commit in between!). For minor things (fixes and mini enhancements), increase the third number, for major update that lead to a committed plan change the second number. The first number I will change manually, as it stands for major upGrades, not majore upDates. Leave the JetKVM firmware version (Makefile, upstream releases) alone.
- Track all major updates and upgrades in a RELEASES.md release notes file.

## Spelling

Always write **LeanScope** with a capital L and a capital S: in prose, comments, UI text and in
PascalCase or camelCase identifiers (`LeanScopeConfig`, `LeanScopeSessionService`,
`provideLeanScope`). Never write `Leanscope` or `Lean Scope`.

Exceptions, where the casing is fixed by the convention of the context:
- all lowercase: file and folder names, URLs, package and repository names, CSS classes
  (`leanscope-session.service.ts`, `leanscope-agent-kit`, `app.leanscope.ai`)
- all caps: constants and environment variables (`LEANSCOPE_CONFIG`, `LEANSCOPE_OPENAI_API_KEY`)

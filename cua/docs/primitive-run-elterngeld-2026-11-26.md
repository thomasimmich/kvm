# Primitive-tool run: Elterngeldantrag Saarland (2026-11-26)

**Task.** Fill out the Elterngeld online application (service.buergerdienste-saar.de, Safari on the
remote Mac) for the persona Sabine Meier, with the fictitious test data. Only the primitive MCP tools
were allowed (`cua_observe`, `cua_click_element`, `cua_type_text`, `cua_press_keys`, …), not
`cua_run_goal`. They were called through a one-shot Python MCP client against `cua serve` on :8767.

## Outcome: aborted on page 2 of about 20 (progress indicator 5 %)

| Page | Done | Calls | Wall time |
|---|---|---|---|
| Informationen zum Elterngeldantrag | Weiter | 2 | ~35 s |
| Wohnsitz / Arbeitsplatz | "Ja, PLZ 66740", Weiter | 5 (1 wrong click) | ~3 min |
| Angaben zum Kind | first and last name, Adoption "nein". The due date is still wrong (26.11.0026). Not done: birth date, place of birth | 10 | ~35 min |

The run was stopped once one primitive took **5–7 minutes** (parse 314–439 s). At the same time the
portal's per-page timer ("30 Minuten Zeit zum Ausfüllen für diese Seite") was at "noch eine Minute".
At that speed, a form with ~20 pages cannot be finished.

**Environment finding:** the macOS swap on the CUA host was full (31.9 of 32 GB used), and the host
had paged out heavily. That explains most of the slowdown from about 16 s to over 400 s per parse.
It is still a tool problem: the service has no time budget per primitive and no way to skip the
parse.

## Stuck incidents, grouped by root cause

### 1. Latency: every primitive does a full OmniParser pass (blocking)
- Act tools always re-observe: OCR plus icon captioning on the full 1920×1080 frame. The parse took
  16 s when healthy, 30–85 s on a normal page and 155 s with a date popover open. Under memory
  pressure it took 300–440 s.
- Moving through four text fields cost about 5 minutes. The Tab key worked well between fields, but
  each Tab still paid for a full parse.

### 2. Element ids are not stable → a wrong click
- Each act tool re-observes and renumbers. An id from the screen before the previous action, the
  postcode box (52), was the "Nein" radio on the new screen. The click selected "Nein", and nothing in
  the result said so.

### 3. Form controls are not recognised as form controls
- Radio button → `icon 'a loading or buffering indicator'`, empty text box → `'a line or rectangle'`,
  checkbox → `'A rectangle or rectangle shape'`, select → `'a text editor or screen view'`. All are of
  kind `icon` and are not linked to their label. I had to read the screenshot to find the inputs.
- Two radios on one line merged into a single OCR element, `'O nein O ja'`. There is no element for
  "nein", so a click on the id lands at the bbox center, between the two options. It worked here only
  because the center happened to fall on the "nein" label.

### 4. Native date inputs (Safari segmented DD.MM.YYYY + calendar popover)
- A click on the bbox center selects the **year** segment, so typing "21.08.2026" put "0026" into the
  year.
- Fixing it took shift+tab ×2 and then typing "21082026", three calls and about 18 minutes. The final
  screenshot still shows the old day, so the typed digits did not visibly land. It was not possible
  to check this further.
- The open popover adds about 40 elements (calendar digits) and roughly doubles the parse time.

### 5. Readback and verification
- Typed values are read back with caret artefacts ("Lucal" for "Luca|").
- No result reports what the action hit or which element now has focus.

### 6. Setup friction
- The `.venv`'s MCP SDK renamed its client API (`streamable_http_client`, `is_error`).
- The server ran on :8767 while `.env` says 8765.

## Proposed improvements (ranked)

1. **`observe: bool = true` (or `parse: "full"|"none"|"ocr"`) on every act tool** (`mcp_server.py`,
   `service._act_then_observe`). With `none` the tool returns only the screenshot. A caller can chain
   click → type → tab → type and parse once at the end.
2. **A hard time budget per primitive.** On timeout, return the raw screenshot plus a `parse_timeout`
   flag instead of blocking for minutes. Report the host's memory pressure on `/healthz` and the
   dashboard.
3. **Stable, seq-scoped ids.** Carry ids over between consecutive screens by matching bbox and label,
   and/or accept `"<seq>:<id>"` and reject stale ids with an error. Every click result should echo
   `clicked [id] <kind> '<label>' at (x,y)`.
4. **Form-widget classification in `perception/parser.py`.** Add kinds `radio`, `checkbox`,
   `textbox`, `select`, `date`, `button`, using shape heuristics or a small widget detector, and link
   each widget to its neighbouring label: `radio 'nein' (checked)` and `textbox 'Vorname' = 'Luca'`.
   Skip Florence captions for these widgets; that is also faster.
5. **Split OCR lines** at radio or checkbox glyphs and at large gaps, so that each option is its own
   element.
6. **New primitives:**
   - `cua_click_at(x, y)` as an escape hatch.
   - `cua_click_element(id, anchor="left|center|right")`.
   - `cua_press_keys` taking a list (`["shift+tab","shift+tab"]`).
   - `cua_fill_field(label, value)`: find the textbox by its label, click it, select all, type.
   - `cua_set_date(label|id, "2026-08-21")`: click the left edge, type DDMMYYYY.
   - `cua_select_option(label|id, option)`.
7. **Knowledge** (`knowledge/additional_agent_instructions.md`):
   - Safari date inputs are segmented, so click the left edge and type digits without dots.
   - Tab moves between text fields.
   - This portal has a 30-minute limit per page and offers "Ich brauche mehr Zeit".
8. **Readback hygiene:** strip a trailing caret glyph from field values, and report the focused
   element after typing or pressing a key.

## Data not entered yet
Everything after "Angaben zum Kind": parents, health insurance, bank details, income, tax and the
choice of Lebensmonate. The run never reached the submit step.

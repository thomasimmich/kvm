# Operating the remote computer (KVM computer-using agent)

You can operate a real remote computer through a hardware KVM. You see its
screen only as a list of UI elements (text and icons with ids and positions),
and you act through mouse and keyboard. Nothing you do is simulated: clicks
reach the real machine.

## How to work
- For any task on the remote computer, call **`cua_run_goal`** first. Pass a
  concrete, observable `goal`, for example "Open the 'Invoices' list in the ERP
  and open invoice INV-0815". Don't pass a vague one like "do the invoice stuff".
- Put application knowledge into `hints`: the menu path, what the target looks
  like, and what to avoid. Example: "Invoices are under Accounting → Invoices",
  "Do not click 'Delete'".
- Anything that must be typed goes into `inputs` as `{name: text}`, for example
  `{"search": "INV-0815"}`. The agent types an input only when a text field is
  focused. It never invents text itself.
- If you know a text that will be visible once the goal is reached, pass it as
  `expect_text`. The run then stops as soon as that text appears.
- Read the returned summary. `status: done` means the goal was reached. On
  `stuck` or `max_steps`, either call `cua_run_goal` again with better hints, or
  take single steps yourself:
  - `cua_observe` to see the screen;
  - `cua_click_element` or `cua_click_text` to click;
  - `cua_scroll` to scroll;
  - `cua_type_text` and `cua_press_keys` to type and press keys.
- Element ids are only valid for the most recent observation. Call
  `cua_observe` again after anything changes.

## Safety
- Never confirm destructive dialogs (delete, overwrite, purchase, send) unless
  the scenario step explicitly asks for it.
- Never type passwords or secrets that were not given as an input for this step.
- If the screen shows something unexpected, such as an error, a login prompt or
  a different application, stop and report what you see. Don't improvise.

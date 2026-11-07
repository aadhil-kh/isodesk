# isodesk

Isolated Windows desktop sessions, exposed as an MCP server.

Each session is a separate Win32 desktop object with its own foreground window,
focus, z-order and cursor. An agent can launch a GUI app there, look at it, and
drive it — while you keep working on your own desktop, undisturbed. No RDP, no
VM, no admin rights, no licensing constraints. Works on Windows 11 Home.

The calling model *is* the agent. There is no HostAgent, no AppAgent and no LLM
in this process — just tools.

```
Claude Code (or any MCP client)
      │  MCP over stdio
      ▼
isodesk MCP server              ← stays on your desktop
      │  named pipe, JSON-RPC
      ▼
isodesk worker                  ← one process per session, on the isolated desktop
      │  UI Automation · PrintWindow
      ▼
your WinUI 3 / WPF / WinForms app
```

## Install

This repository is a plugin marketplace for Claude Code, OpenAI Codex and GitHub
Copilot CLI. The `isodesk` plugin in it carries the MCP server and the skill that
tells the agent how to drive it.

Requirements: Windows 10/11, and Python 3.10+ on `PATH` as `python`. No admin
rights.

**Claude Code** - the terminal, the IDE extensions and the Claude desktop app's
**Code** tab, which all share one plugin install:

```bash
claude plugin marketplace add aadhil-kh/isodesk
claude plugin install isodesk@isodesk
```

Or inside a session: `/plugin marketplace add aadhil-kh/isodesk`, then
`/plugin install isodesk@isodesk`. In the desktop app's Code tab, add the
marketplace the same way and install from **+ > Add plugins**.

**Codex:**

```bash
codex plugin marketplace add aadhil-kh/isodesk
```

then install `isodesk` from the plugin directory (`/plugins`).

**GitHub Copilot CLI:**

```bash
copilot plugin marketplace add aadhil-kh/isodesk
copilot plugin install isodesk@isodesk
```

The first time the server starts, `plugins/isodesk/scripts/launch.py` builds a
private venv under the host's data directory for the plugin (for Claude Code,
`~/.claude/plugins/data/isodesk-isodesk/`) and installs the exact dependency
set pinned in `plugins/isodesk/server/requirements.lock`. That takes about a
minute, once, which is longer than clients wait for a server to start, so on
that first start isodesk reports that it is still installing and fails to
connect. The install carries on in the background; reconnect it a minute later
(`/mcp` in Claude Code) or start a new session. Later starts reuse the venv,
plugin updates that only change
isodesk's code reuse it too, and a change to the lock file or to the system
Python builds a new one. To build it ahead of time, or to see install errors
directly:

```bash
python ~/.claude/plugins/cache/isodesk/isodesk/<version>/scripts/launch.py --prepare
```

The install log is `install.log` in the same data directory.

### Updating

Third-party marketplaces do not auto-update by default. Pull a new version with

```bash
claude plugin marketplace update isodesk
claude plugin update isodesk@isodesk
```

or turn on auto-update for the marketplace in `/plugin` > **Marketplaces**.
In Codex, `codex plugin marketplace upgrade isodesk`.

The plugin's `version` pins what users get, so bump it with every release, in
both `plugins/isodesk/plugin.json` and `plugins/isodesk/.claude-plugin/plugin.json`.

### Other MCP clients

The server itself is a plain stdio MCP server with no client-specific
behaviour. For any other client, point it at `python
<checkout>/plugins/isodesk/scripts/launch.py`, or install the package into an
environment of your own with `pip install ./plugins/isodesk/server` and run
`isodesk-mcp`.

## Use as a library

```python
from isodesk import SessionManager

manager = SessionManager()
session = manager.create(label="my-app")
try:
    session.call("activate", aumid="Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App")

    controls = session.call("get_controls")["controls"]
    design = next(c for c in controls if c["name"] == "Design")
    session.call("click_control", control_id=design["id"])

    shot = session.call("screenshot", annotated=True)   # base64 PNG
finally:
    manager.destroy(session.id)
```

## What works, and what does not

Everything runs through UI Automation, because a background desktop does not own
the input queue and so cannot receive synthetic keyboard or mouse events.

**Works**

- Launching unpackaged exes (`session_launch_app`) and packaged/Store apps
  (`session_activate_app`)
- Screenshots of DirectComposition-rendered windows, via
  `PrintWindow(PW_RENDERFULLCONTENT)` — the only capture path that works here
- Set-of-mark annotated screenshots for visual grounding
- UIA control enumeration and nested control trees, reporting the real XAML type
  (`xaml_type`) beside UIA's lossy `control_type`, and control state
  (`toggle_state`, `selected`, `expanded`, `value`) so reading a toggle does not
  cost a screenshot
- Listings written for an agent's context rather than for a parser: one line per
  control, rects in DIPs alone, and fields at their default omitted — about a
  fifth of the equivalent JSON, which `format="json"` still returns
- Scoped and filtered enumeration — `get_controls(scope=…)` for one subtree, and
  `xaml_type` / `control_type` / `name_contains` applied server-side — so a
  question about one card does not re-send the navigation pane
- `refresh_controls`, re-reading specific ids in one tree walk and answering
  `STALE` when the listing has moved on, instead of re-listing the window
- `measure`, comparing a few controls with the pairwise deltas and the verdicts
  (aligned, same width, the gap between them) computed rather than eyeballed
- DPI-aware geometry: `dpi_scale` and `rect_dips` on every payload, and
  `window_command` sizing in DIPs by default, matching `AdaptiveTrigger`
- `audit_accessibility`, listing actionable controls with no name and no
  AutomationId — unreachable for both automation and screen readers
- `module_path` / `module_mtime` on launch, to catch a stale deploy before it is
  mistaken for a change that did not work
- `click_control`, `set_text`, `toggle_control`, `select_control`,
  `expand_control`, `scroll_control`, `scroll_to_control`, `get_text`
- `scroll_to_control` for anything not selectable (an `Expander`, a `Group`), and
  scroll offsets (`vertical_percent`, `at_bottom`) on every scroll, so "did that
  go far enough" costs nothing
- Screenshots cropped to one control (`screenshot(control_id=…)`), for when the
  question is about a card rather than a screen — scrolling it into view first,
  because UIA reports the *clipped* rect and a control cut down by a scroll
  viewport otherwise crops to a sliver of nothing
- A `clipped` word on any listing row whose rect is a cut-down one, so "44x4" on a
  44×44 swatch reads as clipping rather than as a short control
- Crop echoes that name all three frames they pass through — what you requested
  (screen-absolute, in your units), what was cut from the capture (window-relative
  pixels, after clamping) and what was delivered (after the downscale)
- `click_xy`, which resolves the UIA element under the point and invokes its
  pattern — reporting `retargeted` when UIA hands back an ancestor or sibling of
  the point, refusing a point on the window frame's own caption buttons, and
  taking `method="uia"` to refuse rather than fall back to PostMessage
- Running console commands inside the session
- `session_list(include_orphans=true)` and `session_reap`, for desktops left
  behind by a host process that was killed rather than exiting

**Does not work — by nature of the platform, not for lack of trying**

- **Keyboard input of any kind.** No keystrokes, no shortcuts like Ctrl+S.
  `send_keys` raises with an explanatory message instead of silently doing
  nothing. Put text in with `set_text`; reach commands through their menu or
  toolbar control.
- **Drag operations.**
- **Pixel-precise clicking.** `click_xy` lands on the UIA element at that point,
  so custom-rendered UI with no automation peers cannot be driven — and UIA
  hit-testing may hand back an ancestor or a sibling of what is visually there,
  which the response reports as `retargeted` rather than leaving to be inferred.

If you need real keyboard input or true pixel-level control, that requires a
backend with its own input queue — a VM guest, or RDP on Windows Pro/Server. The
session abstraction is deliberately narrow enough that such a backend could slot
in behind the same MCP surface.

## Caveats

- **Single-instance apps.** Windows 11 Notepad, and apps like it, hand a new
  launch off to the already-running instance — which may live on your desktop, not
  the session's. Teardown never kills a process that owns any window on the host
  desktop, so your app will not be closed out from under you, but the session may
  not get its own instance.
- **`session_show` deliberately breaks isolation.** It switches the physical
  display to the session's desktop; `session_hide` switches back.
- This relies on compositing behaviour Microsoft does not document. Re-run
  `python spikes/spike_desktop.py --targets notepad` after major Windows or
  WindowsAppRuntime updates.

## Development

Layout:

```
.claude-plugin/marketplace.json      marketplace for Claude Code and Copilot CLI
.agents/plugins/marketplace.json     marketplace for Codex
plugins/isodesk/                     the plugin, copied as-is into users' plugin cache
  plugin.json                        Agent Plugins 1.0 manifest (Codex, Copilot CLI)
  mcp.json                           Agent Plugins 1.0 MCP config (Codex, Copilot CLI)
  .claude-plugin/plugin.json         Claude Code manifest
  .mcp.json                          Claude Code MCP config
  scripts/launch.py                  venv bootstrap + server launch (stdlib only)
  skills/isodesk/                    the skill
  server/isodesk/                    the Python package
  server/requirements.lock           pinned dependency set
tests/  spikes/                      development only; not part of the plugin
```

For a dev loop against this checkout, add it as a local marketplace; Claude Code
then reads the plugin straight from disk, and `/reload-plugins` picks up edits:

```bash
claude plugin marketplace add ./
claude plugin install isodesk@isodesk
claude plugin validate .
```

For the tests and spikes, use an environment with the package installed
editable (`pip install -e "plugins/isodesk/server[dev]"`) or the plugin's venv:

```bash
python spikes/spike_desktop.py --targets notepad   # capability matrix
python spikes/spike_input.py                       # input mechanism matrix
python tests/smoke_e2e.py                          # full stack against the Gallery
python tests/smoke_tuning.py                       # WinUI tuning, first cut
python tests/smoke_popups_scroll.py                # popups, scrolling, captions
python tests/smoke_lean_scope.py                   # lean listings, scope, measure
python -m pytest tests/test_host_isolation.py -v   # the invariant that matters
```

`plugins/isodesk/skills/isodesk/` holds the consuming harness's skill: the
operating discipline that does not belong in per-tool docstrings. It ships with
the plugin, so there is no separate copy to mirror.

To change a dependency, edit `server/pyproject.toml`, then regenerate
`server/requirements.lock` from a fresh venv (`pip install -e plugins/isodesk/server`
then `pip freeze --exclude-editable`), run the tests against it, and bump the
plugin version.

`tests/test_host_isolation.py` is the important one: it asserts that session
input never reaches a host sentinel window, that session windows never appear on
your desktop, that sessions cannot see each other, and that teardown never kills
your processes.

---
name: isodesk
description: "Drive a Windows GUI app (WinUI 3, WPF, WinForms) in an isolated isodesk desktop session on this machine, through UI Automation, without touching the user's own desktop and with no setup: launch it, read its control tree, invoke controls, measure and screenshot to verify. Use for quick checks while developing - confirming a UI change really renders, reproducing a GUI bug, comparing two controls' size or alignment, checking adaptive layout at several window sizes, auditing a screen - and for proving the running binary matches your build. It cannot type, send shortcuts, drag, hover or open file pickers. For those, or for a repeatable UI test script, use winapp-ui-automation or winui-ui-testing (Windows Sandbox keeps their input off the user's desktop); to see why a XAML property, style or binding resolved as it did, use winui-devtools."
---

# Driving apps with isodesk

The isodesk MCP tools (`mcp__plugin_isodesk_isodesk__*`) and their descriptions already state the session loop
(create → launch/activate → observe → act → destroy) and the platform limits
(UI Automation only; no keystrokes, no shortcuts, no drag). Do not re-derive those.

This skill is the operating discipline around them: what to ask first, what to distrust,
how to find a control the default listing omits, and how to tell a real result from a
stale one.

## Scope — and what this is not

| Situation | Use |
| --- | --- |
| Check a change renders, reproduce a bug, measure, try window sizes, audit a screen - without touching the user's desktop and with no setup | **isodesk** (this skill) |
| The flow needs typing, a keyboard shortcut, drag, hover, or a file or folder picker | `winapp ui` (`winapp-ui-automation` skill), with `--on sandbox` when Windows Sandbox is available. Without Sandbox its input verbs take over the user's real foreground, cursor and keyboard, so tell the user before running them |
| Repeatable pass/fail UI tests as a script | `winui-ui-testing` skill (`winapp ui`, in Sandbox or locally) |
| Why a margin, style or binding resolved as it did, or which XAML line made an element | `winui-devtools` skill |
| Web page, not a desktop app | Browser tools |

isodesk and `winapp ui` are different mechanisms against different desktops. Never mix
their identifiers: isodesk control ids (`c14`) and `winapp` AutomationIds are unrelated.

## Cheapest question first

Every question here has a cheap form and an expensive one, and the expensive one is the
default habit. Pick the smallest call that actually answers what you are asking:

| Question | Call | Cost, roughly |
| --- | --- | --- |
| Are these two controls the same size, aligned, spaced right? | `measure(control_ids=[…])` | a few hundred characters |
| Did that row change — move, toggle, disappear? | `refresh_controls(ids=[…])` | ~170 chars for three ids |
| What is on this part of the page? | `get_controls(scope=…)`, or `xaml_type=` / `name_contains=` | a fraction of a listing |
| What is on this page? | `get_controls` | ~2.4k chars for 36 controls |
| Does this one control look right? | `screenshot(control_id=…, padding=8)` | a few hundred bytes |
| Does this screen look right? | `screenshot` | a few hundred **kilo**bytes |

Three rules follow:

- **Never re-list a window to ask about controls you already have ids for.**
  `refresh_controls` re-reads exactly those, in one tree walk, and answers `STALE` when
  the control is gone — which was the only reason a full re-listing was ever necessary
  after acting.
- **Scope before you widen.** On a NavigationView app an unscoped listing re-sends the
  pane's 20-odd items every single time, whether or not the question is about them.
  `scope="c4"` (or `scope="aid:ContentRoot"`) enumerates one subtree.
- **Never diff two listings by hand.** "Is the new card the same height and x as the old
  one" is `measure`, which re-reads both rects and states the verdict — aligned, same
  width, the gap between them — in about 200 tokens instead of two 5k listings and mental
  arithmetic.

## Reading a listing

`get_controls` returns one line per control:

```
c23 Expander[Button] "Startup" #StartupCard 472,558 716x70 expanded
```

id, `xaml_type[control_type]` (the second only when it differs), quoted name,
`#AutomationId`, `left,top widthxheight`, then state words. The header carries the counts,
the DPI scale and the listing token.

**Rects are DIPs.** That is the unit XAML adaptive triggers are written in, so a
breakpoint comparison needs no arithmetic. `units="px"` or `"both"` bring back physical
pixels when you specifically want them.

**Absence means default.** Empty names and AutomationIds are omitted, and so are
`enabled: true` and `visible: true` — a row that says nothing about them is enabled and
visible. Everything printed is therefore a deviation:

| Word | Means |
| --- | --- |
| `on` / `off` / `indeterminate` | ToggleSwitch, CheckBox, ToggleButton state |
| `selected` | this item is selected (absence means it is not) |
| `expanded` / `collapsed` | Expander, ComboBox, TreeViewItem, SettingsExpander |
| `="text"` | the control's ValuePattern value |
| `disabled` / `hidden` | not enabled / not visible |
| `enabled?` / `visible?` | the property would not read — unknown, not true |
| `caption` | a window frame button; `click_control` refuses these |

`format="json"` returns the structured payload when you need to compute over it. The
field selection is the same, so nothing appears in JSON that the compact row hid.

Prefer a listing over a screenshot for anything textual, structural or stateful: it is
cheap, exact and greppable. Spend a screenshot when the question is genuinely visual —
spacing, overlap, theming, clipping, "does this look right" — and crop it when the
subject is one control.

## Finding controls

**Ids are positional, filter-dependent, and belong to one listing.** `get_controls`
numbers survivors `c0, c1, …` *after* filtering, so changing `scope`, `xaml_type`,
`name_contains`, `actionable_only` or `include_unnamed` shifts every id. The header names
the listing (`listing L4`); pass `listing="L4"` to any action tool and an id from a
superseded listing is refused instead of resolving against whatever now holds that
position. Do that whenever more than one listing has happened — it converts the sharpest
failure mode here into an error message.

**Within its listing, an id means one element.** It resolves by the element's UIA
RuntimeId, so controls that share a name and type — two unnamed TextBoxes, two
"Source code" expanders, XML Hub's two Monaco editors — are kept apart, and an edit box
whose name is its own content still resolves after `set_text` changes it. Only when the
element itself is gone does an id fall back to name, type and AutomationId, and only if
exactly one live control matches; when several do, the call fails (or `refresh_controls`
answers `STALE … several live controls match`) instead of guessing.

**The default listing drops unnamed controls.** `include_unnamed=false` (default) discards
anything with neither a name nor an AutomationId. Two very common casualties:

- An **empty text box** — its name comes from its content, so it is invisible until it
  has text.
- A **button whose content is a panel** rather than a string (icon + label in a
  `StackPanel`) — no name is derived, so it never appears.

Escalate only as far as you need:

1. `get_controls` with a filter — `xaml_type="Expander"` is how you ask for "every
   expander on this page"; UIA's `control_type` calls them all `Button` and cannot express
   it. `name_contains` and `control_type` filter server-side too.
2. `get_controls(scope=…)` — one subtree, when you know roughly where the thing lives.
3. `get_controls` with `include_unnamed=true, actionable_only=false` — finds the two cases
   above. Expect a much longer list.
4. `get_control_tree` — when the flat list is ambiguous about nesting.
5. `screenshot(annotated=true)` — visual grounding when names are unhelpful.
6. `click_xy` — last resort; it resolves the UIA element under the point, so it buys
   nothing on UI with no automation peer, and its `PostMessage` fallback can report
   success without acting. See "Dead ends" below.

The listing truncates at 300 controls and says so on its own line. If you are near that,
scope or filter — do not widen the flags.

**A control with no name is also a finding.** If you had to pass `include_unnamed` to
reach a button, screen readers cannot announce it either. Report it as the accessibility
bug it is — a missing `AutomationProperties.Name` — rather than only working around it.

## Choosing an action verb

`click_control` tries Invoke → SelectionItem → Toggle → ExpandCollapse → LegacyIAccessible
and takes the **first pattern that does not throw**. That fallback can succeed with the
wrong semantic on a control that supports several.

- Use the specific verb when you know the intent: `select_control` for nav items, tabs and
  list items; `toggle_control` for checkboxes and switches; `expand_control` for
  expanders, tree items and combo boxes.
- **Read the `method` field in every response.** It names the pattern that actually fired.
  A verb that "worked" via an unexpected pattern usually did something else.
- `set_text` falls back from `ValuePattern` to `type_keys`, and reads the value back
  either way: `verified: true` means the control now holds the text. `verified: false`
  comes with a `warning` saying what it reads instead. Web content is the usual cause:
  Monaco in WebView2 accepts a `ValuePattern` write, ignores it, and reads back empty, so
  a Monaco editor can be neither written nor read here. A read-only box is refused by
  name.
- **A pattern that fires is not the user gesture.** Setting an AutoSuggestBox's text and
  selecting a suggestion does both of those things and still submits nothing: there is no
  Enter to press, and `QuerySubmitted` never runs. Navigate through the real tree (nav
  items, tabs, menus) instead.

  What decides this is whether the box needs a *submit*, not whether it is a search box. A
  plain `TextBox` bound with `UpdateSourceTrigger=PropertyChanged` filters on the
  `ValuePattern` write alone, and `set_text` drives it perfectly — which makes a filter box
  the cheapest way to reach an item buried in a long, scrolled gallery. Check `xaml_type`
  before writing one off.

After any action that navigates, opens a dialog, or shows/hides a region, the tree has
changed shape. That does **not** mean re-listing the window: `refresh_controls` on the ids
you still care about tells you which survived and what they now say, and only a `STALE`
answer means the listing genuinely has to be taken again.

**Ordering saves re-listings.** Ids are assigned in tree order, so an action that only
changes things *below* your next target leaves that target's id valid. Collapsing several
expanded groups is therefore cheapest bottom-up: collapse the last group first and the ids
above it still hold. Every action response echoes back the control it resolved — check that
name is the one you meant, and you have the confirmation that makes this safe. The echoed
row carries the control's state and rect as read *after* the action, with `state_before`
beside it, so a toggle or an expand needs no follow-up `refresh_controls` to confirm. The
rect may still be mid-layout straight after an expand or collapse; `measure` a moment
later if the geometry is the question.

## Dead ends — things that cannot be driven at all

Some targets are not merely awkward here, they are unreachable. Recognising one early
saves a dozen calls, and — more importantly — stops you inferring that a feature works
because the tool did not complain.

**File and folder pickers never appear.** `FileOpenPicker`, `FileSavePicker` and
`FolderPicker` are brokered by the shell, which does not draw on a background desktop. The
browse button invokes cleanly, `click_control` reports `InvokePattern` success, and then
nothing: `list_windows` shows only your app, no popup, no error. Any flow whose first step
is "pick a file" stops there. Verify the empty state, and reach the populated state some
other way — an integration test against the same view model, a command-line argument that
takes a path, or a fixture the app loads on start. If the picker itself is what needs testing, hand that step
to `winapp ui` (see Scope above), which runs on a real desktop.

**Hover and pressed states cannot be produced or observed.** There is no pointer here:
input goes through UIA patterns, which invoke a control without ever entering
`PointerOver`. UIA exposes no pointer-over or pressed property either, so the state is
both undrivable and unreadable. Do not report hover feedback as working because a click
worked — say it is **unverified**, and check it either by reading the visual state in the
XAML (`PointerOver` / `Pressed` in the control's `VisualStateManager`, or the
`ThemeResource` it swaps) or with `winapp ui hover` on a real desktop (see Scope above).

**A control can list cleanly and still expose no pattern.** Observed on
CommunityToolkit `Segmented`: its items enumerate as named, enabled, visible
`ListViewItem`s, yet `click_control` and `select_control` both fail with
`NoPatternInterfaceError` for Invoke *and* SelectionItem. Do not read that as a stale id —
re-listing returns the same thing.

**`click_xy` can report success and do nothing.** When no UIA pattern resolves under the
point it falls back to `PostMessage`, which WinUI's pointer input largely ignores. The
response looks like every other success, carrying `method: PostMessage` and a
`uia_fallback_reason`. Treat that combination as *not done* until a fresh listing or
screenshot proves the state changed — or pass `method="uia"`, which refuses instead of
falling back.

**`click_xy` may not click where you pointed.** It resolves the UIA element under the
point and invokes *that element's* pattern, and UIA hit-testing readily hands back an
ancestor or a sibling of whatever is visually there. When the resolved element's rect does
not contain your point, the response carries `retargeted` with the point you asked for and
the rect it landed in; read it before believing the click did what you meant. A point that
resolves onto the window frame's own Minimize/Maximize/Close is refused outright, the same
way `click_control` refuses one by id.

Search boxes belong on this list too — see "A pattern that fires is not the user gesture"
above.

When something turns out to be undrivable, say so plainly in what you report. "The page
renders and every binding resolves; the grid and the export could not be driven here" is a
useful result. "Verified" is not.

## What you can read back, and what you cannot

A listing gives `name`, `control_type`, `xaml_type`, `automation_id`, rect, and — unless
you pass `include_state=false` — the control's actual state. That is enough to assert:

- **State** — a toggle's `on`/`off`, an item's `selected`, an expander's
  `expanded`/`collapsed`, an edit's `="value"`. Do **not** take a screenshot to see any of
  these.
- **Clipping** — `clipped` means the rect on that row is a cut-down one: the control runs
  past a scroll viewport's edge, or past the window's. A 44×44 swatch clipped to four DIPs
  reports `44x4`, which reads as a short control and is not one. Do not measure it and do
  not judge it from a crop as it stands — `scroll_to_control` first, or let
  `screenshot(control_id=)` do it, which it does by default.
- **Command availability** — `disabled` on a row is a disabled command; this is how you
  check `CanExecute` without clicking anything.
- **Layout** — rects in DIPs answer "is it off-screen", "did the pane collapse", "do these
  overlap". For comparisons between controls, use `measure` rather than reading the
  numbers off two rows.
- **The real XAML type** — `xaml_type` beside UIA's lossy `control_type`. UIA reports a
  ToggleSwitch, an Expander and an ordinary Button all as `Button`, and a
  NavigationViewItem, ListViewItem and SelectorBarItem all as `ListItem`.

`get_text` reads an edit control's current value when you want just the one field.

## Flyouts, menus and drop-downs — usually their own window

Most WinUI 3 flyouts, menus, ComboBox drop-downs and tooltips get their own top-level
window (class `Microsoft.UI.Content.PopupWindowSiteBridge`, titled `PopupHost`), and
`screenshot` captures one window. In that common case:

- The parent's capture does not contain the popup. An open drop-down is simply absent and
  the control under it renders as closed. Concluding "the flyout did not open" from that
  frame is wrong, and it is the natural conclusion to reach.
- `screenshot` names any popup over the target window, with its hwnd. `list_windows` flags
  them with `popup: true`.
- To see it: `screenshot(hwnd=<popup hwnd>)`. To act on it: `get_controls(hwnd=<popup
  hwnd>)` — the items are fully enumerable there, with state, and `select_control` /
  `click_control` work on them normally. Listing tokens are per window, so the popup's ids
  and the parent's are separate sets.

**Not every popup takes that route, and the exception fails convincingly.** A XAML
`Flyout` is sometimes hosted inside the parent's own content. There is then no popup
window at all: `list_windows` shows only the main window and `screenshot` prints no
`POPUP OPEN` line — so both signals read "nothing opened" about a flyout that did.
Checking the second after the first proves nothing; they are the same signal.

**The discriminator is the parent's UIA tree, and it costs one call.** An in-frame
flyout's content *is* in the parent window's control tree, so the test is three-way with
no ambiguous branch:

| After opening it | What happened | What to do |
| --- | --- | --- |
| A popup window is listed | Hosted in its own window | `screenshot(hwnd=…)`, `get_controls(hwnd=…)` |
| No popup window, but the parent's `get_controls` now carries the flyout's items | Hosted in-frame | Work in the parent's listing — and the parent's own capture *does* show it |
| No popup window and nothing new in the parent | It genuinely did not open | Reach the command another way |

A classic Win32 ComboBox (WinForms, or an app like Character Map) is the second row: its
list opens inside the window, so scope a listing to the combo's id to read the items.

Opening one is its own question. `expand_control` reliably opens a ComboBox drop-down.
A `Button.Flyout` may not open under UIA Invoke at all — so run the three-way test rather
than re-invoking, and only conclude "broken flyout" on the third row.

## Scrolling, and bringing a control into view

There is no wheel. Mouse wheel goes through SendInput, which a background desktop cannot
receive, so it delivers nothing.

**To reach a specific control, use `scroll_to_control`.** It tries
`ScrollItemPattern.ScrollIntoView`, then selection, then computes the position from the
scroller's offset — and reports which route fired, the rect before and after, and whether
the control is now inside the viewport. This is the answer for an `Expander`, a
`SettingsCard` or a `Group`, none of which are selectable, which is why the old advice
("use `select_control`") did not cover them.

Note its second route *selects* the control, which on a NavigationViewItem or ListViewItem
also activates it. Check `method` in the response before reading anything into what the
app did next.

**To move a view rather than reach a control, use `scroll_control`.** It scrolls via
ScrollPattern, resolving to the nearest element that actually scrolls — the ScrollViewer
inside the container you named, or an ancestor — and says which in `scrolled`. Name the
visible container; you do not need to hunt for the ScrollViewer yourself.

- The response carries `vertical_percent`, `at_top`, `at_bottom` and `vertical_view_size`
  (the share of the content visible). "Did I go far enough" is in there — never take a
  screenshot to find out where you landed.
- `amount` is clamped to 10 lines, because each line is a separate COM call. A big
  `amount` is slow, not far; for a real jump use `to_percent` (0–100), which is one call
  whatever the distance.

When nothing in reach scrolls at all, that is the genuine answer — do not fall back to
coordinates.

## Testing layout at several sizes

`window_command` with `command="move"` resizes without any user interaction, which makes
adaptive-layout verification a two-call loop: move, then read a listing (or a cropped
capture) rather than a full screenshot.

Sizes are in DIPs by default, matching `AdaptiveTrigger.MinWindowWidth`, and listing rects
are in DIPs too, so no scale arithmetic is needed anywhere. If glyphs look twice the size
you expect in a capture, you are at 200% — the header states the scale.

Test at least one size on each side of every breakpoint you care about, and confirm the
narrow side has no content pushed off-screen (compare control rects against the window
rect — or `measure` a control against the window's own bounds).

## Prove the binary before you believe the pixels

A screenshot only proves what is on screen. It does not prove your edit is in the process
you are looking at — and a stale binary produces no error of any kind, just an old UI that
looks like your change did nothing. Every conclusion drawn after that is wrong.

You do not need an extra call. `session_activate_app` and `session_launch_app` both return
`module.module_path` and `module.module_mtime` for the process they started — that is the
file actually loaded. Compare it against your edit and move on.

**Packaged (MSIX) apps registered for development are the trap.** They load from a loose
`AppX\` layout that `dotnet build` does not refresh, so the app runs old code behind a
green build — and the recovery has an architecture trap of its own (`0x80073CF3` when the
registration is x86 and you built x64; `-ForceUpdateFromAnyVersion` does not help). When
you hit this, read `references/deploy-refresh.md` for the full recipe.

## Cleanup

- `session_destroy` when finished — always. Sessions leak a desktop and its processes.
- **Destroy before rebuilding.** A running session app holds its binaries open; builds and
  file copies will block or fail on the lock, sometimes by retrying silently for minutes.
- Destroy and recreate rather than reusing a session across a rebuild — it guarantees the
  new binary is loaded and costs about a second.
- If a previous host process was killed rather than exiting, its desktop and app survive
  invisibly and hold the same locks. `session_list(include_orphans=true)` finds them and
  `session_reap` clears them. Suspect this when a build or copy blocks for no visible
  reason.
- `session_show` puts the isolated desktop on the physical screen and takes the user's own
  desktop away. Only on explicit request, and `session_hide` after.

## Triage

| Symptom | Likely cause |
| --- | --- |
| `new_windows: []` after launch | Packaged app launched via `session_launch_app` — use `find_installed_app` then `session_activate_app`. Or the app needs package identity and cannot run loose. |
| Screenshot flagged as blank | Window minimized or still starting. Re-capture; `window_command restore` if needed. |
| UI looks unchanged after your edit | Stale binary — see "Prove the binary" above, before assuming a XAML or logic bug. |
| Control not in the listing | Unnamed — retry with `include_unnamed=true, actionable_only=false`. Or a filter is still applied; the header echoes them. |
| `screenshot(control_id=)` came back near-empty | The control is `clipped` — UIA reports the cut-down rect, so it crops to a sliver. The capture scrolls it into view for you; the response says whether that moved anything. |
| An empty listing | A filter that matched nothing, or a `scope` pointing at a leaf. The header names both. |
| "that id came from listing L4…" | You quoted a superseded listing. Re-read, or use `refresh_controls` to check specific ids. |
| Action reports success, nothing happens | Stale id from an earlier listing, or a fallback pattern fired — check `method`. |
| `click_xy` returned `method: PostMessage` | No UIA pattern under the point; the fallback probably did nothing. Re-read state before believing it. |
| A browse/open/save button invokes but no window appears | Shell-brokered file picker. Not drivable on a background desktop — see "Dead ends". |
| Packaged app shows pre-edit UI after a clean build | The registered loose `AppX\` layout is stale; a plain build does not refresh it. See `references/deploy-refresh.md`. |
| `Add-AppxPackage -Register` fails `0x80073CF3` | Architecture mismatch between the payload and the existing registration. See `references/deploy-refresh.md`. |
| A flyout/menu/drop-down looks like it never opened | Usually it opened in its own window: `list_windows` → capture the `popup: true` hwnd. If there is no popup window, re-read the *parent* with `get_controls` — an in-frame flyout's items are there. |
| `scroll_control` says nothing scrolls | Genuinely nothing in reach exposes ScrollPattern. Try `scroll_to_control` on the target; do not reach for coordinates or a wheel. |
| Scrolled but cannot tell where you are | Read `vertical_percent` / `at_bottom` from the response — do not screenshot to find out. |
| Tools start failing with rect/DC errors mid-run | The app process died. `screenshot` now says so outright; confirm with `list_windows` (empty) and relaunch. |
| "pipe desynchronized" or a call timing out | A previous call was abandoned while the worker was still busy. Retry; if it repeats, destroy the session. |
| A build or file copy blocks for minutes | An app is holding the binaries — a live session, or an orphaned desktop from a dead host. `session_list(include_orphans=true)` then `session_reap`. |
| `set_text` did not stick | The response says `verified: false` and what the control reads instead. On web content (Monaco) that is expected and cannot be worked around here; otherwise re-list, re-set, re-read. |
| A second launch attaches to an existing instance | Single-instance app (e.g. Windows 11 Notepad) handed off to a copy on the host desktop. Teardown will not kill it — that is deliberate. |
| `session_activate_app` says the app "opened on the user's desktop" | A UWP app with a CoreWindow (e.g. Calculator): Windows frames it in ApplicationFrameHost on the user's desktop, so it cannot be isolated. The instance the call started was closed again. Use `winapp ui` for it. |

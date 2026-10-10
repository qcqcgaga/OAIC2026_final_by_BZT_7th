---
name: arcbench-frontend-spec
description: Frontend interaction design specification for ARC Bench applications. Read before implementing UI code (buttons, menus, dialogs, notifications, hover behavior, forms) so the generated interface follows consistent accessibility, predictability, and feedback-semantics rules.
---

# ARC Bench Frontend Interaction Specification

Design rules for the application's interactive UI. Follow these rules when implementing or
modifying frontend interaction code. The rules are task-independent: they describe how a
well-formed interface should behave, not how any particular scenario is checked.

## When to Use

Apply when the module touches frontend interaction: buttons, menus, dialogs, notifications,
hover behavior, forms, or keyboard handling. Pure backend modules can skip this skill.

## Rules

### A. Accessibility-Tree Residency (hover-revealed controls)

Interactive controls that are revealed on hover/focus (action rows, icon buttons, menus)
MUST stay inside the accessibility tree at all times. Reveal them with `opacity` only —
never `visibility`/`display` — and never unmount them while their owning card/list item is
mounted.

- Why: `getByRole` resolves against the accessibility tree.
  `visibility:hidden` / `display:none` removes the control from that tree, so a card-scoped
  `card.getByRole('button', ...)` only matches while the pointer happens to be exactly on the
  card — hover-timing fragility that passes or fails by luck. `opacity:0` keeps the control
  in the tree at all times; hover only changes its visual appearance. Duplicate same-named
  controls across cards are fine: card-scoped resolution isolates them, so they never collide.
- Good: `opacity-0 group-hover:opacity-100 group-focus-within:opacity-100`.
- Avoid: `invisible group-hover:visible`, `hidden group-hover:flex`, mount-on-hover.

### B. Exact Accessible Names (strict-locator contract)

Every interactive control a user may target must have an accessible name that EXACTLY equals
the name the requirement scenario text quotes for it (e.g. "Take a note", "Settings",
"Undo", "Archive", "More options", "Pin note", "Change color", "Note editor", "Search",
"Work", "Reminders", "Toggle sidebar"). Accessible-name resolution is anchored and
case-insensitive — `getByRole(role, { name: /^<name>$/i })` — so one extra suffix, prefix,
ellipsis or colon breaks the match.

- **Where the name comes from**: the requirement scenario text (the quoted string) is
  authoritative. A screenshot's visible text is NOT — screenshots show the real product,
  which may decorate the label (e.g. a note-taking app shows "Take a note..."). When the quoted
  name and the visible text differ, use the quoted name for the accessible name.
- **No decorations in the accessible name**: no "..." (ellipsis), no ": <detail>" suffix,
  no "— <detail>" suffix, no extra " menu"/" button"/" note" suffix, no "Open ..." prefix.
  Decorative detail belongs in visible text / placeholder / `title`, never in `aria-label`
  or in the element's text when that text IS the accessible name.
- **Icon buttons** carry `aria-label` with the exact name. A visible-text button gets the
  exact name either by writing the exact text or by an `aria-label` that overrides it
  (e.g. visible "Take a note..." + `aria-label="Take a note"`).
- **Duplicate names are fine when the query is scoped** to one container
  (`card.getByRole('button', ...)`). Do not "fix" duplicates by renaming with suffixes.
- **Quoted user-visible text is verbatim too**: every message the requirement text quotes
  (validation errors, empty-state and confirmation wording) must render as visible text
  with the exact words — including the template shape. "between <min> and <max>" and
  "from <min> to <max>" are different wordings; substitute the actual values but keep the
  surrounding words unchanged. A paraphrase never matches an exact-text lookup.
- **A fixture string used by unscoped exact-text lookups must be unique per page**: a
  username (or other fixture name) rendered both in global chrome and in page content
  resolves an exact text lookup to two elements. Render such a string exactly once as its
  own text node — do not repeat it in a welcome banner, breadcrumb or footer.
- Avoid: `aria-label="Settings menu"` for a button the scenario calls "Settings";
  `aria-label="Undo: Note trashed"` for a button the scenario calls "Undo";
  a clickable button whose only accessible name is the text "Take a note..." (no exact-name
  `aria-label`).

### C. Explicit Action Feedback

Destructive or state-changing actions (delete, archive, restore, clear, ...) must be followed
by a notification that states the action and its result, and should offer an undo entry point.
Dialog primary actions must use a verb (Save / Delete / Apply) and provide a cancel path.

- Why: feedback must be recognizable as an explicit outcome; vague wording is not feedback.
- Good: "Note deleted" with an Undo action; dialog primary "Save" with "Cancel" beside it.
- Avoid: "Note trashed" when the action is a delete; a dialog whose confirm button has no verb.

Dialog primary actions must be operable on click: never render Apply/Save/Create disabled
as a pre-validation gate. Invalid input is handled AFTER the click — the quoted error
message appears and the previous result stays unchanged.

- Why (no disabling): a disabled primary button cannot be clicked at all; a scenario that
  clicks it and waits for the quoted error times out, and the error path the requirement
  describes is unreachable. Validate on submit, not by disabling the control.

Server-side validation order is part of the feedback contract: when a request is BOTH
invalid (duplicate name, bad length, wrong type) AND missing a state precondition (the
parent entity / active filter does not exist), the data-integrity check runs FIRST and
its quoted message is what the client sees. A state-missing-first handler reports the
wrong error for a scenario whose THEN quotes the integrity message — the quoted message's
source is stored records, which exist regardless of client state, so it must not be
shadowed by a precondition the client may not carry.

### D. Compatibility with the E2E Locator Resolver

Accessible-name resolution works against the accessibility tree using **exact roles and
anchored names**: `getByRole(role, { name: /^<exact name>$/i })`, scoped to a container
(card/dialog/sidebar) when the app renders repeated items. Matching is case-insensitive
but NOT substring: one extra word, ellipsis or suffix kills the match, and a control hidden
with `visibility`/`display` is not in the tree at all. These are generic accessibility
semantics, independent of any single task:

1. **Dialog/menu options: match the standard role semantics.**
   When the requirement text describes an option the user selects/clears (e.g. `select the
   "Work" label`, `clear the selected label`), render it as a native `checkbox`
   (`<input type="checkbox" aria-label={name}>`) inside the dialog; the exact-role lookup
   (`getByRole('dialog', ...).getByRole('checkbox', {name:...})`) requires exactly that
   role, so a button-simulated checkbox never matches. When the text describes an action
   (e.g. "Delete Note", "Save"), render a `button`. Give the dialog itself the exact name
   the requirement implies (e.g. "Note editor"), and keep only one dialog with that name
   open at a time.
   A popup opened by a trigger that advertises `aria-haspopup` must carry the MATCHING
   container role: `role="menu"` for a menu trigger (`aria-haspopup="true"`/`"menu"`),
   `role="listbox"` for a listbox trigger. Entries inside keep the role the requirement
   gives them — a navigation entry the requirement calls a link stays a real `<a>` (a
   menu-container query chains `getByRole('menu').getByRole('link', …)`), and an option
   the requirement calls a choice stays `role="menuitem"`/`"option"`. A popup without its
   container role makes every menu/listbox-scoped query resolve to zero elements.
   **Every command the requirement places in a menu stays rendered as a menu entry
   whenever that menu is open** — its presence is never conditioned on runtime state
   (an active filter, a selection, an existing record). Hiding a requirement-named
   command makes it undiscoverable; if the command is meaningless in the current state,
   keep it present and let the click show the quoted error or no-op. The dialog a
   requirement-named command opens likewise renders unconditionally; a save flow must
   not dead-end on missing client state — fall back to the server-side current entity
   or surface the quoted message.
2. **Names come from the requirement text; no field-name shadowing; no decorated names.**
   - The accessible name of any control a scenario quotes must equal that quoted name
     verbatim (rule B). A search box the text calls "Search" is named "Search", and form
     fields use the exact quoted labels (e.g. "Title", "Note content") — never decorated
     variants like "Take a note...".
   - Do not put a form-field's exact name on a non-form container (`form`, `li`, `div`,
     `span`, `toolbar`, `group`, `aside`, `complementary`) or on a global control (search
     box, filter, cart): that makes an exact-name field lookup ambiguous, and a container
     whose aria-label contains the field word resolves `getByLabel(/word/i)` to the
     container instead of the real control.
   - **Field word roots** (match case-insensitively) that commonly name form fields:
     Description, Name, Tags, Title, Content, Email, Password, Search, Note, Book, Shelf,
     Page, Draft, Chapter, Label, Address, City, Country, Phone, Zip/Postal, Alias, Cart,
     Favorites, Details. A non-form control carrying one of these roots in its accessible
     name is a defect.
   - **A data-entry `<form>`'s own aria-label (rule E1) must share NO word with any field
     label inside it.** The field resolver tries `getByLabel(/word/i)` first and keeps the
     first hit in document order, and an ancestor always precedes its descendants — so a
     container named after a field inside it wins the lookup and `fill()` dies with
     "Element is not an <input>". Renaming is not a fix unless the word is dropped
     entirely: "Address form" → "Address details" still shadows the "Address" field.
     Avoid `"Header search"` (shadows a "Search" input), `"Address details"`, `"New review"`
     (shadows a "Review" field); use names outside the field vocabulary — `"Catalog lookup"`,
     `"Recipient information"`, `"Your feedback"`. The gate runs this check as
     `label-shadowing` and blocks on any hit.
   - **List-row wrappers must not repeat the row's entity name**: a `<li>`/`<div>` around
     a list row must not carry `aria-label={entityName}` — that makes
     `getByLabel(/Name/i).first()` resolve to the non-clickable wrapper instead of the
     row's link/button. Put the name on the interactive element itself.
   - **Detail text read through a control-scoped text query must be nested inside that
     control.** When a record "shows <detail text> and provides a link/button named
     <name>", render the detail text INSIDE that control — a scoped text lookup matches
     descendants only, so sibling text beside the control is never found. Keep the
     control's accessible name exact with an `aria-label` (the aria-label overrides the
     text content for name resolution).
   - **Dialog names must be the plain exact name** ("Note editor"), never a
     "Note editor: <title>" / "Note editor — <title>" pattern — suffix patterns break
     `getByRole('dialog', {name:...})` anchors.
   - When a dialog is opened from a quoted launcher (menu item / button) and the
     requirement gives the dialog no separate name, the dialog's accessible name IS the
     launcher's exact label: a "Find and replace" dialog opened from the "Find and
     replace" menu item is named "Find and replace" — never a summary of its contents
     ("Text tools"). A dialog-scoped name query anchors on the launcher label.
   - **Every modal dialog provides a named "Close" button** in addition to any
     confirm/cancel affordance. A dialog whose only exits are an overlay click or a
     differently-named control cannot be dismissed through a dialog-scoped role/name
     query — the standard dismissal affordance is a button named exactly "Close".
   - **Indicator glyphs inside cells** (note dot, dropdown caret) must contribute NO
     text to their container: `aria-hidden` removes the glyph from the accessibility
     tree but NOT from `textContent`, and exact text assertions (`toHaveText`) compare
     `textContent` — render such indicators as CSS shapes or inline SVG (aria-hidden),
     never as a text character.
   - When the requirement states a view shows NO X (no secrets/tokens), the page copy
     must not contain the forbidden nouns themselves — an absence check is a
     text-substring match over the whole page, and explanatory prose naming the noun
     trips it. Phrase the guarantee without naming the protected noun.
   - When a submission is rejected, the rejected user-provided value (duplicate
     tag/name) renders as its own exact-text element — standalone, count 1 — never
     fused into a longer sentence and never nested inside the message element. The
     message itself keeps the requirement's exact wording separately.
   - **A dialog that the requirement says "contains" a named control must render that
     control whenever the dialog is open** — for existing records too, not only while
     creating or editing. A view/edit mode split that replaces the named text box with
     static text breaks the dialog's control inventory: keep the named input rendered
     in every mode, with the stored text as its VALUE, and let an "Edit …" affordance
     focus/prepare it instead of gating its presence. Do not remove or hide a
     requirement-named control to satisfy an exactly-once text rule — that rule
     targets standalone exact-text lookups, never a control's presence.
   - **A state indicator renders exactly once per state word family.** When the
     requirement gives alternate wordings for a state ("<A>" or an "<B> marker"),
     render one canonical indicator whose text carries the state; a transient
     confirmation message must not reuse the same word family — a tolerant lookup
     over the requirement's own wordings then resolves to two elements. Word the
     transient message so it does not duplicate the status label (or drop it — the
     persistent marker is the requirement's deliverable). This exactly-once
     discipline covers any text a tolerant query may resolve, not only quoted
     fixture strings.
3. **Modals must not unmount with their trigger.**
   Mount modal dialogs via a portal at the top level (or keep the dialog state at page
   level), never inside a list-item component that a filter/search can remove. If a list
   re-render unmounts the card hosting the editor, unsaved edits are silently discarded.
4. **Which control family to render is decided by the requirement's own interaction
   description — never by a single noun.**
   - When the requirement describes OPENING a visible selector and selecting a named
     choice through its visible list — it names a textbox to filter/type into, says
     "clicks the exact '<name>' option", or describes clicking entries of a list the
     flow opens — render the ARIA option contract: trigger `role="combobox"` +
     `aria-haspopup="listbox"` + `aria-expanded`, popup `role="listbox"`, items
     `role="option"` whose accessible names are the exact quoted values. (A native
     `<select>` renders its options in a browser/OS-level popup — the `<option>`
     elements are not individually visible to an option-role click, so a flow
     expressed as "click the named option" stalls under that rendering.)
   - Counter-example — a value noun, NOT an interaction: a requirement that merely
     names the chosen value through the control ("choose a '<Field>' option named
     '<Value>'", "chooses '<Value>' in '<Field>'", "selects <Value>") never names an
     option-role interaction; render a native `<select>` carrying the exact
     `aria-label` and the exact quoted `<option>` texts. The bare word "option" is a
     value noun and is never, by itself, a family signal.
   - When the requirement is SILENT about the control type, calls it merely a
     "combobox", a "field", or says only "selects <value>" — render a native
     `<select>` carrying the exact `aria-label` and the exact quoted option texts: a
     native select's implicit role IS combobox, so a "combobox" wording remains
     satisfied, and the control stays drivable by both a role query and a
     selectOption-style API.
   - Self-tests mirror the requirement's described interaction: a description with
     visible option clicks is self-tested by clicking the option role; a plain
     "selects <value>" description is self-tested through the selection API — never
     ship a suite that exercises only one style.
5. **On a listing/home page, the `link` role belongs to the listed entity records and
   nothing else.** The records the requirement text addresses as links (e.g. "a link
   whose accessible name is the entity name") carry the bare link role; chrome,
   navigation and action controls keep button semantics (E.5), and the logo uses the
   dual-role pattern (E.2). Why: an unscoped link-role lookup on such a page must resolve
   to the listed records — every extra bare `<a>` (nav entry, action, decoration) joins
   the result set. When the requirement text itself calls a chrome element a link, that
   explicit wording wins.
6. **Global chrome controls stay mounted on every route.** The top search control —
   the global control the requirement names — remains present under its exact name on
   every page, nested detail routes included. A page-scoped toolbar may ADD its
   own controls, but must not remove, replace or rename the global control within its
   route, and a secondary page control (e.g. an in-page code filter) must not reuse
   the global control's exact accessible name — keep exactly one element with the
   global name per page. **Existing code is a starting point, not an authority**:
   "preserve existing work" means not losing unrelated behavior — it does NOT mean a
   pre-existing design which removes, hides or replaces the global control on some
   routes stays. Reconcile the conflict in the requirement's favor: keep the global
   control mounted and give the page-scoped control its own distinct name (matching
   its placeholder is a good default), even when the requirement's prose informally
   calls the page control a "search box".
7. **In-cell interactive overlays must not swallow the cell's double-click editing
   gesture.** A grid-cell overlay (note / filter / dropdown chip) may stop `mousedown`
   to avoid cell selection, but must NOT stop `dblclick`: with a center-point
   `locator.dblclick()` on the cell the chip would receive the gesture instead of the
   cell's edit handler, and the inline editor never appears. Let `dblclick` bubble to
   the cell (or forward it explicitly), unless the chip defines its own double-click
   action. Self-tests drive the editing gesture with `locator.dblclick()` on the cell
   (center point) — never with coordinate offsets that dodge in-cell overlays.
8. **A requirement-named dialog keeps its role and exact name in every revision.**
   - A dialog the requirement names keeps `role="dialog"` and its exact `aria-label` in
     EVERY shipped revision; a refactor must never drop or rename either — the
     role+name pair is the locator surface platform queries anchor on, and a cleanup
     that changes either turns a green revision red.
9. **After a "save" succeeds, the dialog's presence follows the requirement's THEN
   verbatim.** If it says the dialog lists X, keep the dialog open and refresh its
   list; close it only when the THEN says it closes — a save that also dismisses the
   dialog breaks every dialog-scoped query that runs after the save.

### E. Form & Branding Semantics

1. **Every data-entry `<form>` must have a unique accessible name.** Add `aria-label`
   (or `aria-labelledby` pointing at a visible heading) — e.g. `<form aria-label="Login
   form">`. Unnamed forms are a gate-blocking defect: `getByRole('form', {name:...})`
   cannot target them. The name must simultaneously satisfy
   rule D.2's word-disjointness (no word shared with a field label inside the form) —
   a named form that shadows its own field is equally gate-blocking under
   `label-shadowing`.
2. **The brand logo must be locatable by BOTH `button` and `link` role
   queries.** A role query may resolve the logo as
   `getByRole('button', {name: /logo/i})` OR as `getByRole('link', ...)` — the
   requirement text usually quotes only the logo's name, never its role. A bare
   `<a aria-label="App logo" href="/">` answers only the link query and
   fails the button query. Cover both:
   - Preferred: `<a href="/" role="button" aria-label="App logo">` — link
     semantics with an explicit button role, so `getByRole('button')` and
     `getByRole('link')` both match.
   - Acceptable: `<button aria-label="App logo" onClick={...home}>`.
   - Avoid: a bare `<a>` with no `role="button"` — the button query finds
     nothing, and the click targets the same page either way.
   Keep "logo" in the accessible name; do not put the only name-bearing text in
   a `hidden`/`sm:hidden` span (it disappears under small viewports) or
   `aria-hidden` it, and do not rely on a `title` attribute alone (it is not
   part of the accessible name).
3. **List items (notes, shelves, books, chapters, pages, posts) must expose the entity
   name in their accessible name.** Render the name as link/button text (or in its
   `aria-label`); do not `aria-hidden` the name span.
4. **Form-field labels must use the common field word** so both substring and exact-name
   queries match: main name field → label "Name"; description → "Description"; tags →
   "Tags"; title → "Title". A label like "Page title" does not match `getByLabel(/Name/i)`.
5. **The logo dual-role pattern (E.2) is for the LOGO ONLY — do not generalize
   `<a role="button">` to every navigation entry.** A global
   `getByRole('button', {name:/X/i})` query keeps the first hit in document
   order, so a chrome BUTTON whose accessible name equals a page action's name —
   the login form's "SIGN IN" submit, a filter checkbox, a menu option — can steal
   that click. Generic conflict rule: global naming must not duplicate page-action
   names. Rules, in priority order:
   - **A navigation entry inside a landmark (`<nav>`, `<aside>`,
     `role="complementary"`, `role="navigation"`) keeps the `button` role and is
     NEVER demoted to a plain link** — landmark-scoped button queries resolve it by
     role inside the landmark
     (`getByRole('complementary').getByRole('button',{name:/^Archive$/i})`).
     The gate probe `landmark-roles` blocks on such a demotion.
   - For a chrome/page-action name collision, prefer fixes that keep every role:
     make the page action win document order (render it before the chrome entry)
     or give the page action its own requirement qualifier. Demote the chrome
     entry to a plain link only when neither is possible.
   - The gate probe `name-shadow` blocks on an exact chrome-button ↔ page-action
     collision; the instruction is to reorder rather than remove a role;
     substring overlaps stay advisory.

### F. Assertable Success & State Semantics

1. **After create/save, navigate to the created entity's details page** so its name
   becomes a visible heading (a heading lookup for the created entity's name sees it on
   the entity page, not hidden behind a toast or a list redirect). Returning to the
   list hides the created entity from role/heading queries. The entity's heading names
   it by the identifier the requirement quotes when opening it (tag/slug/branch). When
   a display title differs from that identifier, the identifier wins the heading, and
   the display title stays as visible text — a heading lookup for the quoted identifier
   must resolve on the detail page itself.
2. **Destructive/state-change actions show feedback whose wording matches the action**
   ("deleted", "archived", "restored", "favourited") with an undo entry point where
   applicable; avoid synonyms for the action's wording (a "trashed" note does not satisfy
   `/deleted/i`). When the resulting state must remain observable (a revoked session
   row, an archived item, a disabled entry), keep the target object present in its list
   rendering that new state — still visible after a reload. Never filter the row out
   and rely on a transient toast.
3. **The authenticated homepage must render the state-widget sections the product
   defines** — e.g. "My Recent Drafts", "My Recently Viewed", "My Most Viewed
   Favorites"/"Favourites", "Recently Updated Pages", "Recent Activity". These section
   headings and the entities inside them must be locatable by role/heading queries; do
   not collapse them into one generic "Recent" list.
4. **If the reference product uses a collapsible section toggle** (e.g. a
   "Shelf Tags" collapsible), implement the toggle button plus the collapsible section;
   do not flatten it into a bare labelled input.
5. **Metadata lines render as "Label: value"** — the label wording the requirement
   uses, a colon, then the exact value in its own short line (`Target branch: main`
   style), never fused without a separator.
6. **Counts render as a text template "<n> <noun>"** with singular/plural forms
   (1 reaction / 0 reactions / 2 reactions) as their OWN element — a bare number is
   not an assertable count. When a toggle/removal brings a count to zero, the zero
   state remains visible for the toggled item (removal is observable: the count drops
   to 0 in place rather than the row disappearing).

### G. Fixture Data Naming: Keyword-Neutral Display Names

User accounts, aliases, wishlist/folder/shelf names, and any other fixture display name
the requirement does NOT quote verbatim are the agent's choice — keep them free of action
verbs and field word roots that substring locators use as loose matchers (delete, remove, edit,
rename, create, update, save, cart, name, address, search, view, ...). Requirements fix
emails and product names; the human-readable name gets rendered in global chrome — the
header full-name link, sidebar, breadcrumb, footer — where a substring locator
(`getByRole('link', {name:/delete/i})`) hits it and steers the interaction away from the
intended control.

- Why: a header link "Dana Deleter" matches `/delete/i` before the address row's Delete
  button renders; the click navigates away instead of deleting (same pattern for
  "Remy Remover", "Ryan Renamer").
- Good: "Dana Smith", "Remy Martin", "Ryan Brown", "Cathy Green".
- Avoid: "Dana Deleter", "Remy Remover", "Ryan Renamer", "Cathy Carter".
- This does NOT apply to control names a scenario quotes ("Delete", "Rename", "Save"):
  those must keep their exact quoted name (rule B). It applies only to names the agent
  invents.
- Before finishing, run every invented display name against the keyword list and report
  the outcome in the seed evidence ("Dana Smith -> no keyword collision").
- In multi-row history/feed/log seeds, a row the agent invents must not sort AHEAD of
  a row for an entity the requirement names, in the list's default order — a
  `first()`/latest-style lookup then reads the invented row. Order seeded rows so the
  requirement-named entity's newest event is the newest of its kind (the requirement's
  own chronology wins).

### H. Locator-API Scoping: State Indicators Live INSIDE the Card

Playwright locator APIs are scoped to the **subtree** of the locator they are chained
onto. `card.getByTitle('Pinned')` / `card.getByLabel('...')` / `card.getByRole(...)`
match **descendant elements only** — they never match the card element itself, even
when that element carries the `title`/`aria-label` attribute.

- Why: state indicators (pinned / favorited / archived / unread / status) are located
  by card-scoped queries such as `card.getByTitle('Pinned')`. Putting `title="Pinned"`
  on the card's own `<article>`/`<li>` matches nothing — the element is the root, not
  a descendant — and the lookup times out while the state is actually correct.
- Good: render the indicator as a **descendant element inside the card**:
  `{note.pinned ? <span className="..." title="Pinned"><PinIcon /></span> : null}`.
  The same holds for `aria-label`-based indicators (`getByLabel`) and for icons
  located with `getByTitle`.
- `title` is NOT part of the accessible name (E.2 note): `getByRole('button', {name:...})`
  never reads a `title` attribute, so never rely on `title` alone for a control's name.
- The card root's `aria-label`/`title` may still exist for other reasons, but it can
  never substitute for an inner indicator element.

### I. Keyboard & Clipboard Paths

When the requirement expresses an action through a keyboard shortcut — copy, cut, paste
(Ctrl/Cmd+C/X/V) — implement the real clipboard path, not just a visual selection:
`onCopy`/`onCut`/`onPaste` handlers with a `clipboardData` text round-trip (the selected
range serializes to plain text, and a paste inserts it at the target).

- Why: a selection without handlers leaves the clipboard empty — the paste inserts nothing
  and every downstream state assertion fails. The handlers must live on the element that
  owns the selection (add `tabIndex` where a non-input container must receive focus).
- Good: `onPaste={e => { const text = e.clipboardData.getData('text/plain'); ... }}` on the
  selection container; self-tests drive it with real key events.
- Avoid: relying on browser-default paste for a custom region; calling handlers directly in
  self-tests instead of pressing the keys — the keyboard path then stays unverified.

## Self-Check

After implementing frontend interaction code, verify:

1. Hover-only controls stay in the accessibility tree: revealed via `opacity`, never
   `visibility`/`display`-hidden and never unmounted while their owning card is mounted (A).
2. Every control a scenario quotes by name has that exact accessible name — no ellipsis, no
   ": detail" / "— detail" / " menu" / " note" suffix, no decorative prefix (B). Duplicate
   names across repeated cards are fine; icon buttons carry the exact `aria-label` (B).
   Quoted messages render byte-for-byte as visible text, and a fixture string used by an
   unscoped exact-text lookup (e.g. the username) appears exactly once per page (B).
3. Every state-changing action shows explicit feedback with an undo path where relevant,
   and dialog confirm buttons are verb-labeled with a cancel option. Primary dialog actions
   are clickable — invalid input produces the quoted error message after the click, never a
   disabled button (C).
4. Selectable dialog options (labels/tags/flags) are native `checkbox`es inside a dialog
   with the exact required name; action options are `button`s; no global control (search
   box, filter) carries a form-field's exact name; every modal dialog is mounted
   independently of any list item's lifecycle (D). A selector control's family follows
   the requirement's own interaction description: a described
   open-and-click-the-named-option flow is an ARIA option list (combobox/listbox/option);
   merely naming the chosen value ('choose a "<Field>" option named "<Value>"' /
   'chooses "<Value>" in "<Field>"' / 'selects <Value>') and silent / merely "combobox" /
   "field" descriptions render a native `<select>` (D.4); on a listing page the unscoped
   link role resolves to the listed entity records only (D.5).
5. Every data-entry `<form>` has an `aria-label`; the home logo is locatable by
   both `button` and `link` role queries with "logo" in its accessible name; list
   items expose entity names; field labels use the exact names the scenario text
   quotes (E).
6. No toolbar/group/aside/complementary control carries a field word root in its
   accessible name, and every dialog name is the plain exact name without a
   "label: suffix" / "label — suffix" pattern (D2).
7. Create/save flows land on the created entity's details page; the authenticated
   homepage renders the state-widget sections named by the product (F).
8. No container (form/li/div/section wrapper) accessible name shares any word with a field
   label inside it, and none repeats a row's entity name (D2/E1; the gate probe
   `label-shadowing` blocks on a hit — renaming that keeps the word does not clear it).
9. Every invented fixture display name is keyword-neutral (G).
10. No global chrome entry (header/nav/footer) carries `role="button"` with an accessible
    name that equals a page action's name on the same page (submit / checkbox / option) —
    on colliding pages prefer making the page action answer the button query first in
    document order, demoting the chrome entry only when necessary (E.5; the gate probe
    `name-shadow` blocks on exact collisions).
11. State indicators (pinned / favorited / archived / status) are rendered as
    **descendant elements inside their card** with the `title`/`aria-label` assistive
    queries target (`card.getByTitle('Pinned')` matches descendants only — never the
    card root; `title` is not part of the accessible name) (H).
12. Copy/cut/paste shortcuts have real clipboard handlers with a text round-trip, and
    self-tests drive them through actual key events rather than calling handlers (I).
13. Every popup opened from an `aria-haspopup` trigger carries the matching container
    role (`menu` / `listbox`), and its entries keep the role the requirement gives them
    (a quoted navigation link stays a real link) (D1).
14. Every modal is named exactly after its quoted launcher when the requirement gives
    it no separate name, and provides a named "Close" exit; in-cell indicator glyphs
    contribute no text to their container; in-cell overlay chips let a center-point
    dblclick reach the cell's edit handler (D2/D7).
15. Global chrome (the top search control) stays mounted on every route under its exact
    name — exactly one element with that global name per page (D6).
16. Rejected input values render as standalone exact text; copy never names a noun the
    requirement forbids; counts use the "<n> <noun>" template with a visible zero state;
    a state-changing action keeps its target row present in its new state after reload;
    seeded history rows keep the requirement-named entity's newest event first (D2/F/G).
17. Every fixture is opened in self-tests through the SAME chain the requirement
    describes (global search → result link → the entity's page → sub-tab → row),
    starting from the application home page — never by direct URL alone: a direct
    goto proves the page renders, not that the navigation chain the scenario walks
    works.
18. Self-tests use platform-strict assertions: exact text/role semantics, `toHaveCSS`
    with the exact rgb the requirement names, and negative assertions that also exclude
    the DEFAULT selected-cell blue `rgb(232,240,254)` — a cell inside a live selection
    must be asserted against every color the platform might check, never just "not the
    fill".
19. GIVEN states named in the scenario (pre-provisioned sessions, releases, reactions,
    permissions) are satisfied by the app's OWN seed; self-tests must not fabricate
    them via API/DB calls — if the seed lacks the state, the seed is the defect.
20. At least one self-test per module opens the feature through the UI entry chain the
    requirement describes (e.g. search → open repo → click the named tab/link), never
    only via direct URLs — a feature reachable only by direct URL is undiscoverable to
    the grader.

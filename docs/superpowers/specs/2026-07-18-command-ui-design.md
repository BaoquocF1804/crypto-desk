# Command UI Design

**Date:** 2026-07-18  
**Status:** Approved for implementation  
**Parent design:** `2026-07-18-interactive-cli-dashboard-design.md`

## Objective

Add the complete browser Command UI to the existing Crypto Desk dashboard. The
UI must expose the command APIs delivered in Tasks 7–10 without weakening the
existing safety boundaries: the dashboard remains readable when command
submission is unavailable, execution commands always require a preview and an
explicit confirmation, and production identity continues to come from the
trusted authentication header.

## Selected Layout

Use option A, the hybrid command deck:

- A fixed `CommandDock` spans the bottom of the dashboard.
- An `ActivityDrawer` opens from the right on desktop and as a bottom sheet on
  narrow screens.
- A modal `PreviewDialog` handles approve, reject, and reconcile confirmation.
- The command deck is mounted once at the dashboard page root so it can observe
  runner availability and request a dashboard refresh without coupling command
  state to individual dashboard cards.

The dock remains compact when idle. It shows the current availability state, a
single command input, keyboard hints, and an activity control with the active
and queued command count.

## Component and State Architecture

The browser implementation consists of:

- `components/command-deck.tsx`: owns API synchronization, polling, submission,
  preview, confirmation, and dashboard refresh events.
- `components/command-dock.tsx`: command input, autocomplete, history movement,
  availability state, and submit affordance.
- `components/activity-drawer.tsx`: active commands, FIFO queue, and the last 50
  terminal commands with expandable safe results.
- `components/preview-dialog.tsx`: accessible execution preview and explicit
  confirmation.
- `lib/command-deck-state.ts`: pure reducer and selectors for deterministic UI
  state.
- `lib/operator-identity.ts`: server-only trusted identity resolution shared by
  all command routes.

The component state distinguishes:

- session initialization;
- runner availability;
- command input and local input history;
- active, queued, and recent commands;
- pending submission;
- preview generation;
- preview ready for confirmation;
- confirmation accepted;
- recoverable and terminal errors;
- drawer and dialog visibility.

## Command Entry

The dock accepts only commands already supported by `parseDeskInput` and only
configured dashboard symbols. It provides:

- `ArrowUp` and `ArrowDown` for local command history;
- `Tab` for autocomplete;
- `Enter` to submit or start an execution preview;
- `Escape` to clear suggestions, then clear the draft;
- visible text and color states for available, offline, disabled, and error
  conditions.

Research and operations commands are queued immediately. `approve`, `reject`,
and `reconcile` create a preview command first and never enqueue execution
directly.

The input is disabled when:

- Command UI is feature-disabled;
- session or CSRF initialization failed;
- the runner heartbeat is older than 30 seconds;
- an applicable operator or global queue limit was reached;
- a submission using the current draft is already in flight.

## Activity Drawer

The drawer presents:

1. the running command, if present;
2. queued commands in FIFO order;
3. up to 50 terminal commands, newest first.

Each item shows command kind, operator, queued/start/finish timestamps, duration,
status, and a compact argument summary. Results and safe errors are expandable.
Raw stack traces, tokens, secrets, and unbounded payloads are never rendered.

On reload, the drawer rebuilds its state from D1 through `GET /api/commands`.
Selected command details are fetched only when an item is expanded or required
for a preview.

## Execution Preview

The preview dialog displays the safe ticket/action summary returned by the
server, the execution environment, and the preview expiry. It provides Cancel
and an action-specific Confirm button.

Confirmation behavior:

- focus is trapped inside the dialog and restored to the invoking control;
- the Confirm button is disabled immediately after the first accepted click;
- the existing preview ID and stable idempotency key are reused for retries;
- an expired preview cannot be confirmed and offers a fresh preview action;
- successful confirmation closes the dialog, opens activity, and refreshes
  commands;
- successful state-changing commands emit `desk:dashboard-refresh`.

## Synchronization

- Fetch the command session and command list on mount.
- Poll every 2 seconds while a command is queued/running or a preview is being
  prepared.
- Poll every 10 seconds while idle.
- Pause polling while the document is hidden.
- Refresh immediately on window focus and when visibility returns.
- Keep dashboard data freshness and runner availability as separate states.
- Runner offline disables new work but leaves dashboard content and command
  history readable.

## Local Development Identity

Production identity remains the
`oai-authenticated-user-email` request header. A shared server-only resolver
uses this order:

1. return the authenticated header when present;
2. otherwise, if the request URL hostname is exactly `localhost` or
   `127.0.0.1`, return `CRYPTO_DESK_LOCAL_OPERATOR_EMAIL` when configured;
3. otherwise return no identity.

The fallback is never accepted from a browser header or request body and is
applied consistently to the session, list, create, detail, preview, and confirm
routes. It is intended only for local development and is not added to production
Sites environment variables.

## Error Handling

API failures are mapped to concise operator-facing messages while retaining the
safe error code for diagnosis. The UI handles at least:

- unauthorized or failed session initialization;
- feature disabled;
- runner offline;
- operator/global queue limits;
- invalid command or symbol;
- invalid/expired CSRF;
- preview expired or already consumed;
- network timeout and transient server failure.

Retries preserve idempotency keys. The UI never invents a successful state:
server responses and subsequent command-list synchronization are authoritative.

## Accessibility and Responsive Behavior

- All controls are keyboard operable.
- Suggestions use combobox/listbox semantics.
- Status changes use an `aria-live` region.
- Drawer and dialog controls have explicit accessible names.
- The preview dialog traps focus and supports `Escape` cancellation when no
  confirmation is in flight.
- Status meaning is conveyed by text in addition to color.
- The desktop drawer becomes a full-width bottom sheet on mobile while the dock
  remains reachable above the safe-area inset.

## Test Strategy

Implementation follows test-driven development:

- unit tests for reducer transitions, selectors, history, autocomplete, and
  client command parsing;
- route tests proving authenticated-header precedence and restricting the local
  email fallback to loopback hostnames;
- component/render tests for dock, drawer, preview, safe result rendering, and
  disabled states;
- browser tests for keyboard behavior, offline recovery, drawer reload
  recovery, preview focus, expiry, double-submit prevention, and dashboard
  refresh;
- an end-to-end local `doctor` command smoke test against the running web and
  desk runner.

## Acceptance Criteria

- The selected hybrid Command UI is visible on the dashboard at desktop and
  mobile widths.
- Every approved non-execution command can be submitted and tracked.
- Approve, reject, and reconcile cannot execute without a valid preview and
  explicit confirmation.
- Activity survives reload and reflects server state.
- Offline, disabled, loading, and error states are clear and safe.
- Local development works with `CRYPTO_DESK_LOCAL_OPERATOR_EMAIL` on loopback
  URLs only.
- Existing dashboard behavior and command API tests remain green.
- Unit, route, render, browser, lint, type/build, and local smoke checks pass.

# Command UI Implementation Plan

> **For Codex:** Execute this plan in order using test-driven development. Keep
> every production change behind a failing test, run the focused test after each
> green step, and run the complete web verification suite before completion.

**Goal:** Add the approved hybrid Command UI to the existing dashboard, including
secure loopback-only local operator identity, activity recovery, execution
preview/confirm, and local end-to-end verification.

**Architecture:** A single client `CommandDeck` coordinates session and command
API state. Pure state helpers keep parsing, autocomplete, reducer transitions,
grouping, and display decisions testable without a browser. Presentation is split
into a bottom dock, responsive activity drawer, and accessible preview dialog.
Command API routes share one server-only operator identity resolver.

**Stack:** React 19, Next/Vinext, TypeScript, Cloudflare Workers/D1, Node test
runner, ESLint, CSS.

## Task 1: Secure local operator identity

**Files:**

- Create: `web/lib/operator-identity.ts`
- Modify: `web/app/api/commands/route.ts`
- Modify: `web/app/api/command-session/route.ts`
- Modify: `web/app/api/commands/[id]/route.ts`
- Modify: `web/app/api/command-previews/route.ts`
- Modify: `web/app/api/command-previews/[id]/confirm/route.ts`
- Test: `web/tests/command-api.test.mjs`

1. Add failing route tests for authenticated-header precedence, loopback fallback
   on `localhost` and `127.0.0.1`, and rejection on non-loopback hosts.
2. Run `npm run build && node --test tests/command-api.test.mjs` and confirm the
   new assertions fail.
3. Add `resolveOperatorEmail(request)` using the trusted header first and
   `CRYPTO_DESK_LOCAL_OPERATOR_EMAIL` only for exact loopback hostnames.
4. Replace route-local identity reads in every operator-facing command route.
5. Re-run the focused API test and commit the green slice in the web repository.

## Task 2: Command deck contracts and pure state

**Files:**

- Create: `web/lib/command-deck-state.ts`
- Create: `web/tests/unit/command-deck-state.test.mjs`
- Modify: `web/lib/command-contract.ts` only if a shared exported type/constant is
  required.

1. Add failing tests for response normalization, active/queued/recent grouping,
   command availability, local input history, autocomplete candidates, stable
   idempotency reuse, duration formatting inputs, and safe result serialization.
2. Run `node --experimental-strip-types --no-warnings --test
   tests/unit/command-deck-state.test.mjs` and confirm failure.
3. Implement the smallest pure types, reducer, and selectors needed by the UI.
4. Re-run the focused unit test and existing command contract tests.
5. Commit the green slice.

## Task 3: Bottom command dock

**Files:**

- Create: `web/components/command-dock.tsx`
- Create: `web/tests/unit/command-dock.test.mjs`

1. Add failing server-render tests for available/offline/disabled states,
   combobox/listbox semantics, keyboard hints, activity count, error/live status,
   and safe labels.
2. Run the focused test and confirm failure.
3. Implement the controlled dock component with `ArrowUp`, `ArrowDown`, `Tab`,
   `Enter`, and `Escape` behavior supplied through explicit callbacks.
4. Re-run the focused test and commit the green slice.

## Task 4: Activity drawer and preview dialog

**Files:**

- Create: `web/components/activity-drawer.tsx`
- Create: `web/components/preview-dialog.tsx`
- Create: `web/tests/unit/command-surfaces.test.mjs`

1. Add failing render tests for active/FIFO/history sections, expandable safe
   results, status text, timestamps, execution mode, preview expiry, action
   labels, dialog semantics, and disabled confirmation.
2. Run the focused test and confirm failure.
3. Implement both controlled presentation components, including dialog focus
   entry/restore/trap and Escape cancellation.
4. Re-run the focused test and commit the green slice.

## Task 5: Command orchestration

**Files:**

- Create: `web/components/command-deck.tsx`
- Create: `web/lib/command-api.ts`
- Create: `web/tests/unit/command-api-client.test.mjs`
- Modify: `web/tests/rendered-html.test.mjs`

1. Add failing tests for API error mapping, safe JSON handling, CSRF headers,
   command submission, preview creation, one-shot confirmation, stable retry
   idempotency, and the presence of the command deck in rendered dashboard HTML.
2. Run the focused tests and confirm failure.
3. Implement session initialization, command polling, visibility/focus refresh,
   submit/preview/confirm flows, recovery from server command state, dashboard
   refresh events, and state wiring to the three presentation components.
4. Re-run focused tests and commit the green slice.

## Task 6: Dashboard integration and responsive styling

**Files:**

- Modify: `web/app/page.tsx`
- Modify: `web/app/globals.css`
- Modify: `web/tests/rendered-html.test.mjs`

1. Extend the rendered HTML assertions for the dock landmark, activity drawer
   trigger, command input, and existing dashboard content.
2. Run the render test and confirm the integration assertions fail.
3. Mount `CommandDeck` with configured symbols, teach dashboard polling to react
   to `desk:dashboard-refresh`, reserve dock space, and add responsive,
   high-contrast styles matching the approved mockup.
4. Re-run render tests, lint, and build.
5. Commit the green slice.

## Task 7: Local configuration and end-to-end behavior

**Files:**

- Modify locally, do not commit: `web/.dev.vars`
- Modify if present: `web/.dev.vars.example`
- Test: existing API/render suites plus live browser and runner.

1. Add `CRYPTO_DESK_LOCAL_OPERATOR_EMAIL=quoc.lb@teko.vn` to local development
   variables without placing it in Sites production configuration.
2. Restart the local Vinext server under Node 22 and confirm the desk runner is
   online.
3. Verify in a real browser: dock visibility, autocomplete and history keys,
   drawer open/close, offline state, reload recovery, preview/cancel, focus
   behavior, and single confirmation submission.
4. Submit `doctor`, observe it progress to a terminal successful state, and
   confirm activity survives reload.

## Task 8: Full verification and delivery

**Files:** all changed web files.

1. Run:

   ```bash
   PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH" npm run test
   PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH" npm run lint
   ```

2. Inspect `git diff --check`, `git status --short`, and the final rendered page.
3. Use the verification-before-completion workflow; fix every in-scope failure
   and re-run the affected checks.
4. Push the exact source commit, save a Sites version using the existing
   `.openai/hosting.json` project ID, deploy that saved version, and inspect the
   terminal deployment status.
5. Report local and production URLs, verification evidence, commits, and any
   intentionally uncommitted local-only configuration.

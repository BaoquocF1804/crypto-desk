# Interactive CLI Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thêm command deck tương tác lên dashboard Sites: browser gửi typed command vào một FIFO queue trong D1, một Python runner local poll outbound, claim từng command, gọi `CryptoDeskService`/`ExecutionService`, và trả safe result — theo spec `docs/superpowers/specs/2026-07-18-interactive-cli-dashboard-design.md`.

**Architecture:** Sites (repo `web/`, vinext trên Cloudflare Worker + D1) là control plane: command contract, queue repository, user API (CSRF + identity header), runner API (bearer token), và UI dock/drawer/preview-dialog. Python (repo gốc) thêm contract models (pydantic `extra="forbid"`), `CommandDispatcher` map command → service hiện có, journal exactly-once trong SQLite (schema v3), và `desk runner` — vòng lặp outbound HTTPS duy nhất. Không WebSocket/SSE, không inbound port, không stream log.

**Tech Stack:** Python 3.12, uv, Typer 0.21, pydantic 2.13.4, httpx 0.28.1, SQLite, pytest; TypeScript 5.9, Next.js 16 App Router qua vinext 0.0.50, Cloudflare D1 (binding `DB`), drizzle-kit (chỉ để sinh migration), node:test (+ `--experimental-strip-types` cho unit test), Playwright (dev-only, 3 kịch bản money-path).

## Global Constraints

- Hai repo Git riêng: repo gốc (Python) và `web/` (Sites). KHÔNG BAO GIỜ `git add web/` từ repo gốc. Mỗi task ghi rõ repo và commit riêng.
- Prose/chuỗi hiển thị người dùng: tiếng Việt. Tên code/test/API: tiếng Anh.
- Mainnet bị khóa cứng: command contract không có trường environment do client gửi; server ghi `environment='testnet'`; dispatcher/runner từ chối khi bất kỳ lớp nào (BINANCE_ENV, `settings.binance.environment`, ticket.environment, command.environment) khác `testnet`.
- Mọi mốc thời gian so sánh (heartbeat, lease, preview expiry, retention) dùng đồng hồ Sites (`new Date().toISOString()` trong route). Runner/browser không bao giờ là nguồn chuẩn.
- Test Python: `uv run pytest -q` + `uv run ruff check .` xanh sau mỗi task. Test web: `npm test` (build + node:test) xanh sau mỗi task.
- Secrets không bao giờ nằm trong file tracked. `.env.example` / `.dev.vars` chỉ chứa placeholder/giá trị local.
- Không thêm dependency runtime mới ở cả hai repo (Playwright là devDependency duy nhất được thêm, theo spec §16.3).

## Command Contract v1 (nguồn chuẩn duy nhất — cả hai repo copy từ đây)

Hằng số (đối chiếu từng giá trị khi self-review):

```text
Statuses:            QUEUED | RUNNING | SUCCEEDED | FAILED | NEEDS_REVIEW
Non-terminal:        QUEUED, RUNNING
Kinds:               doctor, sync, screen, analyze, daily, health, tickets,
                     orders, reflections, preview, execute
Execution kinds:     preview, execute
State-changing:      sync, analyze, daily, health, execute   (publish dashboard sau SUCCEEDED)
Execution actions:   approve | reject | reconcile
Execution modes:     TESTNET_ORDER | DRY_RUN
MAX_ARGS_BYTES:      4096   (canonical JSON của args)
MAX_RESULT_BYTES:    32768  (canonical JSON của result)
MAX_BODY_BYTES:      8192   (HTTP body các route write)
PREVIEW_TTL_MS:      300000 (5 phút, Sites enforce tại Confirm)
RUNNER_OFFLINE_MS:   30000
LEASE_MS:            30000  (renew mỗi 10s; sweep khi lease quá hạn > RUNNER_OFFLINE_MS)
HEARTBEAT_S:         5      (runner)
IDLE_POLL_S:         2      (runner)
HISTORY_LIMIT:       50
MAX_NON_TERMINAL_PER_OPERATOR: 10
MAX_NON_TERMINAL_GLOBAL:       100
RETENTION_DAYS:      30     (cleanup opportunistic, LIMIT 50/lần)
FORBIDDEN_RESULT_KEYS (exact, case-insensitive, đệ quy):
  api_key, api_secret, token, telegram, actor, confirmation,
  report_dir, client_order_id, payload, raw, secret
```

Args theo kind (browser chỉ gửi object đã typed, không gửi chuỗi lệnh):

```text
doctor      {}
sync        {}
screen      {}
analyze     {"symbol": "BTCUSDT"}            # symbol ∈ KNOWN_SYMBOLS
daily       {}
health      {}
tickets     {}
orders      {}
reflections {"symbol": "BTCUSDT" | null}
preview     {"action": "approve"|"reject"|"reconcile", "ticket_id": "<1-64 ký tự [A-Za-z0-9-]>"}
execute     {"action": ..., "ticket_id": ..., "preview_command_id": "<uuid>",
             "fingerprint": "<64 hex>"}       # chỉ server tạo qua route confirm
```

Safe error codes (ổn định, hiển thị được): `INVALID_COMMAND`, `RUNNER_OFFLINE`, `FEATURE_DISABLED`, `QUEUE_LIMIT_OPERATOR`, `QUEUE_LIMIT_GLOBAL`, `PREVIEW_NOT_FOUND`, `PREVIEW_NOT_READY`, `PREVIEW_EXPIRED`, `PREVIEW_OPERATOR_MISMATCH`, `TICKET_NOT_FOUND`, `TICKET_CHANGED`, `ENVIRONMENT_FORBIDDEN`, `COMMAND_INTEGRITY`, `EXECUTION_UNCERTAIN`, `LEASE_EXPIRED`, `RUNNER_RESTART`, `VALIDATION_FAILED`, `DISPATCH_FAILED`, `RESULT_UNSAFE`, `RESULT_TOO_LARGE`, `SESSION_CONFLICT`, `SESSION_MISMATCH`, `LEASE_LOST`, `STATE_CONFLICT`.

Env vars mới:

```text
Web (Worker secrets/vars):  CRYPTO_DESK_RUNNER_TOKEN, CRYPTO_DESK_CSRF_SECRET,
                            CRYPTO_DESK_COMMAND_UI_ENABLED ("1" bật)
Python (.env):              CRYPTO_DESK_COMMAND_API_URL (vd https://<site>/api),
                            CRYPTO_DESK_RUNNER_TOKEN,
                            CRYPTO_DESK_COMMAND_RUNNER_ENABLED (mặc định "1"),
                            (tái dùng CRYPTO_DESK_SITES_BYPASS_TOKEN hiện có)
```

Ticket fingerprint (cả hai phía dùng đúng danh sách field, đúng thứ tự canonical JSON `sort_keys`): sha256 hex của canonical JSON `{"environment","expires_at","id","intent","limit_price","notional_usdt","quantity","side","status","stop_price","symbol","target_price"}` (giá trị Decimal chuyển chuỗi qua `to_jsonable`).

## Target File Map

```text
web/ (repo riêng)
  db/schema.ts                        # + deskCommands, deskRunnerState (drizzle, chỉ để sinh SQL)
  drizzle/0001_*.sql                  # migration sinh bởi npm run db:generate
  db/commands.ts                      # queue repository: FIFO claim, lease, sweep, limits, cleanup
  lib/command-contract.ts             # kinds/statuses/limits, validators, grammar parser, assertSafeResult
  lib/machine-auth.ts                 # bearerTokenMatches (tách từ ingest)
  lib/http-body.ts                    # readBodyCapped (tách từ ingest)
  lib/csrf.ts                         # HMAC nonce theo bucket 15 phút
  app/api/ingest/route.ts             # refactor dùng lib/machine-auth + lib/http-body (hành vi giữ nguyên)
  app/api/command-session/route.ts    # GET: identity, csrf, runner state, feature flag
  app/api/commands/route.ts           # POST enqueue + GET queue/history
  app/api/commands/[id]/route.ts      # GET một command (reload recovery)
  app/api/command-previews/route.ts   # POST preview command
  app/api/command-previews/[id]/confirm/route.ts  # POST execute command
  app/api/runner/session/route.ts     # POST register/resume singleton session
  app/api/runner/heartbeat/route.ts   # POST heartbeat
  app/api/runner/claim/route.ts       # POST atomic FIFO claim (sweep lease trước)
  app/api/runner/lease/route.ts       # POST renew lease
  app/api/runner/result/route.ts      # POST terminal result
  lib/command-deck-state.ts           # pure reducer + format helpers (unit-testable)
  app/command-deck.tsx                # client parent: hook + dock + drawer + dialog
  app/command-dock.tsx                # input, history, autocomplete
  app/activity-drawer.tsx             # active/queued/recent + safe result
  app/preview-dialog.tsx              # focus trap, Confirm idempotent
  app/page.tsx                        # nhúng <CommandDeck/>, thêm sự kiện desk:dashboard-refresh
  tests/support/fake-d1.mjs           # D1 adapter trên node:sqlite
  tests/unit/command-contract.test.mjs
  tests/unit/command-queue.test.mjs
  tests/unit/csrf.test.mjs
  tests/unit/command-deck-state.test.mjs
  tests/command-api.test.mjs          # route tests qua built worker
  tests/e2e/command-deck.spec.ts      # Playwright 3 kịch bản money-path
  playwright.config.ts

repo gốc (Python)
  src/crypto_desk/commands.py         # contract models + safe results + fingerprint + hash
  src/crypto_desk/store.py            # schema v3: bảng command_journal + journal_* methods
  src/crypto_desk/dispatcher.py       # CommandDispatcher + DispatchError + mainnet hard-lock
  src/crypto_desk/runner.py           # CommandRunner: session/heartbeat/claim/lease/report/recovery
  src/crypto_desk/dashboard.py        # + publish_dashboard_if_configured (chuyển từ cli)
  src/crypto_desk/cli.py              # lệnh `desk runner`; wrapper publish giữ nguyên monkeypatch path
  services/crypto-desk-runner.service # systemd user unit
  tests/test_commands.py
  tests/test_dispatcher.py
  tests/test_runner.py
  tests/test_domain_store.py          # + journal tests
  .env.example, README.md             # env mới + hướng dẫn vận hành runner
```

---

### Task 0: [web] D1 schema + queue repository

**Files:**
- Modify: `web/db/schema.ts`
- Create: `web/drizzle/0001_*.sql` (sinh bằng `npm run db:generate`)
- Create: `web/db/commands.ts`
- Create: `web/tests/support/fake-d1.mjs`
- Test: `web/tests/unit/command-queue.test.mjs`
- Modify: `web/package.json` (script `test:unit`, gộp vào `test`)

**Interfaces:**
- Consumes: binding D1 `DB` qua `import { env } from "cloudflare:workers"` (pattern `web/db/dashboard.ts`).
- Produces (dùng bởi Task 2–4): `createCommand(input): Promise<{row: CommandRow; created: boolean}>`, `getCommand(id)`, `listCommands(limit)`, `countNonTerminal(operatorEmail?)`, `sweepExpiredLeases(nowIso)`, `cleanupExpired(nowIso)`, `getRunnerState()`, `registerRunnerSession({sessionId, executionMode, nowIso})`, `heartbeatRunner({sessionId, executionMode, nowIso})`, `claimNext({sessionId, nowIso})`, `renewLease({sessionId, commandId, nowIso})`, `reportResult({sessionId, commandId, status, result, errorCode, nowIso})`, type `CommandRow` (mọi cột snake_case).

- [ ] **Step 1: Thêm bảng vào drizzle schema và sinh migration**

Thêm vào cuối `web/db/schema.ts` (giữ nguyên `deskState`):

```ts
import { index, integer, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

// FIFO command queue cho command deck (spec 2026-07-18-interactive-cli-dashboard-design).
// Hàng này cũng là audit record: operator + mọi timestamp + reason code.
export const deskCommands = sqliteTable(
  "desk_commands",
  {
    id: text("id").primaryKey(),
    kind: text("kind").notNull(),
    args: text("args").notNull(), // canonical JSON
    operatorEmail: text("operator_email").notNull(),
    environment: text("environment").notNull().default("testnet"),
    idempotencyKey: text("idempotency_key").notNull(),
    previewCommandId: text("preview_command_id"),
    status: text("status").notNull(),
    queuedAt: text("queued_at").notNull(),
    startedAt: text("started_at"),
    leaseExpiresAt: text("lease_expires_at"),
    finishedAt: text("finished_at"),
    result: text("result"), // safe result JSON
    errorCode: text("error_code"),
    reasonCode: text("reason_code"),
  },
  (table) => ({
    operatorIdempotency: uniqueIndex("desk_commands_operator_idempotency").on(
      table.operatorEmail,
      table.idempotencyKey
    ),
    statusQueued: index("desk_commands_status_queued").on(
      table.status,
      table.queuedAt,
      table.id
    ),
  })
);

// Singleton (id=1): runner session + heartbeat.
export const deskRunnerState = sqliteTable("desk_runner_state", {
  id: integer("id").primaryKey(),
  sessionId: text("session_id").notNull(),
  lastHeartbeatAt: text("last_heartbeat_at").notNull(),
  activeCommandId: text("active_command_id"),
  executionMode: text("execution_mode").notNull(), // TESTNET_ORDER | DRY_RUN
});
```

Lưu ý: file hiện tại đã import `integer, sqliteTable, text` — chỉ bổ sung `index, uniqueIndex` vào cùng dòng import.

Run: `cd web && npm run db:generate`

Expected: file mới `web/drizzle/0001_<tag>.sql` chứa `CREATE TABLE \`desk_commands\`` và `CREATE TABLE \`desk_runner_state\`` + 2 index; `drizzle/meta/_journal.json` thêm entry thứ hai.

- [ ] **Step 2: Viết fake D1 trên node:sqlite cho unit test**

Tạo `web/tests/support/fake-d1.mjs`:

```js
// D1 adapter tối thiểu trên node:sqlite cho unit test repository.
// Chỉ hỗ trợ prepare().bind().first()/all()/run() — đúng phần db/ sử dụng.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

const DRIZZLE_DIR = new URL("../../drizzle", import.meta.url).pathname;

export function createFakeD1() {
  const db = new DatabaseSync(":memory:");
  for (const file of readdirSync(DRIZZLE_DIR).filter((f) => f.endsWith(".sql")).sort()) {
    const sql = readFileSync(join(DRIZZLE_DIR, file), "utf8");
    for (const statement of sql.split("--> statement-breakpoint")) {
      const trimmed = statement.trim();
      if (trimmed) db.exec(trimmed);
    }
  }
  return wrapD1(db);
}

function wrapD1(db) {
  return {
    prepare(sql) {
      return makeStatement(db, sql, []);
    },
  };
}

function makeStatement(db, sql, params) {
  return {
    bind(...values) {
      return makeStatement(db, sql, values);
    },
    async first() {
      const row = db.prepare(sql).get(...params);
      return row === undefined ? null : row;
    },
    async all() {
      return { results: db.prepare(sql).all(...params) };
    },
    async run() {
      const info = db.prepare(sql).run(...params);
      return { success: true, meta: { changes: Number(info.changes) } };
    },
  };
}
```

- [ ] **Step 3: Viết failing unit tests cho queue repository**

Tạo `web/tests/unit/command-queue.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";
import "../support/cloudflare-workers-stub.mjs";
import { createFakeD1 } from "../support/fake-d1.mjs";

const {
  createCommand, getCommand, listCommands, countNonTerminal,
  sweepExpiredLeases, cleanupExpired,
  registerRunnerSession, heartbeatRunner, getRunnerState,
  claimNext, renewLease, reportResult,
} = await import("../../db/commands.ts");

const T0 = "2026-07-18T10:00:00.000Z";
const T1 = "2026-07-18T10:00:01.000Z";
const T2 = "2026-07-18T10:00:02.000Z";

function useDb() {
  globalThis.__TEST_ENV__ = { DB: createFakeD1() };
}

function baseInput(overrides = {}) {
  return {
    id: crypto.randomUUID(),
    kind: "sync",
    args: "{}",
    operatorEmail: "quoc.lb@teko.vn",
    idempotencyKey: crypto.randomUUID(),
    previewCommandId: null,
    queuedAt: T0,
    ...overrides,
  };
}

test("createCommand inserts QUEUED and dedupes on (operator, idempotency_key)", async () => {
  useDb();
  const input = baseInput();
  const first = await createCommand(input);
  assert.equal(first.created, true);
  assert.equal(first.row.status, "QUEUED");
  assert.equal(first.row.environment, "testnet");

  const retry = await createCommand({ ...baseInput(), idempotencyKey: input.idempotencyKey });
  assert.equal(retry.created, false);
  assert.equal(retry.row.id, first.row.id);
  assert.equal(await countNonTerminal("quoc.lb@teko.vn"), 1);
  assert.equal(await countNonTerminal(), 1);
});

test("claimNext is FIFO, single-flight, and requires the registered session", async () => {
  useDb();
  await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  const a = await createCommand(baseInput({ queuedAt: T0 }));
  const b = await createCommand(baseInput({ queuedAt: T1 }));

  const wrongSession = await claimNext({ sessionId: "other", nowIso: T1 });
  assert.equal(wrongSession.error, "SESSION_MISMATCH");

  const claimed = await claimNext({ sessionId: "s1", nowIso: T1 });
  assert.equal(claimed.command.id, a.row.id);
  assert.equal(claimed.command.status, "RUNNING");

  const second = await claimNext({ sessionId: "s1", nowIso: T2 });
  assert.equal(second.command, null); // vẫn còn RUNNING → không claim thêm

  const state = await getRunnerState();
  assert.equal(state.active_command_id, a.row.id);
  assert.equal(b.row.status, "QUEUED");
});

test("reportResult finishes RUNNING once and refuses a second terminal write", async () => {
  useDb();
  await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  const { row } = await createCommand(baseInput());
  await claimNext({ sessionId: "s1", nowIso: T1 });

  const done = await reportResult({
    sessionId: "s1", commandId: row.id, status: "SUCCEEDED",
    result: '{"nav_usdt":"1000"}', errorCode: null, nowIso: T2,
  });
  assert.equal(done.applied, true);
  assert.equal(done.row.status, "SUCCEEDED");
  assert.equal((await getRunnerState()).active_command_id, null);

  const late = await reportResult({
    sessionId: "s1", commandId: row.id, status: "FAILED",
    result: null, errorCode: "DISPATCH_FAILED", nowIso: T2,
  });
  assert.equal(late.applied, false);
  assert.equal(late.row.status, "SUCCEEDED"); // terminal bất biến
});

test("sweepExpiredLeases moves stale RUNNING to NEEDS_REVIEW/LEASE_EXPIRED and unblocks claim", async () => {
  useDb();
  await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  const { row } = await createCommand(baseInput({ queuedAt: T0 }));
  await claimNext({ sessionId: "s1", nowIso: T0 }); // lease = T0 + 30s

  // 10:01:05 > lease(10:00:30) + offline threshold(30s)
  await sweepExpiredLeases("2026-07-18T10:01:05.000Z");
  const swept = await getCommand(row.id);
  assert.equal(swept.status, "NEEDS_REVIEW");
  assert.equal(swept.reason_code, "LEASE_EXPIRED");

  await createCommand(baseInput({ queuedAt: T1 }));
  await registerRunnerSession({ sessionId: "s2", executionMode: "DRY_RUN", nowIso: "2026-07-18T10:01:06.000Z" });
  const next = await claimNext({ sessionId: "s2", nowIso: "2026-07-18T10:01:06.000Z" });
  assert.notEqual(next.command, null); // queue không bị wedge
});

test("renewLease extends only the owned RUNNING command", async () => {
  useDb();
  await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  const { row } = await createCommand(baseInput());
  await claimNext({ sessionId: "s1", nowIso: T0 });
  assert.equal((await renewLease({ sessionId: "s1", commandId: row.id, nowIso: T1 })).ok, true);
  assert.equal((await renewLease({ sessionId: "s1", commandId: "missing", nowIso: T1 })).ok, false);
});

test("registerRunnerSession is singleton while heartbeat is fresh", async () => {
  useDb();
  const first = await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  assert.equal(first.ok, true);
  const resume = await registerRunnerSession({ sessionId: "s1", executionMode: "TESTNET_ORDER", nowIso: T1 });
  assert.equal(resume.ok, true); // cùng session id = resume
  const conflict = await registerRunnerSession({ sessionId: "s2", executionMode: "DRY_RUN", nowIso: T1 });
  assert.equal(conflict.ok, false);
  assert.equal(conflict.error, "SESSION_CONFLICT");
  // heartbeat cũ > 30s → session mới thay thế được
  const takeover = await registerRunnerSession({
    sessionId: "s2", executionMode: "DRY_RUN", nowIso: "2026-07-18T10:00:40.000Z",
  });
  assert.equal(takeover.ok, true);
  assert.equal((await heartbeatRunner({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: "2026-07-18T10:00:41.000Z" })).ok, false);
});

test("listCommands returns active FIFO + recent terminal capped at limit; cleanupExpired prunes old rows", async () => {
  useDb();
  await registerRunnerSession({ sessionId: "s1", executionMode: "DRY_RUN", nowIso: T0 });
  const a = await createCommand(baseInput({ queuedAt: T0 }));
  await createCommand(baseInput({ queuedAt: T1 }));
  await claimNext({ sessionId: "s1", nowIso: T1 });
  await reportResult({ sessionId: "s1", commandId: a.row.id, status: "FAILED", result: null, errorCode: "DISPATCH_FAILED", nowIso: T2 });

  const listed = await listCommands(50);
  assert.equal(listed.active.length, 1);
  assert.equal(listed.recent.length, 1);
  assert.equal(listed.recent[0].error_code, "DISPATCH_FAILED");

  // finished_at cũ hơn 30 ngày → bị xóa
  await cleanupExpired("2026-09-01T00:00:00.000Z");
  assert.equal((await listCommands(50)).recent.length, 0);
});
```

Thêm scripts vào `web/package.json` (giữ nguyên các script khác):

```json
"test:unit": "node --experimental-strip-types --no-warnings --test tests/unit/",
"test": "npm run test:unit && npm run build && node --test tests/rendered-html.test.mjs tests/dashboard-api.test.mjs"
```

Run: `cd web && npm run test:unit`

Expected: FAIL — `Cannot find module '../../db/commands.ts'`.

- [ ] **Step 4: Implement queue repository**

Tạo `web/db/commands.ts` (raw D1 như `db/dashboard.ts`, không dùng drizzle lúc runtime):

```ts
// Queue repository cho command deck. Mọi timestamp là ISO string do route
// truyền vào (đồng hồ Sites). Import từ API route, không từ page/component.
import { env } from "cloudflare:workers";

export const RUNNER_OFFLINE_MS = 30_000;
export const LEASE_MS = 30_000;
export const RETENTION_DAYS = 30;
const CLEANUP_BATCH = 50;

export interface CommandRow {
  id: string;
  kind: string;
  args: string;
  operator_email: string;
  environment: string;
  idempotency_key: string;
  preview_command_id: string | null;
  status: string;
  queued_at: string;
  started_at: string | null;
  lease_expires_at: string | null;
  finished_at: string | null;
  result: string | null;
  error_code: string | null;
  reason_code: string | null;
}

export interface RunnerStateRow {
  session_id: string;
  last_heartbeat_at: string;
  active_command_id: string | null;
  execution_mode: string;
}

function requireDb() {
  if (!env.DB) {
    throw new Error(
      "Cloudflare D1 binding `DB` is unavailable. Set the `d1` field in .openai/hosting.json to `DB` or let your control plane inject the real binding values before using the database."
    );
  }
  return env.DB;
}

const COLUMNS =
  "id, kind, args, operator_email, environment, idempotency_key, preview_command_id, status, queued_at, started_at, lease_expires_at, finished_at, result, error_code, reason_code";

export async function createCommand(input: {
  id: string;
  kind: string;
  args: string;
  operatorEmail: string;
  idempotencyKey: string;
  previewCommandId: string | null;
  queuedAt: string;
}): Promise<{ row: CommandRow; created: boolean }> {
  const db = requireDb();
  try {
    await db
      .prepare(
        `INSERT INTO desk_commands (${COLUMNS})
         VALUES (?1, ?2, ?3, ?4, 'testnet', ?5, ?6, 'QUEUED', ?7, NULL, NULL, NULL, NULL, NULL, NULL)`
      )
      .bind(
        input.id, input.kind, input.args, input.operatorEmail,
        input.idempotencyKey, input.previewCommandId, input.queuedAt
      )
      .run();
  } catch (error) {
    if (!String(error).includes("UNIQUE")) throw error;
    const existing = await db
      .prepare(
        `SELECT ${COLUMNS} FROM desk_commands WHERE operator_email = ?1 AND idempotency_key = ?2`
      )
      .bind(input.operatorEmail, input.idempotencyKey)
      .first<CommandRow>();
    if (!existing) throw error;
    return { row: existing, created: false };
  }
  const row = await getCommand(input.id);
  if (!row) throw new Error("insert did not persist");
  return { row, created: true };
}

export async function getCommand(id: string): Promise<CommandRow | null> {
  return await requireDb()
    .prepare(`SELECT ${COLUMNS} FROM desk_commands WHERE id = ?1`)
    .bind(id)
    .first<CommandRow>();
}

export async function listCommands(
  limit: number
): Promise<{ active: CommandRow[]; recent: CommandRow[] }> {
  const db = requireDb();
  const active = await db
    .prepare(
      `SELECT ${COLUMNS} FROM desk_commands
       WHERE status IN ('QUEUED','RUNNING')
       ORDER BY CASE status WHEN 'RUNNING' THEN 0 ELSE 1 END, queued_at ASC, id ASC`
    )
    .all<CommandRow>();
  const recent = await db
    .prepare(
      `SELECT ${COLUMNS} FROM desk_commands
       WHERE status IN ('SUCCEEDED','FAILED','NEEDS_REVIEW')
       ORDER BY finished_at DESC LIMIT ?1`
    )
    .bind(limit)
    .all<CommandRow>();
  return { active: active.results, recent: recent.results };
}

export async function countNonTerminal(operatorEmail?: string): Promise<number> {
  const db = requireDb();
  const row = operatorEmail
    ? await db
        .prepare(
          "SELECT COUNT(*) AS n FROM desk_commands WHERE status IN ('QUEUED','RUNNING') AND operator_email = ?1"
        )
        .bind(operatorEmail)
        .first<{ n: number }>()
    : await db
        .prepare(
          "SELECT COUNT(*) AS n FROM desk_commands WHERE status IN ('QUEUED','RUNNING')"
        )
        .first<{ n: number }>();
  return row?.n ?? 0;
}

// Spec §7.2: RUNNING có lease quá hạn hơn RUNNER_OFFLINE_MS → NEEDS_REVIEW,
// không bao giờ quay lại QUEUED.
export async function sweepExpiredLeases(nowIso: string): Promise<void> {
  const cutoff = new Date(Date.parse(nowIso) - RUNNER_OFFLINE_MS).toISOString();
  await requireDb()
    .prepare(
      `UPDATE desk_commands
       SET status = 'NEEDS_REVIEW', reason_code = 'LEASE_EXPIRED',
           error_code = 'LEASE_EXPIRED', finished_at = ?1
       WHERE status = 'RUNNING' AND lease_expires_at < ?2`
    )
    .bind(nowIso, cutoff)
    .run();
  await requireDb()
    .prepare(
      `UPDATE desk_runner_state SET active_command_id = NULL
       WHERE id = 1 AND active_command_id IS NOT NULL
         AND active_command_id NOT IN (SELECT id FROM desk_commands WHERE status = 'RUNNING')`
    )
    .run();
}

export async function cleanupExpired(nowIso: string): Promise<void> {
  const cutoff = new Date(
    Date.parse(nowIso) - RETENTION_DAYS * 24 * 60 * 60 * 1000
  ).toISOString();
  await requireDb()
    .prepare(
      `DELETE FROM desk_commands WHERE id IN (
         SELECT id FROM desk_commands
         WHERE finished_at IS NOT NULL AND finished_at < ?1 LIMIT ${CLEANUP_BATCH}
       )`
    )
    .bind(cutoff)
    .run();
}

export async function getRunnerState(): Promise<RunnerStateRow | null> {
  return await requireDb()
    .prepare(
      "SELECT session_id, last_heartbeat_at, active_command_id, execution_mode FROM desk_runner_state WHERE id = 1"
    )
    .first<RunnerStateRow>();
}

export function runnerOnline(state: RunnerStateRow | null, nowIso: string): boolean {
  if (!state) return false;
  return Date.parse(nowIso) - Date.parse(state.last_heartbeat_at) <= RUNNER_OFFLINE_MS;
}

export async function registerRunnerSession(input: {
  sessionId: string;
  executionMode: string;
  nowIso: string;
}): Promise<{ ok: boolean; error?: string }> {
  const db = requireDb();
  await sweepExpiredLeases(input.nowIso);
  const current = await getRunnerState();
  const fresh = runnerOnline(current, input.nowIso);
  const unresolved =
    current?.active_command_id != null &&
    (await getCommand(current.active_command_id))?.status === "RUNNING";
  if (current && current.session_id !== input.sessionId && (fresh || unresolved)) {
    return { ok: false, error: "SESSION_CONFLICT" };
  }
  await db
    .prepare(
      `INSERT INTO desk_runner_state (id, session_id, last_heartbeat_at, active_command_id, execution_mode)
       VALUES (1, ?1, ?2, NULL, ?3)
       ON CONFLICT(id) DO UPDATE SET
         session_id = excluded.session_id,
         last_heartbeat_at = excluded.last_heartbeat_at,
         execution_mode = excluded.execution_mode`
    )
    .bind(input.sessionId, input.nowIso, input.executionMode)
    .run();
  return { ok: true };
}

export async function heartbeatRunner(input: {
  sessionId: string;
  executionMode: string;
  nowIso: string;
}): Promise<{ ok: boolean }> {
  const result = await requireDb()
    .prepare(
      "UPDATE desk_runner_state SET last_heartbeat_at = ?1, execution_mode = ?2 WHERE id = 1 AND session_id = ?3"
    )
    .bind(input.nowIso, input.executionMode, input.sessionId)
    .run();
  return { ok: (result.meta?.changes ?? 0) > 0 };
}

export async function claimNext(input: {
  sessionId: string;
  nowIso: string;
}): Promise<{ command: CommandRow | null; error?: string }> {
  const db = requireDb();
  await sweepExpiredLeases(input.nowIso);
  const state = await getRunnerState();
  if (!state || state.session_id !== input.sessionId) {
    return { command: null, error: "SESSION_MISMATCH" };
  }
  const leaseUntil = new Date(Date.parse(input.nowIso) + LEASE_MS).toISOString();
  // Một statement duy nhất → atomic trong SQLite/D1: chỉ claim khi không còn
  // command RUNNING nào, theo đúng thứ tự FIFO (queued_at, id).
  const claimed = await db
    .prepare(
      `UPDATE desk_commands SET status = 'RUNNING', started_at = ?1, lease_expires_at = ?2
       WHERE id = (
         SELECT id FROM desk_commands WHERE status = 'QUEUED'
         ORDER BY queued_at ASC, id ASC LIMIT 1
       )
       AND NOT EXISTS (SELECT 1 FROM desk_commands WHERE status = 'RUNNING')
       RETURNING ${COLUMNS}`
    )
    .bind(input.nowIso, leaseUntil)
    .first<CommandRow>();
  if (!claimed) return { command: null };
  await db
    .prepare("UPDATE desk_runner_state SET active_command_id = ?1, last_heartbeat_at = ?2 WHERE id = 1")
    .bind(claimed.id, input.nowIso)
    .run();
  return { command: claimed };
}

export async function renewLease(input: {
  sessionId: string;
  commandId: string;
  nowIso: string;
}): Promise<{ ok: boolean }> {
  const state = await getRunnerState();
  if (!state || state.session_id !== input.sessionId) return { ok: false };
  const leaseUntil = new Date(Date.parse(input.nowIso) + LEASE_MS).toISOString();
  const result = await requireDb()
    .prepare(
      "UPDATE desk_commands SET lease_expires_at = ?1 WHERE id = ?2 AND status = 'RUNNING'"
    )
    .bind(leaseUntil, input.commandId)
    .run();
  return { ok: (result.meta?.changes ?? 0) > 0 };
}

export async function reportResult(input: {
  sessionId: string;
  commandId: string;
  status: "SUCCEEDED" | "FAILED" | "NEEDS_REVIEW";
  result: string | null;
  errorCode: string | null;
  nowIso: string;
}): Promise<{ applied: boolean; row: CommandRow | null }> {
  const db = requireDb();
  const updated = await db
    .prepare(
      `UPDATE desk_commands
       SET status = ?1, result = ?2, error_code = ?3, finished_at = ?4, lease_expires_at = NULL
       WHERE id = ?5 AND status = 'RUNNING'
       RETURNING ${COLUMNS}`
    )
    .bind(input.status, input.result, input.errorCode, input.nowIso, input.commandId)
    .first<CommandRow>();
  if (updated) {
    await db
      .prepare("UPDATE desk_runner_state SET active_command_id = NULL WHERE id = 1 AND active_command_id = ?1")
      .bind(input.commandId)
      .run();
    return { applied: true, row: updated };
  }
  // Terminal rồi (vd LEASE_EXPIRED đã sweep): báo lại row hiện tại, không đổi
  // terminal state (spec §7.3).
  return { applied: false, row: await getCommand(input.commandId) };
}
```

- [ ] **Step 5: Chạy unit test rồi commit (repo web/)**

Run:

```bash
cd web && npm run test:unit && npm run lint
```

Expected: PASS toàn bộ `command-queue.test.mjs`.

Commit (trong `web/`):

```bash
cd web
git add db/schema.ts db/commands.ts drizzle/ tests/support/fake-d1.mjs tests/unit/command-queue.test.mjs package.json
git commit -m "feat: add desk command queue schema and repository"
```

---

### Task 1: [web] Command contract + grammar parser

**Files:**
- Create: `web/lib/command-contract.ts`
- Test: `web/tests/unit/command-contract.test.mjs`

**Interfaces:**
- Consumes: không phụ thuộc module nào (dependency-free để client + worker + unit test cùng dùng).
- Produces (dùng bởi Task 2–4, 8): `COMMAND_KINDS`, `EXECUTION_ACTIONS`, `COMMAND_STATUSES`, `NON_TERMINAL_STATUSES`, `STATE_CHANGING_KINDS`, `KNOWN_SYMBOLS` (re-export từ dashboard-contract), các hằng `MAX_ARGS_BYTES=4096`, `MAX_RESULT_BYTES=32768`, `MAX_BODY_BYTES=8192`, `PREVIEW_TTL_MS=300000`, `HISTORY_LIMIT=50`, `MAX_NON_TERMINAL_PER_OPERATOR=10`, `MAX_NON_TERMINAL_GLOBAL=100`, `FORBIDDEN_RESULT_KEYS`; `class CommandValidationError`; `canonicalJson(value): string`; `parseCommandSubmission(value): {kind, args}`; `parsePreviewRequest(value): {action, ticket_id}`; `parseRunnerReport(value): {session_id, command_id, status, result, error_code}`; `assertSafeResult(value): void`; `parseDeskInput(input, symbols): DeskInputResult`.

- [ ] **Step 1: Viết failing tests**

Tạo `web/tests/unit/command-contract.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

const {
  parseCommandSubmission, parsePreviewRequest, parseDeskInput,
  assertSafeResult, canonicalJson, CommandValidationError,
  MAX_ARGS_BYTES, FORBIDDEN_RESULT_KEYS,
} = await import("../../lib/command-contract.ts");

const SYMBOLS = ["BTCUSDT", "ETHUSDT"];

test("parseCommandSubmission accepts every non-execution kind with typed args", () => {
  assert.deepEqual(
    parseCommandSubmission({ kind: "analyze", args: { symbol: "BTCUSDT" }, idempotency_key: "k1" }).args,
    { symbol: "BTCUSDT" }
  );
  assert.deepEqual(
    parseCommandSubmission({ kind: "reflections", args: {}, idempotency_key: "k2" }).args,
    { symbol: null }
  );
  for (const kind of ["doctor", "sync", "screen", "daily", "health", "tickets", "orders"]) {
    assert.equal(parseCommandSubmission({ kind, args: {}, idempotency_key: "k" }).kind, kind);
  }
});

test("parseCommandSubmission rejects execution kinds, unknown kinds, extra keys, and missing idempotency key", () => {
  for (const bad of [
    { kind: "execute", args: {}, idempotency_key: "k" },
    { kind: "preview", args: {}, idempotency_key: "k" },
    { kind: "rm -rf", args: {}, idempotency_key: "k" },
    { kind: "sync", args: { extra: 1 }, idempotency_key: "k" },
    { kind: "sync", args: {} },
    { kind: "analyze", args: { symbol: "DOGEUSDT'; DROP" }, idempotency_key: "k" },
  ]) {
    assert.throws(() => parseCommandSubmission(bad), CommandValidationError);
  }
});

test("args over 4 KiB fail closed", () => {
  const big = { kind: "analyze", args: { symbol: "B".repeat(MAX_ARGS_BYTES) }, idempotency_key: "k" };
  assert.throws(() => parseCommandSubmission(big), CommandValidationError);
});

test("parsePreviewRequest validates action and ticket id shape", () => {
  const ok = parsePreviewRequest({ action: "approve", ticket_id: "a1b2-c3", idempotency_key: "k" });
  assert.equal(ok.action, "approve");
  for (const bad of [
    { action: "submit", ticket_id: "t", idempotency_key: "k" },
    { action: "approve", ticket_id: "bad ticket!", idempotency_key: "k" },
    { action: "approve", ticket_id: "", idempotency_key: "k" },
  ]) {
    assert.throws(() => parsePreviewRequest(bad), CommandValidationError);
  }
});

test("parseDeskInput implements the approved grammar only", () => {
  assert.deepEqual(parseDeskInput("sync", SYMBOLS), { type: "command", kind: "sync", args: {} });
  assert.deepEqual(parseDeskInput("  analyze btcusdt ", SYMBOLS), {
    type: "command", kind: "analyze", args: { symbol: "BTCUSDT" },
  });
  assert.deepEqual(parseDeskInput("reflections", SYMBOLS), {
    type: "command", kind: "reflections", args: { symbol: null },
  });
  assert.deepEqual(parseDeskInput("approve ticket-1", SYMBOLS), {
    type: "preview", action: "approve", ticket_id: "ticket-1",
  });
  for (const bad of ["daily --catch-up", "orders --reconcile", "doctor --online",
    "analyze", "analyze DOGEUSDT", "approve", "sync | cat", "publish-dashboard", ""]) {
    assert.equal(parseDeskInput(bad, SYMBOLS).type, "error", bad);
  }
});

test("assertSafeResult rejects forbidden keys exactly (case-insensitive, recursive) but not substrings", () => {
  assert.throws(() => assertSafeResult({ Token: "x" }), CommandValidationError);
  assert.throws(() => assertSafeResult({ nested: [{ api_key: "x" }] }), CommandValidationError);
  // exact match: drawdown chứa "raw" nhưng hợp lệ (spec §6.2)
  assertSafeResult({ drawdown: "0.1", token_count: 3 });
  assert.throws(() => assertSafeResult({ raw: {} }), CommandValidationError);
  assert.equal(FORBIDDEN_RESULT_KEYS.includes("client_order_id"), true);
});

test("canonicalJson is stable-sorted and compact", () => {
  assert.equal(canonicalJson({ b: 1, a: { d: 2, c: 3 } }), '{"a":{"c":3,"d":2},"b":1}');
});
```

Run: `cd web && npm run test:unit`

Expected: FAIL — `Cannot find module '../../lib/command-contract.ts'`.

- [ ] **Step 2: Implement contract module**

Tạo `web/lib/command-contract.ts` (theo phong cách hand-written validator của `lib/dashboard-contract.ts`, không Zod):

```ts
// Command contract v1 — nguồn: plan 2026-07-18-interactive-cli-dashboard.md.
// Dependency-free: dùng chung cho route handler, client component và unit test.
import { KNOWN_SYMBOLS } from "./dashboard-contract";

export { KNOWN_SYMBOLS };

export const COMMAND_KINDS = [
  "doctor", "sync", "screen", "analyze", "daily", "health",
  "tickets", "orders", "reflections", "preview", "execute",
] as const;
export type CommandKind = (typeof COMMAND_KINDS)[number];

export const SUBMITTABLE_KINDS: readonly CommandKind[] = [
  "doctor", "sync", "screen", "analyze", "daily", "health",
  "tickets", "orders", "reflections",
];
export const EXECUTION_ACTIONS = ["approve", "reject", "reconcile"] as const;
export type ExecutionAction = (typeof EXECUTION_ACTIONS)[number];

export const COMMAND_STATUSES = [
  "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "NEEDS_REVIEW",
] as const;
export type CommandStatus = (typeof COMMAND_STATUSES)[number];
export const NON_TERMINAL_STATUSES: readonly CommandStatus[] = ["QUEUED", "RUNNING"];
export const STATE_CHANGING_KINDS: readonly CommandKind[] = [
  "sync", "analyze", "daily", "health", "execute",
];

export const MAX_ARGS_BYTES = 4096;
export const MAX_RESULT_BYTES = 32768;
export const MAX_BODY_BYTES = 8192;
export const PREVIEW_TTL_MS = 300_000;
export const HISTORY_LIMIT = 50;
export const MAX_NON_TERMINAL_PER_OPERATOR = 10;
export const MAX_NON_TERMINAL_GLOBAL = 100;

export const FORBIDDEN_RESULT_KEYS = [
  "api_key", "api_secret", "token", "telegram", "actor", "confirmation",
  "report_dir", "client_order_id", "payload", "raw", "secret",
] as const;
const FORBIDDEN_SET = new Set<string>(FORBIDDEN_RESULT_KEYS);

export class CommandValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CommandValidationError";
  }
}

function fail(reason: string): never {
  throw new CommandValidationError(reason);
}

const TICKET_ID_PATTERN = /^[A-Za-z0-9-]{1,64}$/;
const IDEMPOTENCY_PATTERN = /^[A-Za-z0-9_-]{8,128}$/;

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortValue(value));
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortValue((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

function checkRecord(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function checkIdempotencyKey(value: unknown): string {
  if (typeof value !== "string" || !IDEMPOTENCY_PATTERN.test(value)) {
    fail("idempotency_key is required");
  }
  return value;
}

function checkSymbol(value: unknown): string {
  if (typeof value !== "string") fail("symbol must be a string");
  const symbol = value.toUpperCase();
  if (!(KNOWN_SYMBOLS as readonly string[]).includes(symbol)) {
    fail("symbol is outside the configured allowlist");
  }
  return symbol;
}

function checkNoExtraKeys(args: Record<string, unknown>, allowed: readonly string[]) {
  for (const key of Object.keys(args)) {
    if (!allowed.includes(key)) fail(`unexpected argument: ${key}`);
  }
}

function checkArgsSize(args: unknown) {
  if (byteLength(canonicalJson(args)) > MAX_ARGS_BYTES) {
    fail("arguments exceed the 4 KiB limit");
  }
}

export interface CommandSubmission {
  kind: CommandKind;
  args: Record<string, unknown>;
  idempotency_key: string;
}

export function parseCommandSubmission(value: unknown): CommandSubmission {
  const body = checkRecord(value, "body");
  const idempotencyKey = checkIdempotencyKey(body.idempotency_key);
  const kind = body.kind;
  if (typeof kind !== "string" || !(SUBMITTABLE_KINDS as readonly string[]).includes(kind)) {
    fail("unknown or non-submittable command kind");
  }
  const rawArgs = checkRecord(body.args ?? {}, "args");
  checkArgsSize(rawArgs);
  let args: Record<string, unknown>;
  if (kind === "analyze") {
    checkNoExtraKeys(rawArgs, ["symbol"]);
    args = { symbol: checkSymbol(rawArgs.symbol) };
  } else if (kind === "reflections") {
    checkNoExtraKeys(rawArgs, ["symbol"]);
    args = {
      symbol: rawArgs.symbol == null ? null : checkSymbol(rawArgs.symbol),
    };
  } else {
    checkNoExtraKeys(rawArgs, []);
    args = {};
  }
  return { kind: kind as CommandKind, args, idempotency_key: idempotencyKey };
}

export interface PreviewRequest {
  action: ExecutionAction;
  ticket_id: string;
  idempotency_key: string;
}

export function parsePreviewRequest(value: unknown): PreviewRequest {
  const body = checkRecord(value, "body");
  const idempotencyKey = checkIdempotencyKey(body.idempotency_key);
  const action = body.action;
  if (typeof action !== "string" || !(EXECUTION_ACTIONS as readonly string[]).includes(action)) {
    fail("unknown execution action");
  }
  const ticketId = body.ticket_id;
  if (typeof ticketId !== "string" || !TICKET_ID_PATTERN.test(ticketId)) {
    fail("invalid ticket id");
  }
  return {
    action: action as ExecutionAction,
    ticket_id: ticketId,
    idempotency_key: idempotencyKey,
  };
}

export interface RunnerReport {
  session_id: string;
  command_id: string;
  status: "SUCCEEDED" | "FAILED" | "NEEDS_REVIEW";
  result: Record<string, unknown> | null;
  error_code: string | null;
}

export function parseRunnerReport(value: unknown): RunnerReport {
  const body = checkRecord(value, "body");
  const sessionId = body.session_id;
  const commandId = body.command_id;
  const status = body.status;
  if (typeof sessionId !== "string" || sessionId.length === 0) fail("session_id required");
  if (typeof commandId !== "string" || commandId.length === 0) fail("command_id required");
  if (status !== "SUCCEEDED" && status !== "FAILED" && status !== "NEEDS_REVIEW") {
    fail("status must be terminal");
  }
  const errorCode = body.error_code;
  if (errorCode != null && (typeof errorCode !== "string" || errorCode.length > 64)) {
    fail("error_code must be a short string");
  }
  const result = body.result == null ? null : checkRecord(body.result, "result");
  return {
    session_id: sessionId,
    command_id: commandId,
    status,
    result,
    error_code: (errorCode as string | null) ?? null,
  };
}

// Spec §6.2: exact match (case-insensitive), đệ quy; KHÔNG dùng substring
// (raw ⊄ drawdown). Fail closed trước khi ghi D1.
export function assertSafeResult(value: unknown): void {
  const text = canonicalJson(value ?? null);
  if (byteLength(text) > MAX_RESULT_BYTES) {
    fail("RESULT_TOO_LARGE");
  }
  walkForbidden(value);
}

function walkForbidden(value: unknown): void {
  if (Array.isArray(value)) {
    for (const item of value) walkForbidden(item);
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (FORBIDDEN_SET.has(key.toLowerCase())) fail(`forbidden result key: ${key}`);
      walkForbidden(item);
    }
  }
}

export type DeskInputResult =
  | { type: "command"; kind: CommandKind; args: Record<string, unknown> }
  | { type: "preview"; action: ExecutionAction; ticket_id: string }
  | { type: "error"; message: string };

// Grammar duyệt (spec §6): không flag, không pipe/redirect/quote.
export function parseDeskInput(input: string, symbols: readonly string[]): DeskInputResult {
  const tokens = input.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return { type: "error", message: "Nhập một lệnh" };
  if (tokens.some((t) => /[|&;<>"'`$\\]/.test(t))) {
    return { type: "error", message: "Cú pháp không được hỗ trợ" };
  }
  const [head, ...rest] = tokens;
  const kind = head.toLowerCase();
  const noArg = ["doctor", "sync", "screen", "daily", "health", "tickets", "orders"];
  if (noArg.includes(kind)) {
    if (rest.length > 0) return { type: "error", message: `${kind} không nhận tham số` };
    return { type: "command", kind: kind as CommandKind, args: {} };
  }
  if (kind === "analyze") {
    if (rest.length !== 1) return { type: "error", message: "Cú pháp: analyze <symbol>" };
    const symbol = rest[0].toUpperCase();
    if (!symbols.includes(symbol)) return { type: "error", message: "Symbol ngoài allowlist" };
    return { type: "command", kind: "analyze", args: { symbol } };
  }
  if (kind === "reflections") {
    if (rest.length > 1) return { type: "error", message: "Cú pháp: reflections [symbol]" };
    if (rest.length === 0) return { type: "command", kind: "reflections", args: { symbol: null } };
    const symbol = rest[0].toUpperCase();
    if (!symbols.includes(symbol)) return { type: "error", message: "Symbol ngoài allowlist" };
    return { type: "command", kind: "reflections", args: { symbol } };
  }
  if ((EXECUTION_ACTIONS as readonly string[]).includes(kind)) {
    if (rest.length !== 1 || !TICKET_ID_PATTERN.test(rest[0])) {
      return { type: "error", message: `Cú pháp: ${kind} <ticket_id>` };
    }
    return { type: "preview", action: kind as ExecutionAction, ticket_id: rest[0] };
  }
  return { type: "error", message: `Lệnh không được hỗ trợ: ${head}` };
}
```

- [ ] **Step 3: Chạy test và commit (repo web/)**

Run:

```bash
cd web && npm run test:unit && npm run lint
```

Expected: PASS.

Commit:

```bash
cd web
git add lib/command-contract.ts tests/unit/command-contract.test.mjs
git commit -m "feat: add desk command contract and grammar parser"
```

---

### Task 2: [web] Machine auth tách chung + 5 runner API routes

**Files:**
- Create: `web/lib/machine-auth.ts`, `web/lib/http-body.ts`
- Modify: `web/app/api/ingest/route.ts` (refactor dùng 2 lib mới, hành vi giữ nguyên)
- Create: `web/app/api/runner/session/route.ts`, `web/app/api/runner/heartbeat/route.ts`, `web/app/api/runner/claim/route.ts`, `web/app/api/runner/lease/route.ts`, `web/app/api/runner/result/route.ts`
- Test: `web/tests/command-api.test.mjs` (phần runner), Modify: `web/package.json` (thêm file test vào script `test`)

**Interfaces:**
- Consumes: Task 0 (`db/commands.ts`), Task 1 (`parseRunnerReport`, `assertSafeResult`, `CommandValidationError`).
- Produces: `bearerTokenMatches(request: Request, expected: string): Promise<boolean>`; `readBodyCapped(request: Request, maxBytes: number): Promise<Uint8Array | null>`; 5 route POST cho runner. Secret worker: `CRYPTO_DESK_RUNNER_TOKEN` (khác token ingest). Response shapes: session → `{ok:true}` | 409 `{error:"SESSION_CONFLICT"}`; heartbeat → `{ok:true}` | 409 `{error:"SESSION_MISMATCH"}`; claim → `{command: CommandRow & {args: object} | null}`; lease → `{ok:true}` | 409 `{error:"LEASE_LOST"}`; result → `{ok:true, applied: boolean}`.

- [ ] **Step 1: Tách helper từ ingest**

Tạo `web/lib/machine-auth.ts` (chuyển nguyên văn `sha256`, `constantTimeEqual` và tổng quát hóa `isAuthorized` từ `web/app/api/ingest/route.ts`):

```ts
// Bearer-token machine auth dùng chung cho /api/ingest và /api/runner/*.
// So sánh digest constant-time; secret chưa cấu hình → không bao giờ authorize.
async function sha256(text: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return new Uint8Array(digest);
}

function constantTimeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a[i] ^ b[i];
  }
  return diff === 0;
}

export async function bearerTokenMatches(request: Request, expected: string): Promise<boolean> {
  if (!expected) return false;
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer (.+)$/.exec(header);
  const provided = match ? match[1] : "";
  const [providedDigest, expectedDigest] = await Promise.all([sha256(provided), sha256(expected)]);
  return constantTimeEqual(providedDigest, expectedDigest);
}
```

Tạo `web/lib/http-body.ts`: chuyển NGUYÊN VĂN hàm `readBodyCapped(request, maxBytes)` hiện có trong `web/app/api/ingest/route.ts` (dòng 46–75) sang đây và `export` nó. Sửa `web/app/api/ingest/route.ts`: xóa `sha256`, `constantTimeEqual`, `isAuthorized`, `readBodyCapped` nội bộ; thay bằng:

```ts
import { bearerTokenMatches } from "../../../lib/machine-auth";
import { readBodyCapped } from "../../../lib/http-body";
// ... trong POST:
const expected = (env as { CRYPTO_DESK_INGEST_TOKEN?: string }).CRYPTO_DESK_INGEST_TOKEN ?? "";
if (!(await bearerTokenMatches(request, expected))) {
  return jsonResponse({ error: "UNAUTHORIZED" }, 401);
}
```

Mọi status/error body của ingest giữ nguyên — test `dashboard-api.test.mjs` hiện có phải tiếp tục pass nguyên trạng.

- [ ] **Step 2: Viết failing route tests (qua built worker, theo pattern dashboard-api.test.mjs)**

Tạo `web/tests/command-api.test.mjs` — phần khung + runner tests (Task 3–4 sẽ nối thêm test user routes vào cùng file):

```js
import assert from "node:assert/strict";
import { before, test } from "node:test";
import "./support/cloudflare-workers-stub.mjs";
import { createFakeD1 } from "./support/fake-d1.mjs";

const RUNNER_TOKEN = "runner-token-for-tests";
const CSRF_SECRET = "csrf-secret-for-tests";

let worker;
before(async () => {
  worker = (await import(`../dist/server/index.js?t=${Date.now()}`)).default;
});

function makeEnv(db) {
  return {
    DB: db,
    CRYPTO_DESK_RUNNER_TOKEN: RUNNER_TOKEN,
    CRYPTO_DESK_CSRF_SECRET: CSRF_SECRET,
    CRYPTO_DESK_COMMAND_UI_ENABLED: "1",
  };
}

function makeCtx() {
  return { waitUntil() {}, passThroughOnException() {} };
}

async function call(env, path, init = {}) {
  globalThis.__TEST_ENV__ = env;
  const request = new Request(`https://desk.example${path}`, init);
  return worker.fetch(request, env, makeCtx());
}

function runnerPost(path, body, token = RUNNER_TOKEN) {
  return {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  };
}

async function registerAndClaimHarness(env) {
  const reg = await call(env, "/api/runner/session",
    runnerPost("/api/runner/session", { session_id: "s1", execution_mode: "DRY_RUN" }));
  assert.equal(reg.status, 200);
  return reg;
}

test("runner routes reject a missing or wrong bearer token", async () => {
  const env = makeEnv(createFakeD1());
  const noAuth = await call(env, "/api/runner/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ session_id: "s1", execution_mode: "DRY_RUN" }),
  });
  assert.equal(noAuth.status, 401);
  const wrong = await call(env, "/api/runner/claim",
    runnerPost("", { session_id: "s1" }, "not-the-token"));
  assert.equal(wrong.status, 401);
});

test("runner routes never authorize when the secret is unconfigured", async () => {
  const env = { ...makeEnv(createFakeD1()), CRYPTO_DESK_RUNNER_TOKEN: "" };
  const res = await call(env, "/api/runner/session",
    runnerPost("", { session_id: "s1", execution_mode: "DRY_RUN" }));
  assert.equal(res.status, 401);
});

test("session register/resume/conflict follows the singleton rule", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const resume = await call(env, "/api/runner/session",
    runnerPost("", { session_id: "s1", execution_mode: "TESTNET_ORDER" }));
  assert.equal(resume.status, 200);
  const conflict = await call(env, "/api/runner/session",
    runnerPost("", { session_id: "s2", execution_mode: "DRY_RUN" }));
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).error, "SESSION_CONFLICT");
});

test("claim returns null with an empty queue and heartbeats require the session", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const claim = await call(env, "/api/runner/claim", runnerPost("", { session_id: "s1" }));
  assert.equal(claim.status, 200);
  assert.equal((await claim.json()).command, null);
  const badBeat = await call(env, "/api/runner/heartbeat",
    runnerPost("", { session_id: "ghost", execution_mode: "DRY_RUN" }));
  assert.equal(badBeat.status, 409);
});

test("result route stores a safe result once and downgrades unsafe results to FAILED/RESULT_UNSAFE", async () => {
  const db = createFakeD1();
  const env = makeEnv(db);
  await registerAndClaimHarness(env);
  // seed 2 lệnh QUEUED trực tiếp qua repository để không phụ thuộc user routes
  globalThis.__TEST_ENV__ = env;
  const { createCommand } = await import("../db/commands.ts");
  const a = await createCommand({
    id: "cmd-a", kind: "sync", args: "{}", operatorEmail: "op@x", idempotencyKey: "k-a",
    previewCommandId: null, queuedAt: "2026-07-18T09:00:00.000Z",
  });
  await createCommand({
    id: "cmd-b", kind: "tickets", args: "{}", operatorEmail: "op@x", idempotencyKey: "k-b",
    previewCommandId: null, queuedAt: "2026-07-18T09:00:01.000Z",
  });

  const claim = await call(env, "/api/runner/claim", runnerPost("", { session_id: "s1" }));
  const claimed = (await claim.json()).command;
  assert.equal(claimed.id, a.row.id);
  assert.deepEqual(claimed.args, {}); // args trả về đã parse

  const ok = await call(env, "/api/runner/result", runnerPost("", {
    session_id: "s1", command_id: "cmd-a", status: "SUCCEEDED",
    result: { environment: "testnet", nav_usdt: "1000" }, error_code: null,
  }));
  assert.equal(ok.status, 200);
  assert.deepEqual(await ok.json(), { ok: true, applied: true });

  // command thứ hai: result chứa forbidden key → server hạ xuống FAILED/RESULT_UNSAFE
  await call(env, "/api/runner/claim", runnerPost("", { session_id: "s1" }));
  const unsafe = await call(env, "/api/runner/result", runnerPost("", {
    session_id: "s1", command_id: "cmd-b", status: "SUCCEEDED",
    result: { client_order_id: "leak" }, error_code: null,
  }));
  assert.equal(unsafe.status, 200);
  const { getCommand } = await import("../db/commands.ts");
  const row = await getCommand("cmd-b");
  assert.equal(row.status, "FAILED");
  assert.equal(row.error_code, "RESULT_UNSAFE");
  assert.equal(row.result, null);
});

test("lease renew succeeds for the active command and reports LEASE_LOST after sweep", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  globalThis.__TEST_ENV__ = env;
  const { createCommand } = await import("../db/commands.ts");
  await createCommand({
    id: "cmd-l", kind: "daily", args: "{}", operatorEmail: "op@x", idempotencyKey: "k-l",
    previewCommandId: null, queuedAt: "2026-07-18T09:00:00.000Z",
  });
  await call(env, "/api/runner/claim", runnerPost("", { session_id: "s1" }));
  const renew = await call(env, "/api/runner/lease",
    runnerPost("", { session_id: "s1", command_id: "cmd-l" }));
  assert.equal(renew.status, 200);
  const lost = await call(env, "/api/runner/lease",
    runnerPost("", { session_id: "s1", command_id: "cmd-unknown" }));
  assert.equal(lost.status, 409);
  assert.equal((await lost.json()).error, "LEASE_LOST");
});
```

Cập nhật script test trong `web/package.json`:

```json
"test": "npm run test:unit && npm run build && node --test tests/rendered-html.test.mjs tests/dashboard-api.test.mjs tests/command-api.test.mjs"
```

Run: `cd web && npm test`

Expected: FAIL — các route `/api/runner/*` trả 404.

- [ ] **Step 3: Implement 5 runner routes**

Mẫu chung (mọi route runner): chỉ POST, bearer `CRYPTO_DESK_RUNNER_TOKEN`, body JSON ≤ `MAX_BODY_BYTES`, không bao giờ đọc cookie/identity header. Tạo `web/app/api/runner/session/route.ts`:

```ts
import { env } from "cloudflare:workers";
import { registerRunnerSession } from "../../../../db/commands";
import { jsonResponse } from "../../../../lib/json-response";
import { bearerTokenMatches } from "../../../../lib/machine-auth";
import { readBodyCapped } from "../../../../lib/http-body";
import { MAX_BODY_BYTES } from "../../../../lib/command-contract";

type RunnerEnv = { CRYPTO_DESK_RUNNER_TOKEN?: string };

export async function runnerBody(request: Request): Promise<Record<string, unknown> | Response> {
  const expected = (env as RunnerEnv).CRYPTO_DESK_RUNNER_TOKEN ?? "";
  if (!(await bearerTokenMatches(request, expected))) {
    return jsonResponse({ error: "UNAUTHORIZED" }, 401);
  }
  const raw = await readBodyCapped(request, MAX_BODY_BYTES);
  if (raw === null) return jsonResponse({ error: "PAYLOAD_TOO_LARGE" }, 413);
  try {
    const parsed = JSON.parse(new TextDecoder().decode(raw));
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return jsonResponse({ error: "INVALID_JSON" }, 400);
    }
    return parsed as Record<string, unknown>;
  } catch {
    return jsonResponse({ error: "INVALID_JSON" }, 400);
  }
}

export async function POST(request: Request): Promise<Response> {
  const body = await runnerBody(request);
  if (body instanceof Response) return body;
  const sessionId = body.session_id;
  const executionMode = body.execution_mode;
  if (typeof sessionId !== "string" || sessionId.length === 0) {
    return jsonResponse({ error: "INVALID_COMMAND" }, 400);
  }
  if (executionMode !== "TESTNET_ORDER" && executionMode !== "DRY_RUN") {
    return jsonResponse({ error: "INVALID_COMMAND" }, 400);
  }
  const outcome = await registerRunnerSession({
    sessionId,
    executionMode,
    nowIso: new Date().toISOString(),
  });
  if (!outcome.ok) return jsonResponse({ error: outcome.error }, 409);
  return jsonResponse({ ok: true }, 200);
}
```

`heartbeat/route.ts` — import `runnerBody` từ `../session/route` (named export ở trên), validate `session_id` + `execution_mode` như trên rồi:

```ts
const outcome = await heartbeatRunner({ sessionId, executionMode, nowIso: new Date().toISOString() });
if (!outcome.ok) return jsonResponse({ error: "SESSION_MISMATCH" }, 409);
return jsonResponse({ ok: true }, 200);
```

`claim/route.ts` — validate `session_id` rồi:

```ts
const outcome = await claimNext({ sessionId, nowIso: new Date().toISOString() });
if (outcome.error) return jsonResponse({ error: outcome.error }, 409);
if (!outcome.command) return jsonResponse({ command: null }, 200);
return jsonResponse(
  { command: { ...outcome.command, args: JSON.parse(outcome.command.args) } },
  200
);
```

`lease/route.ts` — validate `session_id`, `command_id` (string không rỗng) rồi:

```ts
const outcome = await renewLease({ sessionId, commandId, nowIso: new Date().toISOString() });
if (!outcome.ok) return jsonResponse({ error: "LEASE_LOST" }, 409);
return jsonResponse({ ok: true }, 200);
```

`result/route.ts`:

```ts
import {
  assertSafeResult, canonicalJson, parseRunnerReport, CommandValidationError,
} from "../../../../lib/command-contract";
import { reportResult } from "../../../../db/commands";
import { runnerBody } from "../session/route";
import { jsonResponse } from "../../../../lib/json-response";

export async function POST(request: Request): Promise<Response> {
  const body = await runnerBody(request);
  if (body instanceof Response) return body;
  let report;
  try {
    report = parseRunnerReport(body);
  } catch (error) {
    if (error instanceof CommandValidationError) {
      return jsonResponse({ error: "INVALID_COMMAND" }, 400);
    }
    throw error;
  }
  const nowIso = new Date().toISOString();
  let status = report.status;
  let resultText: string | null = null;
  let errorCode = report.error_code;
  if (report.result !== null) {
    try {
      assertSafeResult(report.result);
      resultText = canonicalJson(report.result);
    } catch {
      // Fail closed phía server (spec §6.2): không lưu result, hạ thành FAILED.
      status = "FAILED";
      resultText = null;
      errorCode = "RESULT_UNSAFE";
    }
  }
  const outcome = await reportResult({
    sessionId: report.session_id,
    commandId: report.command_id,
    status,
    result: resultText,
    errorCode,
    nowIso,
  });
  return jsonResponse({ ok: true, applied: outcome.applied }, 200);
}
```

- [ ] **Step 4: Chạy test và commit (repo web/)**

Run:

```bash
cd web && npm test && npm run lint
```

Expected: PASS — gồm cả `dashboard-api.test.mjs` cũ (ingest refactor không đổi hành vi).

Commit:

```bash
cd web
git add lib/machine-auth.ts lib/http-body.ts app/api/ingest/route.ts app/api/runner tests/command-api.test.mjs package.json
git commit -m "feat: add runner protocol API with shared machine auth"
```

---

### Task 3: [web] CSRF + user command API (session, enqueue, history, detail)

**Files:**
- Create: `web/lib/csrf.ts`
- Create: `web/app/api/command-session/route.ts`, `web/app/api/commands/route.ts`, `web/app/api/commands/[id]/route.ts`
- Test: `web/tests/unit/csrf.test.mjs`, Modify: `web/tests/command-api.test.mjs`

**Interfaces:**
- Consumes: Task 0 repo, Task 1 contract, `getChatGPTUser`-style header đọc trực tiếp `oai-authenticated-user-email` từ `request.headers` (route handler nhận `Request`, không dùng `next/headers`).
- Produces: `issueCsrfToken(secret, email, nowMs): Promise<string>`, `verifyCsrfToken(secret, email, token, nowMs): Promise<boolean>` (HMAC-SHA256, bucket 900s, chấp nhận bucket hiện tại + trước đó); `GET /api/command-session` → `{enabled, operator:{email}, csrf, runner:{online, execution_mode, last_heartbeat_at, active_command_id}}`; `POST /api/commands` → 200 `{command, created}`; `GET /api/commands?limit=50` → `{runner, active, recent}`; `GET /api/commands/{id}` → `{command}` | 404. Header CSRF: `x-desk-csrf`.

- [ ] **Step 1: Viết failing CSRF unit test**

Tạo `web/tests/unit/csrf.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

const { issueCsrfToken, verifyCsrfToken } = await import("../../lib/csrf.ts");

const SECRET = "csrf-secret";
const EMAIL = "quoc.lb@teko.vn";
const T = Date.parse("2026-07-18T10:00:00Z");

test("token verifies for the same email within the bucket and the next bucket", async () => {
  const token = await issueCsrfToken(SECRET, EMAIL, T);
  assert.equal(await verifyCsrfToken(SECRET, EMAIL, token, T), true);
  assert.equal(await verifyCsrfToken(SECRET, EMAIL, token, T + 899_000), true);
  // sang bucket kế tiếp vẫn chấp nhận (bucket trước đó)
  assert.equal(await verifyCsrfToken(SECRET, EMAIL, token, T + 901_000), true);
  // quá 2 bucket → hết hạn
  assert.equal(await verifyCsrfToken(SECRET, EMAIL, token, T + 1_801_000), false);
});

test("token is bound to email and secret", async () => {
  const token = await issueCsrfToken(SECRET, EMAIL, T);
  assert.equal(await verifyCsrfToken(SECRET, "other@teko.vn", token, T), false);
  assert.equal(await verifyCsrfToken("other-secret", EMAIL, token, T), false);
  assert.equal(await verifyCsrfToken(SECRET, EMAIL, "deadbeef", T), false);
});
```

Run: `cd web && npm run test:unit` — Expected: FAIL (`lib/csrf.ts` chưa tồn tại).

- [ ] **Step 2: Implement `web/lib/csrf.ts`**

```ts
// CSRF nonce stateless: HMAC-SHA256(secret, `${email}:${bucket}`), bucket 15
// phút; verify chấp nhận bucket hiện tại và bucket liền trước (spec §12).
const BUCKET_MS = 900_000;

async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return Array.from(new Uint8Array(signature))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function issueCsrfToken(secret: string, email: string, nowMs: number): Promise<string> {
  const bucket = Math.floor(nowMs / BUCKET_MS);
  return hmacHex(secret, `${email}:${bucket}`);
}

export async function verifyCsrfToken(
  secret: string,
  email: string,
  token: string,
  nowMs: number
): Promise<boolean> {
  const bucket = Math.floor(nowMs / BUCKET_MS);
  for (const candidate of [bucket, bucket - 1]) {
    if ((await hmacHex(secret, `${email}:${candidate}`)) === token) return true;
  }
  return false;
}
```

- [ ] **Step 3: Nối thêm failing route tests vào `web/tests/command-api.test.mjs`**

```js
function userPost(body, csrf, email = "quoc.lb@teko.vn") {
  return {
    method: "POST",
    headers: {
      "oai-authenticated-user-email": email,
      origin: "https://desk.example",
      "content-type": "application/json",
      "x-desk-csrf": csrf,
    },
    body: JSON.stringify(body),
  };
}

async function getCsrf(env, email = "quoc.lb@teko.vn") {
  const res = await call(env, "/api/command-session", {
    headers: { "oai-authenticated-user-email": email },
  });
  assert.equal(res.status, 200);
  return (await res.json()).csrf;
}

test("command-session returns identity, csrf, feature flag and runner offline state", async () => {
  const env = makeEnv(createFakeD1());
  const res = await call(env, "/api/command-session", {
    headers: { "oai-authenticated-user-email": "quoc.lb@teko.vn" },
  });
  const body = await res.json();
  assert.equal(body.enabled, true);
  assert.equal(body.operator.email, "quoc.lb@teko.vn");
  assert.equal(typeof body.csrf, "string");
  assert.equal(body.runner.online, false);
});

test("command-session without identity header returns 401", async () => {
  const env = makeEnv(createFakeD1());
  const res = await call(env, "/api/command-session", {});
  assert.equal(res.status, 401);
});

test("POST /api/commands enforces the full guard ladder", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env); // runner online
  const csrf = await getCsrf(env);

  const noIdentity = await call(env, "/api/commands", {
    method: "POST",
    headers: { origin: "https://desk.example", "content-type": "application/json", "x-desk-csrf": csrf },
    body: JSON.stringify({ kind: "sync", args: {}, idempotency_key: "k-guard-1" }),
  });
  assert.equal(noIdentity.status, 401);

  const badOrigin = await call(env, "/api/commands", {
    ...userPost({ kind: "sync", args: {}, idempotency_key: "k-guard-2" }, csrf),
    headers: {
      "oai-authenticated-user-email": "quoc.lb@teko.vn",
      origin: "https://evil.example",
      "content-type": "application/json",
      "x-desk-csrf": csrf,
    },
  });
  assert.equal(badOrigin.status, 403);

  const badCsrf = await call(env, "/api/commands",
    userPost({ kind: "sync", args: {}, idempotency_key: "k-guard-3" }, "wrong"));
  assert.equal(badCsrf.status, 403);

  const badSchema = await call(env, "/api/commands",
    userPost({ kind: "execute", args: {}, idempotency_key: "k-guard-4" }, csrf));
  assert.equal(badSchema.status, 400);

  const ok = await call(env, "/api/commands",
    userPost({ kind: "sync", args: {}, idempotency_key: "k-guard-5" }, csrf));
  assert.equal(ok.status, 200);
  const created = await ok.json();
  assert.equal(created.created, true);
  assert.equal(created.command.status, "QUEUED");
  assert.equal(created.command.operator_email, "quoc.lb@teko.vn");

  const dup = await call(env, "/api/commands",
    userPost({ kind: "sync", args: {}, idempotency_key: "k-guard-5" }, csrf));
  assert.equal((await dup.json()).created, false);
});

test("POST /api/commands rejects when the runner is offline or the feature flag is off", async () => {
  const env = makeEnv(createFakeD1());
  const csrf = await getCsrf(env);
  const offline = await call(env, "/api/commands",
    userPost({ kind: "sync", args: {}, idempotency_key: "k-off-1" }, csrf));
  assert.equal(offline.status, 409);
  assert.equal((await offline.json()).error, "RUNNER_OFFLINE");

  const disabledEnv = { ...makeEnv(createFakeD1()), CRYPTO_DESK_COMMAND_UI_ENABLED: "0" };
  const csrf2 = await getCsrf(disabledEnv);
  const disabled = await call(disabledEnv, "/api/commands",
    userPost({ kind: "sync", args: {}, idempotency_key: "k-off-2" }, csrf2));
  assert.equal(disabled.status, 403);
  assert.equal((await disabled.json()).error, "FEATURE_DISABLED");
});

test("per-operator limit returns 429 at the 11th non-terminal command", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);
  for (let i = 0; i < 10; i += 1) {
    const res = await call(env, "/api/commands",
      userPost({ kind: "tickets", args: {}, idempotency_key: `k-limit-${i}` }, csrf));
    assert.equal(res.status, 200);
  }
  const over = await call(env, "/api/commands",
    userPost({ kind: "tickets", args: {}, idempotency_key: "k-limit-10" }, csrf));
  assert.equal(over.status, 429);
  assert.equal((await over.json()).error, "QUEUE_LIMIT_OPERATOR");
});

test("GET /api/commands returns runner state, FIFO active list and recent history; GET by id restores one command", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);
  const created = await (await call(env, "/api/commands",
    userPost({ kind: "screen", args: {}, idempotency_key: "k-list-1" }, csrf))).json();

  const list = await call(env, "/api/commands?limit=50", {
    headers: { "oai-authenticated-user-email": "quoc.lb@teko.vn" },
  });
  const body = await list.json();
  assert.equal(body.active.length, 1);
  assert.equal(body.runner.online, true);

  const one = await call(env, `/api/commands/${created.command.id}`, {
    headers: { "oai-authenticated-user-email": "quoc.lb@teko.vn" },
  });
  assert.equal((await one.json()).command.id, created.command.id);
  const missing = await call(env, "/api/commands/not-a-command", {
    headers: { "oai-authenticated-user-email": "quoc.lb@teko.vn" },
  });
  assert.equal(missing.status, 404);
});
```

Run: `cd web && npm test` — Expected: FAIL (routes chưa tồn tại).

- [ ] **Step 4: Implement 3 user routes**

Guard chain dùng chung — tạo helper trong `web/app/api/commands/route.ts` và export cho các route khác:

```ts
import { env } from "cloudflare:workers";
import {
  createCommand, countNonTerminal, listCommands, getRunnerState, runnerOnline,
  sweepExpiredLeases, cleanupExpired,
} from "../../../db/commands";
import {
  parseCommandSubmission, CommandValidationError,
  HISTORY_LIMIT, MAX_BODY_BYTES, MAX_NON_TERMINAL_GLOBAL, MAX_NON_TERMINAL_PER_OPERATOR,
  canonicalJson,
} from "../../../lib/command-contract";
import { verifyCsrfToken } from "../../../lib/csrf";
import { readBodyCapped } from "../../../lib/http-body";
import { jsonResponse } from "../../../lib/json-response";

type CommandEnv = {
  CRYPTO_DESK_CSRF_SECRET?: string;
  CRYPTO_DESK_COMMAND_UI_ENABLED?: string;
};

export function operatorEmail(request: Request): string | null {
  return request.headers.get("oai-authenticated-user-email");
}

export function featureEnabled(): boolean {
  return (env as CommandEnv).CRYPTO_DESK_COMMAND_UI_ENABLED === "1";
}

// Guard ladder cho mọi write route (spec §12). Trả Response khi bị chặn,
// ngược lại trả context đã xác thực.
export async function guardWrite(
  request: Request
): Promise<{ email: string; body: Record<string, unknown> } | Response> {
  const email = operatorEmail(request);
  if (!email) return jsonResponse({ error: "UNAUTHORIZED" }, 401);
  if (!featureEnabled()) return jsonResponse({ error: "FEATURE_DISABLED" }, 403);
  const origin = request.headers.get("origin");
  if (!origin || origin !== new URL(request.url).origin) {
    return jsonResponse({ error: "FORBIDDEN_ORIGIN" }, 403);
  }
  const contentType = (request.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    return jsonResponse({ error: "UNSUPPORTED_MEDIA_TYPE" }, 415);
  }
  const secret = (env as CommandEnv).CRYPTO_DESK_CSRF_SECRET ?? "";
  const csrf = request.headers.get("x-desk-csrf") ?? "";
  if (!secret || !(await verifyCsrfToken(secret, email, csrf, Date.now()))) {
    return jsonResponse({ error: "INVALID_CSRF" }, 403);
  }
  const raw = await readBodyCapped(request, MAX_BODY_BYTES);
  if (raw === null) return jsonResponse({ error: "PAYLOAD_TOO_LARGE" }, 413);
  try {
    const parsed = JSON.parse(new TextDecoder().decode(raw));
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return jsonResponse({ error: "INVALID_JSON" }, 400);
    }
    return { email, body: parsed as Record<string, unknown> };
  } catch {
    return jsonResponse({ error: "INVALID_JSON" }, 400);
  }
}

export async function enqueueGuards(email: string): Promise<Response | null> {
  const nowIso = new Date().toISOString();
  await sweepExpiredLeases(nowIso);
  const runner = await getRunnerState();
  if (!runnerOnline(runner, nowIso)) {
    return jsonResponse({ error: "RUNNER_OFFLINE" }, 409);
  }
  if ((await countNonTerminal(email)) >= MAX_NON_TERMINAL_PER_OPERATOR) {
    return jsonResponse({ error: "QUEUE_LIMIT_OPERATOR" }, 429);
  }
  if ((await countNonTerminal()) >= MAX_NON_TERMINAL_GLOBAL) {
    return jsonResponse({ error: "QUEUE_LIMIT_GLOBAL" }, 429);
  }
  return null;
}

export async function POST(request: Request): Promise<Response> {
  const guarded = await guardWrite(request);
  if (guarded instanceof Response) return guarded;
  let submission;
  try {
    submission = parseCommandSubmission(guarded.body);
  } catch (error) {
    if (error instanceof CommandValidationError) {
      return jsonResponse({ error: "INVALID_COMMAND", message: error.message }, 400);
    }
    throw error;
  }
  const blocked = await enqueueGuards(guarded.email);
  if (blocked) return blocked;
  const nowIso = new Date().toISOString();
  await cleanupExpired(nowIso); // retention 30 ngày, opportunistic
  const { row, created } = await createCommand({
    id: crypto.randomUUID(),
    kind: submission.kind,
    args: canonicalJson(submission.args),
    operatorEmail: guarded.email,
    idempotencyKey: submission.idempotency_key,
    previewCommandId: null,
    queuedAt: nowIso,
  });
  return jsonResponse({ command: publicCommand(row), created }, 200);
}

export function publicCommand(row: {
  args: string;
  result: string | null;
  [key: string]: unknown;
}): Record<string, unknown> {
  return {
    ...row,
    args: JSON.parse(row.args),
    result: row.result === null ? null : JSON.parse(row.result),
  };
}

export async function GET(request: Request): Promise<Response> {
  if (!operatorEmail(request)) return jsonResponse({ error: "UNAUTHORIZED" }, 401);
  const nowIso = new Date().toISOString();
  await sweepExpiredLeases(nowIso);
  const runner = await getRunnerState();
  const { active, recent } = await listCommands(HISTORY_LIMIT);
  return jsonResponse(
    {
      runner: {
        online: runnerOnline(runner, nowIso),
        execution_mode: runner?.execution_mode ?? null,
        last_heartbeat_at: runner?.last_heartbeat_at ?? null,
        active_command_id: runner?.active_command_id ?? null,
      },
      active: active.map(publicCommand),
      recent: recent.map(publicCommand),
    },
    200
  );
}
```

`web/app/api/commands/[id]/route.ts`:

```ts
import { getCommand } from "../../../../db/commands";
import { jsonResponse } from "../../../../lib/json-response";
import { operatorEmail, publicCommand } from "../route";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> }
): Promise<Response> {
  if (!operatorEmail(request)) return jsonResponse({ error: "UNAUTHORIZED" }, 401);
  const { id } = await params;
  const row = await getCommand(id);
  if (!row) return jsonResponse({ error: "NOT_FOUND" }, 404);
  return jsonResponse({ command: publicCommand(row) }, 200);
}
```

`web/app/api/command-session/route.ts`:

```ts
import { env } from "cloudflare:workers";
import { getRunnerState, runnerOnline, sweepExpiredLeases } from "../../../db/commands";
import { issueCsrfToken } from "../../../lib/csrf";
import { jsonResponse } from "../../../lib/json-response";
import { featureEnabled, operatorEmail } from "../commands/route";

export async function GET(request: Request): Promise<Response> {
  const email = operatorEmail(request);
  if (!email) return jsonResponse({ error: "UNAUTHORIZED" }, 401);
  const secret = (env as { CRYPTO_DESK_CSRF_SECRET?: string }).CRYPTO_DESK_CSRF_SECRET ?? "";
  const nowIso = new Date().toISOString();
  await sweepExpiredLeases(nowIso);
  const runner = await getRunnerState();
  return jsonResponse(
    {
      enabled: featureEnabled() && secret.length > 0,
      operator: { email },
      csrf: secret ? await issueCsrfToken(secret, email, Date.now()) : null,
      runner: {
        online: runnerOnline(runner, nowIso),
        execution_mode: runner?.execution_mode ?? null,
        last_heartbeat_at: runner?.last_heartbeat_at ?? null,
        active_command_id: runner?.active_command_id ?? null,
      },
    },
    200
  );
}
```

- [ ] **Step 5: Chạy test và commit (repo web/)**

Run:

```bash
cd web && npm test && npm run lint
```

Expected: PASS.

Commit:

```bash
cd web
git add lib/csrf.ts app/api/command-session app/api/commands tests/unit/csrf.test.mjs tests/command-api.test.mjs
git commit -m "feat: add authenticated user command API with CSRF and queue limits"
```

---

### Task 4: [web] Preview + Confirm routes

**Files:**
- Create: `web/app/api/command-previews/route.ts`, `web/app/api/command-previews/[id]/confirm/route.ts`
- Test: Modify `web/tests/command-api.test.mjs`

**Interfaces:**
- Consumes: `guardWrite`, `enqueueGuards`, `publicCommand` (Task 3), `parsePreviewRequest`, `PREVIEW_TTL_MS`, `canonicalJson` (Task 1), repo Task 0.
- Produces: `POST /api/command-previews` → 200 `{command, created}` (command kind=`preview`); `POST /api/command-previews/{id}/confirm` body `{idempotency_key}` → 200 `{command, created}` (kind=`execute`, args gồm `action`, `ticket_id`, `preview_command_id`, `fingerprint` copy từ preview result). Lỗi: `PREVIEW_NOT_FOUND` 404, `PREVIEW_NOT_READY` 409, `PREVIEW_EXPIRED` 410, `PREVIEW_OPERATOR_MISMATCH` 403.

- [ ] **Step 1: Nối failing tests vào `web/tests/command-api.test.mjs`**

```js
async function runPreviewToSuccess(env, csrf, { fingerprint = "a".repeat(64) } = {}) {
  const preview = await (await call(env, "/api/command-previews",
    userPost({ action: "approve", ticket_id: "ticket-1", idempotency_key: crypto.randomUUID() }, csrf))).json();
  await call(env, "/api/runner/claim", runnerPost("", { session_id: "s1" }));
  await call(env, "/api/runner/result", runnerPost("", {
    session_id: "s1", command_id: preview.command.id, status: "SUCCEEDED",
    result: {
      action: "approve", ticket_id: "ticket-1", environment: "testnet",
      execution_mode: "DRY_RUN", status: "PENDING", symbol: "BTCUSDT",
      side: "BUY", intent: "OPEN", quantity: "0.00025", notional_usdt: "25",
      entry: "100000.00", stop: "95000.00", target: "110000.00",
      created_at: "2026-07-18T09:00:00+00:00", expires_at: "2026-07-18T09:30:00+00:00",
      submission_status: null, fingerprint,
    },
    error_code: null,
  }));
  return preview.command;
}

test("preview enqueues a preview command and confirm creates one execute command", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);
  const previewCommand = await runPreviewToSuccess(env, csrf);

  const confirm = await call(env, `/api/command-previews/${previewCommand.id}/confirm`,
    userPost({ idempotency_key: "k-confirm-1" }, csrf));
  assert.equal(confirm.status, 200);
  const body = await confirm.json();
  assert.equal(body.command.kind, "execute");
  assert.equal(body.command.args.action, "approve");
  assert.equal(body.command.args.preview_command_id, previewCommand.id);
  assert.equal(body.command.args.fingerprint, "a".repeat(64));

  // Double-click / retry cùng idempotency key → không tạo command thứ hai
  const again = await call(env, `/api/command-previews/${previewCommand.id}/confirm`,
    userPost({ idempotency_key: "k-confirm-1" }, csrf));
  assert.equal((await again.json()).created, false);
});

test("confirm rejects: unknown preview, not-ready preview, wrong operator", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);

  const unknown = await call(env, "/api/command-previews/nope/confirm",
    userPost({ idempotency_key: "k-c-1" }, csrf));
  assert.equal(unknown.status, 404);

  const pending = await (await call(env, "/api/command-previews",
    userPost({ action: "reject", ticket_id: "ticket-2", idempotency_key: "k-c-2" }, csrf))).json();
  const notReady = await call(env, `/api/command-previews/${pending.command.id}/confirm`,
    userPost({ idempotency_key: "k-c-3" }, csrf));
  assert.equal(notReady.status, 409);
  assert.equal((await notReady.json()).error, "PREVIEW_NOT_READY");
});

test("confirm rejects an expired preview with PREVIEW_EXPIRED and creates nothing", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);
  const previewCommand = await runPreviewToSuccess(env, csrf);

  // Già hóa finished_at quá 5 phút bằng SQL trực tiếp trên fake D1
  globalThis.__TEST_ENV__ = env;
  const { getCommand } = await import("../db/commands.ts");
  const stale = new Date(Date.now() - 301_000).toISOString();
  await env.DB.prepare("UPDATE desk_commands SET finished_at = ?1 WHERE id = ?2")
    .bind(stale, previewCommand.id).run();

  const expired = await call(env, `/api/command-previews/${previewCommand.id}/confirm`,
    userPost({ idempotency_key: "k-exp-1" }, csrf));
  assert.equal(expired.status, 410);
  assert.equal((await expired.json()).error, "PREVIEW_EXPIRED");
  assert.equal((await getCommand("k-exp-1")), null); // không có execute command nào
});

test("confirm by a different authenticated user is rejected", async () => {
  const env = makeEnv(createFakeD1());
  await registerAndClaimHarness(env);
  const csrf = await getCsrf(env);
  const previewCommand = await runPreviewToSuccess(env, csrf);
  const otherCsrf = await getCsrf(env, "someone.else@teko.vn");
  const res = await call(env, `/api/command-previews/${previewCommand.id}/confirm`,
    userPost({ idempotency_key: "k-op-1" }, otherCsrf, "someone.else@teko.vn"));
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, "PREVIEW_OPERATOR_MISMATCH");
});
```

Run: `cd web && npm test` — Expected: FAIL (2 route chưa có).

- [ ] **Step 2: Implement 2 routes**

`web/app/api/command-previews/route.ts`:

```ts
import { createCommand, cleanupExpired } from "../../../db/commands";
import {
  parsePreviewRequest, CommandValidationError, canonicalJson,
} from "../../../lib/command-contract";
import { jsonResponse } from "../../../lib/json-response";
import { enqueueGuards, guardWrite, publicCommand } from "../commands/route";

export async function POST(request: Request): Promise<Response> {
  const guarded = await guardWrite(request);
  if (guarded instanceof Response) return guarded;
  let preview;
  try {
    preview = parsePreviewRequest(guarded.body);
  } catch (error) {
    if (error instanceof CommandValidationError) {
      return jsonResponse({ error: "INVALID_COMMAND", message: error.message }, 400);
    }
    throw error;
  }
  const blocked = await enqueueGuards(guarded.email);
  if (blocked) return blocked;
  const nowIso = new Date().toISOString();
  await cleanupExpired(nowIso);
  const { row, created } = await createCommand({
    id: crypto.randomUUID(),
    kind: "preview",
    args: canonicalJson({ action: preview.action, ticket_id: preview.ticket_id }),
    operatorEmail: guarded.email,
    idempotencyKey: preview.idempotency_key,
    previewCommandId: null,
    queuedAt: nowIso,
  });
  return jsonResponse({ command: publicCommand(row), created }, 200);
}
```

`web/app/api/command-previews/[id]/confirm/route.ts`:

```ts
import { createCommand, getCommand } from "../../../../../db/commands";
import { canonicalJson, PREVIEW_TTL_MS } from "../../../../../lib/command-contract";
import { jsonResponse } from "../../../../../lib/json-response";
import { enqueueGuards, guardWrite, publicCommand } from "../../../commands/route";

// Spec §9: expiry enforce MỘT LẦN tại Confirm submission theo đồng hồ Sites.
// Sau khi execute command được tạo, thời gian chờ FIFO không làm expire nữa;
// staleness do runner revalidate fingerprint bắt (TICKET_CHANGED).
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> }
): Promise<Response> {
  const guarded = await guardWrite(request);
  if (guarded instanceof Response) return guarded;
  const idempotencyKey = guarded.body.idempotency_key;
  if (typeof idempotencyKey !== "string" || idempotencyKey.length < 8) {
    return jsonResponse({ error: "INVALID_COMMAND" }, 400);
  }
  const { id } = await params;
  const preview = await getCommand(id);
  if (!preview || preview.kind !== "preview") {
    return jsonResponse({ error: "PREVIEW_NOT_FOUND" }, 404);
  }
  if (preview.operator_email !== guarded.email) {
    return jsonResponse({ error: "PREVIEW_OPERATOR_MISMATCH" }, 403);
  }
  if (preview.status !== "SUCCEEDED" || preview.result === null || preview.finished_at === null) {
    return jsonResponse({ error: "PREVIEW_NOT_READY" }, 409);
  }
  if (Date.now() - Date.parse(preview.finished_at) > PREVIEW_TTL_MS) {
    return jsonResponse({ error: "PREVIEW_EXPIRED" }, 410);
  }
  const previewResult = JSON.parse(preview.result) as {
    fingerprint?: string;
    action?: string;
    ticket_id?: string;
  };
  if (typeof previewResult.fingerprint !== "string" || !previewResult.action || !previewResult.ticket_id) {
    return jsonResponse({ error: "PREVIEW_NOT_READY" }, 409);
  }
  const blocked = await enqueueGuards(guarded.email);
  if (blocked) return blocked;
  const { row, created } = await createCommand({
    id: crypto.randomUUID(),
    kind: "execute",
    args: canonicalJson({
      action: previewResult.action,
      ticket_id: previewResult.ticket_id,
      preview_command_id: preview.id,
      fingerprint: previewResult.fingerprint,
    }),
    operatorEmail: guarded.email,
    idempotencyKey,
    previewCommandId: preview.id,
    queuedAt: new Date().toISOString(),
  });
  return jsonResponse({ command: publicCommand(row), created }, 200);
}
```

- [ ] **Step 3: Chạy test và commit (repo web/)**

Run:

```bash
cd web && npm test && npm run lint
```

Expected: PASS toàn bộ, gồm test cũ.

Commit:

```bash
cd web
git add app/api/command-previews tests/command-api.test.mjs
git commit -m "feat: add execution preview and confirm API"
```

---

### Task 5: [python] Contract models + safe results + fingerprint

**Files:**
- Create: `src/crypto_desk/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `TradeTicket`, `to_jsonable` từ `crypto_desk.domain`.
- Produces (dùng bởi Task 7–9): hằng `COMMAND_KINDS`, `EXECUTION_KINDS = frozenset({"preview", "execute"})`, `STATE_CHANGING_KINDS = frozenset({"sync", "analyze", "daily", "health", "execute"})`, `FORBIDDEN_RESULT_KEYS`, `MAX_ARGS_BYTES = 4096`, `MAX_RESULT_BYTES = 32768`; `canonical_json(value: Any) -> str`; `command_hash(command_id: str, kind: str, args: dict) -> str`; `ticket_fingerprint(ticket: TradeTicket) -> str`; `parse_args(kind: str, args: dict) -> BaseModel` (raise `ValueError`); args models `EmptyArgs`, `AnalyzeArgs(symbol)`, `ReflectionsArgs(symbol|None)`, `PreviewArgs(action, ticket_id)`, `ExecuteArgs(action, ticket_id, preview_command_id, fingerprint)`; safe result models (pydantic v2, `extra="forbid"`): `SafeDoctorResult`, `SafeSyncResult`, `SafeScreenResult`, `SafeAnalyzeResult`, `SafeDailyResult`, `SafeHealthResult`, `SafeTicketsResult`, `SafeOrdersResult`, `SafeReflectionsResult`, `SafePreviewResult`, `SafeExecuteResult`; `ensure_safe_result(model: BaseModel) -> dict` (raise `UnsafeResultError`); `class UnsafeResultError(RuntimeError)`.

- [ ] **Step 1: Viết failing tests**

Tạo `tests/test_commands.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_desk.commands import (
    EXECUTION_KINDS,
    FORBIDDEN_RESULT_KEYS,
    MAX_ARGS_BYTES,
    STATE_CHANGING_KINDS,
    AnalyzeArgs,
    ExecuteArgs,
    SafeExecuteResult,
    SafePreviewResult,
    SafeSyncResult,
    UnsafeResultError,
    canonical_json,
    command_hash,
    ensure_safe_result,
    parse_args,
    ticket_fingerprint,
)
from crypto_desk.domain import TradeTicket, iso

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def make_ticket(**overrides) -> TradeTicket:
    values = dict(
        id="ticket-1",
        environment="testnet",
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000.00"),
        stop_price=Decimal("95000.00"),
        target_price=Decimal("110000.00"),
        notional_usdt=Decimal("25.0000000"),
        risk_snapshot={"limiting_rule": "per_trade"},
        created_at=iso(NOW),
        expires_at=iso(NOW + timedelta(minutes=30)),
    )
    values.update(overrides)
    return TradeTicket(**values)


def test_parse_args_accepts_every_kind_and_rejects_extras():
    assert parse_args("sync", {}).model_dump() == {}
    assert parse_args("analyze", {"symbol": "btcusdt"}).symbol == "BTCUSDT"
    assert parse_args("reflections", {}).symbol is None
    assert parse_args("preview", {"action": "approve", "ticket_id": "t-1"}).action == "approve"
    execute = parse_args(
        "execute",
        {
            "action": "reconcile",
            "ticket_id": "t-1",
            "preview_command_id": "c-1",
            "fingerprint": "a" * 64,
        },
    )
    assert isinstance(execute, ExecuteArgs)
    with pytest.raises(ValueError):
        parse_args("sync", {"extra": 1})
    with pytest.raises(ValueError):
        parse_args("analyze", {"symbol": "BTCUSDT; DROP"})
    with pytest.raises(ValueError):
        parse_args("preview", {"action": "submit", "ticket_id": "t"})
    with pytest.raises(ValueError):
        parse_args("shutdown", {})


def test_args_size_fails_closed():
    with pytest.raises(ValueError, match="4 KiB"):
        parse_args("analyze", {"symbol": "B" * (MAX_ARGS_BYTES + 1)})


def test_canonical_json_and_command_hash_are_stable():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    first = command_hash("cmd-1", "analyze", {"symbol": "BTCUSDT"})
    assert first == command_hash("cmd-1", "analyze", {"symbol": "BTCUSDT"})
    assert first != command_hash("cmd-1", "analyze", {"symbol": "ETHUSDT"})
    assert len(first) == 64


def test_ticket_fingerprint_changes_with_any_covered_field():
    base = ticket_fingerprint(make_ticket())
    assert base == ticket_fingerprint(make_ticket())
    assert base != ticket_fingerprint(make_ticket(status="APPROVED"))
    assert base != ticket_fingerprint(make_ticket(quantity=Decimal("0.00026")))
    assert len(base) == 64


def test_ensure_safe_result_blocks_forbidden_keys_exactly():
    good = SafeSyncResult(
        environment="testnet",
        nav_usdt="1000",
        free_usdt="500",
        positions_count=2,
        open_orders_count=1,
        as_of=iso(NOW),
    )
    dumped = ensure_safe_result(good)
    assert dumped["nav_usdt"] == "1000"

    class Sneaky(SafeSyncResult):
        model_config = {"extra": "allow"}

    bad = Sneaky(
        environment="testnet", nav_usdt="1", free_usdt="1",
        positions_count=0, open_orders_count=0, as_of=iso(NOW),
    )
    object.__setattr__ = object.__setattr__  # giữ ruff yên lặng về unused
    bad.__pydantic_extra__["client_order_id"] = "leak"
    with pytest.raises(UnsafeResultError, match="client_order_id"):
        ensure_safe_result(bad)


def test_every_safe_result_model_declares_no_forbidden_field():
    from crypto_desk import commands

    for name in dir(commands):
        model = getattr(commands, name)
        if isinstance(model, type) and name.startswith("Safe") and hasattr(model, "model_fields"):
            for field in model.model_fields:
                assert field.lower() not in FORBIDDEN_RESULT_KEYS, f"{name}.{field}"


def test_preview_and_execute_results_round_trip():
    preview = SafePreviewResult(
        action="approve", ticket_id="t-1", environment="testnet",
        execution_mode="DRY_RUN", status="PENDING", symbol="BTCUSDT",
        side="BUY", intent="OPEN", quantity="0.00025", notional_usdt="25",
        entry="100000.00", stop="95000.00", target="110000.00",
        created_at=iso(NOW), expires_at=iso(NOW + timedelta(minutes=30)),
        submission_status=None, fingerprint="a" * 64,
    )
    assert ensure_safe_result(preview)["fingerprint"] == "a" * 64
    execute = SafeExecuteResult(action="approve", ticket_id="t-1", status="APPROVED_DRY_RUN")
    assert ensure_safe_result(execute)["status"] == "APPROVED_DRY_RUN"
    assert "execute" in EXECUTION_KINDS and "execute" in STATE_CHANGING_KINDS


def test_result_size_fails_closed():
    result = SafeExecuteResult(action="approve", ticket_id="x" * 40000, status="S")
    with pytest.raises(UnsafeResultError, match="32 KiB"):
        ensure_safe_result(result)
```

Run: `uv run pytest tests/test_commands.py -v`

Expected: FAIL — `ModuleNotFoundError: crypto_desk.commands`.

- [ ] **Step 2: Implement `src/crypto_desk/commands.py`**

```python
"""Command contract v1 — mirror của web/lib/command-contract.ts.

Nguồn chuẩn: docs/superpowers/plans/2026-07-18-interactive-cli-dashboard.md.
Mọi model dùng extra="forbid"; safe result đi qua ensure_safe_result trước khi
rời tiến trình (fail closed theo spec §6.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .domain import TradeTicket, to_jsonable

COMMAND_KINDS = (
    "doctor", "sync", "screen", "analyze", "daily", "health",
    "tickets", "orders", "reflections", "preview", "execute",
)
EXECUTION_KINDS = frozenset({"preview", "execute"})
STATE_CHANGING_KINDS = frozenset({"sync", "analyze", "daily", "health", "execute"})

MAX_ARGS_BYTES = 4096
MAX_RESULT_BYTES = 32768

FORBIDDEN_RESULT_KEYS = frozenset({
    "api_key", "api_secret", "token", "telegram", "actor", "confirmation",
    "report_dir", "client_order_id", "payload", "raw", "secret",
})

_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z]{2,10}USDT$")

ExecutionAction = Literal["approve", "reject", "reconcile"]
ExecutionMode = Literal["TESTNET_ORDER", "DRY_RUN"]


class UnsafeResultError(RuntimeError):
    """Safe result vi phạm forbidden keys hoặc giới hạn kích thước."""


def canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def command_hash(command_id: str, kind: str, args: dict[str, Any]) -> str:
    material = canonical_json({"args": args, "id": command_id, "kind": kind})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_FINGERPRINT_FIELDS = (
    "environment", "expires_at", "id", "intent", "limit_price", "notional_usdt",
    "quantity", "side", "status", "stop_price", "symbol", "target_price",
)


def ticket_fingerprint(ticket: TradeTicket) -> str:
    material = canonical_json({name: getattr(ticket, name) for name in _FINGERPRINT_FIELDS})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyArgs(_Args):
    pass


def _check_symbol(value: str) -> str:
    symbol = value.upper()
    if not _SYMBOL_PATTERN.match(symbol):
        raise ValueError("symbol is outside the configured allowlist")
    return symbol


class AnalyzeArgs(_Args):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return _check_symbol(value)


class ReflectionsArgs(_Args):
    symbol: str | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str | None) -> str | None:
        return None if value is None else _check_symbol(value)


def _check_ticket_id(value: str) -> str:
    if not _TICKET_ID_PATTERN.match(value):
        raise ValueError("invalid ticket id")
    return value


class PreviewArgs(_Args):
    action: ExecutionAction
    ticket_id: str

    @field_validator("ticket_id")
    @classmethod
    def _ticket(cls, value: str) -> str:
        return _check_ticket_id(value)


class ExecuteArgs(_Args):
    action: ExecutionAction
    ticket_id: str
    preview_command_id: str
    fingerprint: str

    @field_validator("ticket_id")
    @classmethod
    def _ticket(cls, value: str) -> str:
        return _check_ticket_id(value)

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint(cls, value: str) -> str:
        if not re.match(r"^[0-9a-f]{64}$", value):
            raise ValueError("invalid fingerprint")
        return value


_ARGS_MODELS: dict[str, type[_Args]] = {
    "doctor": EmptyArgs, "sync": EmptyArgs, "screen": EmptyArgs,
    "daily": EmptyArgs, "health": EmptyArgs, "tickets": EmptyArgs,
    "orders": EmptyArgs, "analyze": AnalyzeArgs, "reflections": ReflectionsArgs,
    "preview": PreviewArgs, "execute": ExecuteArgs,
}


def parse_args(kind: str, args: dict[str, Any]) -> _Args:
    model = _ARGS_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unknown command kind: {kind}")
    if len(canonical_json(args).encode("utf-8")) > MAX_ARGS_BYTES:
        raise ValueError("arguments exceed the 4 KiB limit")
    try:
        return model(**args)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from None


class _SafeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SafeDoctorResult(_SafeResult):
    python: str
    package_versions: dict[str, str | None]
    provider: str
    binance_environment: str
    binance_keys_present: bool
    testnet_execution_enabled: bool
    execution_mode: ExecutionMode
    database_schema_version: int
    schedule: dict[str, str]


class SafeSyncResult(_SafeResult):
    environment: str
    nav_usdt: str
    free_usdt: str
    positions_count: int
    open_orders_count: int
    as_of: str


class SafeScreenItem(_SafeResult):
    symbol: str
    passes: bool
    score: str
    reasons: list[str]


class SafeScreenResult(_SafeResult):
    items: list[SafeScreenItem]


class SafeAnalyzeResult(_SafeResult):
    run_id: str
    symbol: str
    cutoff: str
    action: str
    conviction: str
    reason: str
    entry: str | None
    stop: str | None
    target: str | None
    current_price: str | None
    ticket_id: str | None


class SafeDailyResult(_SafeResult):
    status: str
    bucket: str
    run_ids: list[str]
    screen: list[SafeScreenItem]


class SafeHealthResult(_SafeResult):
    status: str
    bucket: str
    alerts: list[str]
    run_ids: list[str]
    reconciled_count: int


class SafeTicketRow(_SafeResult):
    id: str
    environment: str
    symbol: str
    status: str
    created_at: str
    expires_at: str


class SafeTicketsResult(_SafeResult):
    tickets: list[SafeTicketRow]


class SafeOrderRow(_SafeResult):
    ticket_id: str
    environment: str
    status: str
    updated_at: str


class SafeOrdersResult(_SafeResult):
    orders: list[SafeOrderRow]


class SafeReflectionRow(_SafeResult):
    run_id: str
    symbol: str
    created_at: str
    realized_return: str | None
    alpha: str | None


class SafeReflectionsResult(_SafeResult):
    reflections: list[SafeReflectionRow]


class SafePreviewResult(_SafeResult):
    action: ExecutionAction
    ticket_id: str
    environment: str
    execution_mode: ExecutionMode
    status: str
    symbol: str
    side: str
    intent: str
    quantity: str
    notional_usdt: str
    entry: str
    stop: str
    target: str
    created_at: str
    expires_at: str
    submission_status: str | None
    fingerprint: str


class SafeExecuteResult(_SafeResult):
    action: ExecutionAction
    ticket_id: str
    status: str


def ensure_safe_result(model: BaseModel) -> dict[str, Any]:
    dumped = model.model_dump(mode="json")
    _walk_forbidden(dumped)
    if len(canonical_json(dumped).encode("utf-8")) > MAX_RESULT_BYTES:
        raise UnsafeResultError("safe result exceeds the 32 KiB limit")
    return dumped


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in FORBIDDEN_RESULT_KEYS:
                raise UnsafeResultError(f"forbidden result key: {key}")
            _walk_forbidden(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_forbidden(item)
```

Lưu ý cho test `test_ensure_safe_result_blocks_forbidden_keys_exactly`: pydantic v2 với `extra="allow"` giữ extra field trong `__pydantic_extra__` và `model_dump` xuất chúng — vì vậy `ensure_safe_result` bắt được key cấm dù model tĩnh không khai báo.

- [ ] **Step 3: Chạy test và commit (repo gốc)**

Run:

```bash
uv run pytest tests/test_commands.py -v && uv run ruff check src/crypto_desk/commands.py tests/test_commands.py
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/commands.py tests/test_commands.py
git commit -m "feat: add web command contract models and safe results"
```

---

### Task 6: [python] Store schema v3 — command journal

**Files:**
- Modify: `src/crypto_desk/store.py`
- Test: Modify `tests/test_domain_store.py`

**Interfaces:**
- Consumes: schema v2 hiện có + pattern migration v1→v2 (store.py:99–114).
- Produces (dùng bởi Task 9): `Store.journal_start(command_id: str, command_hash: str, kind: str) -> None` (trùng id → `ValueError`); `journal_entry(command_id: str) -> dict | None` (keys: `command_id, command_hash, kind, state, reported, result, error_code, created_at, updated_at`; `result` đã parse JSON hoặc None; `reported` là bool); `journal_finish(command_id: str, *, state: str, result: dict | None = None, error_code: str | None = None) -> None`; `journal_mark_reported(command_id: str) -> None`; `journal_running() -> list[dict]`; `journal_unreported() -> list[dict]` (terminal + chưa reported); `schema_version()` trả 3.

- [ ] **Step 1: Viết failing tests (nối vào `tests/test_domain_store.py`)**

```python
def test_store_uses_schema_version_three(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.schema_version() == 3


def test_journal_lifecycle_first_run_finish_report(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")

    assert store.journal_entry("cmd-1") is None
    store.journal_start("cmd-1", "hash-1", "sync")
    entry = store.journal_entry("cmd-1")
    assert entry["state"] == "RUNNING"
    assert entry["reported"] is False

    store.journal_finish("cmd-1", state="SUCCEEDED", result={"nav_usdt": "1"})
    assert store.journal_running() == []
    unreported = store.journal_unreported()
    assert [item["command_id"] for item in unreported] == ["cmd-1"]
    assert unreported[0]["result"] == {"nav_usdt": "1"}

    store.journal_mark_reported("cmd-1")
    assert store.journal_unreported() == []


def test_journal_duplicate_start_raises(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.journal_start("cmd-1", "hash-1", "sync")

    with pytest.raises(ValueError, match="cmd-1"):
        store.journal_start("cmd-1", "hash-2", "sync")


def test_journal_failed_state_records_error_code(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.journal_start("cmd-2", "hash-2", "execute")
    store.journal_finish("cmd-2", state="NEEDS_REVIEW", error_code="EXECUTION_UNCERTAIN")

    entry = store.journal_entry("cmd-2")
    assert entry["state"] == "NEEDS_REVIEW"
    assert entry["error_code"] == "EXECUTION_UNCERTAIN"
    assert entry["result"] is None


def test_existing_v2_database_migrates_to_v3(tmp_path: Path):
    path = tmp_path / "crypto.db"
    first = Store(path)
    first.close()
    # giả lập DB v2: xóa bảng journal + hạ version
    second = sqlite3.connect(path)
    second.execute("DROP TABLE command_journal")
    second.execute("UPDATE schema_meta SET version = 2")
    second.commit()
    second.close()

    migrated = Store(path)
    assert migrated.schema_version() == 3
    migrated.journal_start("cmd-1", "hash-1", "sync")
```

Thêm `import sqlite3` vào đầu file test nếu chưa có.

Run: `uv run pytest tests/test_domain_store.py -v`

Expected: FAIL — schema vẫn là 2, các method `journal_*` chưa tồn tại.

- [ ] **Step 2: Implement schema v3 + journal methods**

Trong `src/crypto_desk/store.py`:

1. Thêm bảng vào `executescript` khởi tạo (cạnh các bảng hiện có) và đổi seed version thành 3:

```sql
CREATE TABLE IF NOT EXISTS command_journal (
    command_id TEXT PRIMARY KEY,
    command_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

2. Thêm migration v2→v3 theo đúng pattern v1→v2 hiện có (kiểm tra `schema_version() == 2` → `CREATE TABLE command_journal ...` + `UPDATE schema_meta SET version = 3`).

3. Thêm methods (cuối class, dùng `iso()` và `_json` sẵn có):

```python
def journal_start(self, command_id: str, command_hash: str, kind: str) -> None:
    try:
        self.db.execute(
            "INSERT INTO command_journal("
            "command_id, command_hash, kind, state, reported, created_at, updated_at"
            ") VALUES (?, ?, ?, 'RUNNING', 0, ?, ?)",
            (command_id, command_hash, kind, iso(), iso()),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"Command {command_id} is already journaled") from None
    self.db.commit()

def journal_entry(self, command_id: str) -> dict[str, Any] | None:
    row = self.db.execute(
        "SELECT command_id, command_hash, kind, state, reported, result,"
        " error_code, created_at, updated_at"
        " FROM command_journal WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    return self._journal_row(row) if row else None

def journal_finish(
    self,
    command_id: str,
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    cursor = self.db.execute(
        "UPDATE command_journal SET state = ?, result = ?, error_code = ?, updated_at = ?"
        " WHERE command_id = ?",
        (state, _json(result) if result is not None else None, error_code, iso(), command_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Command {command_id} has no journal entry")
    self.db.commit()

def journal_mark_reported(self, command_id: str) -> None:
    cursor = self.db.execute(
        "UPDATE command_journal SET reported = 1, updated_at = ? WHERE command_id = ?",
        (iso(), command_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Command {command_id} has no journal entry")
    self.db.commit()

def journal_running(self) -> list[dict[str, Any]]:
    rows = self.db.execute(
        "SELECT command_id, command_hash, kind, state, reported, result,"
        " error_code, created_at, updated_at"
        " FROM command_journal WHERE state = 'RUNNING' ORDER BY created_at",
    ).fetchall()
    return [self._journal_row(row) for row in rows]

def journal_unreported(self) -> list[dict[str, Any]]:
    rows = self.db.execute(
        "SELECT command_id, command_hash, kind, state, reported, result,"
        " error_code, created_at, updated_at"
        " FROM command_journal WHERE state != 'RUNNING' AND reported = 0"
        " ORDER BY created_at",
    ).fetchall()
    return [self._journal_row(row) for row in rows]

def _journal_row(self, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "command_id": row["command_id"],
        "command_hash": row["command_hash"],
        "kind": row["kind"],
        "state": row["state"],
        "reported": bool(row["reported"]),
        "result": json.loads(row["result"]) if row["result"] else None,
        "error_code": row["error_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
```

- [ ] **Step 3: Chạy toàn bộ suite và commit (repo gốc)**

Run:

```bash
uv run pytest -q && uv run ruff check .
```

Expected: PASS — các test store cũ vẫn xanh (bảng mới không đụng bảng cũ; test nào assert version 2 phải được cập nhật thành 3 nếu tồn tại — kiểm tra `tests/test_domain_store.py::test_store_uses_schema_version_two` và đổi tên/expected thành 3).

Commit:

```bash
git add src/crypto_desk/store.py tests/test_domain_store.py
git commit -m "feat: add command journal table with schema v3"
```

---

### Task 7: [python] CommandDispatcher — các lệnh research/operations

**Files:**
- Create: `src/crypto_desk/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: Task 5 (`parse_args`, safe result models, `ensure_safe_result`), `Store` (list_tickets/list_submissions/list_reflections/schema_version), `CryptoDeskService` (sync/screen/analyze/daily/health), wiring mặc định lazy-import từ `crypto_desk.cli` (`_service`, `_execution_service`) — KHÔNG di chuyển helper nào khỏi cli.py (giữ nguyên các monkeypatch path của test hiện có).
- Produces (dùng bởi Task 8–9): `class DispatchError(RuntimeError)` với thuộc tính `code: str`; `execution_mode() -> Literal["TESTNET_ORDER","DRY_RUN"]`; `class CommandDispatcher` với `__init__(self, settings: Settings, *, service_factory=None, execution_factory=None, store_factory=None, now: Callable[[], datetime] = utcnow)` và `dispatch(self, kind: str, args: dict, *, operator_email: str, environment: str = "testnet") -> BaseModel`.

- [ ] **Step 1: Viết failing tests**

Tạo `tests/test_dispatcher.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.commands import (
    SafeDailyResult,
    SafeHealthResult,
    SafeOrdersResult,
    SafeReflectionsResult,
    SafeScreenResult,
    SafeSyncResult,
    SafeTicketsResult,
)
from crypto_desk.config import Settings
from crypto_desk.dispatcher import CommandDispatcher, DispatchError, execution_mode
from crypto_desk.domain import PortfolioSnapshot, iso
from crypto_desk.store import Store

NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
OPERATOR = "quoc.lb@teko.vn"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )


def make_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        environment="testnet",
        nav_usdt=Decimal("1000"),
        free_usdt=Decimal("400"),
        positions=({"asset": "BTC", "free": "0.01"},),
        open_orders=(),
        as_of=iso(NOW),
    )


@dataclass
class FakeScreenItem:
    symbol: str = "BTCUSDT"
    passes: bool = True
    score: Decimal = Decimal("2")
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ("ev-1",)


class FakeService:
    def __init__(self):
        self.calls: list[str] = []

    def sync(self):
        self.calls.append("sync")
        return make_snapshot()

    def screen(self):
        self.calls.append("screen")
        return [FakeScreenItem()]

    def daily(self, *, due: bool = False, catch_up: bool = False):
        self.calls.append(f"daily:{due}:{catch_up}")
        return {
            "status": "COMPLETED",
            "bucket": "2026-07-17",
            "run_ids": ["run-1"],
            "screen": [
                {"symbol": "BTCUSDT", "passes": True, "score": "2", "reasons": []}
            ],
        }

    def health(self, *, due: bool = False):
        self.calls.append("health")
        return {
            "status": "COMPLETED",
            "bucket": "2026-07-18T09:00Z",
            "alerts": ["quote:BTCUSDT:BrokerError"],
            "run_ids": [],
            "reconciled": [{"ticket_id": "t-1", "status": "FILLED"}],
            "snapshot": None,
        }


def make_dispatcher(tmp_path: Path, *, service: FakeService | None = None) -> tuple[CommandDispatcher, Store]:
    settings = make_settings(tmp_path)
    store = Store(settings.database)
    selected = service or FakeService()
    dispatcher = CommandDispatcher(
        settings,
        service_factory=lambda **kwargs: selected,
        execution_factory=lambda: pytest.fail("execution factory must not be built here"),
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    return dispatcher, store


def test_sync_maps_to_safe_result(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    result = dispatcher.dispatch("sync", {}, operator_email=OPERATOR)
    assert isinstance(result, SafeSyncResult)
    assert result.nav_usdt == "1000"
    assert result.positions_count == 1


def test_screen_and_daily_and_health_map_screen_items_and_counts(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    screen = dispatcher.dispatch("screen", {}, operator_email=OPERATOR)
    assert isinstance(screen, SafeScreenResult)
    assert screen.items[0].score == "2"

    daily = dispatcher.dispatch("daily", {}, operator_email=OPERATOR)
    assert isinstance(daily, SafeDailyResult)
    assert daily.run_ids == ["run-1"]

    health = dispatcher.dispatch("health", {}, operator_email=OPERATOR)
    assert isinstance(health, SafeHealthResult)
    assert health.reconciled_count == 1


def test_analyze_enforces_the_allowlist_like_the_cli(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("analyze", {"symbol": "ETHUSDT"}, operator_email=OPERATOR)
    assert excinfo.value.code == "VALIDATION_FAILED"


def test_tickets_orders_reflections_read_from_the_store(tmp_path: Path):
    dispatcher, store = make_dispatcher(tmp_path)
    store.save_submission("t-1", "testnet", "cdt-abc", {"status": "SUBMITTED"})

    tickets = dispatcher.dispatch("tickets", {}, operator_email=OPERATOR)
    assert isinstance(tickets, SafeTicketsResult)

    orders = dispatcher.dispatch("orders", {}, operator_email=OPERATOR)
    assert isinstance(orders, SafeOrdersResult)
    assert orders.orders[0].ticket_id == "t-1"
    dumped = orders.model_dump()
    assert "client_order_id" not in str(dumped)

    reflections = dispatcher.dispatch("reflections", {}, operator_email=OPERATOR)
    assert isinstance(reflections, SafeReflectionsResult)
    assert reflections.reflections == []


def test_doctor_is_offline_and_curated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    dispatcher, _ = make_dispatcher(tmp_path)
    result = dispatcher.dispatch("doctor", {}, operator_email=OPERATOR)
    assert result.execution_mode == "DRY_RUN"
    assert result.binance_keys_present is True
    assert result.database_schema_version == 3
    dumped = result.model_dump()
    assert "k" not in str(dumped) and "s" not in str(dumped)


def test_non_testnet_command_envelope_is_forbidden(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("sync", {}, operator_email=OPERATOR, environment="mainnet")
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"


def test_unknown_kind_or_bad_args_raise_validation_error(tmp_path: Path):
    dispatcher, _ = make_dispatcher(tmp_path)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("shutdown", {}, operator_email=OPERATOR)
    assert excinfo.value.code == "INVALID_COMMAND"
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch("sync", {"extra": 1}, operator_email=OPERATOR)
    assert excinfo.value.code == "INVALID_COMMAND"


def test_execution_mode_follows_the_env_flag(monkeypatch):
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "1")
    assert execution_mode() == "TESTNET_ORDER"
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    assert execution_mode() == "DRY_RUN"
```

Run: `uv run pytest tests/test_dispatcher.py -v`

Expected: FAIL — `ModuleNotFoundError: crypto_desk.dispatcher`.

- [ ] **Step 2: Implement `src/crypto_desk/dispatcher.py`**

```python
"""CommandDispatcher: map typed command → service call, trả safe result model.

Không bao giờ gọi Typer/subprocess/shell (spec §5.2). Wiring mặc định
lazy-import các helper của cli để CLI và web runner dùng chung một đường
dựng service (spec: dispatcher-level parity); test inject factory riêng.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .commands import (
    AnalyzeArgs,
    ReflectionsArgs,
    SafeAnalyzeResult,
    SafeDailyResult,
    SafeDoctorResult,
    SafeHealthResult,
    SafeOrderRow,
    SafeOrdersResult,
    SafeReflectionRow,
    SafeReflectionsResult,
    SafeScreenItem,
    SafeScreenResult,
    SafeSyncResult,
    SafeTicketRow,
    SafeTicketsResult,
    parse_args,
)
from .config import Settings
from .domain import utcnow
from .store import Store

_DOCTOR_PACKAGES = ("crypto-desk", "binance-sdk-spot", "google-genai", "openai", "httpx", "pydantic")


class DispatchError(RuntimeError):
    """Lỗi dispatch với safe code ổn định (không mang chi tiết nhạy cảm)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def execution_mode() -> str:
    return "TESTNET_ORDER" if os.getenv("TESTNET_EXECUTION_ENABLED") == "1" else "DRY_RUN"


class CommandDispatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        service_factory: Callable[..., Any] | None = None,
        execution_factory: Callable[[], Any] | None = None,
        store_factory: Callable[[], Store] | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self._service_factory = service_factory
        self._execution_factory = execution_factory
        self._store_factory = store_factory
        self.now = now

    # -- wiring -----------------------------------------------------------

    def _service(self, **kwargs: Any) -> Any:
        if self._service_factory is not None:
            return self._service_factory(**kwargs)
        from .cli import _service  # lazy: tránh vòng import cli ↔ dispatcher

        return _service(self.settings, **kwargs)

    def _execution(self) -> Any:
        if self._execution_factory is not None:
            return self._execution_factory()
        from .cli import _execution_service

        return _execution_service(self.settings)

    def _store(self) -> Store:
        if self._store_factory is not None:
            return self._store_factory()
        return Store(self.settings.database)

    # -- entry point ------------------------------------------------------

    def dispatch(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        operator_email: str,
        environment: str = "testnet",
    ) -> BaseModel:
        if environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        try:
            parsed = parse_args(kind, args)
        except ValueError as exc:
            raise DispatchError("INVALID_COMMAND", str(exc)) from None
        handler = getattr(self, f"_dispatch_{kind}")
        return handler(parsed, operator_email)

    # -- research / operations --------------------------------------------

    def _dispatch_doctor(self, args: Any, operator_email: str) -> SafeDoctorResult:
        prefix = self.settings.binance.environment.upper()
        versions: dict[str, str | None] = {}
        for package in _DOCTOR_PACKAGES:
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        store = self._store()
        return SafeDoctorResult(
            python=platform.python_version(),
            package_versions=versions,
            provider=self.settings.models.provider,
            binance_environment=self.settings.binance.environment,
            binance_keys_present=bool(
                os.getenv(f"BINANCE_{prefix}_API_KEY") and os.getenv(f"BINANCE_{prefix}_API_SECRET")
            ),
            testnet_execution_enabled=os.getenv("TESTNET_EXECUTION_ENABLED") == "1",
            execution_mode=execution_mode(),
            database_schema_version=store.schema_version(),
            schedule={
                "daily_utc": self.settings.schedule.daily_utc,
                "health_minutes": str(self.settings.schedule.health_minutes),
            },
        )

    def _dispatch_sync(self, args: Any, operator_email: str) -> SafeSyncResult:
        snapshot = self._service(broker=True, committee=False).sync()
        return SafeSyncResult(
            environment=snapshot.environment,
            nav_usdt=str(snapshot.nav_usdt),
            free_usdt=str(snapshot.free_usdt),
            positions_count=len(snapshot.positions),
            open_orders_count=len(snapshot.open_orders),
            as_of=snapshot.as_of,
        )

    @staticmethod
    def _screen_item(item: Any) -> SafeScreenItem:
        if isinstance(item, dict):
            return SafeScreenItem(
                symbol=item["symbol"],
                passes=bool(item["passes"]),
                score=str(item["score"]),
                reasons=[str(reason) for reason in item.get("reasons", [])],
            )
        return SafeScreenItem(
            symbol=item.symbol,
            passes=item.passes,
            score=str(item.score),
            reasons=[str(reason) for reason in item.reasons],
        )

    def _dispatch_screen(self, args: Any, operator_email: str) -> SafeScreenResult:
        results = self._service(committee=False).screen()
        return SafeScreenResult(items=[self._screen_item(item) for item in results])

    def _dispatch_analyze(self, args: AnalyzeArgs, operator_email: str) -> SafeAnalyzeResult:
        if args.symbol not in self.settings.symbols:
            raise DispatchError("VALIDATION_FAILED", "Symbol is outside the configured allowlist")
        run = self._service().analyze(args.symbol)
        decision = run.decision
        return SafeAnalyzeResult(
            run_id=run.run_id,
            symbol=decision.symbol,
            cutoff=run.cutoff,
            action=decision.action,
            conviction=str(decision.conviction),
            reason=decision.reason,
            entry=None if decision.entry is None else str(decision.entry),
            stop=None if decision.stop is None else str(decision.stop),
            target=None if decision.target is None else str(decision.target),
            current_price=None if run.current_price is None else str(run.current_price),
            ticket_id=run.ticket_id,
        )

    def _dispatch_daily(self, args: Any, operator_email: str) -> SafeDailyResult:
        result = self._service().daily(due=False, catch_up=False)
        return SafeDailyResult(
            status=result["status"],
            bucket=str(result["bucket"]),
            run_ids=[str(run_id) for run_id in result.get("run_ids", [])],
            screen=[self._screen_item(item) for item in result.get("screen", [])],
        )

    def _dispatch_health(self, args: Any, operator_email: str) -> SafeHealthResult:
        result = self._service(broker=True, execution=True).health(due=False)
        return SafeHealthResult(
            status=result["status"],
            bucket=str(result["bucket"]),
            alerts=[str(alert) for alert in result.get("alerts", [])],
            run_ids=[str(run_id) for run_id in result.get("run_ids", [])],
            reconciled_count=len(result.get("reconciled", [])),
        )

    def _dispatch_tickets(self, args: Any, operator_email: str) -> SafeTicketsResult:
        rows = self._store().list_tickets()
        return SafeTicketsResult(
            tickets=[
                SafeTicketRow(
                    id=row["id"],
                    environment=row["environment"],
                    symbol=row["symbol"],
                    status=row["status"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                )
                for row in rows
            ]
        )

    def _dispatch_orders(self, args: Any, operator_email: str) -> SafeOrdersResult:
        rows = self._store().list_submissions()
        return SafeOrdersResult(
            orders=[
                SafeOrderRow(
                    ticket_id=row["ticket_id"],
                    environment=row["environment"],
                    status=row["status"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]
        )

    def _dispatch_reflections(self, args: ReflectionsArgs, operator_email: str) -> SafeReflectionsResult:
        rows = self._store().list_reflections(args.symbol)
        reflections = []
        for row in rows:
            payload = row.get("payload") or {}
            reflections.append(
                SafeReflectionRow(
                    run_id=row["run_id"],
                    symbol=row["symbol"],
                    created_at=row["created_at"],
                    realized_return=_optional_str(payload.get("realized_return")),
                    alpha=_optional_str(payload.get("alpha")),
                )
            )
        return SafeReflectionsResult(reflections=reflections)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
```

(`_dispatch_preview` / `_dispatch_execute` được thêm ở Task 8.)

- [ ] **Step 3: Chạy test và commit (repo gốc)**

Run:

```bash
uv run pytest tests/test_dispatcher.py tests/test_service_cli.py -q && uv run ruff check .
```

Expected: PASS (test_service_cli không đổi — cli.py chưa bị sửa).

Commit:

```bash
git add src/crypto_desk/dispatcher.py tests/test_dispatcher.py
git commit -m "feat: add command dispatcher for research and operations commands"
```

---

### Task 8: [python] Dispatcher — preview/execute + mainnet hard-lock

**Files:**
- Modify: `src/crypto_desk/dispatcher.py`
- Test: Modify `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: Task 5 (`PreviewArgs`, `ExecuteArgs`, `SafePreviewResult`, `SafeExecuteResult`, `ticket_fingerprint`), `ExecutionService.approve/reject/reconcile` (signature thật: `approve(ticket_id, *, actor, channel, code=None, telegram_proof=None)`), `Store.ticket/submission`.
- Produces: `_dispatch_preview`, `_dispatch_execute`; safe codes mới dùng: `TICKET_NOT_FOUND`, `TICKET_CHANGED`, `ENVIRONMENT_FORBIDDEN`, `VALIDATION_FAILED`. Execute gọi `ExecutionService` với `actor=operator_email, channel="local"`.

- [ ] **Step 1: Viết failing tests (nối vào `tests/test_dispatcher.py`)**

```python
from crypto_desk.commands import SafeExecuteResult, SafePreviewResult, ticket_fingerprint
from crypto_desk.domain import TradeTicket
from crypto_desk.execution import ExecutionResult

from datetime import timedelta


def make_ticket(environment: str = "testnet") -> TradeTicket:
    return TradeTicket(
        id="ticket-1",
        environment=environment,
        symbol="BTCUSDT",
        intent="OPEN",
        side="BUY",
        quantity=Decimal("0.00025"),
        limit_price=Decimal("100000.00"),
        stop_price=Decimal("95000.00"),
        target_price=Decimal("110000.00"),
        notional_usdt=Decimal("25.0000000"),
        risk_snapshot={"limiting_rule": "per_trade"},
        created_at=iso(NOW),
        expires_at=iso(NOW + timedelta(minutes=30)),
    )


class FakeExecution:
    def __init__(self):
        self.approve_kwargs = None
        self.reject_kwargs = None
        self.reconcile_ids: list[str] = []

    def approve(self, ticket_id: str, **kwargs):
        self.approve_kwargs = kwargs
        return ExecutionResult(ticket_id, "APPROVED_DRY_RUN", {"status": "APPROVED_DRY_RUN"})

    def reject(self, ticket_id: str, **kwargs):
        self.reject_kwargs = kwargs
        return ExecutionResult(ticket_id, "REJECTED", {"status": "REJECTED"})

    def reconcile(self, ticket_id: str):
        self.reconcile_ids.append(ticket_id)
        return ExecutionResult(ticket_id, "FILLED", {"status": "FILLED"})


def make_execution_dispatcher(
    tmp_path: Path, monkeypatch, *, environment: str = "testnet"
) -> tuple[CommandDispatcher, Store, FakeExecution]:
    monkeypatch.setenv("BINANCE_ENV", environment)
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    settings = Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )
    store = Store(settings.database)
    store.save_ticket(make_ticket())
    execution = FakeExecution()
    dispatcher = CommandDispatcher(
        settings,
        service_factory=lambda **kwargs: pytest.fail("service must not be built"),
        execution_factory=lambda: execution,
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    return dispatcher, store, execution


def test_preview_returns_sanitized_ticket_with_fingerprint(tmp_path: Path, monkeypatch):
    dispatcher, store, _ = make_execution_dispatcher(tmp_path, monkeypatch)
    result = dispatcher.dispatch(
        "preview", {"action": "approve", "ticket_id": "ticket-1"}, operator_email=OPERATOR
    )
    assert isinstance(result, SafePreviewResult)
    assert result.execution_mode == "DRY_RUN"
    assert result.fingerprint == ticket_fingerprint(store.ticket("ticket-1"))
    assert result.entry == "100000.00"
    assert result.submission_status is None


def test_preview_unknown_ticket_fails_with_safe_code(tmp_path: Path, monkeypatch):
    dispatcher, _, _ = make_execution_dispatcher(tmp_path, monkeypatch)
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "preview", {"action": "approve", "ticket_id": "missing"}, operator_email=OPERATOR
        )
    assert excinfo.value.code == "TICKET_NOT_FOUND"


def test_execute_revalidates_fingerprint_before_calling_execution(tmp_path: Path, monkeypatch):
    dispatcher, store, execution = make_execution_dispatcher(tmp_path, monkeypatch)
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))

    result = dispatcher.dispatch(
        "execute",
        {
            "action": "approve",
            "ticket_id": "ticket-1",
            "preview_command_id": "cmd-preview",
            "fingerprint": fingerprint,
        },
        operator_email=OPERATOR,
    )
    assert isinstance(result, SafeExecuteResult)
    assert result.status == "APPROVED_DRY_RUN"
    assert execution.approve_kwargs == {"actor": OPERATOR, "channel": "local"}


def test_execute_with_stale_fingerprint_fails_closed_as_ticket_changed(tmp_path: Path, monkeypatch):
    dispatcher, store, execution = make_execution_dispatcher(tmp_path, monkeypatch)
    stale = ticket_fingerprint(store.ticket("ticket-1"))
    store.record_approval("ticket-1", actor="owner", channel="local", status="APPROVED")

    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": stale,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "TICKET_CHANGED"
    assert execution.approve_kwargs is None


@pytest.mark.parametrize("action", ["reject", "reconcile"])
def test_execute_maps_reject_and_reconcile(action, tmp_path: Path, monkeypatch):
    dispatcher, store, execution = make_execution_dispatcher(tmp_path, monkeypatch)
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))
    result = dispatcher.dispatch(
        "execute",
        {
            "action": action,
            "ticket_id": "ticket-1",
            "preview_command_id": "cmd-preview",
            "fingerprint": fingerprint,
        },
        operator_email=OPERATOR,
    )
    assert isinstance(result, SafeExecuteResult)
    if action == "reject":
        assert execution.reject_kwargs == {"actor": OPERATOR, "channel": "local"}
    else:
        assert execution.reconcile_ids == ["ticket-1"]


def test_mainnet_is_hard_locked_at_every_layer(tmp_path: Path, monkeypatch):
    # Lớp 1: BINANCE_ENV khác testnet
    dispatcher, store, execution = make_execution_dispatcher(tmp_path, monkeypatch, environment="mainnet")
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": fingerprint,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"
    assert execution.approve_kwargs is None


def test_mainnet_ticket_is_forbidden_even_on_testnet_gate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("TESTNET_EXECUTION_ENABLED", "0")
    settings = Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )
    store = Store(settings.database)
    store.save_ticket(make_ticket(environment="mainnet"))
    dispatcher = CommandDispatcher(
        settings,
        execution_factory=lambda: pytest.fail("must not execute"),
        store_factory=lambda: store,
        now=lambda: NOW,
    )
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "preview", {"action": "approve", "ticket_id": "ticket-1"}, operator_email=OPERATOR
        )
    assert excinfo.value.code == "ENVIRONMENT_FORBIDDEN"


def test_execution_validation_error_becomes_safe_code(tmp_path: Path, monkeypatch):
    dispatcher, store, execution = make_execution_dispatcher(tmp_path, monkeypatch)
    fingerprint = ticket_fingerprint(store.ticket("ticket-1"))

    def raising_approve(ticket_id: str, **kwargs):
        raise ValueError("Ticket ticket-1 is expired")

    execution.approve = raising_approve
    with pytest.raises(DispatchError) as excinfo:
        dispatcher.dispatch(
            "execute",
            {
                "action": "approve",
                "ticket_id": "ticket-1",
                "preview_command_id": "cmd-preview",
                "fingerprint": fingerprint,
            },
            operator_email=OPERATOR,
        )
    assert excinfo.value.code == "VALIDATION_FAILED"
```

Run: `uv run pytest tests/test_dispatcher.py -v` — Expected: FAIL (`_dispatch_preview` chưa có).

- [ ] **Step 2: Implement preview/execute trong `dispatcher.py`**

Thêm import: `from .commands import ExecuteArgs, PreviewArgs, SafeExecuteResult, SafePreviewResult, ticket_fingerprint` và:

```python
    # -- execution (preview / confirm) --------------------------------------

    def _require_testnet(self, ticket_environment: str | None = None) -> None:
        # Mainnet hard-lock (spec §9, §15): mọi lớp phải là testnet.
        if self.settings.binance.environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        if os.getenv("BINANCE_ENV") != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")
        if ticket_environment is not None and ticket_environment != "testnet":
            raise DispatchError("ENVIRONMENT_FORBIDDEN")

    def _load_ticket(self, store: Store, ticket_id: str) -> Any:
        try:
            return store.ticket(ticket_id)
        except ValueError:
            raise DispatchError("TICKET_NOT_FOUND") from None

    def _dispatch_preview(self, args: PreviewArgs, operator_email: str) -> SafePreviewResult:
        self._require_testnet()
        store = self._store()
        ticket = self._load_ticket(store, args.ticket_id)
        self._require_testnet(ticket.environment)
        submission = store.submission(ticket.id)
        return SafePreviewResult(
            action=args.action,
            ticket_id=ticket.id,
            environment=ticket.environment,
            execution_mode=execution_mode(),
            status=ticket.status,
            symbol=ticket.symbol,
            side=ticket.side,
            intent=ticket.intent,
            quantity=str(ticket.quantity),
            notional_usdt=str(ticket.notional_usdt),
            entry=str(ticket.limit_price),
            stop=str(ticket.stop_price),
            target=str(ticket.target_price),
            created_at=ticket.created_at,
            expires_at=ticket.expires_at,
            submission_status=None if submission is None else submission["status"],
            fingerprint=ticket_fingerprint(ticket),
        )

    def _dispatch_execute(self, args: ExecuteArgs, operator_email: str) -> SafeExecuteResult:
        self._require_testnet()
        store = self._store()
        ticket = self._load_ticket(store, args.ticket_id)
        self._require_testnet(ticket.environment)
        # Reread + revalidate (spec §9 bước 9): fingerprint hiện tại phải khớp
        # fingerprint đã preview; mọi khác biệt → fail closed, không side effect.
        if ticket_fingerprint(ticket) != args.fingerprint:
            raise DispatchError("TICKET_CHANGED")
        execution = self._execution()
        try:
            if args.action == "approve":
                result = execution.approve(ticket.id, actor=operator_email, channel="local")
            elif args.action == "reject":
                result = execution.reject(ticket.id, actor=operator_email, channel="local")
            else:
                result = execution.reconcile(ticket.id)
        except ValueError as exc:
            # ValueError của ExecutionService luôn xảy ra trước side effect.
            raise DispatchError("VALIDATION_FAILED", str(exc)) from None
        return SafeExecuteResult(action=args.action, ticket_id=ticket.id, status=result.status)
```

Lưu ý: các exception KHÔNG phải `ValueError`/`DispatchError` (Timeout, BrokerError…) được để nguyên cho runner phân loại `NEEDS_REVIEW: EXECUTION_UNCERTAIN` (Task 9) — dispatcher không nuốt.

- [ ] **Step 3: Chạy toàn bộ suite và commit (repo gốc)**

Run:

```bash
uv run pytest -q && uv run ruff check .
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/dispatcher.py tests/test_dispatcher.py
git commit -m "feat: add execution preview and confirm dispatch with mainnet hard lock"
```

---

### Task 9: [python] CommandRunner — protocol, journal exactly-once, recovery

**Files:**
- Create: `src/crypto_desk/runner.py`
- Modify: `src/crypto_desk/dashboard.py` (thêm `publish_dashboard_if_configured`), `src/crypto_desk/cli.py` (wrapper `_publish_dashboard_if_configured` delegate — giữ nguyên tên/hành vi để monkeypatch path cũ sống)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 5–8 (`command_hash`, `ensure_safe_result`, `EXECUTION_KINDS`, `STATE_CHANGING_KINDS`, `UnsafeResultError`, `CommandDispatcher`, `DispatchError`, `execution_mode`, `Store.journal_*`), runner API Task 2, `SITES_BYPASS_TOKEN_ENV` từ `crypto_desk.dashboard`.
- Produces: `class CommandRunner` với `__init__(settings, *, base_url, runner_token, sites_bypass_token, store=None, dispatcher=None, client=None, session_id=None, enabled=True, sleep=time.sleep, log=...)`; methods `register()`, `heartbeat()`, `claim() -> dict | None`, `renew_lease(command_id) -> bool`, `report(command_id, *, status, result=None, error_code=None) -> bool`, `recover()`, `process_one() -> bool`, `run_forever()`, `stop()`; `class RunnerProtocolError(RuntimeError)`; hằng `COMMAND_API_URL_ENV="CRYPTO_DESK_COMMAND_API_URL"`, `RUNNER_TOKEN_ENV="CRYPTO_DESK_RUNNER_TOKEN"`, `RUNNER_ENABLED_ENV="CRYPTO_DESK_COMMAND_RUNNER_ENABLED"`, `HEARTBEAT_SECONDS=5.0`, `IDLE_POLL_SECONDS=2.0`, `LEASE_RENEW_SECONDS=10.0`; `dashboard.publish_dashboard_if_configured(settings) -> str | None`.

- [ ] **Step 1: Refactor hook publish (không đổi hành vi)**

Trong `src/crypto_desk/dashboard.py` thêm (dưới `publish_dashboard_from_env`):

```python
def publish_dashboard_if_configured(settings: "Settings") -> str | None:
    """Best-effort publish sau lệnh đổi trạng thái.

    Trả về thông điệp cảnh báo an toàn (không token/URL) hoặc None khi thành
    công/không cấu hình. Không bao giờ raise.
    """
    if not os.getenv(DASHBOARD_INGEST_URL_ENV):
        return None
    store = None
    try:
        from .store import Store

        store = Store(settings.database)
        snapshot = build_dashboard_snapshot(settings, store)
        publish_dashboard_from_env(snapshot, strict=False)
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort theo thiết kế
        return f"Warning: dashboard publish failed ({type(exc).__name__})"
    finally:
        if store is not None:
            store.close()
```

(Thêm `import os` nếu module chưa có; import `Settings` dưới `TYPE_CHECKING`.) Sửa `cli.py` `_publish_dashboard_if_configured` thành wrapper — giữ nguyên tên hàm và vị trí để `monkeypatch.setattr("crypto_desk.cli._publish_dashboard_if_configured", ...)` trong test cũ (nếu có) vẫn hoạt động:

```python
def _publish_dashboard_if_configured(settings: Settings) -> None:
    warning = publish_dashboard_if_configured(settings)
    if warning:
        typer.echo(warning, err=True)
```

Run: `uv run pytest tests/test_service_cli.py tests/test_dashboard.py -q` — Expected: PASS (hành vi giữ nguyên).

- [ ] **Step 2: Viết failing runner tests**

Tạo `tests/test_runner.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crypto_desk.commands import SafeExecuteResult, SafeSyncResult, command_hash
from crypto_desk.config import Settings
from crypto_desk.dispatcher import DispatchError
from crypto_desk.runner import CommandRunner, RunnerProtocolError
from crypto_desk.store import Store

OPERATOR = "quoc.lb@teko.vn"


class FakeSites:
    """Fake runner API qua httpx.MockTransport."""

    def __init__(self):
        self.queue: list[dict] = []
        self.reports: list[dict] = []
        self.sessions: list[dict] = []
        self.heartbeats: list[dict] = []
        self.leases: list[dict] = []
        self.session_conflict = False
        self.fail_next_reports = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        path = request.url.path
        if path == "/api/runner/session":
            self.sessions.append(body)
            if self.session_conflict:
                return httpx.Response(409, json={"error": "SESSION_CONFLICT"})
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/heartbeat":
            self.heartbeats.append(body)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/claim":
            command = self.queue.pop(0) if self.queue else None
            return httpx.Response(200, json={"command": command})
        if path == "/api/runner/lease":
            self.leases.append(body)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/result":
            if self.fail_next_reports > 0:
                self.fail_next_reports -= 1
                raise httpx.ConnectError("connection refused")
            self.reports.append(body)
            return httpx.Response(200, json={"ok": True, "applied": True})
        return httpx.Response(404, json={"error": "NOT_FOUND"})


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.raise_for_kind: dict[str, Exception] = {}

    def dispatch(self, kind, args, *, operator_email, environment="testnet"):
        self.calls.append((kind, dict(args)))
        if kind in self.raise_for_kind:
            raise self.raise_for_kind[kind]
        if kind == "execute":
            return SafeExecuteResult(action=args["action"], ticket_id=args["ticket_id"], status="APPROVED_DRY_RUN")
        return SafeSyncResult(
            environment="testnet", nav_usdt="1000", free_usdt="500",
            positions_count=0, open_orders_count=0, as_of="2026-07-18T09:00:00+00:00",
        )


def make_runner(tmp_path: Path, sites: FakeSites, **kwargs) -> tuple[CommandRunner, Store, FakeDispatcher]:
    settings = Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )
    store = Store(settings.database)
    dispatcher = FakeDispatcher()
    client = httpx.Client(
        base_url="https://sites.example/api",
        transport=httpx.MockTransport(sites.handler),
    )
    runner = CommandRunner(
        settings,
        base_url="https://sites.example/api",
        runner_token="runner-token",
        sites_bypass_token="bypass-token",
        store=store,
        dispatcher=dispatcher,
        client=client,
        session_id="session-1",
        **kwargs,
    )
    return runner, store, dispatcher


def make_command(command_id: str = "cmd-1", kind: str = "sync", args: dict | None = None) -> dict:
    return {
        "id": command_id,
        "kind": kind,
        "args": args or {},
        "operator_email": OPERATOR,
        "environment": "testnet",
        "queued_at": "2026-07-18T09:00:00.000Z",
    }


def test_process_one_claims_dispatches_reports_and_publishes(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured", lambda settings: published.append(True) or None
    )
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)

    assert runner.process_one() is True
    assert dispatcher.calls == [("sync", {})]
    assert sites.reports[0]["command_id"] == "cmd-1"
    assert sites.reports[0]["status"] == "SUCCEEDED"
    assert sites.reports[0]["result"]["nav_usdt"] == "1000"
    entry = store.journal_entry("cmd-1")
    assert entry["state"] == "SUCCEEDED" and entry["reported"] is True
    assert published == [True]  # sync là state-changing


def test_duplicate_command_same_hash_replays_without_reexecution(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)
    runner.process_one()

    sites.queue.append(make_command())  # Sites giao lại cùng command (ack lạc)
    assert runner.process_one() is True
    assert len(dispatcher.calls) == 1  # KHÔNG chạy lại
    assert [r["status"] for r in sites.reports] == ["SUCCEEDED", "SUCCEEDED"]


def test_duplicate_command_different_hash_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)
    runner.process_one()

    sites.queue.append(make_command(args={"tampered": True}))
    runner.process_one()
    assert len(dispatcher.calls) == 1
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "COMMAND_INTEGRITY"


def test_restart_recovery_never_repeats_a_running_command(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    runner, store, dispatcher = make_runner(tmp_path, sites)
    # giả lập crash giữa chừng: journal RUNNING, không có report
    store.journal_start("cmd-exec", command_hash("cmd-exec", "execute", {}), "execute")
    store.journal_start("cmd-read", command_hash("cmd-read", "tickets", {}), "tickets")

    runner.recover()
    assert store.journal_entry("cmd-exec")["state"] == "NEEDS_REVIEW"
    assert store.journal_entry("cmd-exec")["error_code"] == "EXECUTION_UNCERTAIN"
    assert store.journal_entry("cmd-read")["state"] == "FAILED"
    assert store.journal_entry("cmd-read")["error_code"] == "RUNNER_RESTART"
    statuses = {r["command_id"]: r["status"] for r in sites.reports}
    assert statuses == {"cmd-exec": "NEEDS_REVIEW", "cmd-read": "FAILED"}
    assert dispatcher.calls == []


def test_unacked_report_is_retried_before_claiming_new_work(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.fail_next_reports = 1
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)

    runner.process_one()  # dispatch OK nhưng report rớt mạng
    assert store.journal_entry("cmd-1")["reported"] is False

    sites.queue.append(make_command("cmd-2"))
    assert runner.process_one() is False  # chỉ retry report, không claim cmd-2
    assert store.journal_entry("cmd-1")["reported"] is True
    assert len(dispatcher.calls) == 1

    assert runner.process_one() is True  # giờ mới claim cmd-2
    assert dispatcher.calls[-1][0] == "sync"


def test_unexpected_exception_maps_by_kind(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.queue.append(make_command("cmd-x", kind="execute", args={
        "action": "approve", "ticket_id": "t-1",
        "preview_command_id": "p-1", "fingerprint": "a" * 64,
    }))
    runner, store, dispatcher = make_runner(tmp_path, sites)
    dispatcher.raise_for_kind["execute"] = TimeoutError("ambiguous")
    runner.process_one()
    assert sites.reports[-1]["status"] == "NEEDS_REVIEW"
    assert sites.reports[-1]["error_code"] == "EXECUTION_UNCERTAIN"

    sites.queue.append(make_command("cmd-y", kind="screen"))
    dispatcher.raise_for_kind["screen"] = RuntimeError("boom")
    runner.process_one()
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "DISPATCH_FAILED"


def test_dispatch_error_reports_safe_code(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.queue.append(make_command("cmd-z", kind="execute", args={
        "action": "approve", "ticket_id": "t-1",
        "preview_command_id": "p-1", "fingerprint": "a" * 64,
    }))
    runner, store, dispatcher = make_runner(tmp_path, sites)
    dispatcher.raise_for_kind["execute"] = DispatchError("TICKET_CHANGED")
    runner.process_one()
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "TICKET_CHANGED"


def test_register_conflict_raises(tmp_path):
    sites = FakeSites()
    sites.session_conflict = True
    runner, _, _ = make_runner(tmp_path, sites)
    with pytest.raises(RunnerProtocolError, match="SESSION_CONFLICT"):
        runner.register()


def test_disabled_runner_never_claims(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_desk.runner.publish_dashboard_if_configured", lambda settings: None)
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, _, dispatcher = make_runner(tmp_path, sites, enabled=False, sleep=lambda _: runner.stop())
    runner.run_forever()
    assert dispatcher.calls == []
    assert sites.queue  # command vẫn nằm trong queue

def test_runner_sends_both_auth_headers(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    settings = Settings(database=tmp_path / "c.sqlite3", artifacts=tmp_path / "a", symbols=("BTCUSDT",))
    runner = CommandRunner(
        settings,
        base_url="https://sites.example/api",
        runner_token="runner-token",
        sites_bypass_token="bypass-token",
        store=Store(settings.database),
        dispatcher=FakeDispatcher(),
        client=None,
        session_id="session-1",
        transport=httpx.MockTransport(handler),
    )
    runner.heartbeat()
    assert captured["authorization"] == "Bearer runner-token"
    assert captured["oai-sites-authorization"] == "Bearer bypass-token"
```

Run: `uv run pytest tests/test_runner.py -v`

Expected: FAIL — `ModuleNotFoundError: crypto_desk.runner`.

- [ ] **Step 3: Implement `src/crypto_desk/runner.py`**

```python
"""Command runner: outbound HTTPS duy nhất — poll, claim, dispatch, report.

Spec §10: heartbeat 5s, idle poll 2s, lease renew 10s. Journal (Store v3) đảm
bảo exactly-once tại local; không bao giờ tự lặp lại command đã RUNNING.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from .commands import (
    EXECUTION_KINDS,
    STATE_CHANGING_KINDS,
    UnsafeResultError,
    command_hash,
    ensure_safe_result,
)
from .config import Settings
from .dashboard import SITES_BYPASS_TOKEN_ENV, publish_dashboard_if_configured  # noqa: F401
from .dispatcher import CommandDispatcher, DispatchError, execution_mode
from .store import Store

COMMAND_API_URL_ENV = "CRYPTO_DESK_COMMAND_API_URL"
RUNNER_TOKEN_ENV = "CRYPTO_DESK_RUNNER_TOKEN"
RUNNER_ENABLED_ENV = "CRYPTO_DESK_COMMAND_RUNNER_ENABLED"

HEARTBEAT_SECONDS = 5.0
IDLE_POLL_SECONDS = 2.0
LEASE_RENEW_SECONDS = 10.0


class RunnerProtocolError(RuntimeError):
    """Lỗi giao thức với Sites; thông điệp luôn an toàn để log."""


class CommandRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        runner_token: str,
        sites_bypass_token: str,
        store: Store | None = None,
        dispatcher: CommandDispatcher | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        session_id: str | None = None,
        enabled: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda message: None,
    ):
        self.settings = settings
        self.store = store if store is not None else Store(settings.database)
        self.dispatcher = dispatcher if dispatcher is not None else CommandDispatcher(settings)
        headers = {
            "Authorization": f"Bearer {runner_token}",
            "OAI-Sites-Authorization": f"Bearer {sites_bypass_token}",
        }
        self.client = client or httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )
        if client is not None:
            self.client.headers.update(headers)
        self.session_id = session_id or self._load_session_id()
        self.enabled = enabled
        self._sleep = sleep
        self._log = log
        self._active_command_id: str | None = None
        self._active_lock = threading.Lock()
        self._last_lease_renew = 0.0
        self._stop = threading.Event()

    # -- session identity ---------------------------------------------------

    def _load_session_id(self) -> str:
        path = self.settings.database.parent / "runner_session"
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        session_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")
        return session_id

    # -- protocol -------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        response = self.client.post(path, json=payload)
        if response.status_code >= 500:
            raise RunnerProtocolError(f"runner API failed (status {response.status_code})")
        return response

    def register(self) -> None:
        response = self._post(
            "/runner/session",
            {"session_id": self.session_id, "execution_mode": execution_mode()},
        )
        if response.status_code == 409:
            raise RunnerProtocolError("SESSION_CONFLICT: một runner session khác đang hoạt động")
        if response.status_code != 200:
            raise RunnerProtocolError(f"session register rejected (status {response.status_code})")

    def heartbeat(self) -> None:
        response = self._post(
            "/runner/heartbeat",
            {"session_id": self.session_id, "execution_mode": execution_mode()},
        )
        if response.status_code == 409:
            raise RunnerProtocolError("SESSION_MISMATCH: session không còn là singleton")

    def claim(self) -> dict[str, Any] | None:
        response = self._post("/runner/claim", {"session_id": self.session_id})
        if response.status_code != 200:
            raise RunnerProtocolError(f"claim rejected (status {response.status_code})")
        return response.json().get("command")

    def renew_lease(self, command_id: str) -> bool:
        response = self._post(
            "/runner/lease", {"session_id": self.session_id, "command_id": command_id}
        )
        return response.status_code == 200

    def report(
        self,
        command_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        response = self._post(
            "/runner/result",
            {
                "session_id": self.session_id,
                "command_id": command_id,
                "status": status,
                "result": result,
                "error_code": error_code,
            },
        )
        if response.status_code != 200:
            raise RunnerProtocolError(f"result report rejected (status {response.status_code})")
        return True

    # -- exactly-once execution ------------------------------------------------

    def recover(self) -> None:
        # Spec §8: journal RUNNING sau restart — không bao giờ tự chạy lại.
        for entry in self.store.journal_running():
            if entry["kind"] in EXECUTION_KINDS:
                self.store.journal_finish(
                    entry["command_id"], state="NEEDS_REVIEW", error_code="EXECUTION_UNCERTAIN"
                )
            else:
                self.store.journal_finish(
                    entry["command_id"], state="FAILED", error_code="RUNNER_RESTART"
                )
        for entry in self.store.journal_unreported():
            self._report_entry(entry)

    def _report_entry(self, entry: dict[str, Any]) -> None:
        try:
            self.report(
                entry["command_id"],
                status=entry["state"],
                result=entry["result"],
                error_code=entry["error_code"],
            )
        except (httpx.HTTPError, RunnerProtocolError) as exc:
            self._log(f"report failed ({type(exc).__name__}); sẽ thử lại")
            return
        self.store.journal_mark_reported(entry["command_id"])

    def process_one(self) -> bool:
        self.recover()
        if self.store.journal_unreported():
            # Chưa ack xong kết quả trước đó → không claim thêm (spec §10).
            return False
        claimed = self.claim()
        if claimed is None:
            return False
        command_id = str(claimed["id"])
        kind = str(claimed["kind"])
        args = dict(claimed.get("args") or {})
        digest = command_hash(command_id, kind, args)
        entry = self.store.journal_entry(command_id)
        if entry is not None:
            if entry["command_hash"] != digest:
                # Spec §8: cùng ID khác hash → fail closed, không thực thi.
                try:
                    self.report(command_id, status="FAILED", error_code="COMMAND_INTEGRITY")
                except (httpx.HTTPError, RunnerProtocolError) as exc:
                    self._log(f"integrity report failed ({type(exc).__name__})")
                return True
            if entry["state"] != "RUNNING":
                self._report_entry(entry)  # replay terminal state đã lưu
            return True
        self.store.journal_start(command_id, digest, kind)
        with self._active_lock:
            self._active_command_id = command_id
        try:
            model = self.dispatcher.dispatch(
                kind,
                args,
                operator_email=str(claimed.get("operator_email", "")),
                environment=str(claimed.get("environment", "testnet")),
            )
            self.store.journal_finish(command_id, state="SUCCEEDED", result=ensure_safe_result(model))
        except DispatchError as exc:
            self.store.journal_finish(command_id, state="FAILED", error_code=exc.code)
        except UnsafeResultError:
            self.store.journal_finish(command_id, state="FAILED", error_code="RESULT_UNSAFE")
        except Exception as exc:  # noqa: BLE001 - phân loại theo kind, không nuốt chi tiết vào result
            if kind in EXECUTION_KINDS:
                # Có thể đã chạm side-effect boundary → không bao giờ tự retry.
                self.store.journal_finish(
                    command_id, state="NEEDS_REVIEW", error_code="EXECUTION_UNCERTAIN"
                )
            else:
                self.store.journal_finish(command_id, state="FAILED", error_code="DISPATCH_FAILED")
            self._log(f"dispatch {kind} failed ({type(exc).__name__})")
        finally:
            with self._active_lock:
                self._active_command_id = None
        entry = self.store.journal_entry(command_id)
        self._report_entry(entry)
        if entry["state"] == "SUCCEEDED" and kind in STATE_CHANGING_KINDS:
            warning = publish_dashboard_if_configured(self.settings)
            if warning:
                self._log(warning)
        return True

    # -- vòng lặp -----------------------------------------------------------

    def run_forever(self) -> None:
        self.register()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        try:
            while not self._stop.is_set():
                if not self.enabled:
                    # Kill switch (spec §15): vẫn recover/report, không claim.
                    self.recover()
                    self._sleep(IDLE_POLL_SECONDS)
                    continue
                worked = False
                try:
                    worked = self.process_one()
                except (httpx.HTTPError, RunnerProtocolError) as exc:
                    self._log(f"poll failed ({type(exc).__name__})")
                if not worked:
                    self._sleep(IDLE_POLL_SECONDS)
        finally:
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self.heartbeat()
                with self._active_lock:
                    active = self._active_command_id
                if active and time.monotonic() - self._last_lease_renew >= LEASE_RENEW_SECONDS:
                    if self.renew_lease(active):
                        self._last_lease_renew = time.monotonic()
            except (httpx.HTTPError, RunnerProtocolError):
                # Lỡ nhịp heartbeat: Sites tự coi runner offline sau 30s.
                continue
```

- [ ] **Step 4: Chạy toàn bộ suite và commit (repo gốc)**

Run:

```bash
uv run pytest -q && uv run ruff check .
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/runner.py src/crypto_desk/dashboard.py src/crypto_desk/cli.py tests/test_runner.py
git commit -m "feat: add command runner with exactly-once journal and recovery"
```

---

### Task 10: [python] Lệnh `desk runner` + env/docs/systemd

**Files:**
- Modify: `src/crypto_desk/cli.py`, `.env.example`, `README.md`
- Create: `services/crypto-desk-runner.service`
- Test: Modify `tests/test_service_cli.py`

**Interfaces:**
- Consumes: Task 9 `CommandRunner`, `COMMAND_API_URL_ENV`, `RUNNER_TOKEN_ENV`, `RUNNER_ENABLED_ENV`; `SITES_BYPASS_TOKEN_ENV` từ dashboard.
- Produces: lệnh `desk runner` (Typer command tên `runner`); systemd user unit theo pattern `web/openbb-mcp.service`.

- [ ] **Step 1: Viết failing CLI tests (nối vào `tests/test_service_cli.py`)**

```python
def test_runner_command_builds_and_runs_the_runner(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_API_URL", "https://sites.example/api")
    monkeypatch.setenv("CRYPTO_DESK_RUNNER_TOKEN", "runner-token")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "bypass-token")
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_RUNNER_ENABLED", "1")

    captured = {}

    class FakeRunner:
        def __init__(self, settings, **kwargs):
            captured["kwargs"] = kwargs

        def run_forever(self):
            captured["ran"] = True

        def stop(self):
            pass

    monkeypatch.setattr("crypto_desk.cli.CommandRunner", FakeRunner)
    result = CliRunner().invoke(app, ["--config", str(config), "runner"])

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["kwargs"]["base_url"] == "https://sites.example/api"
    assert captured["kwargs"]["runner_token"] == "runner-token"
    assert captured["kwargs"]["sites_bypass_token"] == "bypass-token"
    assert captured["kwargs"]["enabled"] is True
    assert "runner-token" not in result.output


def test_runner_command_requires_all_env_vars(tmp_path, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_API_URL", "")
    monkeypatch.setenv("CRYPTO_DESK_RUNNER_TOKEN", "")
    monkeypatch.setenv("CRYPTO_DESK_SITES_BYPASS_TOKEN", "")

    result = CliRunner().invoke(app, ["--config", str(config), "runner"])

    assert result.exit_code == 2
    assert "CRYPTO_DESK_COMMAND_API_URL" in result.output
```

Đồng thời thêm `"runner"` vào danh sách tên lệnh trong test help-listing hiện có (`tests/test_service_cli.py:182-201`).

Run: `uv run pytest tests/test_service_cli.py -q` — Expected: FAIL (lệnh chưa tồn tại).

- [ ] **Step 2: Implement lệnh `runner` trong `cli.py`**

Thêm import: `import signal` và `from .runner import COMMAND_API_URL_ENV, RUNNER_ENABLED_ENV, RUNNER_TOKEN_ENV, CommandRunner`; `from .dashboard import SITES_BYPASS_TOKEN_ENV` (đã có import dashboard khác — gộp).

```python
@app.command()
def runner(ctx: typer.Context) -> None:
    """Chạy command runner cho dashboard tương tác (poll outbound, FIFO)."""
    settings = _load(ctx)
    base_url = os.getenv(COMMAND_API_URL_ENV)
    runner_token = os.getenv(RUNNER_TOKEN_ENV)
    bypass_token = os.getenv(SITES_BYPASS_TOKEN_ENV)
    missing = [
        name
        for name, value in (
            (COMMAND_API_URL_ENV, base_url),
            (RUNNER_TOKEN_ENV, runner_token),
            (SITES_BYPASS_TOKEN_ENV, bypass_token),
        )
        if not value
    ]
    if missing:
        _fail(f"Thiếu biến môi trường bắt buộc: {', '.join(missing)}")
    worker = CommandRunner(
        settings,
        base_url=base_url,
        runner_token=runner_token,
        sites_bypass_token=bypass_token,
        enabled=os.getenv(RUNNER_ENABLED_ENV, "1") == "1",
        log=lambda message: typer.echo(message, err=True),
    )
    signal.signal(signal.SIGTERM, lambda signum, frame: worker.stop())
    signal.signal(signal.SIGINT, lambda signum, frame: worker.stop())
    try:
        worker.run_forever()
    except RunnerProtocolError as exc:
        _fail(str(exc))
```

(import `RunnerProtocolError` cùng dòng với `CommandRunner`.)

- [ ] **Step 3: Env, README, systemd unit**

Nối vào `.env.example`:

```dotenv
CRYPTO_DESK_COMMAND_API_URL=
CRYPTO_DESK_RUNNER_TOKEN=
CRYPTO_DESK_COMMAND_RUNNER_ENABLED=1
```

Tạo `services/crypto-desk-runner.service` (pattern `web/openbb-mcp.service`):

```ini
[Unit]
Description=Crypto Desk Command Runner
After=network-online.target

[Service]
Type=simple
ExecStart=%h/crypto-desk-rewrite/scripts/desk runner
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Nối vào README.md (sau mục Hermes cron) mục "Command runner cho dashboard tương tác":

```markdown
## Command runner cho dashboard tương tác

Runner là tiến trình outbound duy nhất phục vụ command deck trên dashboard:

1. Điền `CRYPTO_DESK_COMMAND_API_URL` (vd `https://<site>/api`),
   `CRYPTO_DESK_RUNNER_TOKEN` (token riêng, KHÔNG dùng lại ingest token) và
   `CRYPTO_DESK_SITES_BYPASS_TOKEN` vào `.env` (quyền 600).
2. Chạy thử foreground: `scripts/desk runner` (Ctrl-C để dừng sạch).
3. Chạy thường trực bằng systemd user unit:

    mkdir -p ~/.config/systemd/user
    ln -s "$PWD/services/crypto-desk-runner.service" ~/.config/systemd/user/crypto-desk-runner.service
    systemctl --user daemon-reload
    systemctl --user enable --now crypto-desk-runner

Kill switch: đặt `CRYPTO_DESK_COMMAND_RUNNER_ENABLED=0` rồi restart service —
runner vẫn báo cáo nốt command đã journal nhưng không claim command mới.
Mainnet không bao giờ được thực thi từ web; mọi lệnh execute yêu cầu
`BINANCE_ENV=testnet` ở tất cả các lớp.
```

- [ ] **Step 4: Chạy toàn bộ suite và commit (repo gốc)**

Run:

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: PASS (gồm `tests/test_operations.py` — README mới chỉ nối thêm, không sửa nội dung được assert).

Commit:

```bash
git add src/crypto_desk/cli.py .env.example README.md services/crypto-desk-runner.service tests/test_service_cli.py
git commit -m "feat: add desk runner command with systemd unit and env wiring"
```

---

<!-- CONTINUE -->






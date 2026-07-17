# Crypto Desk Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the equity/IBKR/TradingAgents implementation with a crypto-native, long-only Binance Spot desk that supports Testnet and tightly gated Mainnet execution.

**Architecture:** Create a new `crypto_desk` package and keep the public `desk` command. Public market evidence is collected independently from authenticated execution, a custom OpenAI committee produces structured decisions, a deterministic `Decimal`-based risk engine creates tickets, and a Binance Spot adapter executes/reconciles them. SQLite, Hermes, Telegram approval, Markdown reports, and JSON CLI output remain, but equity calendars, IBKR, TradingAgents, yfinance, and equity-specific OpenBB calls leave the critical path.

**Tech Stack:** Python 3.12, uv, Typer, Pydantic, OpenAI Python SDK, HTTPX, official `binance-sdk-spot`, SQLite, Hermes Agent, pytest, Ruff.

## Global Constraints

- Execution supports Binance Spot only: no Margin, Futures, Options, borrowing, shorting, staking, conversion, or withdrawal endpoints.
- The initial symbol allowlist is exactly `BTCUSDT`, `ETHUSDT`, `BNBUSDT`, and `SOLUSDT`; configuration may remove symbols but may not add non-USDT pairs in V1.
- Research evidence includes Spot OHLCV/order book/24-hour volume, RSS news, USD-M funding rate, USD-M open interest, and an independent CoinGecko price.
- Derivatives data is evidence only; authenticated Futures credentials must never exist in the application.
- Full committee analysis runs once per UTC day at 00:15; health runs every 15 minutes.
- Risk limits are 0.5% NAV per trade, 20% NAV per coin, 80% total crypto exposure, and at least 20% NAV retained as free USDT.
- Mainnet order notional is capped at 25 USDT until at least 20 Mainnet order chains have reached a terminal reconciled state.
- Mainnet submission requires all three gates: `BINANCE_ENV=mainnet`, `LIVE_EXECUTION_ENABLED=1`, and an allowlisted Telegram approval carrying a valid five-minute confirmation code.
- Testnet and Mainnet use separate API keys and fixed, code-owned base URLs. Configuration may select an environment but may not provide an arbitrary execution URL.
- Every price, quantity, notional, balance, fee, and filter calculation uses `decimal.Decimal`; floats are forbidden in the domain, risk, ticket, and execution modules.
- Entry uses a Binance OTOCO order list with a `LIMIT FOK` working order, take-profit child, and stop-loss child. `FOK` prevents an unprotected partial entry.
- Ticket approval expires after 30 minutes. The Mainnet confirmation code expires after five minutes.
- Every ticket maps to one environment-specific `listClientOrderId`; unknown submission status is reconciled and is never automatically resubmitted.
- Core market evidence older than its freshness window, a Binance/CoinGecko price deviation over 0.5%, time leakage, missing symbol filters, or invalid LLM output forces `NO_TRADE`.
- Reports are Vietnamese; enum values, database columns, JSON keys, and Python identifiers remain English.
- Secrets stay in `.env`, are never serialized to SQLite, reports, structured logs, exception messages, or CLI JSON.
- Existing `data/desk.sqlite3` and equity artifacts are preserved. Crypto uses `data/crypto_desk.sqlite3` and `artifacts/crypto/`.

---

## Target File Map

```text
src/crypto_desk/
  __init__.py       Package version.
  domain.py         Decimal-safe immutable domain models and JSON encoding.
  config.py         YAML/.env loading, validation, and fixed environment URLs.
  store.py          Versioned SQLite schema and idempotent persistence.
  data.py           Binance/CoinGecko/RSS public-data clients and evidence builder.
  screener.py       Deterministic allowlist liquidity/momentum shortlist.
  committee.py      Crypto-native analysts, debate, manager, and structured outputs.
  risk.py           Decimal risk sizing and Binance filter rounding.
  broker.py         Authenticated Binance Spot Testnet/Mainnet adapter only.
  execution.py      Approval gates, confirmation codes, ticket state machine, reconcile.
  service.py        Analyze, sync, daily, health, reflection, and report orchestration.
  cli.py            Typer commands and stable JSON/Markdown output.

tests/
  fixtures/         Captured, redacted Binance/CoinGecko/RSS responses.
  test_config.py
  test_domain_store.py
  test_data.py
  test_screener.py
  test_committee.py
  test_risk.py
  test_broker.py
  test_execution.py
  test_service_cli.py
```

The old `src/investment_desk/` package remains untouched until Task 10, when all new tests and Testnet smoke checks pass.

---

### Task 0: Preserve the current implementation as a clean Git baseline

**Files:**
- Modify: `.gitignore`
- Track: the current source, tests, scripts, service unit, examples, README, and lockfile.
- Exclude: `.env`, `config.yaml`, databases, artifacts, virtual environments, and caches.

**Interfaces:**
- Produces: one reproducible baseline commit from which the rewrite can be reviewed or rolled back.

- [ ] **Step 1: Protect local runtime configuration**

Add `config.yaml` to `.gitignore` while keeping `config.example.yaml` tracked:

```gitignore
.env
config.yaml
.venv/
.pytest_cache/
__pycache__/
*.py[cod]
data/
artifacts/
.openbb_platform/
```

- [ ] **Step 2: Verify that no secret-bearing file will be committed**

Run:

```bash
git status --short
git check-ignore -v .env config.yaml data/desk.sqlite3
rg -n "(API_KEY|API_SECRET|BOT_TOKEN)=.+" \
  .env.example config.example.yaml README.md src tests scripts hermes services
```

Expected: `.env`, `config.yaml`, and the database are ignored; the final command finds no populated secret.

- [ ] **Step 3: Run the existing verification suite**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
```

Expected: 11 tests pass and all remaining checks exit zero.

- [ ] **Step 4: Create the baseline commit**

Run:

```bash
git add .env.example .gitignore README.md config.example.yaml hermes pyproject.toml \
  scripts services src tests uv.lock
git commit -m "chore: checkpoint investment desk baseline"
```

Expected: one root commit exists and `git status --short` is empty.

---

### Task 1: Crypto package, configuration, and domain contracts

**Files:**
- Create: `src/crypto_desk/__init__.py`
- Create: `src/crypto_desk/domain.py`
- Create: `src/crypto_desk/config.py`
- Create: `tests/test_config.py`
- Modify: `pyproject.toml`
- Modify: `config.example.yaml`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Environment`, `Action`, `EvidenceItem`, `SymbolRules`, `PortfolioSnapshot`, `ResearchDecision`, `TradeTicket`, `Settings`, and `load_settings(path)`.
- All `Decimal` values serialize as strings through `to_jsonable(value)`.

- [ ] **Step 1: Replace project dependencies and point the CLI entry at the new package**

Set the project name to `crypto-desk`, description to `A human-approved Binance Spot crypto research and execution desk`, and dependencies to:

```toml
dependencies = [
  "binance-sdk-spot==10.0.0",
  "httpx==0.28.1",
  "openai==2.45.0",
  "pydantic==2.13.4",
  "python-dotenv==1.2.1",
  "PyYAML==6.0.2",
  "typer==0.21.0",
]

[project.optional-dependencies]
dev = ["pytest==8.4.2", "ruff==0.15.5"]

[project.scripts]
desk = "crypto_desk.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/crypto_desk"]
```

Run: `uv lock`

Expected: `uv.lock` resolves without `ib_async`, `tradingagents`, `yfinance`, `exchange-calendars`, or the OpenBB Python packages.

- [ ] **Step 2: Write failing configuration and Decimal serialization tests**

```python
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_desk.config import MAINNET_URL, TESTNET_URL, load_settings
from crypto_desk.domain import to_jsonable


def test_urls_are_owned_by_code():
    assert TESTNET_URL == "https://testnet.binance.vision"
    assert MAINNET_URL == "https://api.binance.com"


def test_config_rejects_non_usdt_and_unknown_symbols(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("symbols: [BTCUSDT, ETHBTC]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="allowlist"):
        load_settings(config)


def test_decimal_serializes_as_string():
    assert to_jsonable({"qty": Decimal("0.00123000")}) == {"qty": "0.00123000"}
```

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL because `crypto_desk.config` and `crypto_desk.domain` do not exist.

- [ ] **Step 3: Implement the immutable domain types and strict settings**

Use these exact public model fields:

```python
Environment = Literal["testnet", "mainnet"]
Action = Literal["HOLD", "ACCUMULATE", "REDUCE", "EXIT", "NO_TRADE"]


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    environment: Environment
    nav_usdt: Decimal
    free_usdt: Decimal
    positions: tuple[dict[str, str], ...]
    open_orders: tuple[dict[str, Any], ...]
    as_of: str


@dataclass(frozen=True, slots=True)
class TradeTicket:
    id: str
    environment: Environment
    symbol: str
    intent: Literal["OPEN", "ADD", "REDUCE", "CLOSE"]
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    notional_usdt: Decimal
    risk_snapshot: dict[str, Any]
    created_at: str
    expires_at: str
    status: str = "PENDING"
```

Define the evidence and decision types exactly as:

```python
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    id: str
    kind: Literal["spot", "news", "derivatives", "reference"]
    provider: str
    source: str
    fetched_at: str
    as_of: str
    delayed: bool
    stale: bool
    payload: dict[str, Any]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    symbol: str
    action: Action
    conviction: Decimal
    bull_case: str
    bear_case: str
    catalysts: tuple[str, ...]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    evidence_ids: tuple[str, ...]
    reason: str
```

`Settings` must expose:

```python
@dataclass(frozen=True, slots=True)
class RiskSettings:
    per_trade: Decimal = Decimal("0.005")
    max_symbol: Decimal = Decimal("0.20")
    max_gross: Decimal = Decimal("0.80")
    min_usdt_reserve: Decimal = Decimal("0.20")
    max_quote_deviation: Decimal = Decimal("0.005")
    mainnet_initial_order_cap_usdt: Decimal = Decimal("25")
    ticket_ttl_minutes: int = 30


@dataclass(frozen=True, slots=True)
class ModelSettings:
    quick: str = "gpt-5.4-mini"
    deep: str = "gpt-5.5"
    debate_rounds: int = 2


@dataclass(frozen=True, slots=True)
class BinanceSettings:
    environment: Environment = "testnet"


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    daily_utc: str = "00:15"
    health_minutes: int = 15


symbols: tuple[str, ...]
coingecko_ids: dict[str, str]
news_feeds: tuple[str, ...]
models: ModelSettings
risk: RiskSettings
binance: BinanceSettings
schedule: ScheduleSettings
telegram_allowlist: tuple[str, ...]
database: Path
artifacts: Path
```

Reject symbols outside the fixed set, non-USDT symbols, duplicate symbols, risk values outside `(0, 1]`, and Mainnet cap values below the conservative V1 floor of 5 USDT. The live symbol-specific `minNotional` is checked again from `exchangeInfo`.

- [ ] **Step 4: Update sample configuration and secret names**

`config.example.yaml` must contain:

```yaml
base_currency: USDT
database: data/crypto_desk.sqlite3
artifacts: artifacts/crypto
symbols: [BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT]
coingecko_ids:
  BTCUSDT: bitcoin
  ETHUSDT: ethereum
  BNBUSDT: binancecoin
  SOLUSDT: solana
news_feeds: []
models:
  quick: gpt-5.4-mini
  deep: gpt-5.5
  debate_rounds: 2
risk:
  per_trade: 0.005
  max_symbol: 0.20
  max_gross: 0.80
  min_usdt_reserve: 0.20
  max_quote_deviation: 0.005
  mainnet_initial_order_cap_usdt: "25"
  ticket_ttl_minutes: 30
binance:
  environment: testnet
schedule:
  daily_utc: "00:15"
  health_minutes: 15
telegram_allowlist: []
```

`.env.example` must contain only names and empty values:

```dotenv
OPENAI_API_KEY=
COINGECKO_DEMO_API_KEY=
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
BINANCE_MAINNET_API_KEY=
BINANCE_MAINNET_API_SECRET=
BINANCE_ENV=testnet
TESTNET_EXECUTION_ENABLED=0
LIVE_EXECUTION_ENABLED=0
LIVE_CONFIRMATION_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_HOME_CHANNEL=
```

- [ ] **Step 5: Run package checks and commit**

Run:

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/crypto_desk tests/test_config.py
uv lock --check
```

Expected: PASS and `uv lock --check` exits zero.

Commit:

```bash
git add pyproject.toml uv.lock config.example.yaml .env.example src/crypto_desk tests/test_config.py
git commit -m "feat: define crypto desk domain and configuration"
```

---

### Task 2: Versioned crypto SQLite store

**Files:**
- Create: `src/crypto_desk/store.py`
- Modify: `tests/test_domain_store.py`

**Interfaces:**
- Consumes: `PortfolioSnapshot`, `ResearchDecision`, and `TradeTicket`.
- Produces: `Store.save_snapshot`, `latest_snapshot`, `save_run`, `save_ticket`, `approve_ticket`, `save_submission`, `record_order_event`, `completed_mainnet_chains`, and idempotency queries.

- [ ] **Step 1: Write failing schema and idempotency tests**

```python
from pathlib import Path

import pytest

from crypto_desk.store import Store


def test_store_uses_schema_version_one(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    assert store.schema_version() == 1


def test_client_order_id_is_unique_per_environment(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})
    with pytest.raises(ValueError, match="already submitted"):
        store.save_submission("ticket-1", "testnet", "desk_ticket_1", {"status": "NEW"})
    store.save_submission("ticket-2", "mainnet", "desk_ticket_1", {"status": "NEW"})


def test_mainnet_count_requires_terminal_reconcile(tmp_path: Path):
    store = Store(tmp_path / "crypto.db")
    store.save_submission("ticket-1", "mainnet", "desk_ticket_1", {"status": "NEW"})
    assert store.completed_mainnet_chains() == 0
    store.record_order_event("ticket-1", "FILLED", {"executedQty": "0.001"})
    assert store.completed_mainnet_chains() == 1
```

Run: `uv run pytest tests/test_domain_store.py -v`

Expected: FAIL because `Store` is not implemented.

- [ ] **Step 2: Implement schema version 1**

Create these tables and constraints:

```sql
CREATE TABLE schema_meta (
  version INTEGER NOT NULL
);
CREATE TABLE portfolio_snapshots (
  id INTEGER PRIMARY KEY,
  environment TEXT NOT NULL,
  as_of TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE research_runs (
  id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  cutoff TEXT NOT NULL,
  decision TEXT NOT NULL,
  report_dir TEXT NOT NULL
);
CREATE TABLE tickets (
  id TEXT PRIMARY KEY,
  environment TEXT NOT NULL,
  symbol TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE approvals (
  ticket_id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  channel TEXT NOT NULL,
  approved_at TEXT NOT NULL
);
CREATE TABLE submissions (
  ticket_id TEXT PRIMARY KEY,
  environment TEXT NOT NULL,
  client_order_id TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(environment, client_order_id)
);
CREATE TABLE order_events (
  id INTEGER PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  status TEXT NOT NULL,
  event_time TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE scheduled_runs (
  kind TEXT NOT NULL,
  bucket TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY(kind, bucket)
);
CREATE TABLE reflections (
  run_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
```

Open SQLite with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, and a five-second busy timeout. Serialize through `to_jsonable` so no float enters persisted payloads.

- [ ] **Step 3: Run tests and commit**

Run:

```bash
uv run pytest tests/test_domain_store.py -v
uv run ruff check src/crypto_desk/store.py tests/test_domain_store.py
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/store.py tests/test_domain_store.py
git commit -m "feat: add versioned crypto desk store"
```

---

### Task 3: Public market, derivatives, news, and evidence validation

**Files:**
- Create: `src/crypto_desk/data.py`
- Create: `tests/test_data.py`
- Create: `tests/fixtures/binance_exchange_info.json`
- Create: `tests/fixtures/binance_klines.json`
- Create: `tests/fixtures/binance_depth.json`
- Create: `tests/fixtures/binance_book_ticker.json`
- Create: `tests/fixtures/binance_funding.json`
- Create: `tests/fixtures/binance_open_interest.json`
- Create: `tests/fixtures/coingecko_price.json`
- Create: `tests/fixtures/news.xml`

**Interfaces:**
- Produces: `PublicDataClient`, `EvidenceBuilder`, `EvidenceSnapshot`, and `EvidenceError`.
- `EvidenceBuilder.build(symbol, cutoff)` is the only input to the committee.

- [ ] **Step 1: Write failing evidence gate tests**

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_desk.data import EvidenceBuilder, EvidenceError


def test_price_deviation_over_half_percent_blocks_trade(fake_public_client):
    fake_public_client.binance_price = Decimal("100")
    fake_public_client.coingecko_price = Decimal("100.51")
    with pytest.raises(EvidenceError, match="price deviation"):
        EvidenceBuilder(fake_public_client).build("BTCUSDT", datetime.now(UTC))


def test_future_timestamp_is_rejected(fake_public_client):
    cutoff = datetime.now(UTC)
    fake_public_client.kline_as_of = cutoff + timedelta(seconds=1)
    with pytest.raises(EvidenceError, match="future"):
        EvidenceBuilder(fake_public_client).build("BTCUSDT", cutoff)


def test_missing_derivatives_signal_blocks_actionable_research(fake_public_client):
    fake_public_client.open_interest = None
    with pytest.raises(EvidenceError, match="open interest"):
        EvidenceBuilder(fake_public_client).build("BTCUSDT", datetime.now(UTC))
```

Run: `uv run pytest tests/test_data.py -v`

Expected: FAIL because `crypto_desk.data` does not exist.

- [ ] **Step 2: Implement public clients with fixed endpoints**

Use unauthenticated HTTPX calls only:

```python
SPOT_PUBLIC = "https://api.binance.com"
FUTURES_PUBLIC = "https://fapi.binance.com"
COINGECKO_PUBLIC = "https://api.coingecko.com/api/v3"
```

Fetch:

```text
GET /api/v3/exchangeInfo?symbol={symbol}
GET /api/v3/klines?symbol={symbol}&interval=1d&limit=120
GET /api/v3/klines?symbol={symbol}&interval=4h&limit=180
GET /api/v3/depth?symbol={symbol}&limit=20
GET /api/v3/ticker/bookTicker?symbol={symbol}
GET /api/v3/ticker/24hr?symbol={symbol}
GET /fapi/v1/premiumIndex?symbol={symbol}
GET /fapi/v1/openInterest?symbol={symbol}
GET /simple/price?ids={coingecko_id},tether&vs_currencies=usd
```

RSS feeds come from `settings.news_feeds` and are parsed with `xml.etree.ElementTree`. Store title, canonical URL, publication time, and SHA-256 content hash; do not store full copyrighted article bodies.

Normalize the independent reference to USDT before comparison:

```python
reference_usdt = coin_usd / tether_usd
deviation = abs(binance_mid - reference_usdt) / reference_usdt
```

- [ ] **Step 3: Implement freshness and consistency gates**

`EvidenceBuilder.build` must enforce:

```text
book ticker fetched within 60 seconds
latest closed 4h candle no older than 5 hours
latest closed daily candle no older than 26 hours
funding and open-interest fetch no older than 15 minutes
all as_of timestamps <= cutoff
Binance mid-price versus CoinGecko USD deviation <= 0.5%
at least one news item published in the previous 48 hours
symbol status TRADING, quoteAsset USDT, spot trading allowed, OCO allowed, OTO allowed
```

Extract `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize/minQty`, and `NOTIONAL` or `MIN_NOTIONAL` from `exchangeInfo` into `SymbolRules`.

- [ ] **Step 4: Test deterministic fixtures and commit**

Run:

```bash
uv run pytest tests/test_data.py -v
uv run ruff check src/crypto_desk/data.py tests/test_data.py
```

Expected: PASS without network access.

Commit:

```bash
git add src/crypto_desk/data.py tests/test_data.py tests/fixtures
git commit -m "feat: build validated crypto evidence snapshots"
```

---

### Task 4: Deterministic crypto screener

**Files:**
- Create: `src/crypto_desk/screener.py`
- Create: `tests/test_screener.py`

**Interfaces:**
- Consumes: validated `EvidenceSnapshot` objects.
- Produces: `ScreenResult(symbol, passes, score, reasons, evidence_ids)`.

- [ ] **Step 1: Write failing shortlist tests**

```python
from crypto_desk.screener import screen


def test_screen_rejects_illiquid_or_wide_spread(snapshot_factory):
    illiquid = snapshot_factory(quote_volume="49999999", spread="0.001")
    wide = snapshot_factory(quote_volume="50000000", spread="0.0021")
    assert not screen(illiquid).passes
    assert not screen(wide).passes


def test_screen_orders_passes_by_deterministic_score(snapshot_factory):
    weak = snapshot_factory(momentum_20d="0.02", momentum_60d="0.05")
    strong = snapshot_factory(momentum_20d="0.08", momentum_60d="0.15")
    results = sorted((screen(weak), screen(strong)), key=lambda x: x.score, reverse=True)
    assert results[0].symbol == strong.symbol
```

Run: `uv run pytest tests/test_screener.py -v`

Expected: FAIL because `crypto_desk.screener` does not exist.

- [ ] **Step 2: Implement exact V1 filters**

Reject when:

```text
24h quote volume < 50,000,000 USDT
best bid/ask spread > 0.20%
fewer than 90 closed daily candles
20-day momentum <= 0
evidence has any stale/core-error flag
symbol is outside the configured allowlist
```

Score passing symbols as:

```python
normalized_log_volume = min(
    Decimal("1"),
    quote_volume / Decimal("500000000"),
)
score = (
    Decimal("0.45") * momentum_20d
    + Decimal("0.25") * momentum_60d
    + Decimal("0.20") * normalized_log_volume
    - Decimal("0.10") * spread
)
```

The screener does not call an LLM. It returns at most four results because the V1 allowlist contains four symbols.

- [ ] **Step 3: Run tests and commit**

Run: `uv run pytest tests/test_screener.py -v`

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/screener.py tests/test_screener.py
git commit -m "feat: add deterministic crypto screener"
```

---

### Task 5: Custom crypto research committee

**Files:**
- Create: `src/crypto_desk/committee.py`
- Create: `tests/test_committee.py`

**Interfaces:**
- Consumes: one validated `EvidenceSnapshot` and immutable prior reflections.
- Produces: `ResearchDecision` plus named analyst reports.
- Public entry point: `CryptoCommittee.run(snapshot, reflections=())`.

- [ ] **Step 1: Write failing committee-order and schema tests**

```python
from crypto_desk.committee import CryptoCommittee


def test_committee_runs_specialists_before_debate(fake_llm, valid_snapshot):
    result = CryptoCommittee(fake_llm).run(valid_snapshot)
    assert fake_llm.calls == [
        "technical",
        "liquidity",
        "news",
        "derivatives",
        "bull_round_1",
        "bear_round_1",
        "bull_round_2",
        "bear_round_2",
        "manager",
    ]
    assert result.decision.evidence_ids == valid_snapshot.evidence_ids


def test_invalid_manager_schema_retries_once_then_no_trade(fake_llm, valid_snapshot):
    fake_llm.invalid_for = {"manager"}
    result = CryptoCommittee(fake_llm).run(valid_snapshot)
    assert fake_llm.count("manager") == 2
    assert result.decision.action == "NO_TRADE"
```

Run: `uv run pytest tests/test_committee.py -v`

Expected: FAIL because `CryptoCommittee` does not exist.

- [ ] **Step 2: Define strict Pydantic outputs**

Define:

```python
class AnalystReport(BaseModel):
    stance: Literal["bullish", "neutral", "bearish"]
    confidence: Annotated[Decimal, Field(ge=0, le=10)]
    observations: list[str] = Field(min_length=1, max_length=8)
    risks: list[str] = Field(max_length=8)
    evidence_ids: list[str] = Field(min_length=1)


class ManagerDecision(BaseModel):
    action: Action
    conviction: Annotated[Decimal, Field(ge=0, le=10)]
    bull_case: str
    bear_case: str
    catalysts: list[str]
    invalidation: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    evidence_ids: list[str]
```

Reject unknown evidence IDs, `ACCUMULATE` without `stop < entry < target`, `REDUCE/EXIT` without an existing position, and any numeric claim absent from the evidence snapshot.

- [ ] **Step 3: Implement the crypto-native committee**

Use `gpt-5.4-mini` for four specialists and `gpt-5.5` for bull/bear and manager. Each call receives only:

```text
system prompt for its role
bounded evidence JSON
reports from earlier committee stages
bounded reflections for the same symbol
```

No agent receives tools, network access, API secrets, raw SQLite paths, or permission to alter prompts/code. Debate is exactly two rounds. Use OpenAI structured parsing; retry an invalid structured response once. An evidence error skips all LLM calls and returns `NO_TRADE`.

- [ ] **Step 4: Add explicit crypto roles**

The roles are:

```text
technical: daily/4h trend, volatility, momentum, invalidation levels
liquidity: spread, depth, 24h volume, executable size
news: catalysts and adverse events from bounded headlines/metadata
derivatives: funding and open-interest positioning; never recommends Futures execution
bull/bear: challenge claims using cited evidence IDs
manager: HOLD, ACCUMULATE, REDUCE, EXIT, or NO_TRADE
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
uv run pytest tests/test_committee.py -v
uv run ruff check src/crypto_desk/committee.py tests/test_committee.py
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/committee.py tests/test_committee.py
git commit -m "feat: add crypto-native research committee"
```

---

### Task 6: Decimal risk sizing and ticket construction

**Files:**
- Create: `src/crypto_desk/risk.py`
- Create: `tests/test_risk.py`

**Interfaces:**
- Produces: `size_buy`, `size_sell`, `round_down`, `validate_prices`, and `build_ticket`.
- `size_buy` returns `SizingResult(quantity, notional_usdt, limiting_rule, rooms)`.

- [ ] **Step 1: Write boundary tests for every risk rule**

```python
from decimal import Decimal

import pytest

from crypto_desk.config import RiskSettings
from crypto_desk.domain import SymbolRules
from crypto_desk.risk import size_buy


@pytest.fixture
def default_inputs():
    return {
        "environment": "testnet",
        "nav_usdt": Decimal("10000"),
        "free_usdt": Decimal("5000"),
        "entry": Decimal("100000"),
        "stop": Decimal("90000"),
        "current_symbol_value": Decimal("0"),
        "current_gross_value": Decimal("0"),
        "mainnet_order_cap_usdt": Decimal("25"),
        "risk": RiskSettings(
            per_trade=Decimal("0.005"),
            max_symbol=Decimal("0.20"),
            max_gross=Decimal("0.80"),
            min_usdt_reserve=Decimal("0.20"),
        ),
        "rules": SymbolRules(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
        ),
    }


def test_half_percent_risk_is_tightest(default_inputs):
    result = size_buy(**default_inputs)
    assert result.quantity == Decimal("0.005")
    assert result.limiting_rule == "per_trade"


def test_symbol_room_can_be_tightest(default_inputs):
    default_inputs["current_symbol_value"] = Decimal("1900")
    assert size_buy(**default_inputs).limiting_rule == "max_symbol"


def test_gross_room_can_be_tightest(default_inputs):
    default_inputs["current_gross_value"] = Decimal("7900")
    assert size_buy(**default_inputs).limiting_rule == "max_gross"


def test_reserve_room_can_be_tightest(default_inputs):
    default_inputs["free_usdt"] = Decimal("2100")
    assert size_buy(**default_inputs).limiting_rule == "usdt_reserve"


def test_mainnet_cap_can_be_tightest(default_inputs):
    default_inputs["environment"] = "mainnet"
    assert size_buy(**default_inputs).limiting_rule == "mainnet_cap"


def test_min_notional_and_invalid_stop_are_blocked(default_inputs):
    default_inputs["stop"] = default_inputs["entry"]
    with pytest.raises(ValueError, match="stop"):
        size_buy(**default_inputs)
```

Run: `uv run pytest tests/test_risk.py -v`

Expected: FAIL because `crypto_desk.risk` does not exist.

- [ ] **Step 2: Implement the exact sizing formula**

Compute USDT notional rooms:

```python
risk_notional = risk.per_trade * nav_usdt * entry / (entry - stop)
symbol_room = risk.max_symbol * nav_usdt - current_symbol_value
gross_room = risk.max_gross * nav_usdt - current_gross_value
reserve_room = free_usdt - risk.min_usdt_reserve * nav_usdt
environment_room = (
    mainnet_order_cap_usdt if environment == "mainnet" else reserve_room
)
notional = min(
    risk_notional,
    symbol_room,
    gross_room,
    reserve_room,
    environment_room,
)
quantity = round_down(notional / entry, rules.step_size)
```

Then enforce `quantity >= min_qty`, `quantity * entry >= min_notional`, valid tick-size rounding, `stop < entry < target`, and final exposure limits after rounding. Mainnet always applies the 25 USDT cap while `completed_mainnet_chains() < 20`.

- [ ] **Step 3: Implement long-only sell sizing**

`EXIT` sells the entire free base-asset quantity rounded down to `step_size`. `REDUCE` sells 50% rounded down. Reject a sell below `min_qty`, below `minNotional`, or above the free base balance. Never use locked balances as sellable quantity.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
uv run pytest tests/test_risk.py -v
uv run ruff check src/crypto_desk/risk.py tests/test_risk.py
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/risk.py tests/test_risk.py
git commit -m "feat: add deterministic crypto risk sizing"
```

---

### Task 7: Binance Spot Testnet/Mainnet broker adapter

**Files:**
- Create: `src/crypto_desk/broker.py`
- Create: `tests/test_broker.py`
- Create: `tests/fixtures/binance_account.json`
- Create: `tests/fixtures/binance_otoco_response.json`
- Create: `tests/fixtures/binance_order_list_status.json`

**Interfaces:**
- Produces: `BinanceSpotBroker.account_snapshot`, `symbol_rules`, `latest_quote`, `place_entry_otoco`, `cancel_order_list`, `place_exit_fok`, `place_protection_oco`, and `order_chain`.
- All request quantities/prices are decimal strings, never JSON numbers.

- [ ] **Step 1: Write failing endpoint and credential-isolation tests**

```python
import pytest

from crypto_desk.broker import BinanceSpotBroker


def test_testnet_and_mainnet_use_fixed_urls(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "test-secret")
    assert BinanceSpotBroker("testnet").base_url == "https://testnet.binance.vision"

    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "live-key")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET", "live-secret")
    assert BinanceSpotBroker("mainnet").base_url == "https://api.binance.com"


def test_missing_environment_specific_key_is_rejected(monkeypatch):
    monkeypatch.delenv("BINANCE_MAINNET_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MAINNET"):
        BinanceSpotBroker("mainnet")
```

Run: `uv run pytest tests/test_broker.py -v`

Expected: FAIL because `crypto_desk.broker` does not exist.

- [ ] **Step 2: Wrap the official Spot SDK behind a narrow adapter**

Instantiate `binance-sdk-spot==10.0.0` with the fixed environment URL. Do not expose the SDK client outside `broker.py`. The module must not import Margin, Futures, Wallet, Convert, or withdrawal SDKs.

Convert account balances into USDT NAV using current Spot bid/ask mid-prices. Include free and locked balances separately. Ignore zero balances, but preserve BNB used for fees in NAV.

- [ ] **Step 3: Implement native OTOCO entry with FOK**

Submit:

```text
workingSide=BUY
workingType=LIMIT
workingTimeInForce=FOK
workingQuantity={ticket.quantity}
workingPrice={ticket.limit_price}
pendingSide=SELL
pendingAboveType=LIMIT_MAKER
pendingAbovePrice={ticket.target_price}
pendingBelowType=STOP_LOSS
pendingBelowStopPrice={ticket.stop_price}
listClientOrderId={environment-specific ticket id}
```

`listClientOrderId`, working ID, target ID, and stop ID are deterministic derivatives of the ticket UUID and fit Binance length/character limits. Because the working order is FOK, it either fills completely and activates the OCO children or expires without opening a partial position.

- [ ] **Step 4: Implement reduction/exit protection replacement**

For an approved `REDUCE` or `EXIT`:

1. Reconcile the current protective order list.
2. Cancel that list and confirm cancellation.
3. Submit a `LIMIT FOK` sell for the approved quantity.
4. If the sell expires, recreate protection for the original quantity.
5. If `REDUCE` fills, create a new OCO for the remaining free base balance.
6. If `EXIT` fills, verify free base balance is below `minQty`.
7. If any status is unknown, persist `RECONCILE_REQUIRED`, alert, and do not submit another order.

- [ ] **Step 5: Run mock-adapter tests and commit**

Run:

```bash
uv run pytest tests/test_broker.py -v
uv run ruff check src/crypto_desk/broker.py tests/test_broker.py
```

Expected: PASS without real credentials.

Commit:

```bash
git add src/crypto_desk/broker.py tests/test_broker.py tests/fixtures
git commit -m "feat: add guarded Binance Spot broker"
```

---

### Task 8: Approval gates, Mainnet code, execution, and reconciliation

**Files:**
- Create: `src/crypto_desk/execution.py`
- Create: `tests/test_execution.py`

**Interfaces:**
- Produces: `confirmation_code`, `verify_confirmation_code`, `ExecutionService.approve`, `reject`, and `reconcile`.
- Consumes: `Store`, `BinanceSpotBroker`, `TradeTicket`, `Settings`, and latest public quote.

- [ ] **Step 1: Write failing three-gate Mainnet tests**

```python
import pytest

from crypto_desk.execution import ExecutionService, confirmation_code


@pytest.mark.parametrize(
    "environment,live_enabled,code,reason",
    [
        ("testnet", True, "123456", "environment"),
        ("mainnet", False, "123456", "disabled"),
        ("mainnet", True, "000000", "confirmation"),
    ],
)
def test_mainnet_requires_all_three_gates(
    service_factory, environment, live_enabled, code, reason
):
    service = service_factory(environment=environment, live_enabled=live_enabled)
    with pytest.raises(ValueError, match=reason):
        service.approve("ticket-1", actor="owner", channel="telegram", code=code)


def test_confirmation_code_is_ticket_bound_and_five_minute_scoped(secret, now):
    first = confirmation_code(secret, "ticket-1", now)
    assert first != confirmation_code(secret, "ticket-2", now)
    assert first != confirmation_code(secret, "ticket-1", now + timedelta(minutes=5))
```

Run: `uv run pytest tests/test_execution.py -v`

Expected: FAIL because `crypto_desk.execution` does not exist.

- [ ] **Step 2: Implement the local confirmation code**

Use:

```python
bucket = int(now.timestamp()) // 300
digest = hmac.new(
    secret.encode(),
    f"{ticket_id}:{bucket}".encode(),
    hashlib.sha256,
).digest()
code = f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
```

Verification accepts only the current bucket, uses `hmac.compare_digest`, requires `LIVE_CONFIRMATION_SECRET`, and never logs the code or secret. `desk live-code TICKET_ID` is local CLI only; the Hermes skill must not expose code generation.

- [ ] **Step 3: Implement pre-submit checks**

Immediately before submission:

```text
ticket is PENDING and less than 30 minutes old
actor is in telegram_allowlist; local approval is forbidden for Mainnet
ticket environment equals BINANCE_ENV
symbol is allowlisted and currently TRADING
credentials match the selected environment
latest price deviation from ticket limit <= 0.5%
account snapshot, balances, symbol filters, and risk limits still pass
Mainnet notional <= 25 USDT while completed chain count < 20
no submission exists for the ticket or its client order ID
Mainnet confirmation code is valid
```

Testnet submission additionally requires `TESTNET_EXECUTION_ENABLED=1`. Approval with execution disabled records `APPROVED_DRY_RUN` and does not call the broker.

- [ ] **Step 4: Implement unknown-state reconciliation**

On transport timeout or ambiguous Binance response:

1. Persist `SUBMISSION_UNKNOWN`.
2. Query by `listClientOrderId`.
3. If found, persist the returned order chain.
4. If not found, persist `RECONCILE_REQUIRED`.
5. Never call `place_entry_otoco` again for that ticket automatically.

Terminal states are `FILLED`, `EXPIRED`, `CANCELED`, `REJECTED`, and `FAILED_SAFE`. Only reconciled `FILLED`, `EXPIRED`, or `CANCELED` Mainnet chains count toward the initial 20-chain gate.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
uv run pytest tests/test_execution.py -v
uv run ruff check src/crypto_desk/execution.py tests/test_execution.py
```

Expected: PASS.

Commit:

```bash
git add src/crypto_desk/execution.py tests/test_execution.py
git commit -m "feat: enforce crypto execution approval gates"
```

---

### Task 9: Orchestration, reports, health, reflection, and CLI

**Files:**
- Create: `src/crypto_desk/service.py`
- Create: `src/crypto_desk/cli.py`
- Create: `tests/test_service_cli.py`

**Interfaces:**
- Produces public commands: `doctor`, `sync`, `screen`, `analyze`, `daily`, `health`, `tickets`, `approve`, `reject`, `orders`, `live-code`, and `reflections`.
- `--json` prints one JSON value to stdout; human mode prints Vietnamese Markdown/text.

- [ ] **Step 1: Write failing CLI surface tests**

```python
from typer.testing import CliRunner

from crypto_desk.cli import app


def test_public_commands_exist():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "doctor", "sync", "screen", "analyze", "daily", "health",
        "tickets", "approve", "reject", "orders", "live-code", "reflections"
    ):
        assert command in result.stdout


def test_analyze_rejects_symbol_outside_allowlist(config_path):
    result = CliRunner().invoke(app, ["--config", str(config_path), "analyze", "DOGEUSDT"])
    assert result.exit_code != 0
    assert "allowlist" in result.stdout
```

Run: `uv run pytest tests/test_service_cli.py -v`

Expected: FAIL because the new CLI is not implemented.

- [ ] **Step 2: Implement analysis and artifact orchestration**

For each run create:

```text
artifacts/crypto/YYYY-MM-DD/{run_id}/evidence.json
artifacts/crypto/YYYY-MM-DD/{run_id}/analysts.json
artifacts/crypto/YYYY-MM-DD/{run_id}/decision.json
artifacts/crypto/YYYY-MM-DD/{run_id}/report.md
```

The Markdown report contains decision, conviction, entry/stop/target, technical/liquidity/news/derivatives reports, two debate rounds, manager rationale, evidence IDs, timestamps, environment, and a prominent `NO_TRADE` reason when blocked.

- [ ] **Step 3: Implement UTC daily and 15-minute health buckets**

Daily idempotency bucket: `YYYY-MM-DD`. At 00:15 UTC, run screen and full committee for passing symbols. `daily --catch-up` runs the most recent missing UTC day once.

Health bucket: `YYYY-MM-DDTHH:{00|15|30|45}Z`. Every health run:

```text
syncs balances/open orders
reconciles submitted/unknown order chains
detects stop breach, missing protection, stale evidence, stale thesis over five UTC days
checks 20% per coin, 80% gross, and 20% free-USDT reserve
alerts on API clock drift, rate limiting, symbol suspension, or filter changes
runs a full committee only for alerted symbols or stale theses
never creates or submits a new ticket without a fresh user approval
```

- [ ] **Step 4: Implement 20-day reflection**

Evaluate a research run after 20 complete UTC daily candles. For altcoins, benchmark against `BTCUSDT`; for `BTCUSDT`, report raw return and benchmark alpha as zero. Store return, maximum adverse excursion, maximum favorable excursion, benchmark return, and decision outcome. Reflections are immutable committee context and cannot modify prompts, code, settings, or skills.

- [ ] **Step 5: Implement `doctor` checks**

`desk doctor` reports:

```text
Python and pinned package versions
OpenAI key and one structured-output smoke request when --online is supplied
Binance public Spot/Futures endpoints
CoinGecko and configured RSS feeds
selected execution environment
selected-environment credentials
Testnet/Mainnet execution flags
Mainnet confirmation secret presence
Telegram token and allowlist
symbol TRADING/OCO/OTO/filter capabilities
server time drift
completed Mainnet chain count and active 25 USDT cap
Hermes version and cron jobs
```

It must print secret presence only, never values.

- [ ] **Step 6: Run CLI tests and commit**

Run:

```bash
uv run pytest tests/test_service_cli.py -v
uv run desk --help
uv run desk --json doctor
uv run ruff check src/crypto_desk tests
```

Expected: CLI help succeeds; offline tests pass; `doctor` may exit non-zero only for missing local credentials/configuration and must identify each missing item.

Commit:

```bash
git add src/crypto_desk/service.py src/crypto_desk/cli.py tests/test_service_cli.py
git commit -m "feat: expose crypto desk workflows"
```

---

### Task 10: Hermes schedules, documentation, and equity-code retirement

**Files:**
- Create: `hermes/crypto-desk/SKILL.md`
- Create: `scripts/daily.sh`
- Modify: `scripts/health.sh`
- Modify: `scripts/desk`
- Modify: `services/openbb-mcp.service`
- Modify: `README.md`
- Delete: `hermes/investment-desk/SKILL.md`
- Delete: `src/investment_desk/__init__.py`
- Delete: `src/investment_desk/core.py`
- Delete: `src/investment_desk/integrations.py`
- Delete: `src/investment_desk/cli.py`
- Delete: `tests/test_desk.py`

**Interfaces:**
- Hermes invokes only the stable CLI; it never receives Binance or OpenAI secrets.

- [ ] **Step 1: Update wrapper scripts**

`scripts/health.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$(dirname "$0")/desk" --json health --due
```

`scripts/daily.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$(dirname "$0")/desk" --json daily --due
```

Both scripts must emit nothing when their idempotency bucket is already complete.

- [ ] **Step 2: Replace the Hermes skill contract**

The skill exposes:

```text
/crypto-desk doctor
/crypto-desk sync
/crypto-desk screen
/crypto-desk analyze SYMBOL
/crypto-desk tickets
/crypto-desk approve TICKET_ID [CODE]
/crypto-desk reject TICKET_ID
/crypto-desk orders
```

It refuses symbols outside the allowlist. It never calls `live-code`, never changes `BINANCE_ENV`, and never sets execution flags.

- [ ] **Step 3: Install exact Hermes cron jobs**

Link scripts under `~/.hermes/scripts/` and create:

```bash
hermes cron create "*/15 * * * *" --no-agent \
  --script crypto-desk-health.sh \
  --deliver telegram --name crypto-desk-health

hermes cron create "15 0 * * *" --no-agent \
  --script crypto-desk-daily.sh \
  --deliver telegram --name crypto-desk-daily
```

Verify:

```bash
hermes cron list
```

Expected: exactly one enabled health job and one enabled daily job; remove or disable the obsolete investment-desk job if present.

- [ ] **Step 4: Keep OpenBB MCP optional and outside the critical path**

Change the standalone service category list from:

```text
equity,news,economy
```

to:

```text
crypto,news,economy
```

Restart and verify:

```bash
systemctl --user daemon-reload
systemctl --user restart openbb-mcp
hermes mcp test openbb
```

Expected: the service is active and Hermes discovers its dynamic tools. A Crypto Desk command must still work when this service is stopped.

- [ ] **Step 5: Rewrite README and remove equity implementation**

README must document:

```text
Binance Spot Testnet account and key creation
separate Mainnet API key with trading only, withdrawal disabled, and IP restriction
CoinGecko key and RSS configuration
three Mainnet gates and local live-code flow
25 USDT initial Mainnet cap
no Margin/Futures execution
Testnet smoke sequence
reconcile-required recovery
Hermes cron installation
rollback by selecting testnet and setting both execution flags to zero
```

Delete the old package and tests only after Tasks 1–9 pass. Preserve `data/desk.sqlite3`, old artifacts, local configuration backups, and the optional OpenBB MCP service for interactive Hermes lookup; OpenBB is not part of the Crypto Desk decision or execution path.

- [ ] **Step 6: Run the complete offline suite and commit**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
python -m compileall -q src
rg -n "IBKR|TradingAgents|yfinance|exchange_calendars|PAPER_EXECUTION" \
  src tests pyproject.toml README.md config.example.yaml .env.example
```

Expected: all tests pass; lint/format/compile/lock checks pass; the final `rg` returns no matches.

Commit:

```bash
git add README.md pyproject.toml uv.lock config.example.yaml .env.example \
  hermes scripts services/openbb-mcp.service src tests
git commit -m "refactor: replace investment desk with crypto desk"
```

---

### Task 11: Binance Testnet E2E and Mainnet canary gate

**Files:**
- Create: `docs/runbooks/binance-testnet-e2e.md`
- Create: `docs/runbooks/binance-mainnet-canary.md`
- Modify: `README.md`

**Interfaces:**
- Produces an auditable acceptance record; does not enable Mainnet automatically.

- [ ] **Step 1: Run Testnet read-only smoke**

With `BINANCE_ENV=testnet` and both execution flags `0`, run:

```bash
uv run desk --json doctor --online
uv run desk --json sync
uv run desk --json screen
uv run desk --json analyze BTCUSDT
```

Expected:

```text
doctor confirms Testnet endpoint and rejects Mainnet submission
sync returns virtual balances
screen processes only the four allowlisted symbols
analyze writes all four artifact files
execution remains dry-run
```

- [ ] **Step 2: Run one Testnet OTOCO lifecycle**

Set `TESTNET_EXECUTION_ENABLED=1`, create a BTCUSDT ticket whose notional satisfies Testnet filters, approve it through the allowlisted Telegram user, and verify:

```text
one listClientOrderId exists
working order is LIMIT FOK
filled entry activates one take-profit and one stop-loss child
expired entry opens no partial position
orders --reconcile is idempotent
restarting WSL does not submit a duplicate chain
cancel and reconcile reach a terminal state
```

Record redacted order IDs, timestamps, and terminal states in the Testnet runbook.

- [ ] **Step 3: Exercise failure paths on Testnet**

Verify:

```text
wrong Telegram user
expired ticket
wrong confirmation code
quote moves over 0.5%
symbol filter changes
HTTP timeout with an order found by client ID
HTTP timeout with no order found
429 backoff without retry storm
unknown status produces RECONCILE_REQUIRED
duplicate approval never submits again
```

- [ ] **Step 4: Prove Mainnet remains blocked**

Run the Mainnet matrix with a fake broker:

```text
environment=testnet, live flag=1, valid code -> blocked
environment=mainnet, live flag=0, valid code -> blocked
environment=mainnet, live flag=1, invalid code -> blocked
environment=mainnet, live flag=1, local actor -> blocked
environment=mainnet, live flag=1, valid Telegram approval, notional=25.01 -> blocked
```

Expected: zero calls to the real Mainnet submission method.

- [ ] **Step 5: Perform a separately authorized Mainnet canary**

This step requires a new explicit user instruction after Testnet evidence is reviewed. When authorized:

1. Confirm Mainnet API withdrawal permission is disabled and IP restriction is enabled.
2. Set `BINANCE_ENV=mainnet` and `LIVE_EXECUTION_ENABLED=1`.
3. Generate the five-minute code locally with `desk live-code`.
4. Submit one allowlisted order chain at or below 25 USDT.
5. Reconcile it to a terminal state.
6. Return immediately to `BINANCE_ENV=testnet` and `LIVE_EXECUTION_ENABLED=0`.
7. Review the redacted audit trail before authorizing any further Mainnet order.

- [ ] **Step 6: Final verification and commit**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run desk --json doctor --online
hermes cron list
```

Expected: all automated checks pass; doctor is green for the selected environment; both cron jobs exist; Testnet E2E evidence is recorded; Mainnet remains disabled unless Step 5 received separate explicit authorization.

Commit:

```bash
git add docs/runbooks README.md
git commit -m "docs: record Binance testnet and mainnet runbooks"
```

---

## Acceptance Checklist

- [ ] The package and CLI contain no IBKR, TradingAgents, yfinance, equity-calendar, sector, or whole-share assumptions.
- [ ] Only the four configured USDT pairs can reach research or ticket creation.
- [ ] A validated snapshot includes Spot, news, funding, open interest, CoinGecko comparison, source timestamps, and hashes.
- [ ] Invalid or stale evidence always produces `NO_TRADE` without inventing values.
- [ ] The custom committee runs four specialists, two bull/bear rounds, and one manager with strict schemas.
- [ ] Risk sizing passes exact 0.5%, 20%, 80%, 20%-reserve, filter, and 25-USDT-cap boundaries.
- [ ] Testnet and Mainnet credentials cannot cross environments.
- [ ] No Mainnet order can be submitted without all three agreed gates.
- [ ] One ticket produces at most one environment-specific order chain.
- [ ] Unknown submission states require reconciliation and are never blindly retried.
- [ ] Health runs once per 15-minute UTC bucket and daily analysis once per UTC day.
- [ ] Telegram approval, Testnet OTOCO, cancel, terminal reconcile, restart idempotency, and failure paths have E2E evidence.
- [ ] Mainnet stays disabled after implementation and requires a separate user-authorized canary.

---
name: crypto-desk
description: Use when a Telegram user asks Hermes to inspect, research, approve, reject, or reconcile the personal Binance Spot Crypto Desk.
---

# Crypto Desk

Translate the public Telegram command to the matching local `desk --json`
invocation. Return a concise Vietnamese summary of the JSON result. Do not expose
environment variables, credentials, confirmation secrets, or raw exception bodies.

## Public commands

| Telegram command | CLI invocation |
|---|---|
| `/crypto-desk doctor` | `desk --json doctor` |
| `/crypto-desk sync` | `desk --json sync` |
| `/crypto-desk screen` | `desk --json screen` |
| `/crypto-desk analyze SYMBOL` | `desk --json analyze SYMBOL` |
| `/crypto-desk tickets` | `desk --json tickets` |
| `/crypto-desk approve TICKET_ID [CODE]` | `desk --json approve TICKET_ID --actor TELEGRAM_USER_ID --channel telegram [--code CODE]` |
| `/crypto-desk reject TICKET_ID` | `desk --json reject TICKET_ID --actor TELEGRAM_USER_ID --channel telegram` |
| `/crypto-desk orders` | `desk --json orders` |

Obtain `TELEGRAM_USER_ID` from authenticated Telegram update metadata. Never take
it from a positional command argument. In an approve command, the optional second
argument is always the five-minute confirmation `CODE`, never a user ID.

## Mandatory guards

- Accept only `BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT`. Refuse any other symbol before
  calling the CLI.
- Expose only the public commands in the table. Daily and health are scheduler-only.
- Never run `desk live-code`, generate a confirmation code, or reuse a code.
- Never change `BINANCE_ENV`, execution flags, configuration, or environment
  variables.
- Never add arbitrary CLI flags supplied by the user.
- Never claim an order was submitted unless the JSON result confirms it.
- On an unknown submission state, instruct the user to inspect orders and wait for
  reconciliation. Never retry approval or submission automatically.
- Treat all output as decision support, not financial advice.

---
name: investment-desk
description: Analyze watchlists and manage human-approved IBKR Paper trade tickets.
---

# Investment Desk

The global `desk` wrapper finds the project and its config. Always pass `--json` when
interpreting output.

- Health: `desk --json health --due`
- Catch up after restart: `desk --json health --catch-up`
- Screen: `desk --json screen`
- Analyze: `desk --json analyze TICKER`
- List tickets: `desk --json tickets`
- Evaluate/list reflections: `desk --json reflections --evaluate`
- Approve: `desk --json approve TICKET_ID --user TELEGRAM_USER_ID`
- Reject: `desk --json reject TICKET_ID --user TELEGRAM_USER_ID`

Never set `PAPER_EXECUTION_ENABLED`, modify risk limits, edit this skill, or retry an uncertain
broker submission. Report `NO_TRADE`, validation failures, and broker errors verbatim. Never
describe a paper fill as a live fill or as financial advice.

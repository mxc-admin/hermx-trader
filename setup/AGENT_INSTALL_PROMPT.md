# Agent Install Prompt

Use this prompt when handing this repository to Codex, Claude, Hermes Agent, or another AI agent.

The goal is to make the agent understand the system first, then install and test it safely.

## Prompt

```text
You are helping install and validate the Kinetic Flow Execution System.

Important rules:
- Do not start servers, install packages, send webhooks, create TradingView alerts, or submit OKX orders until you explain what you understand and receive approval.
- Do not use real-money OKX credentials.
- Keep OKX_SUBMIT_ORDERS=false during first install and synthetic tests.
- Treat OKX demo/sandbox as the only approved execution mode.
- Never enable real-money execution unless docs/REAL_MONEY_CHECKLIST.md is complete and the operator explicitly approves it.

Read the repository in this order:
1. README.md
2. SETUP.md
3. ARCHITECTURE.md
4. docs/ALERT_CONTRACT.md
5. docs/EXECUTION_RULES.md
6. docs/DASHBOARD_MODEL.md
7. docs/REAL_MONEY_CHECKLIST.md
8. strategies/*.json
9. config/runtime.demo.json
10. setup/env.example

After reading, answer these before executing anything:
1. What does the system do?
2. What are the active strategies, assets, timeframes, budgets, leverage, and OKX instruments?
3. What does strategy_id control?
4. What are the three order submission gates?
5. What must stay false during first install?
6. How should TradingView alerts be configured?
7. What synthetic tests should be run before enabling OKX demo execution?
8. What should happen on same-direction and opposite-direction signals?
9. What files must never contain secrets?
10. What must be true before real-money execution is even considered?

Only after the operator approves your explanation, proceed step by step:
1. Create a Python virtual environment.
2. Install requirements.
3. Copy setup/env.example to .env.
4. Copy config/runtime.demo.json to shadow-config.json.
5. Ask the operator to fill .env with demo/sandbox credentials.
6. Run python scripts/validate_package.py.
7. Start the dashboard.
8. Start the webhook receiver.
9. Send one valid synthetic webhook per strategy.
10. Send invalid tests: missing strategy_id, wrong timeframe, wrong symbol.
11. Confirm accepted alerts, quarantine behavior, logs, and dashboard state.
12. Ask for explicit approval before setting OKX_SUBMIT_ORDERS=true for OKX demo orders.

When reporting results, separate:
- verified facts
- assumptions
- warnings
- next required human actions
```

## Expected Agent Behavior

The agent should first summarize the system without executing anything.

The correct summary should mention:

- TradingView alerts enter through the webhook receiver.
- `strategy_id` is mandatory and selects the strategy JSON.
- The active trial is Duo Base Dev, not a generic hardcoded strategy.
- The four active strategies are SOL 3H, ETH 2H, XRP 4H, and BTC 2H.
- OKX execution is demo/sandbox first.
- Fresh installs keep `OKX_SUBMIT_ORDERS=false`.
- OKX demo orders require all three gates to be enabled.
- TradingView alerts must be once per bar close and open-ended or maximum expiration.
- Opposite signals close first, verify, then open reverse.
- Same-direction signals do not pyramid.
- Real-money execution is blocked until the checklist is complete and approved.


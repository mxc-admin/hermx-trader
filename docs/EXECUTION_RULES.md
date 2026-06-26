# Execution Rules

This file explains what the system does when a valid alert arrives.

## Direction Mapping

| Alert Side | Target Position |
|---|---|
| `buy` | Long |
| `sell` | Short |

## Core Position Logic

### If Flat

```text
BUY  -> open long
SELL -> open short
```

### If Already Same Direction

```text
LONG + BUY   -> no pyramid
SHORT + SELL -> no pyramid
```

The system should log the event, but it should not add more size.

### If Opposite Direction

```text
LONG + SELL:
  close long
  verify flat
  open short

SHORT + BUY:
  close short
  verify flat
  open long
```

## Close-First Rule

The system must close the existing position before opening the reverse.

Correct sequence:

```text
1. Submit close-position or reduce-only close order.
2. Verify position is closed or reduced to zero.
3. Submit new open order.
4. Verify new position exists.
5. Log both legs.
```

## No-Pyramid Rule

Same-direction alerts do not increase the current position.

This prevents duplicate TradingView alerts from increasing risk.

## Execution Failure Handling

| Failure | Action |
|---|---|
| Close fails | Do not open reverse |
| Close succeeds but verify fails | Pause strategy and alert operator |
| Open fails | Stay flat and alert operator |
| API readback fails | Mark execution uncertain and alert operator |
| Duplicate alert | Log duplicate, do not execute |

## Demo vs Live

In demo mode, orders go to OKX sandbox/demo only.

Live mode must require a separate runtime profile and explicit operator approval.

## Order Submission Gates

Order submission is allowed only when every gate below is enabled:

| Gate | Location | Required value |
|---|---|---|
| Master safety switch | `.env` -> `OKX_SUBMIT_ORDERS` | `true` |
| Runtime permission | `shadow-config.json` -> `execution.submit_orders` | `true` |
| Strategy permission | `strategies/*.json` -> `okx_submit_orders` | `true` |

Fresh installs should keep the master safety switch set to `false` until validation and synthetic webhook tests are complete.

This means the runtime and strategy files can be ready for demo execution while the local operator still blocks order submission from `.env`.

# Skill: OKX Demo Execution

Use this when testing OKX demo/sandbox execution.

## Rules

- Demo first.
- Isolated margin.
- 2x leverage unless the strategy says otherwise.
- Close first, verify flat, then open reverse.
- No same-direction pyramiding.

## Steps

1. Load demo credentials from secure env.
2. Confirm sandbox/demo mode.
3. Confirm instrument exists.
4. Confirm margin mode is isolated.
5. Confirm leverage is 2x.
6. Read open position.
7. If reverse is needed, close current position.
8. Verify position is flat.
9. Open new position.
10. Read order fill.
11. Read position after order.
12. Log order ID, fill price, fee, slippage, and PnL.

## Failure Handling

- If close fails, do not open reverse.
- If verify fails, pause strategy.
- If open fails, stay flat and alert operator.
- If OKX readback fails, mark state uncertain.


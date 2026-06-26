# Skill: Duo Base Dev Optimization Workflow

Use this when researching optimized parameters.

## Scope

Optimization is research until a strategy file is updated and validated.

## Required Inputs

- asset
- timeframe
- indicator version
- chart type
- search space
- windows to test
- fee assumption

## Current Optimized Trial Parameters

| Asset | TF | Upper | Lower |
|---|---:|---:|---:|
| SOLUSDT | 3h | 1.05 | 0.95 |
| ETHUSDT | 2h | 1.40 | 0.95 |
| XRPUSDT | 4h | 1.20 | 0.95 |
| BTCUSDT | 2h | 1.40 | 0.95 |

## Research Rules

- Confirm indicator version before run.
- Confirm available inputs before run.
- Use the current founder tooling when available.
- Validate signal counts against the chart.
- Report results by window.
- Do not promote params from a failed chart run.

## Promotion Rules

Before updating a strategy JSON:

1. Confirm chart indicator is current.
2. Confirm signal count is plausible.
3. Confirm results across multiple windows.
4. Confirm the strategy still has enough trades.
5. Update validation source.
6. Revalidate schema.


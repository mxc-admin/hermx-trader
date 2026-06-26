# Optimization Protocol

Optimization finds candidate parameters. It does not directly approve live trading.

## Current Trial Candidates

| Asset | Timeframe | Upper | Lower |
|---|---:|---:|---:|
| SOLUSDT | 3H | 1.05 | 0.95 |
| ETHUSDT | 2H | 1.40 | 0.95 |
| XRPUSDT | 4H | 1.20 | 0.95 |
| BTCUSDT | 2H | 1.40 | 0.95 |

## Validation Requirements

- correct indicator version
- current chart inputs
- Heikin Ashi where required
- plausible signal count
- multiple time windows
- documented fee assumption
- documented validation source

## Promotion Path

```text
research result -> candidate params -> strategy JSON update -> schema validation -> synthetic alert -> demo trial
```


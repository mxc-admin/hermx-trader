# Skill: Emergency Stop

Use this when execution must be paused immediately.

## Stop Levels

### Level 1: Pause New Alerts

- disable execution in runtime config
- keep dashboard and webhook alive
- alerts are logged but no orders are sent

### Level 2: Stop Webhook Processing

- stop webhook service
- dashboard may remain online

### Level 3: Flatten Exchange

- close open OKX positions
- verify flat
- disable order submission

## Required Log

Every emergency stop must log:

- time
- operator
- reason
- strategies affected
- exchange position before
- exchange position after


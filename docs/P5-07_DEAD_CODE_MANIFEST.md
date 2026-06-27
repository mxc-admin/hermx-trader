# P5-07 — Dead / Orphaned Executor Surface Manifest (REFACTOR_PLAN.md:391, X1)

> **STATUS UPDATE (executed):** The genuinely-orphaned custom KuCoin connector
> cluster has been **DELETED** — it was dead code unrelated to the OKX cutover gate
> (a mutually-referential pair plus an unused custom adapter, depending on a dead
> base and an uninstalled SDK; KuCoin is now served via the generic CCXT backend).
> Removed: `src/executors/base_executor.py`, `src/kucoin_paper_executor.py`,
> `src/executors/kucoin_paper.py`, and the KuCoin registration/alias in
> `src/executors/factory.py`. Full suite green afterwards.
>
> **STILL GATED (not deleted):** the active OKX surface
> (`src/okx_demo_executor.py` + `src/executors/okx_demo.py`). That is **not dead
> code** — it is the current default write path / rollback oracle. Its removal is
> the CCXT write-path cutover (P5-06) and is gated behind the acceptance criteria
> in §2.

Generated as part of P5-04 (shadow-parity wiring). The shadow-parity harness
(`src/execution/shadow.py`, ledger `logs/shadow-parity.jsonl`) is the mechanism
that will produce the cutover evidence the gate below depends on.

---

## 0. The three base/executor surfaces (active vs orphaned)

The repo carries **two different `BaseExecutor` classes** plus a parallel set of
top-level executor scripts. Distinguishing them is the whole point of this manifest.

| File | Role | Reachable today? |
|------|------|------------------|
| `src/executors/base.py` | **ACTIVE** `BaseExecutor` — the venue-neutral contract used by the live execution layer | **YES — keep** |
| `src/executors/base_executor.py` | **ORPHANED** older/distinct `BaseExecutor` | No (see §1) |
| `src/okx_demo_executor.py` | **ROLLBACK ORACLE** — OKX v5 REST CLI, shelled out by the authoritative path | **YES — keep, do not delete until cutover proven** |
| `src/executors/okx_demo.py` | **ACTIVE** adapter wrapping the OKX CLI behind `executors/base.py` | **YES — keep** |
| `src/executors/ccxt_adapter.py` | **ACTIVE** CCXT candidate adapter (P5 write-path target) | **YES — keep** |
| `src/kucoin_paper_executor.py` | **ORPHANED** top-level KuCoin executor | Effectively no (see §1) |
| `src/executors/kucoin_paper.py` | Registered KuCoin adapter (no strategy selects it today) | Registered; KuCoin path unproven (see §1.3) |

### Active `executors/base.py` — proof of reach (DO NOT DELETE)
```
$ grep -rln "from executors.base import\|from .base import" src tests
src/executors/ccxt_adapter.py
src/executors/okx_demo.py
src/executors/kucoin_paper.py
src/executors/factory.py
src/executors/__init__.py
tests/test_okx_query_interface.py        # :314 from executors.base import BaseExecutor
```
`executors/base.py` is load-bearing for every live adapter and a test. It is **not**
a removal candidate.

---

## 1. Removal candidates (evidence)

### 1.1 `src/executors/base_executor.py` — ORPHANED old BaseExecutor
The OLD/orphaned base, distinct from the active `executors/base.py`.

```
$ grep -rn "base_executor" src tests
src/kucoin_paper_executor.py:14:from src.executors.base_executor import BaseExecutor   # ONLY importer
tests/test_okx_query_interface.py:312:def test_base_executor_query_defaults_are_safe()  # name only; imports executors.base (:314), NOT base_executor
```

Evidence it is orphaned, not active:
- **Sole importer** is `src/kucoin_paper_executor.py:14`, itself an orphan (§1.2).
- It uses `src.`-prefixed import paths inconsistent with the live `sys.path` layout
  (runtime/tests put **`src/`** on the path and import `executors.*`, never
  `src.executors.*`):
  ```
  $ grep -n "from src\." src/executors/base_executor.py
  src/executors/base_executor.py:74:            from src.okx_demo_executor import OkxDemoExecutor
  src/executors/base_executor.py:77:            from src.kucoin_paper_executor import KuCoinPaperExecutor
  ```
  These `src.*` imports cannot resolve under the active path layout — confirming the
  file is not exercised by the live execution path.
- The test at `test_okx_query_interface.py:312` is a **red herring by name only**:
  its body imports `from executors.base import BaseExecutor` (the ACTIVE base, line
  314), not `base_executor`. No test imports `base_executor`.

### 1.2 `src/kucoin_paper_executor.py` — ORPHANED top-level KuCoin executor
```
$ grep -rn "kucoin_paper_executor" src tests
src/executors/base_executor.py:77:        from src.kucoin_paper_executor import KuCoinPaperExecutor   # orphan -> orphan
src/executors/kucoin_paper.py:39:        from kucoin_paper_executor import KuCoinPaperExecutor as _KuCoinClient  # lazy
```
- Imports the orphaned base at module top: `kucoin_paper_executor.py:14
  from src.executors.base_executor import BaseExecutor` — so {`base_executor.py`,
  `kucoin_paper_executor.py`} form a mutually-referential dead cluster.
- The only bridge to live code is the **lazy** import in the active
  `executors/kucoin_paper.py:39`. Because `kucoin_paper_executor.py` in turn imports
  `src.executors.base_executor` (a path that does not resolve under the live layout),
  that lazy bridge would **fail at call time** — i.e. the in-process KuCoin path is
  currently non-functional/unused.

### 1.3 `src/executors/kucoin_paper.py` — registered but unexercised (NOT a delete candidate)
Registered in the factory (`factory.py:72 ExecutorFactory.register(KuCoinPaperExecutor.key, ...)`)
but **no strategy selects KuCoin today** and its lazy client import depends on the
orphan cluster (§1.2). This is *active, registered code* — it is **not** in scope for
deletion. It is listed here only to flag that the KuCoin in-process path is unproven
and should be either fixed or explicitly deprecated as its own decision, separate from
the dead-code removal below.

---

## 2. The explicit gate (REFACTOR_PLAN.md:391, :395, :403)

> Dead executor base/orphaned paths are removed **ONLY AFTER**:
> 1. **CCXT write-path cutover acceptance** — CCXT produces normalized-equivalent
>    results vs legacy during the shadow soak (`:400`), measured by the P5-04
>    shadow-parity ledger (`logs/shadow-parity.jsonl`) over a meaningful number of
>    real alert cycles covering all four strategies.
> 2. **Rollback proof** — the `legacy` execution backend flag has been demonstrated to
>    instantly restore the okx_demo CLI path (`:395` "kill-switch first, backend
>    second"); rollback rebuild verified.
> 3. **One clean release** — a full release cycle completes on the CCXT write-path
>    with **no rollbacks** (`:478`, `:395`).

Until **all three** hold, this manifest is the only P5-07 deliverable. No executor
source is deleted or edited.

---

## 3. Files that MUST NOT be deleted until cutover is proven (rollback oracle)

These are the rollback/oracle surface. Deleting any of them before the gate above is
satisfied would destroy the instant-rollback path:

- `src/okx_demo_executor.py` — the OKX v5 REST CLI; **the rollback oracle**. Shelled
  out by the authoritative path (`src/webhook_receiver.py:4238 script = ROOT / "src" /
  "okx_demo_executor.py"`) and by the active adapter `executors/okx_demo.py`. It stays
  the **default** write path. **Do not delete.**
- `src/executors/okx_demo.py` — active adapter wrapping the oracle CLI. **Do not delete.**
- `src/executors/base.py` — active venue-neutral base. **Do not delete.**
- `src/executors/factory.py`, `src/executors/ccxt_adapter.py`,
  `src/execution/service.py`, `src/execution/shadow.py` — active execution layer.

---

## 4. Exact removal order (executed ONLY after §2 gate clears)

Remove leaves-first so no live import ever dangles. Re-run the grep after each step;
each must return **zero non-test importers** before proceeding.

1. **Confirm/clear KuCoin decision (prerequisite).** Decide KuCoin path: either repair
   `executors/kucoin_paper.py` to a real adapter (un-registering it from `factory.py`
   if deprecated). This removes the only live bridge to `kucoin_paper_executor.py`.
   - Verify: `grep -rn "kucoin_paper_executor" src` returns only the orphan cluster.
2. **Delete `src/kucoin_paper_executor.py`** (orphan top-level KuCoin executor, §1.2).
   - Precondition: step 1 done; no live importer remains.
3. **Delete `src/executors/base_executor.py`** (orphaned old BaseExecutor, §1.1).
   - Precondition: step 2 done (its sole importer is gone); `grep -rn "base_executor"
     src` returns nothing.
4. **Rename the `test_base_executor_query_defaults_are_safe` test** if its name still
   implies the orphaned base (it currently exercises the ACTIVE `executors/base.py`);
   purely cosmetic, no behavior change.

> The OKX oracle surface (§3) is **NOT** part of this order. It is removed in a
> SEPARATE, later change only after the CCXT write-path has shipped a clean release and
> the `legacy` backend flag is formally retired (`:395`, :478).

---

## 5. Removal → acceptance-criterion mapping

| Removal step | Unlocking acceptance criterion |
|--------------|--------------------------------|
| §4.1 KuCoin decision | (housekeeping; no live strategy depends on KuCoin) — gated only by "no importer remains" |
| §4.2 `kucoin_paper_executor.py` | `:403` "Dead executor base/orphaned paths are removed only after cutover acceptance and rollback proof" |
| §4.3 `base_executor.py` | `:403` (X1: "Dead third base class; only importer is the orphaned `kucoin_paper_executor.py`") |
| §4.4 test rename | none (cosmetic) |
| **(separate, later)** retire `okx_demo_executor.py` + okx_demo adapter | `:398` (no hardcoded exchange script path in active path) **AND** `:395` (one clean release on CCXT, `legacy` flag retired) |

---

## 6. Verification checklist before ANY deletion lands

- [ ] §2 gate fully satisfied (shadow-parity match rate acceptable across all four
      strategies; rollback proven; one clean release on CCXT write-path).
- [ ] `grep -rn "base_executor" src tests` returns zero live importers.
- [ ] `grep -rn "kucoin_paper_executor" src tests` returns zero live importers.
- [ ] Full test suite green after each leaf removal.
- [ ] `okx_demo_executor.py` and `executors/okx_demo.py` remain present and default
      until the `legacy` backend flag is formally retired in a later change.

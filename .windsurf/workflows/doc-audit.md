---
description: Repeatable documentation audit — catches dead config refs, skill-count drift, dead links, alert-schema drift, legacy naming, and code/doc sync gaps. Run before a release, after renaming/removing files, or whenever docs and code may have diverged.
---

# /doc-audit — Documentation Audit

Static, read-only audit that compares the docs (`README.md`, `ARCHITECTURE.md`,
`INSTALL.md`, `docs/*.md`, `skills/**`) against the actual repo (source files,
`docker-compose.yml`, `schemas/`). It never mutates anything — it only reports.

## When to run

- Before cutting a release or handing the repo to someone new.
- After renaming, deleting, or moving files (docs go stale silently).
- After adding/removing an operator skill or slash command.
- After changing `schemas/tradingview-alert.schema.json` or the state/Docker model.
- Periodically, as a companion to `/learn` and `/evolve`.

## The script

Copy-paste the whole block into a terminal at the repo root (or save it and run
`bash doc-audit.sh`). It self-locates the repo via `git rev-parse`.

```bash
#!/usr/bin/env bash
# doc-audit.sh — Hermx documentation audit (read-only)
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 2

PASS=0; FAIL=0; WARN=0
pass(){ printf 'PASS  %s\n' "$*"; PASS=$((PASS+1)); }
fail(){ printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn(){ printf 'WARN  %s\n' "$*"; WARN=$((WARN+1)); }
info(){ printf '      %s\n' "$*"; }
sec(){  printf '\n=== %s ===\n' "$*"; }

# All .md files, excluding vendored dirs and this audit doc (it mentions the
# very tokens we grep for, so it would self-trigger).
md_files(){
  find . \( -name node_modules -prune \) -o \( -path './.git' -prune \) -o -name '*.md' -print \
    | grep -v 'workflows/doc-audit.md'
}

# ---------------------------------------------------------------------------
sec "1. Dead config references (shadow-config.json)"
hits=$(grep -rn --include='*.md' 'shadow-config\.json' . 2>/dev/null \
        | grep -v 'workflows/doc-audit.md' \
        | grep -viE 'dead|stale|legacy|removed|deprecated|no-op|obsolete|delet|not reference|nowhere|rejected|removing|zero remaining' || true)
if [ -n "$hits" ]; then
  fail "shadow-config.json referenced in docs without a dead/stale label:"
  printf '%s\n' "$hits" | sed 's/^/      /'
else
  pass "no un-annotated shadow-config.json references in docs"
fi
if [ -e shadow-config.json ]; then
  fail "physical shadow-config.json still exists (should be deleted)"
else
  pass "physical shadow-config.json absent"
fi

# ---------------------------------------------------------------------------
sec "2. Skill-count sync"
skill_count=$(ls skills/hermx-*/SKILL.md 2>/dev/null | wc -l | tr -d ' ')
[ -f skills/emergency-stop.md ] && skill_count=$((skill_count + 1))
info "discovered $skill_count operator skills (skills/hermx-*/SKILL.md + emergency-stop.md)"
for doc in README.md ARCHITECTURE.md INSTALL.md; do
  if [ ! -f "$doc" ]; then warn "$doc missing"; continue; fi
  if grep -q 'skills/hermx-help/SKILL\.md' "$doc"; then
    pass "$doc references skills/hermx-help/SKILL.md (slash-command reference)"
  else
    fail "$doc missing reference to skills/hermx-help/SKILL.md (slash-command reference)"
  fi
  if grep -qE '/hx-(status|positions|close|emergency-stop|help|trace|restart|upgrade)' "$doc"; then
    pass "$doc references at least one slash command"
  else
    fail "$doc references no slash command (expected e.g. /hx-status)"
  fi
done

# ---------------------------------------------------------------------------
sec "3. Ops library reference (hermx_ops.py)"
for doc in ARCHITECTURE.md INSTALL.md; do
  if grep -q 'hermx_ops\.py' "$doc" 2>/dev/null; then
    pass "$doc mentions hermx_ops.py"
  else
    fail "$doc does not mention hermx_ops.py"
  fi
done

# ---------------------------------------------------------------------------
sec "4. Docker / state model (HERMX_DATA_DIR, hermx-state)"
compose_dir=$(grep -oE 'HERMX_DATA_DIR=[^ ]+' docker-compose.yml 2>/dev/null | head -1 | cut -d= -f2)
info "docker-compose.yml HERMX_DATA_DIR=${compose_dir:-<unset>}"
for doc in INSTALL.md ARCHITECTURE.md; do
  for token in HERMX_DATA_DIR hermx-state; do
    if grep -q "$token" "$doc" 2>/dev/null; then
      pass "$doc mentions $token"
    else
      fail "$doc does not mention $token"
    fi
  done
  if [ -n "${compose_dir:-}" ] && grep -qF "$compose_dir" "$doc" 2>/dev/null; then
    pass "$doc references data-dir value $compose_dir (matches compose)"
  else
    warn "$doc does not mention the compose data-dir value ${compose_dir:-/app/data}"
  fi
done

# ---------------------------------------------------------------------------
sec "5. Deploy path (deploy/deploy.sh)"
for doc in INSTALL.md ARCHITECTURE.md; do
  if grep -q 'deploy/deploy\.sh' "$doc" 2>/dev/null; then
    pass "$doc references deploy/deploy.sh"
  else
    fail "$doc does not reference deploy/deploy.sh"
  fi
done
if grep -q 'deploy/deploy\.sh' docs/DOCKER_PACKAGE_PLAN.md 2>/dev/null; then
  pass "docs/DOCKER_PACKAGE_PLAN.md references deploy/deploy.sh"
else
  info "docs/DOCKER_PACKAGE_PLAN.md absent or no ref (optional — covered by INSTALL/ARCHITECTURE)"
fi

# ---------------------------------------------------------------------------
sec "6. Dead links in .md files"
missing_links=$(mktemp)
while IFS= read -r f; do
  d=$(dirname "$f")
  # (a) markdown link targets: ](target)
  grep -oE '\]\([^)]+\)' "$f" 2>/dev/null | sed -E 's/^\]\(//; s/\)$//' \
    | while IFS= read -r t; do
        t="${t%% *}"          # drop optional "title"
        case "$t" in http*|mailto:*|\#*|"") continue ;; esac
        t="${t%%#*}"          # strip anchor fragment
        case "$t" in *"*"*) continue ;; esac   # skip globs
        [ -z "$t" ] && continue
        if [ ! -e "$t" ] && [ ! -e "$d/$t" ] && [ ! -e "$ROOT/$t" ]; then
          printf '%s -> %s\n' "$f" "$t" >> "$missing_links"
        fi
      done
  # (b) backticked docs/ and skills/ paths
  grep -oE '`(docs|skills)/[A-Za-z0-9._/*-]+`' "$f" 2>/dev/null | tr -d '`' \
    | while IFS= read -r t; do
        case "$t" in *"*"*) continue ;; esac
        if [ ! -e "$t" ] && [ ! -e "$ROOT/$t" ]; then
          printf '%s -> %s\n' "$f" "$t" >> "$missing_links"
        fi
      done
done < <(md_files)
if [ -s "$missing_links" ]; then
  fail "$(sort -u "$missing_links" | wc -l | tr -d ' ') dead link/path target(s):"
  sort -u "$missing_links" | sed 's/^/      /'
else
  pass "all markdown link targets and backticked docs/skills paths resolve"
fi
rm -f "$missing_links"

# ---------------------------------------------------------------------------
sec "7. Alert schema drift (3-TRADINGVIEW_ALERTS.md vs schema)"
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY'
import json, re, sys
try:
    schema = json.load(open("schemas/tradingview-alert.schema.json"))
except FileNotFoundError:
    print("      schema file missing"); sys.exit(2)
try:
    contract = open("docs/3-TRADINGVIEW_ALERTS.md").read()
except FileNotFoundError:
    print("      docs/3-TRADINGVIEW_ALERTS.md missing"); sys.exit(2)

enum = schema.get("properties", {}).get("exchange", {}).get("enum", [])
missing_enum = [v for v in enum if v not in contract]
for v in enum:
    print(("      ok    venue '%s'" % v) if v in contract
          else ("      DRIFT venue '%s' in schema but not in contract" % v))

for r in schema.get("required", []):
    if not re.search(r"\b" + re.escape(r) + r"\b", contract):
        print("      note  required field '%s' not literally present in contract" % r)

sys.exit(1 if missing_enum else 0)
PY
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "alert venue enum consistent with 3-TRADINGVIEW_ALERTS.md (details above)"
  elif [ "$rc" -eq 2 ]; then
    fail "alert schema drift check could not run (missing file — see above)"
  else
    fail "alert schema drift: schema venues missing from 3-TRADINGVIEW_ALERTS.md (see DRIFT above)"
  fi
else
  warn "python3 unavailable — skipped alert schema drift check"
fi

# ---------------------------------------------------------------------------
sec "8. Legacy naming carryover (SHADOW_PORT, shadow_config)"
for term in SHADOW_PORT shadow_config; do
  hits=$(grep -rn --include='*.md' "$term" . 2>/dev/null \
          | grep -v 'workflows/doc-audit.md' \
          | grep -viE 'dead|stale|legacy|removed|deprecated|obsolete' || true)
  if [ -n "$hits" ]; then
    warn "legacy token '$term' present in docs without an annotation:"
    printf '%s\n' "$hits" | sed 's/^/      /'
  else
    pass "no un-annotated '$term' in docs"
  fi
done

# ---------------------------------------------------------------------------
sec "9. Code/doc sync (src paths in ARCHITECTURE.md exist)"
if [ -f ARCHITECTURE.md ]; then
  missing_src=$(grep -oE 'src/[A-Za-z0-9_/]+\.py' ARCHITECTURE.md | sort -u \
                 | while IFS= read -r p; do [ -e "$p" ] || echo "$p"; done)
  if [ -n "$missing_src" ]; then
    fail "ARCHITECTURE.md references src files that do not exist:"
    printf '%s\n' "$missing_src" | sed 's/^/      /'
  else
    pass "all src/*.py paths referenced in ARCHITECTURE.md exist"
  fi
else
  warn "ARCHITECTURE.md missing"
fi

# ---------------------------------------------------------------------------
sec "10. Undocumented modules (src/*.py >100 lines not mentioned in ARCHITECTURE.md)"
undoc=""
while IFS= read -r f; do
  lines=$(wc -l < "$f" | tr -d ' ')
  [ "$lines" -le 100 ] && continue
  base=$(basename "$f")
  if ! grep -q "$base" ARCHITECTURE.md 2>/dev/null; then
    undoc="${undoc}${f} (${lines} lines)"$'\n'
  fi
done < <(find src -name '*.py' -type f 2>/dev/null)
if [ -n "$undoc" ]; then
  warn "modules >100 lines with no mention in ARCHITECTURE.md (heuristic):"
  printf '%s' "$undoc" | sed 's/^/      /'
else
  pass "all src/*.py modules >100 lines are mentioned in ARCHITECTURE.md"
fi

# ---------------------------------------------------------------------------
sec "11. README completeness (top-level directories)"
for d in src tests skills config deploy docs scripts dashboard-ui setup; do
  if [ ! -d "$d" ]; then info "$d/ not present in repo — skipped"; continue; fi
  if grep -qE "(^|[^A-Za-z0-9_/-])$d(/|[^A-Za-z0-9_-]|$)" README.md 2>/dev/null; then
    pass "README mentions $d/"
  else
    warn "README does not mention $d/"
  fi
done

# ---------------------------------------------------------------------------
sec "Summary"
printf 'checks: PASS=%d  WARN=%d  FAIL=%d\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: OK (warnings are advisory)"
exit 0
```

## How to read the output

Each check prints one or more status lines:

- **`PASS`** — the documented state matches the repo. Nothing to do.
- **`WARN`** — a heuristic flagged a possible gap (e.g. an undocumented large
  module, a missing README directory mention, a legacy token). Review, but it
  does **not** fail the run. Warnings are advisory.
- **`FAIL`** — a hard inconsistency: a dead reference, a broken link, a doc
  claiming a file that does not exist, or schema/doc drift. Fix these.

Indented lines under a status give the specific offenders (file, line, target).

The final `Summary` block prints the counts and a `RESULT:` line. The script
**exits 0 only when there are zero FAILs** (warnings are allowed); any FAIL
exits non-zero, so it is safe to wire into CI or a pre-release gate.

## Fixing findings

| Check | Where to fix |
|-------|--------------|
| 1 — dead config refs | Remove/relabel `shadow-config.json` in the flagged `.md`; source is `engine-config.json` via `load_engine_config()`. Delete any stray physical `shadow-config.json`. |
| 2 — skill-count sync | Add the `skills/hermx-help/SKILL.md` pointer + a slash-command example to `README.md`, `ARCHITECTURE.md`, `INSTALL.md`. Skills live in `skills/hermx-*/SKILL.md` and `skills/emergency-stop.md`. |
| 3 — ops-lib ref | Mention `hermx_ops.py` in `ARCHITECTURE.md` / `INSTALL.md`. |
| 4 — Docker/state | Ensure `HERMX_DATA_DIR` and `hermx-state` (value `/app/data`) appear in `INSTALL.md` + `ARCHITECTURE.md` and match `docker-compose.yml`. |
| 5 — deploy path | Reference `deploy/deploy.sh` in `INSTALL.md` + `ARCHITECTURE.md` (or `docs/DOCKER_PACKAGE_PLAN.md`). |
| 6 — dead links | Fix the target path or the link in the flagged `.md`. Paths are checked relative to the file, the repo root, and CWD. |
| 7 — schema drift | Reconcile the venue enum / required fields between `docs/3-TRADINGVIEW_ALERTS.md` and `schemas/tradingview-alert.schema.json`. |
| 8 — legacy naming | Remove or annotate `SHADOW_PORT` / `shadow_config` carryover in docs. |
| 9 — code/doc sync | Update the ARCHITECTURE File Reference table to point at real `src/` paths (or restore the file). |
| 10 — undocumented modules | Add a File Reference / description entry to `ARCHITECTURE.md` for the flagged module. |
| 11 — README completeness | Add a mention of the missing top-level directory to `README.md`. |

After fixing, re-run the script until `RESULT: OK`. If a finding turns out to be
a genuine, intentional pattern, record it via `/learn` so future audits and
reviewers understand the exception.

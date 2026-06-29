#!/bin/bash
# verify_install.sh — Full Dev Environment installation state report
# SKIP = already installed | MISS = needs action

PASS=0; MISS=0
ITEMS_TO_INSTALL=()

check() {
  local label="$1"
  local test_cmd="$2"
  if eval "$test_cmd" >/dev/null 2>&1; then
    echo "  SKIP: $label"
    ((PASS++))
  else
    echo "  MISS: $label"
    ((MISS++))
    ITEMS_TO_INSTALL+=("$label")
  fi
}

section_exists() {
  grep -q "^## $2" "$1" 2>/dev/null
}

echo "════════════════════════════════════════════════"
echo "  FULL DEV ENVIRONMENT — INSTALLATION STATE"
echo "════════════════════════════════════════════════"
echo ""

echo "── RTK ──"
check "rtk installed"                          "which rtk"

echo ""
echo "── Directories ──"
check ".windsurf/workflows/"                   "test -d .windsurf/workflows"
check ".windsurf/rules/"                       "test -d .windsurf/rules"
check ".claude/rules/"                         "test -d .claude/rules"
check ".claude/skills/"                        "test -d .claude/skills"
check ".claude/commands/"                      "test -d .claude/commands"

echo ""
echo "── Rules files ──"
check ".windsurf/rules/dev-rules.md"           "test -f .windsurf/rules/dev-rules.md"
check ".windsurf/rules/tool-preferences.md"    "test -f .windsurf/rules/tool-preferences.md"
check ".windsurf/rules/code-quality.md"        "test -f .windsurf/rules/code-quality.md"
check ".claude/rules/dev-rules.md"             "test -f .claude/rules/dev-rules.md"
check ".claude/rules/code-quality.md"          "test -f .claude/rules/code-quality.md"

echo ""
echo "── Rules section integrity ──"
check "dev-rules: ## Before Writing Code"      "section_exists .windsurf/rules/dev-rules.md 'Before Writing Code'"
check "dev-rules: ## Output Management"        "section_exists .windsurf/rules/dev-rules.md 'Output Management'"
check "dev-rules: ## MCP & Secrets"            "section_exists .windsurf/rules/dev-rules.md 'MCP & Secrets'"
check "dev-rules: ## Dual-File Rule"           "section_exists .windsurf/rules/dev-rules.md 'Dual-File Rule'"
check "code-quality: ## Known Patterns"        "section_exists .windsurf/rules/code-quality.md 'Known Patterns'"
check "code-quality: ## Anti-Patterns"         "section_exists .windsurf/rules/code-quality.md 'Anti-Patterns'"
check "tool-prefs: ## Tier 1"                  "section_exists .windsurf/rules/tool-preferences.md 'Tier 1'"
check "tool-prefs: ## Tier 3"                  "section_exists .windsurf/rules/tool-preferences.md 'Tier 3'"

echo ""
echo "── Frontmatter asymmetry ──"
check ".claude/rules/dev-rules.md has YAML"   "head -1 .claude/rules/dev-rules.md | grep -q '^---'"
check ".windsurf/rules/dev-rules.md no YAML"  "! head -1 .windsurf/rules/dev-rules.md | grep -q '^---'"

echo ""
echo "── Hardcoded paths ──"
check "tool-preferences.md: paths replaced"   "! grep -q 'absolute/path/to/your/project' .windsurf/rules/tool-preferences.md"

echo ""
echo "── Workflows ──"
for wf in learn evolve deep-bug git-commit git-checkpoint git-push git-undo; do
  check ".windsurf/workflows/${wf}.md"        "test -f .windsurf/workflows/${wf}.md"
done

echo ""
echo "── CLAUDE.md ──"
check ".claude/CLAUDE.md"                     "test -f .claude/CLAUDE.md"
check ".claude/CLAUDE.md <= 50 lines"         "[ \$(wc -l < .claude/CLAUDE.md 2>/dev/null | tr -d ' ') -le 50 ]"
check ".claude/CLAUDE.md: ## Project"         "section_exists .claude/CLAUDE.md 'Project'"
check ".claude/CLAUDE.md: ## Key Files"       "section_exists .claude/CLAUDE.md 'Key Files'"
check ".claude/CLAUDE.md: ## Rules"           "section_exists .claude/CLAUDE.md 'Rules'"
check ".claude/CLAUDE.md: ## Skills"          "section_exists .claude/CLAUDE.md 'Skills'"

echo ""
echo "── Skills ──"
check "_general/SKILL.md"                     "test -f .claude/skills/_general/SKILL.md"
check "_general/references/"                  "test -d .claude/skills/_general/references"
check "_general: rejected-approaches.md"      "test -f .claude/skills/_general/references/rejected-approaches.md"
check "_general: architecture-decisions.md"   "test -f .claude/skills/_general/references/architecture-decisions.md"
check "_general: constraints.md"              "test -f .claude/skills/_general/references/constraints.md"

echo ""
echo "── Settings ──"
check ".claude/settings.local.json"           "test -f .claude/settings.local.json"
check "settings.local.json: Bash(python"      "grep -q 'Bash(python' .claude/settings.local.json"

echo ""
echo "── MCP security ──"
MCP_CFG="$HOME/Library/Application Support/Windsurf/User/globalStorage/codeium.windsurf/settings/mcp_config.json"
check "mcp_config.json: no embedded secrets"  "! grep -qiE '(api_key|secret|token|password)\s*[:=]\s*[^{][^\"]{8,}' \"$MCP_CFG\""

echo ""
echo "── Git ignore ──"
check ".gitignore: settings.local.json"       "grep -q 'settings.local.json' .gitignore"

echo ""
echo "── Dual-file sync ──"
check "code-quality.md in sync"  \
  "diff <(awk '/^---/{found++; if(found==2){skip=0; next} skip=1; next} skip{next} 1' .claude/rules/code-quality.md) .windsurf/rules/code-quality.md > /dev/null"
check "dev-rules.md in sync"  \
  "diff <(awk '/^---/{found++; if(found==2){skip=0; next} skip=1; next} skip{next} 1' .claude/rules/dev-rules.md) .windsurf/rules/dev-rules.md > /dev/null"

echo ""
echo "════════════════════════════════════════════════"
printf "  SKIP: %d  |  MISS: %d\n" "$PASS" "$MISS"
echo "════════════════════════════════════════════════"

if [ "$MISS" -eq 0 ]; then
  echo "  ✓ FULLY INSTALLED — nothing to do"
  exit 0
else
  echo ""
  echo "  Items requiring action:"
  for item in "${ITEMS_TO_INSTALL[@]}"; do
    printf "    → %s\n" "$item"
  done
  echo ""
  echo "  Execute guide steps for MISS items only."
  exit 1
fi

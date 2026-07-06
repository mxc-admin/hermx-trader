#!/usr/bin/env python3
"""skills-guard.py — best-effort pre_tool_call deterrent for Hermes shell hooks.

Protects the skill definitions under ``<repo>/skills/`` and ``~/.hermes/skills/``
from being rewritten or deleted by the agent. Register it as a ``pre_tool_call``
shell hook in ``~/.hermes/config.yaml`` (matcher: ``write_file|patch|terminal``).
It reads the tool-call payload on stdin and, when the call would WRITE to or
DELETE a protected skills path, prints a Hermes-canonical block directive
(``{"action":"block","message":...}``) on stdout. For any allowed call it prints
nothing at all.

KNOWN LIMITATION — this is a DETERRENT, not a hard boundary:
  * The relative-path heuristic (``_mentions_skills_component``) is
    cwd-independent and therefore conservative. A ``terminal`` command that first
    ``cd``s elsewhere and then edits a skills path by a route that never
    textually mentions a ``skills`` component can slip past — we deliberately do
    NOT emulate the shell to resolve ``cd``; that arms race is unwinnable inside
    a stdin heuristic.
  * The hook FAILS OPEN: any internal error, malformed payload, or unparseable
    command yields no block, so it can never wedge the agent.

The real guarantee lives in ``deploy/deploy.sh``'s drift gate, which refuses to
deploy (and blocks the destructive ``git reset --hard`` rollback) whenever
tracked files — ``skills/`` included — drift from HEAD. Treat this hook as a
seatbelt that catches the common, honest cases early and loudly, not a vault
door.
"""
import json
import os
import shlex
import sys

# Base commands that only READ their path arguments. A skills path touched by one
# of these is allowed (e.g. `cat skills/hx-status/SKILL.md`). Anything not on this
# list that references a protected path is treated as a mutation and blocked.
READ_ONLY_CMDS = {
    "cat", "bat", "less", "more", "head", "tail", "grep", "egrep", "fgrep",
    "rg", "ag", "ls", "ll", "find", "stat", "file", "wc", "diff", "cmp",
    "cut", "sort", "uniq", "nl", "od", "xxd", "hexdump", "sha256sum",
    "md5sum", "cksum", "tree", "view", "column", "realpath", "readlink",
}
# Prefix tokens that wrap the real command without being the command itself.
_CMD_WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "then", "do"}


def _protected_dirs():
    """Realpath-resolved skills trees this guard protects (symlink-aware)."""
    candidates = [
        # <repo>/skills/ — repo root is two levels up from setup/hermes/.
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "skills",
        ),
        # ~/.hermes/skills/
        os.path.join(os.path.expanduser("~"), ".hermes", "skills"),
    ]
    resolved = []
    for d in candidates:
        try:
            resolved.append(os.path.realpath(d))
        except Exception:
            pass
    return resolved


def _is_inside(path, parent):
    """True if realpath(path) is `parent` or lives beneath it (symlink-aware)."""
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return rp == parent or rp.startswith(parent + os.sep)


def _mentions_skills_component(path):
    """cwd-INDEPENDENT heuristic for relative paths we cannot resolve reliably.

    True when the path traverses a ``skills`` component that plausibly names a
    protected tree: a leading ``skills/...`` segment, a ``.hermes/skills/...``
    segment, or any ``skills`` directory that has a child (a skill dir/file).
    Conservative on purpose — see the module docstring.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    for i, part in enumerate(parts):
        if part == "skills":
            if i == 0 or parts[i - 1] == ".hermes" or i + 1 < len(parts):
                return True
    return False


def _path_hits_protected(path, protected, cwd=None):
    """True if `path` targets a protected skills tree.

    Absolute paths (or relative paths joined onto a known `cwd`) resolve to a
    definitive containment check; otherwise fall back to the textual heuristic.
    """
    if os.path.isabs(path):
        return any(_is_inside(path, p) for p in protected)
    if cwd:
        joined = os.path.join(cwd, path)
        if any(_is_inside(joined, p) for p in protected):
            return True
    return _mentions_skills_component(path)


def _check_write(tool_input, protected):
    """write_file / patch inherently mutate — any protected path hit blocks."""
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return None
    if _path_hits_protected(path, protected):
        return "write to a protected skill file"
    return None


def _check_terminal(tool_input, protected):
    """Block terminal commands that would write/delete a protected skills path."""
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    cwd = tool_input.get("cwd")  # optional explicit cwd for this terminal call
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()
    if not tokens:
        return None

    # 1) Redirection into a protected path is always a write. Handle both the
    #    split form (`> skills/x`) and the attached form (`>skills/x`).
    for i, tok in enumerate(tokens):
        if tok in (">", ">>") and i + 1 < len(tokens):
            if _path_hits_protected(tokens[i + 1], protected, cwd):
                return "write to a protected skill via shell redirection"
        elif tok.startswith(">"):
            target = tok.lstrip(">")
            if target and _path_hits_protected(target, protected, cwd):
                return "write to a protected skill via shell redirection"

    # 2) Does any plain argument reference a protected path at all?
    if not any(_path_hits_protected(tok, protected, cwd) for tok in tokens):
        return None  # command doesn't touch skills → allow

    # 3) It touches skills — allow only if the base command is read-only.
    for tok in tokens:
        if "=" in tok and tok.split("=", 1)[0].isidentifier() and not tok.startswith(("/", ".", "-")):
            continue  # VAR=value environment prefix
        if tok in _CMD_WRAPPERS:
            continue
        if os.path.basename(tok) in READ_ONLY_CMDS:
            return None  # e.g. `cat skills/hx-status/SKILL.md` → allow
        break  # first real, non-read-only command → fall through to block
    return "modify or delete a protected skill via a terminal command"


def main():
    try:
        payload = json.loads(sys.stdin.read())
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return
        protected = _protected_dirs()
        if tool_name in ("write_file", "patch"):
            reason = _check_write(tool_input, protected)
        elif tool_name == "terminal":
            reason = _check_terminal(tool_input, protected)
        else:
            reason = None
        if reason:
            print(json.dumps({"action": "block", "message": (
                "Blocked by skills-guard: this call would " + reason + ". Skill "
                "definitions under skills/ and ~/.hermes/skills/ are protected — "
                "change them via a reviewed commit, not the agent. (Best-effort "
                "deterrent; deploy.sh's drift gate is the hard backstop.)"
            )}))
    except Exception:
        # Fail OPEN: a guard error must never block or wedge the agent.
        return


if __name__ == "__main__":
    main()

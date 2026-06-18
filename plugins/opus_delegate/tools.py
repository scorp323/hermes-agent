"""Tool implementation for the opus_delegate plugin.

Spawns the local Claude Code CLI (`claude -p`) running Opus 4.8 as a child
worker and returns its final result. Two modes:

  - perspective (default): read-only tools only. For "give me a fresh Opus
    opinion" — cannot edit files or run mutating shell commands.
  - work: full read+write+shell. Gated through Hermes' dangerous-command
    approval system (`prompt_dangerous_approval`), which in gateway mode
    routes the prompt to the originating chat (Telegram) and waits. Fails
    CLOSED: if no approval path resolves, the run is denied — an inbound
    message can never trigger unapproved writes on M4.

Auth: relies on the local `claude` CLI's own subscription credentials
(macOS Keychain / `claude setup-token`). ANTHROPIC_API_KEY is stripped from
the child env so the run can never silently fall back to per-token API
billing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

_CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
_DEFAULT_TIMEOUT = 1800  # 30 min hard ceiling for a delegated run
_DEFAULT_WORKDIR = os.path.expanduser("~/claude")

# Read-only surface for "fresh perspective" runs. Anything not listed is
# denied by `--permission-mode default`, so the worker physically cannot
# mutate the tree.
_READONLY_TOOLS = [
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git status:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(rg:*)",
]
# Full surface for approved "work" runs.
_WORK_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit",
    "Bash", "WebSearch", "WebFetch",
]

OPUS_DELEGATE_SCHEMA = {
    "name": "opus_delegate",
    "description": (
        "Hand a task to Claude Code running Opus 4.8 on this machine (M4), via "
        "the Claude subscription (no API billing). Use mode='perspective' "
        "(default, read-only) to get a fresh Opus opinion or second look at "
        "code; use mode='work' to delegate real coding/ops work with full file "
        "edit + shell — work mode requires Nathan's approval (routed to the "
        "chat) before it runs. Returns Opus's final result text. This is a "
        "blocking call that may take minutes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task or question to delegate to Opus 4.8. Be specific.",
            },
            "mode": {
                "type": "string",
                "enum": ["perspective", "work"],
                "description": "perspective = read-only opinion (default); work = full edit+shell, approval-gated.",
            },
            "cwd": {
                "type": "string",
                "description": "Absolute working dir / repo for the run. Defaults to ~/claude.",
            },
        },
        "required": ["task"],
    },
}


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _check_claude_cli() -> tuple[bool, str]:
    """Runtime gate: the tool is dormant unless the claude CLI is installed."""
    if os.path.exists(_CLAUDE_BIN) or shutil.which("claude"):
        return True, ""
    return False, "claude CLI not found on M4 (install Claude Code to enable opus_delegate)."


def _resolve_bin() -> str:
    return _CLAUDE_BIN if os.path.exists(_CLAUDE_BIN) else (shutil.which("claude") or "claude")


def opus_delegate(
    args: Optional[dict] = None,
    task: Optional[str] = None,
    mode: str = "perspective",
    cwd: Optional[str] = None,
    parent_agent=None,
    task_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Dispatch-compatible handler.

    Hermes registry handlers receive the model arguments as the first positional
    ``args`` dict and runtime metadata such as ``task_id`` as keyword args.
    Keep named parameters too so this remains easy to call directly in tests.
    """
    if isinstance(args, dict):
        task = args.get("task", task)
        mode = args.get("mode", mode)
        cwd = args.get("cwd", cwd)

    if not task or not str(task).strip():
        return _err("Provide a non-empty 'task' to delegate to Opus.")

    workdir = os.path.expanduser(cwd) if cwd else _DEFAULT_WORKDIR
    if not os.path.isdir(workdir):
        return _err(f"cwd does not exist or is not a directory: {workdir}")

    mode = (mode or "perspective").strip().lower()
    if mode not in ("perspective", "work"):
        return _err("mode must be 'perspective' or 'work'.")

    if mode == "work":
        # Gate full read+write+shell. In gateway mode this surfaces to the
        # originating chat (Telegram) and blocks on the reply. Fail closed.
        try:
            from tools.approval import prompt_dangerous_approval
            from tools.terminal_tool import _get_approval_callback

            decision = prompt_dangerous_approval(
                command=f"opus_delegate(mode=work) in {workdir}",
                description=(
                    f"Opus 4.8 will run with FULL file-edit + shell access in "
                    f"{workdir}. Task: {str(task).strip()[:240]}"
                ),
                approval_callback=_get_approval_callback(),
            )
        except Exception as exc:  # no approval path -> deny, never run unapproved
            return _err(f"work-mode approval unavailable; denied for safety: {exc}")
        if decision == "deny":
            return _err("Work-mode delegation was not approved.")
        allowed = _WORK_TOOLS
        perm_mode = "acceptEdits"
    else:
        allowed = _READONLY_TOOLS
        perm_mode = "default"

    cmd = [
        _resolve_bin(), "-p", str(task),
        "--model", "claude-opus-4-8",
        "--output-format", "json",
        "--permission-mode", perm_mode,
        "--add-dir", workdir,
        "--allowedTools", *allowed,
    ]

    # Force subscription auth: an inherited ANTHROPIC_API_KEY would silently
    # switch the run to metered API billing.
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.run(
            cmd, cwd=workdir, env=env,
            capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _err(f"Opus delegation timed out after {_DEFAULT_TIMEOUT}s.")
    except Exception as exc:
        return _err(f"failed to launch claude -p: {exc}")

    if proc.returncode != 0:
        return _err(f"claude -p exited {proc.returncode}: {(proc.stderr or '')[-600:]}")

    # `--output-format json` emits a single JSON object with a `result` field.
    result_text = proc.stdout
    try:
        data = json.loads(proc.stdout)
        result_text = data.get("result") or data.get("text") or proc.stdout
    except Exception:
        pass

    return json.dumps({
        "mode": mode,
        "cwd": workdir,
        "model": "claude-opus-4-8",
        "result": result_text,
    })

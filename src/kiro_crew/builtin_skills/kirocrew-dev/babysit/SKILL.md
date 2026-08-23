---
name: babysit
description: Use when a user asks to babysit, monitor, keep checking, keep an eye on, or report when a pull request, CI run, ticket, deployment, or other changing target reaches an outcome.
inject_on_trigger: false
tags: [skill, kirocrew, monitor, babysit]
---

# Babysit

## Overview

Use a structured monitor whenever the target is a public GitHub pull request and
the objective is review readiness. Kiro Crew probes before invoking the model, so
unchanged polling does not consume agent turns.

## GitHub pull request: one bounded watch

Call this once with the canonical pull-request URL:

```text
monitor_watch({
  "kind": "github_pull_request",
  "target": "https://github.com/OWNER/REPO/pull/NUMBER",
  "objective": "review_ready",
  "interval_secs": 300,
  "max_runtime_secs": 14400,
  "max_agent_turns": 8,
  "max_tokens": 250000,
  "max_provider_errors": 3,
  "wake_instructions": "Inspect the named blocker, fetch only the needed details, act safely, and verify the result."
})
```

The reply is a pending application request, not proof that the monitor armed.
End the turn so the session-aware consumer can apply it. The operator can then
confirm it in the dashboard. The agent cannot inspect after ending that same
turn; at the start of a later user/wake turn, call `monitor_inspect({})` before
acting or reporting. Its retained session-bound state is authoritative and it
takes no monitor id, target, or session key.

The server performs ordinary probes. Unchanged state, provider retry, success,
and terminal blockers use zero model turns. A new actionable fingerprint wakes
the session at most once with a compact, bounded summary. Only then fetch logs,
comments, or diffs needed to act. The token cap applies only when the provider
reports usage; `token_usage_known` exposes whether it did. Positive runtime and
completed-turn caps remain hard fallbacks. Provider errors are also bounded.

Use `monitor_update({...})` without an id to change `interval_secs`, positive
budgets, or `wake_instructions`. Those edits preserve the comparison baseline;
changing `target` or `objective` starts a new baseline. Terminal records are
read-only, so start an explicit new watch to restart. Use
`monitor_stop({"reason": "User ended the watch."})` for a deliberate stop; the
retained outcome is `user_stop`. `autonudge_stop` remains a compatibility alias.

## Legacy — unsupported target only

Use a prompt loop only when `monitor_watch` does not support the target or
objective:

```text
monitor_start({
  "message": "Watch <unsupported target>. On each injected cycle, perform the smallest safe status check. Act only on a real change. If the objective is met, the target is terminal, or a human decision is needed, report the outcome and call autonudge_stop with a reason.",
  "interval_secs": 300,
  "max_cycles": 24,
  "max_runtime_secs": 14400
})
```

All three limits must be nonzero; use a larger finite value, never `0`, for a
longer request. Every delivered legacy cycle is a full agent turn that extends
session context and spends model/tool tokens even when nothing changed. The reply
is only an arm request; verify the loop through the dashboard monitor/goal-loop
surface or `GET /api/autonudge`.

Legacy tool calls remain under normal PreToolUse governance and approval policy.
An unattended Slack or Discord turn has no blanket grant: approval may be
requested, rejected, or time out, and the loop records an approval stall instead
of silently succeeding.

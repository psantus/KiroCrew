from __future__ import annotations

import json
from unittest.mock import patch

from kiro_crew import session_directive
from kiro_crew.mcp_tools import control


def test_monitor_watch_is_stateless_and_canonical():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1"):
        result = control.monitor_watch(
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://www.github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        )

    args = session_directive.decode(result, "monitor_watch")
    assert args is not None
    assert args["target"] == "https://github.com/acme/widgets/pull/7"
    assert "session_key" not in json.dumps(args)
    assert "loop_id" not in json.dumps(args)


def test_monitor_watch_rejects_native_subagent_binding():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:child"):
        result = control.monitor_watch(
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
            },
        )
    assert session_directive.decode(result, "monitor_watch") is None
    assert "only works" in result


def test_structured_update_and_stop_reject_native_subagent_binding():
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:child"):
        update = control.monitor_update(
            "monitor_update",
            {"wake_instructions": "Check CI."},
        )
        stop = control.monitor_stop("monitor_stop", {"reason": "done"})

    assert session_directive.decode(update, "monitor_update") is None
    assert session_directive.decode(stop, "monitor_stop") is None
    assert "only works" in update
    assert "only works" in stop


def test_monitor_inspect_passes_strict_identity_without_fallback():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="slack:123"),
        patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "enabled": True,
                "active": True,
                "monitor": {
                    "kind": "github_pull_request",
                    "wake_count": 3,
                    "token_usage_known": False,
                    "wake_instructions": "large prompt omitted from inspect",
                    "last_observation_status": "pending",
                    "last_observation_reason_code": "checks_pending",
                    "last_observation_summary": "Two checks are pending.",
                    "last_observation": {
                        "head_revision": "abc123",
                        "checks": {"passed": [f"check-{index}" for index in range(20)]},
                    },
                },
            },
        ) as get,
    ):
        result = control.monitor_inspect("monitor_inspect", {})

    get.assert_called_once_with("/api/autonudge/session-monitor", session_key="slack:123")
    payload = json.loads(result)
    assert payload["monitor"]["kind"] == "github_pull_request"
    assert payload["monitor"]["wake_count"] == 3
    assert payload["monitor"]["token_usage_known"] is False
    assert payload["monitor"]["last_observation_status"] == "pending"
    assert payload["monitor"]["last_observation_reason_code"] == "checks_pending"
    assert payload["monitor"]["last_observation_summary"] == "Two checks are pending."
    assert payload["monitor"]["observation"]["checks"]["passed_count"] == 20
    assert "wake_instructions" not in payload["monitor"]
    assert "check-0" not in result


def test_monitor_inspect_never_uses_ancestor_fallback_without_strict_identity():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_core._get") as get,
    ):
        result = control.monitor_inspect("monitor_inspect", {})
    get.assert_not_called()
    assert "unavailable" in result.lower()

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    ProviderErrorKind,
    monitor_state_public_dict,
)

_SESSION_KEY = "dashboard:chat-1"
_BINDING = "chat-1"
_TARGET = "https://github.com/acme/widgets/pull/7"


@pytest.fixture
def monitor_service(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    yield service
    service.stop()


def _canonical(*, head_revision: str = "abc123") -> dict[str, object]:
    return {
        "kind": "github_pull_request",
        "target": "github.com/acme/widgets#7",
        "state": "open",
        "draft": False,
        "head_revision": head_revision,
        "mergeability": "mergeable",
        "review_decision": "approved",
        "blocking_review": "none",
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
        "checks": {"failed": [], "passed": ["ci"], "pending": [], "unknown": []},
    }


def _probe_result(
    status: MonitorObservationStatus,
    *,
    fingerprint: str = "fp-1",
    reason_code: str = "checks_pending",
    provider_error: ProviderErrorKind | None = None,
) -> GitHubPullRequestProbeResult:
    return GitHubPullRequestProbeResult(
        response=None,
        canonical={} if provider_error is not None else _canonical(),
        observation=MonitorObservation(
            "" if provider_error is not None else fingerprint,
            status,
            provider_error=provider_error,
            reason_code=reason_code,
            summary="Safe monitor summary.",
        ),
    )


@dataclass
class _Provider:
    results: list[GitHubPullRequestProbeResult]
    probe_count: int = 0

    def probe(self, target: str, *, previous_observation=None):
        del target, previous_observation
        result = self.results[min(self.probe_count, len(self.results) - 1)]
        self.probe_count += 1
        return result


def _state_and_slot():
    state = SimpleNamespace(
        _slots={_BINDING: SimpleNamespace(workspace="default")},
        sessions=None,
        channel_transports={},
    )
    return state, SimpleNamespace(key=_BINDING, _app="")


def _call_directive(tool_name: str, args: dict[str, object]) -> tuple[str, dict[str, object]]:
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_SESSION_KEY):
        result = mcp_core._call_tool_inner(tool_name, args)
    directive = session_directive.decode(result, tool_name)
    assert directive is not None
    return result, directive


async def _apply(
    service: AutoNudgeService,
    tool_name: str,
    directive: dict[str, object],
) -> str:
    state, slot = _state_and_slot()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
    ):
        return await apply_session_directive(state, slot, _SESSION_KEY, tool_name, directive)


async def _arm_via_session_consumer(
    service: AutoNudgeService,
    *,
    max_runtime_secs: int = 14_400,
) -> tuple[str, dict[str, object]]:
    acknowledgement, directive = _call_directive(
        "monitor_watch",
        {
            "kind": "github_pull_request",
            "target": _TARGET,
            "objective": "review_ready",
            "interval_secs": 300,
            "max_runtime_secs": max_runtime_secs,
            "max_agent_turns": 8,
            "max_tokens": 250_000,
            "max_provider_errors": 3,
            "wake_instructions": "Inspect the named blocker and act only if needed.",
        },
    )
    assert service.get_by_slot(_BINDING) is None
    applied = await _apply(service, "monitor_watch", directive)
    assert "started" in applied
    return acknowledgement, directive


def _inspection(service: AutoNudgeService) -> dict[str, object]:
    loop = service.get_by_slot(_BINDING)
    response: dict[str, object] = {"enabled": True, "monitor": None}
    if loop is not None and loop.monitor is not None:
        response.update(
            {
                "active": loop.active,
                "monitor_id": loop.id,
                "monitor": monitor_state_public_dict(loop.monitor),
            }
        )
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_SESSION_KEY),
        patch("kiro_crew.mcp_core._get", return_value=response),
    ):
        return json.loads(mcp_core._call_tool_inner("monitor_inspect", {}))


@pytest.mark.asyncio
async def test_babysit_create_requires_application_before_state_is_authoritative(
    monitor_service,
):
    acknowledgement, directive = _call_directive(
        "monitor_watch",
        {
            "kind": "github_pull_request",
            "target": _TARGET,
            "objective": "review_ready",
        },
    )

    assert "requested" in acknowledgement.lower()
    assert "later turn" in acknowledgement.lower()
    assert "session_key" not in directive
    assert _inspection(monitor_service)["monitor"] is None

    applied = await _apply(monitor_service, "monitor_watch", directive)
    assert "started" in applied
    inspection = _inspection(monitor_service)
    monitor = inspection["monitor"]
    assert inspection["active"] is True
    assert isinstance(monitor, dict)
    assert monitor["target"] == _TARGET
    assert monitor["objective"] == "review_ready"
    assert monitor["cadence_secs"] == 300
    assert monitor["token_usage_known"] is True
    assert monitor["budgets"] == {
        "max_runtime_secs": 14_400,
        "max_agent_turns": 8,
        "max_tokens": 250_000,
        "max_provider_errors": 3,
    }


@pytest.mark.asyncio
async def test_babysit_unchanged_observations_probe_without_agent_turns(monitor_service):
    await _arm_via_session_consumer(monitor_service)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    created = loop.monitor.created_ts
    provider = _Provider([_probe_result(MonitorObservationStatus.PENDING)])
    dispatch = AsyncMock()
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    first = await controller.tick(loop, now=created + 10)
    second = await controller.tick(loop, now=created + 310)

    assert first is MonitorDecision.RECORD_ONLY
    assert second is MonitorDecision.NO_CHANGE
    assert provider.probe_count == 2
    dispatch.assert_not_awaited()
    inspection = _inspection(monitor_service)["monitor"]
    assert isinstance(inspection, dict)
    assert inspection["probe_count"] == 2
    assert inspection["wake_count"] == inspection["agent_turns"] == 0
    assert inspection["next_probe_at"] == created + 610


@pytest.mark.asyncio
async def test_babysit_actionable_fingerprint_wakes_once_across_restart(monitor_service, tmp_path):
    await _arm_via_session_consumer(monitor_service)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    provider = _Provider(
        [
            _probe_result(
                MonitorObservationStatus.ACTIONABLE,
                fingerprint="head-2",
                reason_code="new_head_revision",
            )
        ]
    )
    dispatch = AsyncMock(return_value=MonitorDispatchResult.DISPATCHED)
    controller = MonitorController(monitor_service, dispatch, provider=provider)
    now = loop.monitor.created_ts + 10

    first = await controller.tick(loop, now=now)
    second = await controller.tick(loop, now=now + 1)

    assert first is MonitorDecision.WAKE_ACTIONABLE
    assert second is MonitorDecision.NO_CHANGE
    assert provider.probe_count == 1
    dispatch.assert_awaited_once()
    inspection = _inspection(monitor_service)["monitor"]
    assert isinstance(inspection, dict)
    assert inspection["last_wake_fingerprint"] == "head-2"
    assert inspection["wake_count"] == 1
    assert inspection["agent_turns"] == 0

    monitor_service.stop()
    restarted = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    await restarted.start()
    try:
        restored = restarted.get_by_slot(_BINDING)
        assert restored is not None and restored.monitor is not None
        restarted_provider = _Provider(
            [_probe_result(MonitorObservationStatus.ACTIONABLE, fingerprint="head-2")]
        )
        restarted_dispatch = AsyncMock(return_value=MonitorDispatchResult.DISPATCHED)
        restarted_controller = MonitorController(
            restarted,
            restarted_dispatch,
            provider=restarted_provider,
        )

        decision = await restarted_controller.tick(restored, now=now + 2)

        assert decision is MonitorDecision.NO_CHANGE
        assert restarted_provider.probe_count == 0
        restarted_dispatch.assert_not_awaited()
        assert restored.monitor.wake_count == 1
    finally:
        restarted.stop()


@pytest.mark.asyncio
async def test_babysit_success_stops_without_an_agent_turn(monitor_service):
    await _arm_via_session_consumer(monitor_service)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    provider = _Provider(
        [
            _probe_result(
                MonitorObservationStatus.SUCCESS,
                reason_code="review_ready",
            )
        ]
    )
    dispatch = AsyncMock()
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    decision = await controller.tick(loop, now=loop.monitor.created_ts + 10)

    assert decision is MonitorDecision.STOP_SUCCESS
    dispatch.assert_not_awaited()
    inspection = _inspection(monitor_service)
    monitor = inspection["monitor"]
    assert inspection["active"] is False
    assert isinstance(monitor, dict)
    assert monitor["outcome"] == MonitorOutcome.SUCCESS.value
    assert monitor["stopped_reason"] == "review_ready"
    assert monitor["agent_turns"] == 0


@pytest.mark.asyncio
async def test_babysit_provider_block_is_safe_and_turn_free(monitor_service):
    await _arm_via_session_consumer(monitor_service)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    provider = _Provider(
        [
            _probe_result(
                MonitorObservationStatus.PROVIDER_ERROR,
                reason_code="github_authentication_failed",
                provider_error=ProviderErrorKind.AUTHENTICATION,
            )
        ]
    )
    dispatch = AsyncMock()
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    decision = await controller.tick(loop, now=loop.monitor.created_ts + 10)

    assert decision is MonitorDecision.STOP_BLOCKED
    dispatch.assert_not_awaited()
    inspection = _inspection(monitor_service)["monitor"]
    assert isinstance(inspection, dict)
    assert inspection["outcome"] == MonitorOutcome.BLOCKED.value
    assert inspection["stopped_reason"] == "github_authentication_failed"
    assert "stderr" not in json.dumps(inspection).lower()


@pytest.mark.asyncio
async def test_babysit_runtime_budget_stops_before_another_probe_or_turn(monitor_service):
    await _arm_via_session_consumer(monitor_service, max_runtime_secs=60)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    provider = _Provider([_probe_result(MonitorObservationStatus.ACTIONABLE)])
    dispatch = AsyncMock()
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    decision = await controller.tick(loop, now=loop.monitor.created_ts + 60)

    assert decision is MonitorDecision.STOP_BUDGET
    assert provider.probe_count == 0
    dispatch.assert_not_awaited()
    inspection = _inspection(monitor_service)["monitor"]
    assert isinstance(inspection, dict)
    assert inspection["outcome"] == MonitorOutcome.BUDGET.value
    assert inspection["stopped_reason"] == "runtime_budget"
    assert inspection["wake_count"] == inspection["agent_turns"] == 0


@pytest.mark.asyncio
async def test_babysit_busy_retry_stops_at_runtime_without_another_action_attempt(
    monitor_service,
):
    await _arm_via_session_consumer(monitor_service, max_runtime_secs=20)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    provider = _Provider([_probe_result(MonitorObservationStatus.ACTIONABLE)])
    dispatch = AsyncMock(return_value=MonitorDispatchResult.BUSY)
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    first = await controller.tick(loop, now=loop.monitor.created_ts + 10)
    expired = await controller.tick(loop, now=loop.monitor.created_ts + 25)

    assert first is MonitorDecision.WAKE_ACTIONABLE
    assert expired is MonitorDecision.STOP_BUDGET
    assert provider.probe_count == 1
    dispatch.assert_awaited_once()
    inspection = _inspection(monitor_service)["monitor"]
    assert isinstance(inspection, dict)
    assert inspection["outcome"] == MonitorOutcome.BUDGET.value
    assert inspection["stopped_reason"] == "runtime_budget"
    assert inspection["wake_count"] == inspection["agent_turns"] == 0


@pytest.mark.asyncio
async def test_babysit_user_stop_uses_real_tool_and_retains_terminal_state(monitor_service):
    await _arm_via_session_consumer(monitor_service)
    _acknowledgement, directive = _call_directive(
        "monitor_stop", {"reason": "User ended the watch."}
    )

    applied = await _apply(monitor_service, "monitor_stop", directive)

    assert "retained" in applied
    inspection = _inspection(monitor_service)
    monitor = inspection["monitor"]
    assert inspection["active"] is False
    assert isinstance(monitor, dict)
    assert monitor["outcome"] == MonitorOutcome.USER_STOP.value
    assert monitor["stopped_reason"] == "user_stop"

    provider = _Provider([_probe_result(MonitorObservationStatus.ACTIONABLE)])
    dispatch = AsyncMock(return_value=MonitorDispatchResult.DISPATCHED)
    loop = monitor_service.get_by_slot(_BINDING)
    assert loop is not None and loop.monitor is not None
    controller = MonitorController(monitor_service, dispatch, provider=provider)

    decision = await controller.tick(loop, now=loop.monitor.stopped_at + 1)

    assert decision is MonitorDecision.STOP_BLOCKED
    dispatch.assert_not_awaited()

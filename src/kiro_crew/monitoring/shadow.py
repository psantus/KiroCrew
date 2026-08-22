"""Persistence-only monitor probing with no action-delivery dependency."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Protocol

from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorDecision,
    MonitorObservationStatus,
    MonitorState,
)

ShadowStatePersistence = Callable[[MonitorState], Awaitable[None]]


class ShadowWakeDeliveryRefused(RuntimeError):
    """Raised when a caller asks the persistence-only path to wake a session."""


class GitHubShadowProvider(Protocol):
    """External probe boundary required by the shadow controller."""

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> GitHubPullRequestProbeResult: ...


async def run_shadow_probe(
    state: MonitorState,
    provider: GitHubShadowProvider,
    persist: ShadowStatePersistence,
    *,
    now: float,
    wake_delivery: bool = False,
) -> MonitorDecision:
    """Probe and persist one decision without acquiring a delivery capability."""
    if wake_delivery:
        raise ShadowWakeDeliveryRefused("wake delivery is unavailable in shadow mode")
    if state.kind != "github_pull_request" or state.objective != "review_ready":
        raise ValueError("shadow mode supports only github_pull_request review_ready")
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
        or now < 0
    ):
        raise ValueError("now must be a finite non-negative number")
    if not callable(persist):
        raise ValueError("persist must be callable")

    result = await asyncio.to_thread(
        provider.probe,
        state.target,
        previous_observation=deepcopy(state.last_observation),
    )
    decision = decide_monitor(state, result.observation, now=now)
    state.probe_count += 1
    state.last_probe_at = now
    state.last_decision = decision
    state.next_probe_at = now + state.cadence_secs
    if result.observation.status is MonitorObservationStatus.PROVIDER_ERROR:
        state.provider_error_count += 1
        state.consecutive_provider_errors += 1
        state.last_provider_error = result.observation.provider_error
    else:
        state.last_observation = deepcopy(result.canonical)
        state.last_fingerprint = result.observation.fingerprint
        state.last_observed_at = now
        state.consecutive_provider_errors = 0
        state.last_provider_error = None
    await persist(state)
    return decision

"""Provider-neutral pull-request readiness observations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    PULL_REQUEST_MERGEABILITY,
    PULL_REQUEST_MONITOR_KINDS,
    PULL_REQUEST_REVIEW_DECISIONS,
    PULL_REQUEST_STATES,
    MonitorObservation,
    MonitorObservationStatus,
    ProviderErrorKind,
)
from kiro_crew.security import redact

PULL_REQUEST_CHECK_STATES = frozenset({"failed", "passed", "pending", "unknown"})
MAX_PULL_REQUEST_HEAD_REVISION_CHARS = 128

_URL_IN_CHECK_IDENTITY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_CONTROL_IN_CHECK_IDENTITY_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_HEAD_REVISION_RE = re.compile(rf"^[0-9a-fA-F]{{1,{MAX_PULL_REQUEST_HEAD_REVISION_CHARS}}}$")

_PROVIDER_ERROR_REASONS = {
    ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
    ProviderErrorKind.AUTHENTICATION: "provider_authentication",
    ProviderErrorKind.AUTHORIZATION: "provider_authorization",
    ProviderErrorKind.NOT_FOUND: "provider_not_found",
    ProviderErrorKind.TRANSIENT: "provider_transient",
}


def opaque_provider_check_identity(namespace: str, raw_identity: object) -> str:
    """Return a stable identity without retaining provider-controlled display text."""
    digest = hashlib.sha256(str(raw_identity).encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


class PullRequestProviderError(Exception):
    """A provider failure carrying only its safe retry category."""

    def __init__(self, kind: ProviderErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def classify_provider_error_text(raw: str) -> ProviderErrorKind:
    """Classify CLI diagnostics without retaining or returning their text."""
    lowered = raw.lower()
    if any(
        marker in lowered for marker in ("http 429", "rate limit", "too many requests", "throttled")
    ):
        return ProviderErrorKind.RATE_LIMITED
    if any(
        marker in lowered
        for marker in ("http 401", "unauthorized", "not logged in", "authentication")
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(marker in lowered for marker in ("http 404", "not found", "does not exist")):
        return ProviderErrorKind.NOT_FOUND
    if any(
        marker in lowered for marker in ("http 403", "forbidden", "permission", "access denied")
    ):
        return ProviderErrorKind.AUTHORIZATION
    return ProviderErrorKind.TRANSIENT


def provider_failure_result(error: PullRequestProviderError) -> PullRequestProbeResult:
    """Convert a safe typed provider failure into a generic monitor result."""
    return provider_error_result(error.kind, _PROVIDER_ERROR_REASONS[error.kind])


@dataclass(frozen=True)
class PullRequestCheck:
    """One normalized, bounded provider check."""

    identity: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("check identity must be a non-empty string")
        if self.state not in PULL_REQUEST_CHECK_STATES:
            raise ValueError("check state is not supported")
        normalized = _CONTROL_IN_CHECK_IDENTITY_RE.sub(" ", self.identity).strip()
        identity = redact(_URL_IN_CHECK_IDENTITY_RE.sub("[provider-url]", normalized))
        if not identity:
            raise ValueError("check identity must remain non-empty after redaction")
        if len(identity) > MAX_MONITOR_CHECK_IDENTITY_CHARS:
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            prefix_length = MAX_MONITOR_CHECK_IDENTITY_CHARS - len(digest) - 1
            identity = f"{identity[:prefix_length]}#{digest}"
        object.__setattr__(self, "identity", identity)


@dataclass(frozen=True)
class PullRequestFacts:
    """Provider-native state normalized into the shared readiness vocabulary."""

    kind: str
    target: str
    state: str
    draft: bool
    head_revision: str
    mergeability: str
    review_decision: str
    checks: tuple[PullRequestCheck, ...]
    unresolved_review_threads: int
    review_threads_complete: bool

    def __post_init__(self) -> None:
        if self.kind not in PULL_REQUEST_MONITOR_KINDS:
            raise ValueError("kind is not a supported pull-request monitor")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("target must be a non-empty string")
        if self.state not in PULL_REQUEST_STATES:
            raise ValueError("state is not supported")
        if not isinstance(self.draft, bool):
            raise ValueError("draft must be a boolean")
        if not isinstance(self.head_revision, str) or (
            self.head_revision and _HEAD_REVISION_RE.fullmatch(self.head_revision) is None
        ):
            raise ValueError("head_revision must be bounded hexadecimal text")
        if self.mergeability not in PULL_REQUEST_MERGEABILITY:
            raise ValueError("mergeability is not supported")
        if self.review_decision not in PULL_REQUEST_REVIEW_DECISIONS:
            raise ValueError("review_decision is not supported")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, PullRequestCheck) for check in self.checks
        ):
            raise ValueError("checks must be normalized pull-request checks")
        if (
            isinstance(self.unresolved_review_threads, bool)
            or not isinstance(self.unresolved_review_threads, int)
            or self.unresolved_review_threads < 0
        ):
            raise ValueError("unresolved_review_threads must be a non-negative integer")
        if not isinstance(self.review_threads_complete, bool):
            raise ValueError("review_threads_complete must be a boolean")


@dataclass(frozen=True)
class PullRequestProbeResult:
    """Canonical facts and their generic monitor classification."""

    response: object | None
    canonical: dict[str, object]
    observation: MonitorObservation


def build_pull_request_probe_result(
    facts: PullRequestFacts,
    *,
    previous_observation: Mapping[str, object] | None = None,
    response: object | None = None,
) -> PullRequestProbeResult:
    """Build the shared canonical snapshot, fingerprint, and classification."""
    canonical = canonical_pull_request_facts(facts)
    status, reason_code = classify_pull_request_facts(facts)
    fingerprint_facts = (
        actionable_fingerprint_facts(canonical)
        if status is MonitorObservationStatus.ACTIONABLE
        else canonical
    )
    previous_head = (
        previous_observation.get("head_revision")
        if isinstance(previous_observation, Mapping)
        else None
    )
    head_changed = (
        facts.state == "open"
        and isinstance(previous_head, str)
        and bool(previous_head)
        and bool(facts.head_revision)
        and previous_head != facts.head_revision
    )
    return PullRequestProbeResult(
        response=facts if response is None else response,
        canonical=canonical,
        observation=MonitorObservation(
            fingerprint_pull_request_facts(fingerprint_facts),
            status,
            reason_code=reason_code,
            head_changed=head_changed,
        ),
    )


def provider_error_result(
    kind: ProviderErrorKind,
    reason_code: str,
) -> PullRequestProbeResult:
    """Return a provider-neutral error result with no durable raw payload."""
    return PullRequestProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=kind,
            reason_code=reason_code,
        ),
    )


def canonical_pull_request_facts(facts: PullRequestFacts) -> dict[str, object]:
    """Project one exact bounded canonical fact object."""
    buckets = {
        state: sorted({check.identity for check in facts.checks if check.state == state})
        for state in ("failed", "passed", "pending", "unknown")
    }
    overflow = any(
        len(values) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET for values in buckets.values()
    )
    checks = {
        state: values[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET] for state, values in buckets.items()
    }
    if overflow:
        checks["unknown"] = [
            *checks["unknown"][: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
            "checks:incomplete",
        ]
    if not facts.review_threads_complete:
        blocking_review = "unknown"
    elif facts.review_decision == "changes_requested":
        blocking_review = "changes_requested"
    elif facts.unresolved_review_threads:
        blocking_review = "unresolved_threads"
    else:
        blocking_review = "none"
    return {
        "blocking_review": blocking_review,
        "checks": checks,
        "draft": facts.draft,
        "head_revision": facts.head_revision,
        "kind": facts.kind,
        "mergeability": facts.mergeability,
        "review_decision": facts.review_decision,
        "review_threads_complete": facts.review_threads_complete,
        "state": facts.state,
        "target": facts.target,
        "unresolved_review_threads": facts.unresolved_review_threads,
    }


def actionable_fingerprint_facts(canonical: Mapping[str, object]) -> dict[str, object]:
    """Keep known blockers stable while unrelated unsettled facts churn."""
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping):
        raise ValueError("canonical pull-request checks are malformed")
    blocking_review = canonical.get("blocking_review")
    mergeability = canonical.get("mergeability")
    return {
        "blocking_review": (
            blocking_review
            if blocking_review in {"changes_requested", "unresolved_threads"}
            else "none"
        ),
        "failed_checks": checks.get("failed"),
        "head_revision": canonical.get("head_revision"),
        "kind": canonical.get("kind"),
        "mergeability": (
            mergeability if mergeability in {"conflicting", "behind", "blocked"} else "none"
        ),
        "state": canonical.get("state"),
        "target": canonical.get("target"),
    }


def classify_pull_request_facts(
    facts: PullRequestFacts,
) -> tuple[MonitorObservationStatus, str]:
    """Apply the one cross-provider review-readiness precedence."""
    if facts.state == "merged":
        return MonitorObservationStatus.SUCCESS, "pull_request_merged"
    if facts.state == "closed":
        return MonitorObservationStatus.BLOCKED, "pull_request_closed"
    if facts.state != "open" or not facts.head_revision:
        return MonitorObservationStatus.PENDING, "pull_request_state_unknown"
    check_states = {check.state for check in facts.checks}
    if "failed" in check_states:
        return MonitorObservationStatus.ACTIONABLE, "checks_failed"
    if facts.review_decision == "changes_requested":
        return MonitorObservationStatus.ACTIONABLE, "changes_requested"
    if facts.unresolved_review_threads:
        return MonitorObservationStatus.ACTIONABLE, "unresolved_review_threads"
    if facts.mergeability == "conflicting":
        return MonitorObservationStatus.ACTIONABLE, "merge_conflict"
    if facts.mergeability == "behind":
        return MonitorObservationStatus.ACTIONABLE, "branch_behind"
    if facts.mergeability == "blocked":
        return MonitorObservationStatus.ACTIONABLE, "merge_blocked"
    if any(
        len({check.identity for check in facts.checks if check.state == state})
        > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET
        for state in PULL_REQUEST_CHECK_STATES
    ):
        return MonitorObservationStatus.PENDING, "checks_incomplete"
    if facts.draft:
        return MonitorObservationStatus.PENDING, "pull_request_draft"
    if "pending" in check_states:
        return MonitorObservationStatus.PENDING, "checks_pending"
    if "unknown" in check_states:
        return MonitorObservationStatus.PENDING, "checks_unknown"
    if not facts.review_threads_complete:
        return MonitorObservationStatus.PENDING, "review_threads_incomplete"
    if facts.review_decision == "unknown":
        return MonitorObservationStatus.PENDING, "review_state_unknown"
    if facts.review_decision == "review_required":
        return MonitorObservationStatus.PENDING, "review_required"
    if facts.mergeability == "pending":
        return MonitorObservationStatus.PENDING, "mergeability_pending"
    return MonitorObservationStatus.SUCCESS, "review_ready"


def fingerprint_pull_request_facts(canonical: Mapping[str, object]) -> str:
    """Hash the stable canonical JSON representation."""
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

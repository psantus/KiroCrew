"""GitLab merge-request readiness provider for structured monitors."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.github_runner import SetupError, gitlab_ambient_token_allowed
from kiro_crew.monitoring.models import ProviderErrorKind
from kiro_crew.monitoring.provider_cli import run_provider_cli
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    PullRequestProbeResult,
    PullRequestProviderError,
    build_pull_request_probe_result,
    classify_provider_error_text,
    opaque_provider_check_identity,
    provider_error_result,
    provider_failure_result,
)
from kiro_crew.monitoring.targets import GitLabMergeRequestTarget, parse_gitlab_merge_request_target

_TIMEOUT_SECS = 30.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_PAGE_SIZE = 100
_MAX_PROVIDER_ITEMS = _PAGE_SIZE * 2

GitLabFetch = Callable[[GitLabMergeRequestTarget, str, str], object]


class GitLabMergeRequestProvider:
    """Read GitLab.com or allowlisted self-managed merge requests through glab."""

    def __init__(
        self,
        *,
        gitlab_hosts: Sequence[str] | None = None,
        fetch: GitLabFetch | None = None,
    ) -> None:
        self._configured_hosts = tuple(gitlab_hosts) if gitlab_hosts is not None else None
        self._fetch = fetch or self._fetch_with_glab

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> PullRequestProbeResult:
        try:
            target = parse_gitlab_merge_request_target(
                raw_target,
                gitlab_hosts=self._gitlab_hosts(),
            )
            mr = _object(self._fetch(target, "merge_request", ""))
            head_revision = str(mr.get("sha", ""))
            pipelines = _list(self._fetch(target, "pipelines", head_revision))
            discussions = _list(self._fetch(target, "discussions", ""))
            facts = _facts(target, mr, pipelines, discussions)
        except PullRequestProviderError as exc:
            return provider_failure_result(exc)
        except PermissionError:
            return provider_error_result(
                ProviderErrorKind.AUTHENTICATION,
                "provider_authentication",
            )
        except SetupError:
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
        except subprocess.TimeoutExpired:
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except FileNotFoundError:
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
        except OSError:
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except (KeyError, TypeError, ValueError):
            return provider_error_result(
                ProviderErrorKind.TRANSIENT,
                "provider_malformed_response",
            )
        return build_pull_request_probe_result(
            facts,
            previous_observation=previous_observation,
        )

    def _gitlab_hosts(self) -> tuple[str, ...]:
        if self._configured_hosts is not None:
            return self._configured_hosts
        return tuple(KiroCrewConfig.load().dashboard.gitlab_hosts)

    @staticmethod
    def _fetch_with_glab(
        target: GitLabMergeRequestTarget,
        resource: str,
        head_revision: str,
    ) -> object:
        project = quote(target.project_path, safe="")
        root = f"projects/{project}/merge_requests/{target.iid}"
        if resource == "merge_request":
            return GitLabMergeRequestProvider._get_glab_page(target, root)
        if resource == "pipelines":
            if not head_revision:
                raise ValueError("GitLab response omitted the head revision")
            collection = f"projects/{project}/pipelines?sha={quote(head_revision, safe='')}"
        elif resource == "discussions":
            collection = f"{root}/discussions"
        else:
            raise ValueError("unsupported GitLab monitor resource")
        first = _list(
            GitLabMergeRequestProvider._get_glab_page(
                target,
                (
                    f"{collection}&per_page={_PAGE_SIZE}&page=1"
                    if "?" in collection
                    else f"{collection}?per_page={_PAGE_SIZE}&page=1"
                ),
            )
        )
        if len(first) < _PAGE_SIZE:
            return first
        second = _list(
            GitLabMergeRequestProvider._get_glab_page(
                target,
                (
                    f"{collection}&per_page={_PAGE_SIZE}&page=2"
                    if "?" in collection
                    else f"{collection}?per_page={_PAGE_SIZE}&page=2"
                ),
            )
        )
        return [*first, *second]

    @staticmethod
    def _get_glab_page(target: GitLabMergeRequestTarget, endpoint: str) -> object:
        proc = run_provider_cli(
            "glab",
            ["api", endpoint, "--hostname", target.host],
            timeout=_TIMEOUT_SECS,
            credentials=(
                None if gitlab_ambient_token_allowed(target.host) else {"GITLAB_TOKEN": ""}
            ),
        )
        if proc.returncode != 0:
            error = (proc.stderr or "").lower()
            raise PullRequestProviderError(classify_provider_error_text(error))
        raw = proc.stdout or ""
        if len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValueError("GitLab response exceeds the monitor bound")
        return json.loads(raw)


def _facts(
    target: GitLabMergeRequestTarget,
    mr: Mapping[str, Any],
    pipelines: list[object],
    discussions: list[object],
) -> PullRequestFacts:
    raw_state = str(mr["state"]).lower()
    state = {"opened": "open", "merged": "merged", "closed": "closed"}.get(
        raw_state,
        "unknown",
    )
    merge_status = str(mr.get("detailed_merge_status", "")).lower()
    if merge_status in {"mergeable", "can_be_merged"}:
        mergeability = "mergeable"
    elif merge_status in {"conflict", "conflicts", "cannot_be_merged"}:
        mergeability = "conflicting"
    elif merge_status == "need_rebase":
        mergeability = "behind"
    else:
        mergeability = "pending"
    checks: list[PullRequestCheck] = []
    for item in pipelines[:_MAX_PROVIDER_ITEMS]:
        pipeline = _object(item)
        pipeline_sha = pipeline.get("sha")
        if isinstance(pipeline_sha, str) and pipeline_sha and pipeline_sha != mr.get("sha"):
            continue
        status = str(pipeline.get("status", "")).lower()
        if status in {"failed", "canceled"}:
            normalized = "failed"
        elif status in {"success", "skipped"}:
            normalized = "passed"
        elif status in {
            "running",
            "pending",
            "created",
            "preparing",
            "waiting_for_resource",
            "manual",
        }:
            normalized = "pending"
        else:
            normalized = "unknown"
        identity = opaque_provider_check_identity(
            "pipeline",
            pipeline.get("id", "unknown"),
        )
        checks.append(PullRequestCheck(identity, normalized))
    if len(pipelines) >= _MAX_PROVIDER_ITEMS:
        checks.append(PullRequestCheck("pipelines:incomplete", "unknown"))
    approved_by = mr.get("approved_by")
    if merge_status == "not_approved":
        review_decision = "review_required"
    elif isinstance(approved_by, list) and approved_by:
        review_decision = "approved"
    else:
        review_decision = "none"
    unresolved = 0
    complete = len(discussions) < _MAX_PROVIDER_ITEMS
    for discussion_raw in discussions[:_MAX_PROVIDER_ITEMS]:
        discussion = _object(discussion_raw)
        notes = _list(discussion.get("notes", []))
        for note_raw in notes:
            note = _object(note_raw)
            if note.get("resolvable") is True and note.get("resolved") is not True:
                unresolved += 1
    return PullRequestFacts(
        kind="gitlab_merge_request",
        target=target.identity,
        state=state,
        draft=bool(mr.get("draft", mr.get("work_in_progress", False))),
        head_revision=str(mr.get("sha", "")),
        mergeability=mergeability,
        review_decision=review_decision,
        checks=tuple(checks),
        unresolved_review_threads=unresolved,
        review_threads_complete=complete,
    )


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be an object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("provider response must be a list")
    return value

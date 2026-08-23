"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import binding_key_for
from kiro_crew.autonudge import get_instance as _autonudge_get

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    authorize_and_add_nudge,
    authorize_and_stop_monitor,
    authorize_and_update_monitor,
    authorize_and_update_nudge,
    resolve_stop_sentinel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_AGENT_TURNS,
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_PROVIDER_ERRORS,
    DEFAULT_MONITOR_RUNTIME_SECS,
    DEFAULT_MONITOR_TOKENS,
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_RUNTIME_SECS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    PULL_REQUEST_MONITOR_KINDS,
    MonitorBudgets,
    MonitorState,
    monitor_state_public_dict,
)
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key, render_snapshot

logger = logging.getLogger(__name__)


async def ensure_gitlab_hosts_loaded() -> frozenset[str]:
    """Load provider host policy only when a monitor mutation needs it."""
    from kiro_crew.dashboard.handlers.source_providers import (
        ensure_gitlab_hosts_loaded as load_hosts,
    )

    return await load_hosts()


def render_nudge_message(message: str, stop_sentinel_path: str | None) -> str:
    """Replace {{STOP_FILE}} template with the resolved sentinel path."""
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


async def compose_nudge_body(
    message: str, stop_sentinel_path: str | None, slot_key: str | None
) -> str:
    """Compose one nudge cycle's full body text — the shared fire-path composer.

    Applies :func:`render_nudge_message`'s template substitution and, when the
    loop's session has a non-empty, non-terminal work ledger, prefixes a
    compact snapshot of it so every cycle starts from the durable state
    instead of from transcript memory. Derived server-side at fire time;
    sessions without a ledger render exactly as before.

    The ledger read is filesystem I/O, so it runs in a worker thread — a slow
    or wedged filesystem costs this loop's snapshot, never the event loop.
    Best-effort throughout: a snapshot failure must not cost the nudge itself.
    """
    body = render_nudge_message(message, stop_sentinel_path)
    if slot_key:
        try:
            snapshot = await asyncio.to_thread(render_snapshot, ledger_key(slot_key))
        except Exception:
            logger.debug("nudge: ledger snapshot failed for %s", slot_key, exc_info=True)
            snapshot = ""
        if snapshot:
            return f"{snapshot}\n\n{body}"
    return body


def _serialize(loop: Any) -> dict:
    payload = asdict(loop)
    if loop.monitor is None:
        # Legacy clients predate structured monitors and require their exact shape.
        payload.pop("monitor", None)
    else:
        payload["monitor"] = monitor_state_public_dict(loop.monitor)
    return payload


def _serialize_monitor(loop: Any) -> dict[str, Any]:
    return _serialize(loop)


def _monitor_error(message: str, code: str, *, status: int = 400) -> web.Response:
    response = web.json_response({"error": message, "code": code})
    response.set_status(status)
    return response


def _bounded_int(body: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = body.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return raw


def _monitor_config(body: dict[str, Any], *, gitlab_hosts: frozenset[str]) -> MonitorState:
    from kiro_crew.monitoring.targets import (
        infer_pull_request_kind,
        normalize_pull_request_target,
    )

    raw_target = body.get("target", "")
    kind = body.get("kind")
    allowed_gitlab_hosts = tuple(gitlab_hosts)
    if kind is None:
        kind = infer_pull_request_kind(
            raw_target,
            gitlab_hosts=allowed_gitlab_hosts,
        )
    objective = body.get("objective", "review_ready")
    if kind not in PULL_REQUEST_MONITOR_KINDS or objective != "review_ready":
        raise ValueError("only supported pull-request review_ready monitors are accepted")
    target = normalize_pull_request_target(
        kind,
        raw_target,
        gitlab_hosts=allowed_gitlab_hosts,
    )
    wake = body.get("wake_instructions", "")
    if not isinstance(wake, str) or len(wake) > MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS:
        raise ValueError(
            f"wake_instructions must be a string of at most "
            f"{MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} characters"
        )
    return MonitorState(
        kind=kind,
        target=target,
        objective=objective,
        created_ts=0.0,
        cadence_secs=_bounded_int(
            body,
            "cadence_secs",
            DEFAULT_MONITOR_CADENCE_SECS,
            MIN_MONITOR_CADENCE_SECS,
            MAX_MONITOR_CADENCE_SECS,
        ),
        budgets=MonitorBudgets(
            max_runtime_secs=_bounded_int(
                body,
                "max_runtime_secs",
                DEFAULT_MONITOR_RUNTIME_SECS,
                1,
                MAX_MONITOR_RUNTIME_SECS,
            ),
            max_agent_turns=_bounded_int(
                body,
                "max_agent_turns",
                DEFAULT_MONITOR_AGENT_TURNS,
                1,
                MAX_MONITOR_AGENT_TURNS,
            ),
            max_tokens=_bounded_int(
                body, "max_tokens", DEFAULT_MONITOR_TOKENS, 1, MAX_MONITOR_TOKENS
            ),
            max_provider_errors=_bounded_int(
                body,
                "max_provider_errors",
                DEFAULT_MONITOR_PROVIDER_ERRORS,
                1,
                MAX_MONITOR_PROVIDER_ERRORS,
            ),
        ),
        wake_instructions=wake.strip(),
    )


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_session_monitor_get(request: web.Request) -> web.Response:
    """Return only the structured monitor owned by the authenticated session."""
    session_key = request.headers.get("X-Session-Key", "")
    binding = binding_key_for(session_key)
    if not binding:
        return web.json_response(
            {"error": "authenticated session binding required", "code": "session_required"},
            status=401,
        )
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "monitor": None})
    loop = svc.get_by_slot(binding)
    if loop is None or loop.monitor is None:
        return web.json_response({"enabled": True, "monitor": None})
    monitor = loop.monitor
    return web.json_response(
        {
            "enabled": True,
            "active": bool(loop.active),
            "monitor_id": loop.id,
            "monitor": monitor_state_public_dict(monitor),
        }
    )


async def api_monitors_list(request: web.Request) -> web.Response:
    """GET /api/monitors — structured records, including terminal outcomes."""
    svc = _autonudge_get()
    monitors = (
        [] if svc is None else [_serialize_monitor(lp) for lp in svc.list_all() if lp.monitor]
    )
    return web.json_response({"enabled": svc is not None, "monitors": monitors})


async def api_monitor_slot_get(request: web.Request) -> web.Response:
    """GET /api/monitors/slot/{slot_key} — one dashboard-owned record."""
    svc = _autonudge_get()
    loop = svc.get_by_slot(request.match_info["slot_key"]) if svc is not None else None
    return web.json_response(
        {
            "enabled": svc is not None,
            "monitor": _serialize_monitor(loop) if loop is not None and loop.monitor else None,
        }
    )


async def api_monitor_create(request: web.Request) -> web.Response:
    """POST /api/monitors — create one bounded structured monitor."""
    svc = _autonudge_get()
    if svc is None:
        return _monitor_error("monitoring disabled", "monitoring_disabled", status=503)
    from kiro_crew.monitoring.targets import GitLabHostNotAllowed

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        gitlab_hosts = await ensure_gitlab_hosts_loaded()
        config = _monitor_config(body, gitlab_hosts=gitlab_hosts)
    except GitLabHostNotAllowed as exc:
        return _monitor_error(str(exc), "gitlab_host_not_allowed")
    except Exception as exc:
        return _monitor_error(str(exc), "invalid_monitor")
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=request.app["state"],
        slot_key=str(body.get("slot_key") or ""),
        message=config.wake_instructions or "structured monitor",
        idle_secs=config.cadence_secs,
        max_cycles=0,
        max_runtime_secs=config.budgets.max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
        monitor=config,
    )
    if error is not None:
        return _monitor_error(error, "monitor_create_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(loop)})


async def api_monitor_update(request: web.Request) -> web.Response:
    """PATCH /api/monitors/{id} — patch a nonterminal structured record."""
    svc = _autonudge_get()
    loop = (
        next(
            (lp for lp in svc.list_all() if lp.id == request.match_info["monitor_id"]),
            None,
        )
        if svc is not None
        else None
    )
    if loop is None or loop.monitor is None:
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    from kiro_crew.monitoring.targets import GitLabHostNotAllowed

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        current = loop.monitor
        merged = {
            "kind": current.kind,
            "target": body.get("target", current.target),
            "objective": body.get("objective", current.objective),
            "cadence_secs": body.get("cadence_secs", current.cadence_secs),
            "max_runtime_secs": body.get("max_runtime_secs", current.budgets.max_runtime_secs),
            "max_agent_turns": body.get("max_agent_turns", current.budgets.max_agent_turns),
            "max_tokens": body.get("max_tokens", current.budgets.max_tokens),
            "max_provider_errors": body.get(
                "max_provider_errors", current.budgets.max_provider_errors
            ),
            "wake_instructions": body.get("wake_instructions", current.wake_instructions),
        }
        gitlab_hosts = await ensure_gitlab_hosts_loaded()
        config = _monitor_config(merged, gitlab_hosts=gitlab_hosts)
    except GitLabHostNotAllowed as exc:
        return _monitor_error(str(exc), "gitlab_host_not_allowed")
    except Exception as exc:
        return _monitor_error(str(exc), "invalid_monitor")
    patch: dict[str, Any] = {}
    for name in ("target", "objective", "cadence_secs", "wake_instructions"):
        if name in body:
            patch[name] = getattr(config, name)
    budget_fields = {
        "max_runtime_secs",
        "max_agent_turns",
        "max_tokens",
        "max_provider_errors",
    }
    if budget_fields & set(body):
        patch["budgets"] = config.budgets
    if not patch:
        return _monitor_error("no monitor fields to update", "monitor_update_empty")
    updated, error, status = await authorize_and_update_monitor(
        svc=svc,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch=patch,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return _monitor_error(error, "monitor_update_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(updated)})


async def api_monitor_stop(request: web.Request) -> web.Response:
    """POST /api/monitors/{id}/stop — retain a durable user-stop outcome."""
    svc = _autonudge_get()
    if svc is None:
        return _monitor_error("monitoring disabled", "monitoring_disabled", status=503)
    loop = next(
        (lp for lp in svc.list_all() if lp.id == request.match_info["monitor_id"]),
        None,
    )
    if loop is None or loop.monitor is None:
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    stopped, error, status = await authorize_and_stop_monitor(
        svc=svc,
        loop_id=loop.id,
        session_key=loop.slot_key,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return _monitor_error(error, "monitor_stop_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(stopped)})


async def api_monitor_restart(request: web.Request) -> web.Response:
    """POST /api/monitors/{id}/restart — the sole browser revival route."""
    svc = _autonudge_get()
    loop = (
        next(
            (lp for lp in svc.list_all() if lp.id == request.match_info["monitor_id"]),
            None,
        )
        if svc is not None
        else None
    )
    if loop is None or loop.monitor is None:
        return _monitor_error("structured monitor not found", "monitor_not_found", status=404)
    if loop.monitor.version != MONITOR_STATE_VERSION:
        return _monitor_error(
            "monitor version is unsupported",
            MONITOR_STOP_UNSUPPORTED_VERSION,
            status=409,
        )
    if loop.monitor.outcome is None:
        return _monitor_error("only terminal monitors can restart", "monitor_not_terminal")
    state: DashboardState = request.app["state"]
    restarted, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=loop.slot_key,
        message=loop.monitor.wake_instructions or "structured monitor",
        idle_secs=loop.monitor.cadence_secs,
        max_cycles=0,
        max_runtime_secs=loop.monitor.budgets.max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
        monitor=loop.monitor,
    )
    if error is not None:
        return _monitor_error(error, "monitor_restart_denied", status=status)
    return web.json_response({"ok": True, "monitor": _serialize_monitor(restarted)})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?, stop_sentinel_path? }
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # idle_secs/max_cycles/max_runtime_secs come straight from the request
    # body: int() raises ValueError on "abc", TypeError on null/list, and
    # OverflowError on float("inf") (1e309 is legal JSON in aiohttp's parser),
    # any of which would surface as a 500 instead of a 400. Non-integral
    # floats are rejected rather than silently truncated (int(1.5) -> 1 would
    # store a value the caller never asked for). Coerce up front and reject
    # bad input, matching the sibling handlers_instances.api_instances_add
    # guard on the same pattern.
    try:
        for _name in ("idle_secs", "max_cycles", "max_runtime_secs"):
            _val = body.get(_name)
            if isinstance(_val, float) and not _val.is_integer():
                return web.json_response(
                    {"error": f"{_name} must be a whole number", "code": "not_a_whole_number"},
                    status=400,
                )
        idle_secs = int(body.get("idle_secs", 60))
        max_cycles = int(body.get("max_cycles", 0))
        max_runtime_secs = int(body.get("max_runtime_secs", 0))
    except (TypeError, ValueError, OverflowError):
        return web.json_response(
            {"error": "idle_secs, max_cycles and max_runtime_secs must be integers"}, status=400
        )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=(body.get("session_key") or body.get("slot_key") or ""),
        message=(body.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path=(body.get("stop_sentinel_path") or ""),
        max_runtime_secs=max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active.

    Thin HTTP mapping over ``authorize_and_update_nudge``, which owns the
    message redaction, the integer coercion, and the audit-or-deny policy — see
    its docstring for why those live in the transport-agnostic module and not
    here.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    if existing is not None and existing.monitor is not None:
        return _monitor_error(
            "structured monitors must use the monitor update API",
            "structured_monitor_requires_monitor_api",
            status=409,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
        max_runtime_secs=body.get("max_runtime_secs"),
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    if existing is not None and existing.monitor is not None:
        _stopped, error, status = await authorize_and_stop_monitor(
            svc=svc,
            loop_id=loop_id,
            session_key=existing.slot_key,
            source="dashboard",
            caller=request.remote or "",
        )
        if error is not None:
            return _monitor_error(error, "monitor_stop_denied", status=status)
        return web.json_response({"ok": True})
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})

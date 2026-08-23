"""Hardened, minimal-environment CLI transport for source monitor probes."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import IO

from kiro_crew import platform_compat
from kiro_crew.apps.registry import minimal_env
from kiro_crew.github_runner import (
    SetupError,
    provider_executable_candidates,
    validate_provider_executable,
)
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv
from kiro_crew.sel import sel

_OVERRIDE_ENV = {
    "glab": "KIROCREW_GLAB_BIN",
    "az": "KIROCREW_AZ_BIN",
}
_PASSTHROUGH = {
    "glab": frozenset({"GLAB_CONFIG_DIR", "GITLAB_TOKEN"}),
    "az": frozenset({"AZURE_CONFIG_DIR", "AZURE_DEVOPS_EXT_PAT"}),
}
_NETWORK_ENV = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
_AMBIENT_IDENTITY_KEYS = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"})
_INJECTION_ENV_KEYS = frozenset(
    {
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "NODE_PATH",
        "NVM_DIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def resolve_provider_cli(executable: str) -> str:
    """Resolve one allowlisted provider CLI through the shared trust policy."""
    if executable not in _OVERRIDE_ENV:
        raise SetupError("unsupported provider CLI")
    override_name = _OVERRIDE_ENV[executable]
    override = os.environ.get(override_name)
    candidates = (override,) if override is not None else provider_executable_candidates(executable)
    last_error = ""
    for candidate in candidates:
        if not candidate:
            last_error = "empty override"
            continue
        try:
            return validate_provider_executable(candidate)
        except ValueError as exc:
            last_error = str(exc)
    detail = f" ({last_error})" if last_error else ""
    raise SetupError(f"no usable `{executable}` CLI found{detail}")


def provider_cli_env(
    executable: str,
    *,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the exact provider-scoped child environment."""
    if executable not in _PASSTHROUGH:
        raise SetupError("unsupported provider CLI")
    supplied = dict(credentials or {})
    allowed = _PASSTHROUGH[executable] | _NETWORK_ENV
    values = {
        key: supplied[key] if key in supplied else os.environ.get(key, "")
        for key in allowed
        if (supplied[key] if key in supplied else os.environ.get(key, ""))
    }
    env = minimal_env(**values)
    for key in tuple(env):
        if key.upper() in _AMBIENT_IDENTITY_KEYS | _INJECTION_ENV_KEYS:
            del env[key]
    system_path = platform_compat.trusted_system_path()
    if system_path is None:
        env.pop("PATH", None)
    else:
        env["PATH"] = system_path
    env["NO_COLOR"] = "1"
    if executable == "glab":
        env["GLAMOUR_STYLE"] = "notty"
    else:
        env.setdefault("AZURE_CONFIG_DIR", os.path.join(os.path.expanduser("~"), ".azure"))
        env["AZURE_CORE_ONLY_SHOW_ERRORS"] = "1"
        env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "no"
    return env


def run_provider_cli(
    executable: str,
    argv: Sequence[str],
    *,
    timeout: float,
    credentials: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a validated provider CLI with bounded time, memory, and credentials."""
    binary = resolve_provider_cli(executable)
    try:
        _audit_provider_cli(executable, "invoked", critical=True)
    except Exception as exc:
        raise SetupError("provider CLI audit unavailable") from exc
    cleanup_path: str | None = None
    try:
        args = [binary, *argv]
        base_env = provider_cli_env(executable, credentials=credentials)
        visible_dirs = (base_env["AZURE_CONFIG_DIR"],) if executable == "az" else ()
        spawn_args, spawn_env, cleanup_path = sandboxed_spawn_argv(
            args,
            mode="standard",
            env=base_env,
            strip_python_env=True,
            extra_visible_dirs=visible_dirs,
        )
        for key in credentials or ():
            if key in base_env:
                spawn_env[key] = base_env[key]
            else:
                spawn_env.pop(key, None)
        with popen_limited(
            spawn_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=spawn_env,
            cwd=os.path.abspath(os.sep),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        ) as proc:
            assert proc.stdout is not None
            assert proc.stderr is not None
            with ThreadPoolExecutor(max_workers=2) as readers:
                stdout_future = readers.submit(
                    _read_limited,
                    proc.stdout,
                    _MAX_STDOUT_BYTES,
                    proc,
                )
                stderr_future = readers.submit(
                    _read_limited,
                    proc.stderr,
                    _MAX_STDERR_BYTES,
                    proc,
                )
                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_provider_tree(proc)
                    proc.wait()
                    raise
                stdout = stdout_future.result()
                stderr = stderr_future.result()
    except Exception:
        _audit_provider_cli(executable, "failed")
        raise
    finally:
        if cleanup_path:
            with suppress(OSError):
                os.unlink(cleanup_path)
    try:
        decoded = subprocess.CompletedProcess(
            args,
            returncode,
            stdout=stdout.decode("utf-8"),
            stderr=stderr.decode("utf-8"),
        )
    except UnicodeDecodeError as exc:
        _audit_provider_cli(executable, "failed")
        raise SetupError("provider CLI returned non-UTF-8 output") from exc
    _audit_provider_cli(executable, "completed" if decoded.returncode == 0 else "failed")
    return decoded


def _read_limited(
    stream: IO[bytes],
    maximum: int,
    proc: subprocess.Popen[bytes],
) -> bytes:
    """Drain one child pipe while terminating the process at the byte ceiling."""
    output = bytearray()
    while True:
        chunk = stream.read(min(_READ_CHUNK_BYTES, maximum + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > maximum:
            _kill_provider_tree(proc)
            raise ValueError("provider CLI output exceeds the monitor bound")


def _kill_provider_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the sandbox wrapper and provider descendants without orphaning."""
    if proc.poll() is not None:
        return
    try:
        platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
    except (OSError, ValueError):
        with suppress(ProcessLookupError):
            proc.kill()


def _audit_provider_cli(executable: str, outcome: str, *, critical: bool = False) -> None:
    """Record only provider identity and coarse lifecycle, never argv or output."""
    try:
        sel().log_api_access(
            caller="core:monitor",
            operation=f"monitor.{executable}_probe",
            outcome=outcome,
            source="builtin-app",
            resources=executable,
            critical=critical,
        )
    except Exception:
        if critical:
            raise

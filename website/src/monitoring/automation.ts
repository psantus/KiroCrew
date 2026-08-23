export const STRUCTURED_MONITOR_LIMITS = {
  cadenceSecs: { minimum: 15, maximum: 86_400, defaultValue: 300 },
  maxRuntimeSecs: { minimum: 1, maximum: 604_800, defaultValue: 14_400 },
  maxAgentTurns: { minimum: 1, maximum: 8, defaultValue: 8 },
  maxTokens: { minimum: 1, maximum: 1_000_000, defaultValue: 250_000 },
  maxProviderErrors: { minimum: 1, maximum: 20, defaultValue: 3 },
  wakeInstructions: { maximumLength: 1_000 },
} as const

export const STRUCTURED_MONITOR_DEFAULTS = {
  cadenceSecs: STRUCTURED_MONITOR_LIMITS.cadenceSecs.defaultValue,
  maxRuntimeSecs: STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.defaultValue,
  maxAgentTurns: STRUCTURED_MONITOR_LIMITS.maxAgentTurns.defaultValue,
  maxTokens: STRUCTURED_MONITOR_LIMITS.maxTokens.defaultValue,
  maxProviderErrors: STRUCTURED_MONITOR_LIMITS.maxProviderErrors.defaultValue,
} as const

export type MonitorStatus =
  | 'arm_pending'
  | 'active'
  | 'backing_off'
  | 'action_running'
  | 'success'
  | 'blocked'
  | 'budget_stopped'
  | 'user_stopped'

export const MONITOR_STATUS_KEYS: Record<MonitorStatus, string> = {
  action_running: 'components.sessionAutomationPopover.statuses.action_running',
  active: 'components.sessionAutomationPopover.statuses.active',
  arm_pending: 'components.sessionAutomationPopover.statuses.arm_pending',
  backing_off: 'components.sessionAutomationPopover.statuses.backing_off',
  blocked: 'components.sessionAutomationPopover.statuses.blocked',
  budget_stopped: 'components.sessionAutomationPopover.statuses.budget_stopped',
  success: 'components.sessionAutomationPopover.statuses.success',
  user_stopped: 'components.sessionAutomationPopover.statuses.user_stopped',
}

export interface LegacyGoalLoop {
  kind: 'legacy_goal_loop'
  id: string
  slotKey: string
  message: string
  idleSecs: number
  maxCycles: number
  cycleCount: number
  active: boolean
  lastFireAt: number
  stoppedReason: string
}

export interface StructuredMonitor {
  kind: 'structured_monitor'
  id: string
  slotKey: string
  active: boolean
  actionable: boolean
  version: number
  monitorKind: 'github_pull_request' | string
  objective: 'review_ready' | string
  target: string
  cadenceSecs: number
  nextProbeAt: number
  wakeInstructions: string
  budgets: {
    maxRuntimeSecs: number
    maxAgentTurns: number
    maxTokens: number
    maxProviderErrors: number
  }
  latest: {
    classification: string
    reasonCode: string
    summary: string
    observedAt: number
    decision: string
  }
  usage: {
    probes: number
    wakes: number
    agentTurns: number
    inputTokens: number
    outputTokens: number
    providerErrors: number
    tokenUsageKnown: boolean
  }
  action: {
    wakeInFlight: boolean
    wakeDelivery: string
  }
  terminal: null | {
    outcome: string
    reason: string
    stoppedAt: number
  }
}

export type AutomationRecord = LegacyGoalLoop | StructuredMonitor

type JsonObject = Record<string, unknown>

/** Match the backend's persisted dashboard-slot filename fold. Channel session
 * keys use transport punctuation (`slack:<ts>`) while dashboard slots use the
 * corresponding safe stem (`slack_<ts>`). */
export function dashboardAutomationSlotKey(key: string): string {
  let folded = key
  if (folded.startsWith('dashboard:')) folded = folded.slice('dashboard:'.length)
  while (folded.startsWith('dashboard_')) folded = folded.slice('dashboard_'.length)
  return folded.replace(/[^a-zA-Z0-9_.-]/g, '_')
}

function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function finite(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function count(value: unknown, fallback = 0): number {
  const n = finite(value, fallback)
  return Number.isInteger(n) && n >= 0 ? n : fallback
}

function positive(value: unknown, fallback: number): number {
  const n = finite(value, fallback)
  return Number.isInteger(n) && n > 0 ? n : fallback
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function isBoundedInteger(
  value: unknown,
  limits: { minimum: number; maximum: number },
): value is number {
  return isPositiveInteger(value) && value >= limits.minimum && value <= limits.maximum
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isTimestamp(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isNullableEnum(value: unknown, values: readonly string[]): boolean {
  return value === null || (typeof value === 'string' && values.includes(value))
}

function isEnum(value: unknown, values: readonly string[]): boolean {
  return typeof value === 'string' && values.includes(value)
}

function owns(value: JsonObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function hasExactKeys(value: JsonObject, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => owns(value, key))
}

function isStringList(value: unknown): boolean {
  return Array.isArray(value)
    && value.every(item => typeof item === 'string' && item.length > 0)
}

function isCanonicalGitHubObservation(value: JsonObject | null): boolean {
  if (!value) return false
  if (Object.keys(value).length === 0) return true
  const checks = object(value.checks)
  return hasExactKeys(value, [
    'blocking_review', 'checks', 'draft', 'head_revision', 'kind', 'mergeability',
    'review_decision', 'review_threads_complete', 'state', 'target',
    'unresolved_review_threads',
  ])
    && !!checks
    && hasExactKeys(checks, ['failed', 'passed', 'pending', 'unknown'])
    && value.kind === 'github_pull_request'
    && typeof value.target === 'string'
    && value.target.length > 0
    && typeof value.head_revision === 'string'
    && typeof value.draft === 'boolean'
    && typeof value.review_threads_complete === 'boolean'
    && isCount(value.unresolved_review_threads)
    && isEnum(value.blocking_review, [
      'unknown', 'changes_requested', 'unresolved_threads', 'none',
    ])
    && isEnum(value.mergeability, [
      'conflicting', 'behind', 'blocked', 'pending', 'mergeable',
    ])
    && isEnum(value.review_decision, [
      'none', 'approved', 'changes_requested', 'review_required', 'unknown',
    ])
    && isEnum(value.state, ['open', 'closed', 'merged', 'unknown'])
    && isStringList(checks.failed)
    && isStringList(checks.passed)
    && isStringList(checks.pending)
    && isStringList(checks.unknown)
}

function structuredFallback(loop: JsonObject, monitor: JsonObject | null): StructuredMonitor {
  const budgets = object(monitor?.budgets)
  const outcome = text(monitor?.outcome)
  return {
    kind: 'structured_monitor',
    id: text(loop.id),
    slotKey: dashboardAutomationSlotKey(text(loop.slot_key)),
    active: false,
    actionable: false,
    version: positive(monitor?.version, 1),
    monitorKind: text(monitor?.kind, 'unknown'),
    objective: text(monitor?.objective, 'unknown'),
    target: text(monitor?.target),
    cadenceSecs: positive(monitor?.cadence_secs, STRUCTURED_MONITOR_DEFAULTS.cadenceSecs),
    nextProbeAt: finite(monitor?.next_probe_at),
    wakeInstructions: text(monitor?.wake_instructions),
    budgets: {
      maxRuntimeSecs: positive(budgets?.max_runtime_secs, STRUCTURED_MONITOR_DEFAULTS.maxRuntimeSecs),
      maxAgentTurns: positive(budgets?.max_agent_turns, STRUCTURED_MONITOR_DEFAULTS.maxAgentTurns),
      maxTokens: positive(budgets?.max_tokens, STRUCTURED_MONITOR_DEFAULTS.maxTokens),
      maxProviderErrors: positive(budgets?.max_provider_errors, STRUCTURED_MONITOR_DEFAULTS.maxProviderErrors),
    },
    latest: {
      classification: text(monitor?.last_observation_status),
      reasonCode: text(monitor?.last_observation_reason_code),
      summary: text(monitor?.last_observation_summary),
      observedAt: finite(monitor?.last_observed_at),
      decision: text(monitor?.last_decision),
    },
    usage: {
      probes: count(monitor?.probe_count),
      wakes: count(monitor?.wake_count),
      agentTurns: count(monitor?.agent_turns),
      inputTokens: count(monitor?.input_tokens),
      outputTokens: count(monitor?.output_tokens),
      providerErrors: count(monitor?.provider_error_count),
      tokenUsageKnown: monitor?.token_usage_known === true,
    },
    action: {
      wakeInFlight: false,
      wakeDelivery: '',
    },
    terminal: outcome ? {
      outcome,
      reason: text(monitor?.stopped_reason, text(loop.stopped_reason)),
      stoppedAt: finite(monitor?.stopped_at),
    } : {
      outcome: 'blocked',
      reason: 'invalid_monitor_record',
      stoppedAt: finite(monitor?.stopped_at),
    },
  }
}

/**
 * Normalize either a REST loop record or an `autonudge_state` WS envelope.
 * A structured marker is never downgraded to legacy when its payload is
 * malformed or from a newer schema: it remains inspectable and inert.
 */
export function normalizeAutomationRecord(raw: unknown): AutomationRecord | null {
  const envelope = object(raw)
  if (!envelope) return null
  const loop = object(envelope.loop) ?? envelope
  const removed = object(envelope.loop) !== null && envelope.event === 'removed'
  if (removed && owns(loop, 'monitor')) return null
  const id = text(loop.id)
  const slotKey = dashboardAutomationSlotKey(text(loop.slot_key, text(envelope.slot)))
  if (!id || !slotKey) return null

  if (!owns(loop, 'monitor')) {
    return {
      kind: 'legacy_goal_loop',
      id,
      slotKey,
      message: text(loop.message),
      idleSecs: count(loop.idle_secs),
      maxCycles: count(loop.max_cycles),
      cycleCount: count(loop.cycle_count),
      active: !removed && loop.active === true,
      lastFireAt: finite(loop.last_fire_ts),
      stoppedReason: text(loop.stopped_reason),
    }
  }

  const monitor = object(loop.monitor)
  const budgets = object(monitor?.budgets)
  const observation = object(monitor?.last_observation)
  const version = positive(monitor?.version, 1)
  const requiredCountsValid = [
    monitor?.wake_count,
    monitor?.agent_turns,
    monitor?.input_tokens,
    monitor?.output_tokens,
    monitor?.probe_count,
    monitor?.provider_error_count,
    monitor?.consecutive_provider_errors,
  ].every(isCount)
  const requiredLimitsValid = !!budgets
    && isBoundedInteger(monitor?.cadence_secs, STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    && isBoundedInteger(budgets.max_runtime_secs, STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs)
    && isBoundedInteger(budgets.max_agent_turns, STRUCTURED_MONITOR_LIMITS.maxAgentTurns)
    && isBoundedInteger(budgets.max_tokens, STRUCTURED_MONITOR_LIMITS.maxTokens)
    && isBoundedInteger(
      budgets.max_provider_errors,
      STRUCTURED_MONITOR_LIMITS.maxProviderErrors,
    )
  const requiredTimestampsValid = [
    loop.last_fire_ts,
    loop.next_due_ts,
    monitor?.last_observed_at,
    monitor?.completion_evidence_deadline,
    monitor?.last_completed_at,
    monitor?.last_probe_at,
    monitor?.next_probe_at,
    monitor?.stopped_at,
  ].every(isTimestamp)
  const requiredStringsValid = [
    loop.stopped_reason,
    monitor?.last_fingerprint,
    monitor?.last_wake_fingerprint,
    monitor?.last_completion_fingerprint,
    monitor?.last_observation_reason_code,
    monitor?.last_observation_summary,
    monitor?.stopped_reason,
  ].every(value => typeof value === 'string')
  const observationValid = isCanonicalGitHubObservation(observation)
  const enumsValid = isNullableEnum(monitor?.wake_delivery, [
    'dispatched', 'busy', 'unavailable',
  ]) && isNullableEnum(monitor?.last_completion_disposition, [
    'success', 'failure', 'cancellation', 'approval_stall',
  ]) && isNullableEnum(monitor?.last_decision, [
    'no_change', 'record_only', 'wake_actionable', 'stop_success', 'stop_blocked',
    'retry_provider', 'stop_budget',
  ]) && isNullableEnum(monitor?.last_provider_error, [
    'transient', 'rate_limited', 'authentication', 'authorization', 'not_found', 'setup',
  ]) && isNullableEnum(monitor?.last_observation_status, [
    'pending', 'actionable', 'success', 'blocked', 'provider_error',
  ]) && isNullableEnum(monitor?.outcome, [
    'success', 'blocked', 'budget', 'user_stop', 'session_close', 'target_unavailable',
  ])
  const outcome = typeof monitor?.outcome === 'string' ? monitor.outcome : null
  const active = !removed && loop.active === true
  const lifecycleValid = typeof loop.active === 'boolean' && active !== (outcome !== null)
  const supported = !!monitor
    && monitor.version === 1
    && text(monitor.kind) === 'github_pull_request'
    && text(monitor.objective) === 'review_ready'
    && !!text(monitor.target)
    && !!budgets
    && requiredLimitsValid
    && requiredCountsValid
    && requiredTimestampsValid
    && requiredStringsValid
    && isPositiveInteger(monitor.config_generation)
    && observationValid
    && enumsValid
    && typeof monitor.wake_in_flight === 'boolean'
    && typeof monitor.token_usage_known === 'boolean'
    && typeof monitor.wake_instructions === 'string'
    && monitor.wake_instructions.length
      <= STRUCTURED_MONITOR_LIMITS.wakeInstructions.maximumLength
    && lifecycleValid
  if (!supported) return structuredFallback(loop, monitor)

  return {
    kind: 'structured_monitor',
    id,
    slotKey,
    active,
    actionable: true,
    version,
    monitorKind: text(monitor.kind),
    objective: text(monitor.objective),
    target: text(monitor.target),
    cadenceSecs: positive(monitor.cadence_secs, STRUCTURED_MONITOR_DEFAULTS.cadenceSecs),
    nextProbeAt: finite(monitor.next_probe_at, finite(loop.next_due_ts)),
    wakeInstructions: text(monitor.wake_instructions),
    budgets: {
      maxRuntimeSecs: positive(budgets.max_runtime_secs, STRUCTURED_MONITOR_DEFAULTS.maxRuntimeSecs),
      maxAgentTurns: positive(budgets.max_agent_turns, STRUCTURED_MONITOR_DEFAULTS.maxAgentTurns),
      maxTokens: positive(budgets.max_tokens, STRUCTURED_MONITOR_DEFAULTS.maxTokens),
      maxProviderErrors: positive(budgets.max_provider_errors, STRUCTURED_MONITOR_DEFAULTS.maxProviderErrors),
    },
    latest: {
      classification: text(monitor.last_observation_status),
      reasonCode: text(monitor.last_observation_reason_code),
      summary: text(monitor.last_observation_summary),
      observedAt: finite(monitor.last_observed_at),
      decision: text(monitor.last_decision),
    },
    usage: {
      probes: count(monitor.probe_count),
      wakes: count(monitor.wake_count),
      agentTurns: count(monitor.agent_turns),
      inputTokens: count(monitor.input_tokens),
      outputTokens: count(monitor.output_tokens),
      providerErrors: count(monitor.provider_error_count),
      tokenUsageKnown: monitor.token_usage_known === true,
    },
    action: {
      wakeInFlight: monitor.wake_in_flight === true,
      wakeDelivery: text(monitor.wake_delivery),
    },
    terminal: outcome ? {
      outcome,
      reason: text(monitor.stopped_reason, text(loop.stopped_reason)),
      stoppedAt: finite(monitor.stopped_at),
    } : null,
  }
}

export function deriveAutomationStatus(record: AutomationRecord): MonitorStatus {
  if (record.kind === 'legacy_goal_loop') return record.active ? 'active' : 'user_stopped'
  if (!record.actionable) return 'blocked'
  if (record.terminal) {
    if (record.terminal.outcome === 'success') return 'success'
    if (record.terminal.outcome === 'budget') return 'budget_stopped'
    if (record.terminal.outcome === 'user_stop') return 'user_stopped'
    return 'blocked'
  }
  if (!record.active) return 'blocked'
  if (record.action.wakeInFlight && record.action.wakeDelivery === 'dispatched') {
    return 'action_running'
  }
  if (record.latest.decision === 'retry_provider' || record.action.wakeDelivery === 'busy') {
    return 'backing_off'
  }
  return record.usage.probes === 0 ? 'arm_pending' : 'active'
}

/** Read a slot-indexed collection without walking Object.prototype. */
export function automationForSlot(
  automations: Record<string, AutomationRecord> | undefined,
  slotKey: string,
): AutomationRecord | null {
  if (!automations || !Object.prototype.hasOwnProperty.call(automations, slotKey)) return null
  return automations[slotKey] ?? null
}

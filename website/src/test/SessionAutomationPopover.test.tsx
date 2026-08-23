import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SessionAutomationPopover from '../components/SessionAutomationPopover'
import { normalizeAutomationRecord, type StructuredMonitor } from '../monitoring/automation'
import { api } from '../api/client'
import { structuredMonitorLoop } from './monitorFixtures'

vi.mock('../api/client', () => ({
  api: {
    monitorCreate: vi.fn(),
    monitorUpdate: vi.fn(),
    monitorStop: vi.fn(),
    monitorRestart: vi.fn(),
  },
}))

const activeMonitor: StructuredMonitor = {
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'chat-1', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', cadenceSecs: 300,
  nextProbeAt: 1_800_000_300, wakeInstructions: 'Address actionable review feedback.',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'pending', reasonCode: 'checks_pending', summary: 'Two checks remain.', observedAt: 1_800_000_000, decision: 'no_change' },
  usage: { probes: 5, wakes: 2, agentTurns: 2, inputTokens: 1200, outputTokens: 300, providerErrors: 1, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
}

function renderPopover(automation: StructuredMonitor | null, onChange = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const props = (next: StructuredMonitor | null) => (
    <QueryClientProvider client={client}>
      <SessionAutomationPopover
        slotKey="chat-1"
        automation={next}
        open
        onOpenChange={() => {}}
        onChange={onChange}
      />
    </QueryClientProvider>
  )
  const view = render(props(automation))
  return {
    onChange,
    ...view,
    rerenderAutomation: (next: StructuredMonitor | null) => view.rerender(props(next)),
  }
}

describe('SessionAutomationPopover', () => {
  beforeEach(() => vi.clearAllMocks())

  it('creates a bounded review monitor with the documented defaults', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith({
      slot_key: 'chat-1',
      kind: 'github_pull_request',
      objective: 'review_ready',
      target: 'https://github.com/kirodotdev/KiroCrew/pull/42',
      cadence_secs: 300,
      max_runtime_secs: 14_400,
      max_agent_turns: 8,
      max_tokens: 250_000,
      max_provider_errors: 3,
      wake_instructions: '',
    }))
  })

  it.each(['', '0', '-1', '1.5', 'NaN'])('rejects %j as an unbounded cadence', async value => {
    renderPopover(null)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }), {
      target: { value },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter a whole number from 15 to 86,400.')).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each([
    ['Probe cadence in seconds', '86401', 'Enter a whole number from 15 to 86,400.'],
    ['Maximum runtime in seconds', '604801', 'Enter a whole number from 1 to 604,800.'],
    ['Maximum agent turns', '9', 'Enter a whole number from 1 to 8.'],
    ['Maximum tokens', '1000001', 'Enter a whole number from 1 to 1,000,000.'],
    ['Maximum provider errors', '21', 'Enter a whole number from 1 to 20.'],
  ])('shows an inline backend-bound error for %s', async (name, value, message) => {
    renderPopover(null)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name }), { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('exposes exact input bounds and rejects oversized wake instructions inline', async () => {
    renderPopover(null)

    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('min', '15')
    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('max', '86400')
    expect(screen.getByRole('spinbutton', { name: 'Maximum agent turns' }))
      .toHaveAttribute('max', '8')
    const wake = screen.getByRole('textbox', { name: 'Instructions for an actionable wake' })
    expect(wake).toHaveAttribute('maxlength', '1000')

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(wake, { target: { value: 'x'.repeat(1001) } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter no more than 1,000 characters.')).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('shows monitor evidence and requires confirmation before stopping', async () => {
    ;(api.monitorStop as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(activeMonitor)

    expect(screen.getByText('Two checks remain.')).toBeInTheDocument()
    expect(screen.getByText('Probes: 5')).toBeInTheDocument()
    expect(screen.getByText('Wakes: 2')).toBeInTheDocument()
    expect(screen.getByText('Tokens: 1,500')).toBeInTheDocument()
    expect(screen.getByText('250,000')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Stop monitor' }))
    expect(api.monitorStop).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop' }))
    await waitFor(() => expect(api.monitorStop).toHaveBeenCalledWith('monitor-1'))
  })

  it('does not overwrite a newer websocket state with a mutation response', async () => {
    let resolveUpdate!: (value: { ok: true, monitor: Record<string, unknown> }) => void
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(resolve => {
      resolveUpdate = resolve
    }))
    const { onChange, rerenderAutomation } = renderPopover(activeMonitor)

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for an actionable wake' }),
      { target: { value: 'Address the latest review.' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    rerenderAutomation({
      ...activeMonitor,
      active: false,
      actionable: false,
      terminal: { outcome: 'success', reason: 'review_ready', stoppedAt: 1_800_000_400 },
    })
    await act(async () => {
      resolveUpdate({ ok: true, monitor: structuredMonitorLoop() })
      await Promise.resolve()
    })

    expect(onChange).not.toHaveBeenCalled()
  })

  it('renders typed classification beside the exact canonical GitHub facts', () => {
    const record = normalizeAutomationRecord(structuredMonitorLoop())
    expect(record?.kind).toBe('structured_monitor')

    renderPopover(record as StructuredMonitor)

    expect(screen.getByText('pending · checks_pending')).toBeInTheDocument()
    expect(screen.getByText('Two checks remain.')).toBeInTheDocument()
  })

  it('uses the static Framer state under reduced motion and the shared Lucide seam', () => {
    const originalMatchMedia = window.matchMedia
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
    try {
      const { container } = renderPopover({
        ...activeMonitor,
        action: { wakeInFlight: true, wakeDelivery: 'dispatched' },
      })

      expect(container.querySelector('[data-monitor-action-pulse="false"]')).toBeTruthy()
      for (const icon of container.querySelectorAll('.lucide-radar, .lucide-x, .lucide-square, .lucide-activity')) {
        expect(icon).toHaveClass('lucide-inline')
      }
      expect(container.querySelector('.animate-pulse')).toBeNull()
    } finally {
      window.matchMedia = originalMatchMedia
    }
  })

  it('keeps dirty fields while reconciling untouched fields and sends a sparse update', async () => {
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, monitor: {},
    })
    const { rerenderAutomation } = renderPopover(activeMonitor)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/99' },
    })
    rerenderAutomation({
      ...activeMonitor,
      cadenceSecs: 600,
      wakeInstructions: 'Use the latest server instructions.',
    })

    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toHaveValue(
      'https://github.com/kirodotdev/KiroCrew/pull/99',
    )
    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' })).toHaveValue(600)
    expect(screen.getByRole('textbox', { name: 'Instructions for an actionable wake' }))
      .toHaveValue('Use the latest server instructions.')

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalledWith('monitor-1', {
      target: 'https://github.com/kirodotdev/KiroCrew/pull/99',
    }))
  })

  it('does not submit unchanged monitor values', () => {
    renderPopover(activeMonitor)

    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(api.monitorUpdate).not.toHaveBeenCalled()
  })

  it('keeps terminal monitors read-only and revives them only through Restart', async () => {
    ;(api.monitorRestart as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover({
      ...activeMonitor,
      active: false,
      terminal: { outcome: 'budget', reason: 'token_budget', stoppedAt: 1_800_000_100 },
    })

    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop monitor' })).not.toBeInTheDocument()
    expect(screen.getByText('token_budget')).toBeInTheDocument()
    expect(screen.getByText('Address actionable review feedback.')).toBeInTheDocument()
    expect(screen.getByText('250,000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart monitor' }))
    await waitFor(() => expect(api.monitorRestart).toHaveBeenCalledWith('monitor-1'))
  })

  it('offers the old costly loop explicitly without changing zero-unlimited semantics', () => {
    renderPopover(null)
    fireEvent.click(screen.getByRole('button', { name: 'Use legacy goal loop (costly)' }))

    expect(screen.getByText('Use legacy goal loop (costly)')).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'Max cycles (0 = infinite)' })).toHaveValue(0)
  })
})

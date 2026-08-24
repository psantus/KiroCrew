import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { VariablesPanel, nameError, valueError } from './VariablesPanel'
import { api, ApiError, type VariablesView } from '../../api/client'

// The REAL ApiError is re-exported through the mock: the component branches on
// `instanceof ApiError`, so a locally-defined stand-in would make the test agree
// with an invention instead of the shipped contract — which is exactly how an
// earlier version of these tests validated a `detail` field that never existed.
vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { api: { variables: vi.fn(), saveVariables: vi.fn() }, ApiError: actual.ApiError }
})

const variables = vi.mocked(api.variables!)
const saveVariables = vi.mocked(api.saveVariables!)

function view(over: Partial<VariablesView> = {}): VariablesView {
  return { global: {}, workspaces: {}, effective: {}, winning_scope: {}, ...over }
}

function mount() {
  // `retry: false` so a rejected fetch settles on the first rejection instead of
  // outliving the test's timeout.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The client is handed back so a test can force a refetch MID-EDIT — the only way
  // to tell a baseline captured when the editor opened from one read at apply time.
  return Object.assign(
    render(
      <QueryClientProvider client={qc}>
        <VariablesPanel />
      </QueryClientProvider>,
    ),
    { qc },
  )
}

/**
 * Wait until the fetch has settled.
 *
 * Not cosmetic: every control is disabled while the query is in flight, because a
 * write merges into the pairs the panel has read and writing before they arrive
 * would erase them. So a test that fires an event too early silently does nothing.
 * The Add button becoming enabled is the observable form of "the read landed".
 */
const ready = () => waitFor(() => expect(screen.getAllByRole('button', { name: 'Add' })[0]).toBeEnabled())

/** The workspace block's own region, so its add form is addressable apart from the
 *  global one's — both carry a `Name` and a `Value` field. */
const workspaceRegion = (name: string) => screen.getByRole('region', { name: `Workspace ${name}` })

/** An add-form field. Addressed by ROLE + exact accessible name: a row's value input
 *  is named "<NAME> Value", so only the add form's field is named plain "Value". */
const field = (scope: { getByRole: typeof screen.getByRole }, name: 'Name' | 'Value') =>
  scope.getByRole('textbox', { name })

beforeEach(() => {
  vi.clearAllMocks()
  saveVariables.mockResolvedValue({ ok: true })
})

describe('validation predicates', () => {
  // These sit apart from the DOM tests for one case the DOM cannot reach: a single-line
  // <input> strips a newline before React ever sees it, so the rule that refuses one —
  // the rule that stops a value from forging a context header in the assembled prompt —
  // has no UI path to exercise it. The rest are here as boundaries, cheap because the
  // predicates are pure.
  it('refuses a newline, which a single-line input can never deliver', () => {
    expect(valueError('first\nsecond')).toBe('value-control')
    expect(valueError('first\rsecond')).toBe('value-control')
  })

  it('refuses DEL as well as the C0 range', () => {
    expect(valueError('a\u007Fb')).toBe('value-control')
    expect(valueError('a\u0000b')).toBe('value-control')
  })

  it('allows tab and ordinary text', () => {
    expect(valueError('a\tb')).toBeNull()
    expect(valueError('https://example.test/v1?q=1')).toBeNull()
  })

  it('puts the length boundary at exactly 4096', () => {
    expect(valueError('x'.repeat(4096))).toBeNull()
    expect(valueError('x'.repeat(4097))).toBe('value-length')
  })

  it('accepts an identifier and refuses everything else', () => {
    expect(nameError('BASE_URL')).toBeNull()
    expect(nameError('a')).toBeNull()
    expect(nameError('')).toBe('name-pattern')
    expect(nameError('_LEADING')).toBe('name-pattern')
    expect(nameError('9LIVES')).toBe('name-pattern')
    expect(nameError('HAS SPACE')).toBe('name-pattern')
    expect(nameError('HAS-DASH')).toBe('name-pattern')
    expect(nameError('CAFÉ')).toBe('name-pattern')
  })

  it('refuses a reserved name case-sensitively, as the backend set does', () => {
    expect(nameError('ALIAS')).toBe('name-reserved')
    expect(nameError('bot_name')).toBe('name-reserved')
    // `alias` is NOT the reserved spelling, so it is an ordinary usable name.
    expect(nameError('alias')).toBeNull()
  })

  it('reports a duplicate only against the names at the same scope', () => {
    expect(nameError('REGION', ['REGION'])).toBe('name-duplicate')
    expect(nameError('REGION', ['BASE_URL'])).toBeNull()
  })

  it('reports the grammar before the duplicate, so the first fix is the useful one', () => {
    expect(nameError('9REGION', ['9REGION'])).toBe('name-pattern')
  })
})

describe('VariablesPanel', () => {
  it('renders both sections and the plain-text warning', async () => {
    variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
    mount()

    expect(await screen.findByText('Global Environment Variables')).toBeInTheDocument()
    expect(screen.getByText('Workspace Environment Variables')).toBeInTheDocument()
    expect(screen.getByText(/not a secret store/i)).toBeInTheDocument()
    expect(screen.getByText(/plain text in variables\.json/i)).toBeInTheDocument()
    expect(await screen.findByText('BASE_URL')).toBeInTheDocument()
  })

  it('renders an empty state instead of throwing when the route is missing', async () => {
    // The route ships after this panel, so a 404 (and a partially mocked client) is
    // the expected first-run condition, not an exception.
    variables.mockRejectedValue(new Error('404'))
    mount()

    expect(await screen.findByText(/could not be read from the gateway/i)).toBeInTheDocument()
    expect(screen.getByText('No variables are defined at this scope yet.')).toBeInTheDocument()
    expect(screen.getByText('No workspace defines variables of its own.')).toBeInTheDocument()
  })

  it('survives a client whose variables method is absent', async () => {
    // Exactly what a partial `vi.mock('../../api/client')` in another suite produces.
    const original = api.variables
    // @ts-expect-error deliberately removing the method to reproduce a partial mock
    api.variables = undefined
    try {
      mount()
      expect(await screen.findByText('Global Environment Variables')).toBeInTheDocument()
    } finally {
      api.variables = original
    }
  })

  describe('global scope', () => {
    it('adds a pair, keeping the pairs already stored', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { REGION: 'eu-west-1' },
      }))
    })

    it('clears the add form once the pair is submitted', async () => {
      variables.mockResolvedValue(view())
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      await waitFor(() => expect(field(screen, 'Name')).toHaveValue(''))
    })

    it('adds on Enter, so the form is keyboard operable without the button', async () => {
      variables.mockResolvedValue(view())
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.keyDown(field(screen, 'Value'), { key: 'Enter' })

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { REGION: 'eu-west-1' },
      }))
    })

    it('edits a value on blur', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://old.test' } }))
      mount()
      await ready()
      const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })

      fireEvent.change(input, { target: { value: 'https://new.test' } })
      fireEvent.blur(input)

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { BASE_URL: 'https://new.test' },
      }))
    })

    it('edits a value on Enter', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://old.test' } }))
      mount()
      await ready()
      const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })

      fireEvent.change(input, { target: { value: 'https://new.test' } })
      fireEvent.keyDown(input, { key: 'Enter' })

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { BASE_URL: 'https://new.test' },
      }))
    })

    it('does not write when a value was opened but left unchanged', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://old.test' } }))
      mount()
      await ready()
      const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })

      fireEvent.focus(input)
      fireEvent.blur(input)

      expect(saveVariables).not.toHaveBeenCalled()
    })

    it('deletes a pair with an explicit delete verb', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test', REGION: 'eu-west-1' } }))
      mount()
      await ready()
      // Two clicks: the first ARMS, the second removes. A single mis-click on a
      // small icon used to be permanent — there is no undo and a value can be 4096
      // characters.
      fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
      expect(saveVariables).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Confirm removing REGION' }))

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        delete: ['REGION'],
      }))
    })

    it('keeps a typed edit when the delete is refused', async () => {
      // The draft used to be cleared BEFORE the request, so a refused delete threw
      // away what the user had typed and put the stored value back in the input —
      // on a surface with no undo, for a value that can be 4096 characters. The
      // row-save path beside it already ties clearing to success; this did not.
      variables.mockResolvedValue(view({ global: { REGION: 'eu-west-1' } }))
      saveVariables.mockRejectedValue(new Error('boom'))
      mount()
      await ready()

      const input = screen.getByDisplayValue('eu-west-1')
      fireEvent.change(input, { target: { value: 'us-east-1 (typed, not yet saved)' } })
      fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
      fireEvent.click(screen.getByRole('button', { name: 'Confirm removing REGION' }))

      await waitFor(() => expect(saveVariables).toHaveBeenCalled())
      expect(screen.getByDisplayValue('us-east-1 (typed, not yet saved)')).toBeTruthy()
    })

    it('clears the draft once the delete lands', async () => {
      // The other half: tying it to success must not mean never clearing it, or the
      // stale draft would shadow a later refetch of the same key.
      variables.mockResolvedValue(view({ global: { REGION: 'eu-west-1' } }))
      mount()
      await ready()

      fireEvent.change(screen.getByDisplayValue('eu-west-1'), { target: { value: 'typed' } })
      fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
      fireEvent.click(screen.getByRole('button', { name: 'Confirm removing REGION' }))

      await waitFor(() => expect(screen.queryByDisplayValue('typed')).toBeNull())
    })

    it('reports a failed write', async () => {
      variables.mockResolvedValue(view())
      saveVariables.mockRejectedValue(new Error('boom'))
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      expect(await screen.findByText('Could not save environment variables.')).toBeInTheDocument()
    })

    it("surfaces the backend's reason for a refusal, not a generic failure", async () => {
      // Constructs a REAL ApiError. An earlier version rejected with a hand-made
      // `{status, detail}` object and the component read `detail` — a field ApiError
      // does not define — so the unwrap was dead code and the test passed by
      // agreeing with the same invention. The field is `body`.
      variables.mockResolvedValue(view())
      saveVariables.mockRejectedValue(
        new ApiError(
          400,
          'Bad Request',
          JSON.stringify({
            error: 'no such workspace: research',
            code: 'variables_unknown_workspace',
          }),
        ),
      )
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      expect(
        await screen.findByText('no such workspace: research'),
      ).toBeInTheDocument()
    })

    it('keeps what the user typed when the save is refused', async () => {
      // Clearing the form on dispatch left the user an error message and an empty
      // form, with nothing to correct or retry.
      variables.mockResolvedValue(view())
      saveVariables.mockRejectedValue(
        new ApiError(400, 'Bad Request', JSON.stringify({ error: 'nope', code: 'x' })),
      )
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      expect(await screen.findByText('nope')).toBeInTheDocument()
      expect(field(screen, 'Name')).toHaveValue('REGION')
      expect(field(screen, 'Value')).toHaveValue('eu-west-1')
    })

    it('clears the form once the save lands', async () => {
      variables.mockResolvedValue(view())
      saveVariables.mockResolvedValue({ ok: true })
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      await waitFor(() => expect(field(screen, 'Name')).toHaveValue(''))
      expect(field(screen, 'Value')).toHaveValue('')
    })

    it('falls back to the generic message when the body is not JSON', async () => {
      variables.mockResolvedValue(view())
      saveVariables.mockRejectedValue(new ApiError(500, 'Server Error', '<html>gateway</html>'))
      mount()
      await ready()

      fireEvent.change(field(screen, 'Name'), { target: { value: 'REGION' } })
      fireEvent.change(field(screen, 'Value'), { target: { value: 'eu-west-1' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))

      expect(
        await screen.findByText('Could not save environment variables.'),
      ).toBeInTheDocument()
      // The raw body must not be shown.
      expect(screen.queryByText(/<html>/)).not.toBeInTheDocument()
    })
  })

  describe('workspace scope', () => {
    const withWorkspace = () => view({
      global: { BASE_URL: 'https://example.test' },
      workspaces: { research: { REGION: 'eu-west-1' } },
    })

    it('adds a pair at the named workspace', async () => {
      variables.mockResolvedValue(withWorkspace())
      mount()
      await ready()
      const ws = within(workspaceRegion('research'))

      fireEvent.change(field(ws, 'Name'), { target: { value: 'STAGE' } })
      fireEvent.change(field(ws, 'Value'), { target: { value: 'beta' } })
      fireEvent.click(ws.getByRole('button', { name: 'Add' }))

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'workspace',
        workspace: 'research',
        set: { STAGE: 'beta' },
      }))
    })

    it('edits a workspace value', async () => {
      variables.mockResolvedValue(withWorkspace())
      mount()
      await ready()
      const input = screen.getByRole('textbox', { name: 'REGION Value' })

      fireEvent.change(input, { target: { value: 'us-east-1' } })
      fireEvent.blur(input)

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'workspace',
        workspace: 'research',
        set: { REGION: 'us-east-1' },
      }))
    })

    it('deletes a workspace pair', async () => {
      variables.mockResolvedValue(withWorkspace())
      mount()
      await ready()
      fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
      fireEvent.click(screen.getByRole('button', { name: 'Confirm removing REGION' }))

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'workspace',
        workspace: 'research',
        delete: ['REGION'],
      }))
    })

    it('keeps the two scopes apart when both define the same name', async () => {
      // A global edit must not carry the workspace's pairs into the global write.
      variables.mockResolvedValue(view({
        global: { REGION: 'eu-west-1' },
        workspaces: { research: { REGION: 'us-east-1' } },
      }))
      mount()
      await ready()
      const globalTable = screen.getAllByRole('table')[0]
      const input = within(globalTable).getByRole('textbox', { name: 'REGION Value' })

      fireEvent.change(input, { target: { value: 'eu-central-1' } })
      fireEvent.blur(input)

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { REGION: 'eu-central-1' },
      }))
    })

    it('does not offer a workspace form when no workspace defines pairs', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'x' } }))
      mount()

      expect(await screen.findByText('No workspace defines variables of its own.')).toBeInTheDocument()
      expect(screen.queryByRole('region', { name: /^Workspace / })).not.toBeInTheDocument()
    })
  })

  describe('client validation, before any round trip', () => {
    beforeEach(() => variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } })))

    /** Fill the global add form and submit it. */
    const add = async (name: string, value = 'v') => {
      await ready()
      fireEvent.change(field(screen, 'Name'), { target: { value: name } })
      fireEvent.change(field(screen, 'Value'), { target: { value } })
      fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    }

    it('rejects a name that does not match the identifier grammar', async () => {
      mount()
      await add('2FA_MODE')

      expect(await screen.findByRole('alert'))
        .toHaveTextContent('A name must start with a letter and may contain only letters, digits and underscores.')
      expect(saveVariables).not.toHaveBeenCalled()
    })

    it('rejects a name holding a hyphen', async () => {
      mount()
      await add('BASE-URL')

      expect(await screen.findByRole('alert')).toHaveTextContent(/must start with a letter/)
      expect(saveVariables).not.toHaveBeenCalled()
    })

    it.each(['MAX_SUBAGENTS', 'VERBOSITY_BLOCK', 'WIDGET_BLOCK', 'STOP_FILE', 'ALIAS', 'bot_name'])(
      'rejects the reserved name %s',
      async reserved => {
        mount()
        await add(reserved)

        expect(await screen.findByRole('alert'))
          .toHaveTextContent('That name is reserved for a built-in prompt placeholder.')
        expect(saveVariables).not.toHaveBeenCalled()
      },
    )

    it('rejects a name already defined at this scope', async () => {
      mount()
      await add('BASE_URL')

      expect(await screen.findByRole('alert')).toHaveTextContent('That name is already defined at this scope.')
      expect(saveVariables).not.toHaveBeenCalled()
    })

    it('rejects a value over 4096 characters', async () => {
      mount()
      await add('LONG', 'x'.repeat(4097))

      expect(await screen.findByRole('alert')).toHaveTextContent('A value may be at most 4096 characters long.')
      expect(saveVariables).not.toHaveBeenCalled()
    })

    it('accepts a value of exactly 4096 characters', async () => {
      mount()
      await add('LONG', 'x'.repeat(4096))

      await waitFor(() => expect(saveVariables).toHaveBeenCalled())
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('rejects a value carrying a control character', async () => {
      mount()
      await add('CTRL', 'a\u0001b')

      expect(await screen.findByRole('alert'))
        .toHaveTextContent('A value may not contain control characters other than tab.')
      expect(saveVariables).not.toHaveBeenCalled()
    })

    it('accepts a tab, the one control character the backend allows', async () => {
      mount()
      await add('COLS', 'a\tb')

      await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { COLS: 'a\tb' },
      }))
    })

    it('rejects an edit that introduces a control character, without writing', async () => {
      mount()
      await ready()
      const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })

      fireEvent.change(input, { target: { value: 'bad\u0007' } })
      fireEvent.blur(input)

      expect(await screen.findByRole('alert')).toHaveTextContent(/control characters other than tab/)
      expect(saveVariables).not.toHaveBeenCalled()
    })
  })

  describe('scope and shadowing', () => {
    const shadowed = () => view({
      global: { BASE_URL: 'https://global.test' },
      workspaces: { research: { BASE_URL: 'https://ws.test' } },
      effective: { BASE_URL: 'https://ws.test' },
      winning_scope: { BASE_URL: 'workspace' },
    })

    it('marks a global pair that a narrower scope overrides', async () => {
      variables.mockResolvedValue(shadowed())
      mount()

      const globalTable = (await screen.findAllByRole('table'))[0]
      expect(within(globalTable).getByText('Shadowed by Workspace')).toBeInTheDocument()
    })

    it('marks the workspace pair that wins as overriding the broader scope', async () => {
      variables.mockResolvedValue(shadowed())
      mount()
      await ready()
      const ws = within(workspaceRegion('research'))

      expect(ws.getByText('Overrides Global')).toBeInTheDocument()
      expect(ws.queryByText(/^Shadowed by/)).not.toBeInTheDocument()
    })

    it('names the winning scope on every row', async () => {
      variables.mockResolvedValue(view({
        global: { BASE_URL: 'https://global.test', REGION: 'eu-west-1' },
        effective: { BASE_URL: 'https://global.test', REGION: 'eu-west-1' },
        winning_scope: { BASE_URL: 'crew', REGION: 'global' },
      }))
      mount()

      const table = within((await screen.findAllByRole('table'))[0])
      expect(table.getByText('Crew')).toBeInTheDocument()
      expect(table.getByText('Global')).toBeInTheDocument()
      // A crew value beats a global one, so the stored global pair is inert.
      expect(table.getByText('Shadowed by Crew')).toBeInTheDocument()
    })

    it('does not claim shadowing when the row itself supplies the value', async () => {
      variables.mockResolvedValue(view({
        global: { REGION: 'eu-west-1' },
        winning_scope: { REGION: 'global' },
      }))
      mount()
      await screen.findByText('REGION')

      expect(screen.queryByText(/^Shadowed by/)).not.toBeInTheDocument()
      expect(screen.queryByText(/^Overrides/)).not.toBeInTheDocument()
    })
  })

  describe('accessibility', () => {
    it('names every value input from the visible row name and column header', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
      mount()

      const input = await screen.findByRole('textbox', { name: 'BASE_URL Value' })
      expect(input.tagName).toBe('INPUT')
    })

    it('associates the add form inputs with their visible labels', async () => {
      variables.mockResolvedValue(view())
      mount()
      await ready()

      for (const label of ['Name', 'Value'] as const) {
        const input = field(screen, label)
        const caption = document.querySelector(`label[for="${input.getAttribute('id')}"]`)
        expect(caption).toHaveTextContent(label)
      }
    })

    it('describes every input with the plain-text warning rather than hiding it', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
      mount()
      await ready()

      const note = screen.getByText(/not a secret store/i)
      // Not `aria-hidden`: this is user-facing copy about where the value lands, so
      // hiding it from assistive tech would withhold the one warning that matters.
      expect(note).not.toHaveAttribute('aria-hidden')
      for (const input of [screen.getByRole('textbox', { name: 'BASE_URL Value' }), field(screen, 'Name')]) {
        expect(input.getAttribute('aria-describedby')).toBe(note.getAttribute('id'))
      }
    })

    it('gives the row name and every column its own header cell', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
      mount()

      const table = within(await screen.findByRole('table'))
      for (const col of ['Name', 'Value', 'Scope', 'Actions']) {
        expect(table.getByRole('columnheader', { name: col })).toBeInTheDocument()
      }
      expect(table.getByRole('rowheader', { name: 'BASE_URL' })).toBeInTheDocument()
    })

    it('keeps the delete affordance a real button with a named target', async () => {
      variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test' } }))
      mount()

      const remove = await screen.findByRole('button', { name: 'Remove BASE_URL' })
      expect(remove.tagName).toBe('BUTTON')
      expect(remove).toHaveAttribute('type', 'button')
    })
  })
})

/**
 * Clicking Remove with an edited value fired BOTH a blur commit (`set`) and the
 * click (`delete`). Two in-flight writes for one key, and a reversed server
 * completion resurrected it. The delete is the user's intent, so the set is dropped.
 */
describe('VariablesPanel remove-vs-edit race', () => {
  beforeEach(() => {
    variables.mockResolvedValue(
      view({
        global: { BASE_URL: 'https://example.test' },
        winning_scope: { BASE_URL: 'global' },
      }),
    )
  })

  it('does not send a set when Remove takes focus from an edited value', async () => {
    mount()
    await ready()

    const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })
    fireEvent.change(input, { target: { value: 'https://edited.test' } })
    const remove = screen.getByRole('button', { name: 'Remove BASE_URL' })
    fireEvent.blur(input, { relatedTarget: remove })
    // The arming click must not resurrect the dropped set either: the blur guard has
    // to hold across BOTH clicks, not just the one that writes.
    fireEvent.click(remove)
    expect(saveVariables).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm removing BASE_URL' }))

    await waitFor(() => expect(saveVariables).toHaveBeenCalledTimes(1))
    expect(saveVariables).toHaveBeenCalledWith({ scope: 'global', delete: ['BASE_URL'] })
  })

  it('still commits on a blur that is not going to Remove', async () => {
    // The guard must not swallow an ordinary edit-then-click-elsewhere.
    mount()
    await ready()

    const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })
    fireEvent.change(input, { target: { value: 'https://edited.test' } })
    fireEvent.blur(input, { relatedTarget: null })

    await waitFor(() =>
      expect(saveVariables).toHaveBeenCalledWith({
        scope: 'global',
        set: { BASE_URL: 'https://edited.test' },
      }),
    )
  })
})

/**
 * A committed edit kept its draft, so the row went on shadowing the refetched
 * value: if another writer changed the key in the meantime, the next blur committed
 * the stale draft back over the newer value.
 */
describe('VariablesPanel stale drafts after a successful edit', () => {
  it('drops the row draft once the save lands, so a later blur cannot resend it', async () => {
    let call = 0
    variables.mockImplementation(() => {
      call += 1
      // The refetch brings in a value another writer stored.
      return Promise.resolve(
        call === 1
          ? view({ global: { BASE_URL: 'https://mine.test' }, winning_scope: { BASE_URL: 'global' } })
          : view({ global: { BASE_URL: 'https://theirs.test' }, winning_scope: { BASE_URL: 'global' } }),
      )
    })

    mount()
    await ready()

    const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })
    fireEvent.change(input, { target: { value: 'https://edited.test' } })
    fireEvent.blur(input, { relatedTarget: null })

    await waitFor(() => expect(saveVariables).toHaveBeenCalledTimes(1))
    // After the refetch the field shows the OTHER writer's value, not the stale draft.
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'BASE_URL Value' })).toHaveValue(
        'https://theirs.test',
      ),
    )

    // A second blur must not resend the old draft.
    fireEvent.blur(screen.getByRole('textbox', { name: 'BASE_URL Value' }), {
      relatedTarget: null,
    })
    await new Promise(r => setTimeout(r, 50))
    expect(saveVariables).toHaveBeenCalledTimes(1)
  })
})

/**
 * Deleting is two clicks: arm, then confirm.
 *
 * There is no undo and a value can be 4096 characters, so a single mis-click on a
 * small icon was permanent work loss. The ACCESSIBLE NAME changes between the two
 * states, not just the colour — a confirm carried only by a colour change is hidden
 * from exactly the users who cannot see it.
 */
describe('VariablesPanel delete confirmation', () => {
  beforeEach(() => {
    variables.mockResolvedValue(view({ global: { BASE_URL: 'https://example.test', REGION: 'eu' } }))
  })

  it('announces the armed state through the accessible name', async () => {
    mount()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))

    expect(screen.getByRole('button', { name: 'Confirm removing REGION' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove REGION' })).toBeNull()
  })

  it('arms only the row that was clicked', async () => {
    mount()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))

    expect(screen.getByRole('button', { name: 'Remove BASE_URL' })).toBeInTheDocument()
  })

  it('arming a second row disarms the first, so only one trigger is ever loaded', async () => {
    mount()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove BASE_URL' }))

    expect(screen.getByRole('button', { name: 'Remove REGION' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm removing BASE_URL' })).toBeInTheDocument()
    expect(saveVariables).not.toHaveBeenCalled()
  })

  it('disarms itself after the timeout rather than staying loaded', async () => {
    vi.useFakeTimers()
    try {
      mount()
      await vi.waitFor(() =>
        expect(screen.getAllByRole('button', { name: 'Add' })[0]).toBeEnabled(),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Remove REGION' }))
      expect(screen.getByRole('button', { name: 'Confirm removing REGION' })).toBeInTheDocument()

      act(() => { vi.advanceTimersByTime(5000) })

      expect(screen.getByRole('button', { name: 'Remove REGION' })).toBeInTheDocument()
      expect(saveVariables).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

/** Bulk edit: a whole scope as dotenv text, the way Postman's does it. */
describe('VariablesPanel arming a delete keeps an in-progress edit', () => {
  it('does not discard the typed value when the first click only arms', async () => {
    // The blur-to-Remove guard used to drop the draft, which was right when Remove
    // deleted immediately. With a confirm step, a user who arms and then walks away
    // (or lets it time out) would silently lose what they typed.
    variables.mockResolvedValue(
      view({ global: { BASE_URL: 'https://example.test' }, winning_scope: { BASE_URL: 'global' } }),
    )
    mount()
    await ready()

    const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })
    fireEvent.change(input, { target: { value: 'https://edited.test' } })
    const remove = screen.getByRole('button', { name: 'Remove BASE_URL' })
    fireEvent.blur(input, { relatedTarget: remove })
    fireEvent.click(remove)

    expect(saveVariables).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'BASE_URL Value' })).toHaveValue('https://edited.test')
  })

  it('still sends no set alongside the delete once confirmed', async () => {
    // Keeping the draft must not resurrect the double-write this guard exists for.
    variables.mockResolvedValue(
      view({ global: { BASE_URL: 'https://example.test' }, winning_scope: { BASE_URL: 'global' } }),
    )
    mount()
    await ready()

    const input = screen.getByRole('textbox', { name: 'BASE_URL Value' })
    fireEvent.change(input, { target: { value: 'https://edited.test' } })
    const remove = screen.getByRole('button', { name: 'Remove BASE_URL' })
    fireEvent.blur(input, { relatedTarget: remove })
    fireEvent.click(remove)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm removing BASE_URL' }))

    await waitFor(() => expect(saveVariables).toHaveBeenCalledTimes(1))
    expect(saveVariables).toHaveBeenCalledWith({ scope: 'global', delete: ['BASE_URL'] })
  })
})

describe('VariablesPanel bulk edit', () => {
  const openBulk = async () => {
    mount()
    await ready()
    fireEvent.click(screen.getAllByRole('button', { name: /Bulk edit/ })[0])
    return screen.getByRole('textbox', { name: 'One NAME=value per line' })
  }

  it('seeds the textarea from the current pairs, sorted', async () => {
    variables.mockResolvedValue(view({ global: { B: 'two', A: 'one' } }))
    const box = await openBulk()
    expect(box).toHaveValue('A=one\nB=two')
  })

  it('quotes only the values that would not survive a bare round trip', async () => {
    variables.mockResolvedValue(view({ global: { EMPTY: '', PAD: ' x ', PLAIN: 'v' } }))
    const box = await openBulk()
    expect(box).toHaveValue('EMPTY=""\nPAD=" x "\nPLAIN=v')
  })

  it('sends the text verbatim as a bulk write', async () => {
    variables.mockResolvedValue(view({ global: { A: 'one' } }))
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'A=changed\nB=new\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
      scope: 'global',
      bulk: 'A=changed\nB=new\n',
      base: { A: 'one' },
    }))
  })

  it('does not pre-validate: the backend owns the grammar and reports the line', async () => {
    variables.mockResolvedValue(view({ global: {} }))
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'not a pair' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveVariables).toHaveBeenCalledWith({
      scope: 'global',
      bulk: 'not a pair',
      base: {},
    }))
  })

  it('surfaces the backend refusal, line number and all', async () => {
    variables.mockResolvedValue(view({ global: {} }))
    saveVariables.mockRejectedValue(
      new ApiError(400, 'Bad Request', JSON.stringify({ error: 'line 2: expected NAME=value' })),
    )
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'A=1\nbad' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(await screen.findByText('line 2: expected NAME=value')).toBeInTheDocument()
  })

  it('stays open on a refusal so the text can be corrected', async () => {
    variables.mockResolvedValue(view({ global: {} }))
    saveVariables.mockRejectedValue(new ApiError(400, 'Bad Request', JSON.stringify({ error: 'nope' })))
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'bad' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await screen.findByText('nope')
    expect(screen.getByRole('textbox', { name: 'One NAME=value per line' })).toHaveValue('bad')
  })

  it('closes once the write lands', async () => {
    variables.mockResolvedValue(view({ global: { A: '1' } }))
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'A=2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'One NAME=value per line' })).toBeNull(),
    )
  })

  it('cancel closes without writing', async () => {
    variables.mockResolvedValue(view({ global: { A: '1' } }))
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'A=2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('textbox', { name: 'One NAME=value per line' })).toBeNull()
    expect(saveVariables).not.toHaveBeenCalled()
  })

  it('sends the scope as the editor OPENED it, values included, not the current one', async () => {
    // The discriminating case, and it needs a refetch mid-edit: another tab adds a
    // key while this editor is open, so `pairs` changes underneath it. Reading the
    // baseline at APPLY time would send the new key set and the server's staleness
    // check would pass — silently deleting the key this operator never saw, which is
    // the whole failure the check exists to stop.
    variables.mockResolvedValue(view({ global: { A: '1', B: '2' } }))
    const { qc } = mount()
    await ready()
    fireEvent.click(screen.getAllByRole('button', { name: /Bulk edit/ })[0])
    const box = screen.getByRole('textbox', { name: 'One NAME=value per line' })

    variables.mockResolvedValue(view({ global: { A: '1', B: '2', ADDED_BY_TAB_B: '3' } }))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['variables'] }) })
    // Proof the refetch actually landed: without this the test cannot tell the two
    // implementations apart and would pass against either.
    await screen.findByText('ADDED_BY_TAB_B')

    fireEvent.change(box, { target: { value: 'A=1\nB=2\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveVariables).toHaveBeenCalledWith(
      expect.objectContaining({ base: { A: '1', B: '2' } }),
    ))
    // The baseline carries VALUES, so a value another writer changed is caught too —
    // not just a key that appeared or vanished.
    expect(saveVariables).not.toHaveBeenCalledWith(
      expect.objectContaining({ base: expect.objectContaining({ ADDED_BY_TAB_B: '3' }) }),
    )
  })

  it('surfaces the stale-baseline refusal in the operator\'s words', async () => {
    variables.mockResolvedValue(view({ global: { A: '1' } }))
    saveVariables.mockRejectedValue(
      new ApiError(409, 'Conflict', JSON.stringify({
        error: 'the variables in this scope changed while the bulk editor was open. Reopen it to pick up the current values, then re-apply.',
        code: 'variables_stale_bulk_base',
      })),
    )
    const box = await openBulk()
    fireEvent.change(box, { target: { value: 'A=2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(await screen.findByText(/changed while the bulk editor was open/)).toBeInTheDocument()
  })

  it('says that applying deletes what the text omits', async () => {
    variables.mockResolvedValue(view({ global: { A: '1' } }))
    await openBulk()
    // The destructive half is invisible: nothing on screen shows a key that is not
    // in the text, so the warning is the only thing that can carry it.
    expect(
      screen.getByText('Applying replaces this scope: a name missing from the text is removed.'),
    ).toBeInTheDocument()
  })
})

/** The read-only dotenv layer a workspace file supplies. */
describe('VariablesPanel workspace file layer', () => {
  const withFile = () =>
    view({
      workspaces: { research: { SHARED: 'from-panel' } },
      workspace_files: { research: { SHARED: 'from-file', FILE_ONLY: 'x' } },
      workspace_file_dir: '/home/u/.kiro/crew/variables/workspaces',
      active_workspace: 'research',
    })

  it('shows the file pairs and names the directory to edit', async () => {
    variables.mockResolvedValue(withFile())
    mount()
    await ready()

    expect(await screen.findByText('FILE_ONLY')).toBeInTheDocument()
    expect(
      screen.getByText(/\/home\/u\/\.kiro\/crew\/variables\/workspaces/),
    ).toBeInTheDocument()
  })

  it('marks a file key the panel overrides, so a shadowed edit does not read as a no-op', async () => {
    variables.mockResolvedValue(withFile())
    mount()
    await ready()

    expect(await screen.findByText('overridden above')).toBeInTheDocument()
  })

  it('offers no edit controls for a file key: this panel does not write those files', async () => {
    variables.mockResolvedValue(withFile())
    mount()
    await ready()

    await screen.findByText('FILE_ONLY')
    expect(screen.queryByRole('button', { name: 'Remove FILE_ONLY' })).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'FILE_ONLY Value' })).toBeNull()
  })

  it('renders nothing when a workspace has no file', async () => {
    variables.mockResolvedValue(view({ workspaces: { research: { A: '1' } }, workspace_files: { research: {} } }))
    mount()
    await ready()

    expect(screen.queryByText('overridden above')).toBeNull()
  })
})

describe('VariablesPanel file-scope shadowing', () => {
  it('names the workspace file as the winner instead of showing a raw scope key', async () => {
    // The file layer now appears in `winning_scope`, so an unlabelled scope would
    // render the wire value `workspace_file` straight into the UI.
    variables.mockResolvedValue(
      view({
        global: { SHARED: 'from-global' },
        winning_scope: { SHARED: 'workspace_file' },
      }),
    )
    mount()
    await ready()

    expect(await screen.findByText('Shadowed by Workspace file')).toBeInTheDocument()
  })

  it('ranks the file above global, so a shadowed global row is marked as shadowed', async () => {
    // RANK decides this: an unranked scope falls back to 0, which reads as
    // "broadest" and would leave the row looking like the winner.
    variables.mockResolvedValue(
      view({
        global: { SHARED: 'from-global' },
        winning_scope: { SHARED: 'workspace_file' },
      }),
    )
    mount()
    await ready()

    expect(await screen.findByText(/Shadowed by/)).toBeInTheDocument()
  })
})

describe('VariablesPanel says why a workspace has no file layer', () => {
  it('explains a name that is not lowercase', async () => {
    // Otherwise the workspace shows no file rows and the reason lives only in a
    // gateway log the operator will never open — the invisible failure this feature
    // avoids everywhere else.
    variables.mockResolvedValue(view({
      workspaces: { Ops: { A: '1' } },
      workspace_files: {},
      workspace_file_blocked: { Ops: 'name_not_lowercase' },
    }))
    mount()
    await ready()

    expect(await screen.findByText(/must be lowercase/)).toBeInTheDocument()
  })

  it('explains a name that cannot be a filename at all', async () => {
    variables.mockResolvedValue(view({
      workspaces: { 'a/b': { A: '1' } },
      workspace_files: {},
      workspace_file_blocked: { 'a/b': 'name_unusable' },
    }))
    mount()
    await ready()

    expect(await screen.findByText(/can't be a filename/)).toBeInTheDocument()
  })

  it('says nothing for a workspace that can have one', async () => {
    variables.mockResolvedValue(view({
      workspaces: { ops: { A: '1' } },
      workspace_files: { ops: {} },
      workspace_file_blocked: {},
    }))
    mount()
    await ready()

    expect(screen.queryByText(/No variables file/)).toBeNull()
  })
})

describe('VariablesPanel usage hint', () => {
  it('teaches the token syntax, which appears in no other shipped string', async () => {
    variables.mockResolvedValue(view())
    mount()
    await ready()

    expect(
      screen.getByText(/Reference a variable as \{\{name\}\} in a message/),
    ).toBeInTheDocument()
  })
})

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  money,
  percent,
  type Attempt,
  type Health,
  type Stats,
  type TicketDetail,
  type TicketSummary,
} from './api'

const FILTERS = [
  { key: 'awaiting_review', label: 'Needs review' },
  { key: 'sent', label: 'Sent' },
  { key: 'all', label: 'All' },
] as const
type Filter = (typeof FILTERS)[number]['key']

const STATUS_LABEL: Record<string, string> = {
  queued: 'Queued',
  processing: 'Processing',
  awaiting_review: 'Needs review',
  sent: 'Sent',
  rejected: 'Rejected',
}

const RESOLUTION_LABEL: Record<string, string> = {
  auto_sent: 'sent automatically',
  approved: 'approved by a reviewer',
  edited: 'edited by a reviewer',
  rejected: 'rejected by a reviewer',
}

export default function App() {
  const [tickets, setTickets] = useState<TicketSummary[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [filter, setFilter] = useState<Filter>('awaiting_review')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [list, s] = await Promise.all([api.tickets(), api.stats()])
      setTickets(list)
      setStats(s)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the API')
    }
  }, [])

  useEffect(() => {
    api.health().then(setHealth).catch(() => undefined)
    const first = setTimeout(refresh, 0)
    const timer = setInterval(refresh, 2000)
    return () => {
      clearTimeout(first)
      clearInterval(timer)
    }
  }, [refresh])

  const visible = useMemo(
    () => (filter === 'all' ? tickets : tickets.filter((t) => t.status === filter)),
    [tickets, filter],
  )
  const count = (key: Filter) =>
    key === 'all' ? tickets.length : tickets.filter((t) => t.status === key).length

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Guarded LLM pipeline</h1>
          <p className="muted">
            The model proposes a reply. Validation decides whether it is sent, retried or handed
            to a person.
          </p>
        </div>
        {health && (
          <div className="pills">
            <span className="pill">model: {health.provider}</span>
            <span className="pill">auto-send: {health.auto_send ? 'on' : 'off'}</span>
          </div>
        )}
      </header>

      {error && <div className="banner error">{error}</div>}
      <StatsBar stats={stats} />

      <main className="columns">
        <section className="list" aria-label="Tickets">
          <div className="tabs" role="tablist">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                role="tab"
                aria-selected={filter === f.key}
                className={filter === f.key ? 'tab active' : 'tab'}
                onClick={() => setFilter(f.key)}
              >
                {f.label} <span className="count">{count(f.key)}</span>
              </button>
            ))}
          </div>

          {tickets.length === 0 ? (
            <EmptyState onLoaded={refresh} />
          ) : visible.length === 0 ? (
            <p className="muted pad">Nothing here.</p>
          ) : (
            <ul>
              {visible.map((t) => (
                <li key={t.id}>
                  <button
                    className={t.id === selectedId ? 'row selected' : 'row'}
                    onClick={() => setSelectedId(t.id)}
                  >
                    <span className="row-top">
                      <strong>{t.subject}</strong>
                      <span className={`badge ${t.status}`}>{STATUS_LABEL[t.status]}</span>
                    </span>
                    <span className="muted small">
                      #{t.id} · {t.customer_name} · {t.attempts}{' '}
                      {t.attempts === 1 ? 'attempt' : 'attempts'}
                      {t.used_fallback ? ' · fallback' : ''}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <NewTicket onCreated={refresh} hasTickets={tickets.length > 0} />
        </section>

        <section className="detail" aria-label="Ticket detail">
          {selectedId === null ? (
            <p className="muted pad">Select a ticket to see the draft and how it was checked.</p>
          ) : (
            <Detail
              key={selectedId}
              id={selectedId}
              version={tickets.find((t) => t.id === selectedId)?.status}
              onChanged={refresh}
            />
          )}
        </section>
      </main>
    </div>
  )
}

function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return null
  const failed = Object.entries(stats.failed_checks)
  return (
    <section className="stats" aria-label="Pipeline statistics">
      <Stat label="Tickets processed" value={String(stats.processed)} />
      <Stat label="Sent automatically" value={percent(stats.auto_sent_share)} />
      <Stat label="First draft passed" value={percent(stats.first_attempt_pass_share)} />
      <Stat label="Avg. attempts" value={stats.avg_attempts?.toFixed(2) ?? '–'} />
      <Stat label="Model cost" value={`$${stats.cost_usd.toFixed(4)}`} />
      <div className="stat wide">
        <span className="stat-label">Checks that rejected a draft</span>
        <span className="chips">
          {failed.length === 0
            ? '–'
            : failed.map(([name, n]) => (
                <span className="chip" key={name}>
                  {name} <b>{n}</b>
                </span>
              ))}
        </span>
      </div>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  )
}

function EmptyState({ onLoaded }: { onLoaded: () => void }) {
  const [busy, setBusy] = useState(false)
  return (
    <div className="pad">
      <p>No tickets yet.</p>
      <button
        className="primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          await api.loadDemo().catch(() => undefined)
          onLoaded()
        }}
      >
        {busy ? 'Loading…' : 'Load sample tickets'}
      </button>
    </div>
  )
}

function NewTicket({ onCreated, hasTickets }: { onCreated: () => void; hasTickets: boolean }) {
  const [customers, setCustomers] = useState<{ id: string; name: string }[]>([])
  const [customerId, setCustomerId] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    api
      .customers()
      .then((list) => {
        setCustomers(list)
        setCustomerId(list[0]?.id ?? '')
      })
      .catch(() => undefined)
  }, [])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    try {
      await api.create({ customer_id: customerId, subject, body })
      setSubject('')
      setBody('')
      setMessage('Ticket queued.')
      onCreated()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Could not create the ticket')
    }
  }

  return (
    <details className="new-ticket">
      <summary>New ticket</summary>
      <form onSubmit={submit}>
        <label>
          Customer
          <select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Subject
          <input value={subject} onChange={(e) => setSubject(e.target.value)} required />
        </label>
        <label>
          Message
          <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} required />
        </label>
        <div className="actions">
          <button className="primary" type="submit">
            Queue ticket
          </button>
          {hasTickets && (
            <button type="button" onClick={() => api.loadDemo().then(onCreated)}>
              Load samples again
            </button>
          )}
        </div>
        {message && <p className="muted small">{message}</p>}
      </form>
    </details>
  )
}

function Detail({
  id,
  version,
  onChanged,
}: {
  id: number
  version: string | undefined
  onChanged: () => void
}) {
  const [ticket, setTicket] = useState<TicketDetail | null>(null)
  const [draft, setDraft] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)

  // Reload when the ticket's status changes, e.g. the worker finishes it.
  useEffect(() => {
    let cancelled = false
    api
      .ticket(id)
      .then((t) => {
        if (cancelled) return
        setTicket(t)
        setDraft(t.draft_reply ?? '')
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load the ticket'))
    return () => {
      cancelled = true
    }
  }, [id, version])

  if (error) return <div className="banner error">{error}</div>
  if (!ticket) return <p className="muted pad">Loading…</p>

  const reviewing = ticket.status === 'awaiting_review'
  const edited = draft.trim() !== (ticket.draft_reply ?? '').trim()

  const decide = async (decision: 'approve' | 'edit' | 'reject') => {
    try {
      setTicket(await api.review(ticket.id, decision, draft, note))
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Review failed')
    }
  }

  return (
    <article className="pad">
      <div className="row-top">
        <h2>{ticket.subject}</h2>
        <span className={`badge ${ticket.status}`}>{STATUS_LABEL[ticket.status]}</span>
      </div>
      <p className="muted small">
        #{ticket.id} · {ticket.customer.name} · {ticket.customer.email}
        {ticket.resolution ? ` · ${RESOLUTION_LABEL[ticket.resolution]}` : ''}
      </p>

      <h3>Customer message</h3>
      <blockquote>{ticket.body}</blockquote>

      {reviewing && (
        <div className="banner warn">
          <strong>Why a person needs to look at this</strong>
          <ul>
            {ticket.review_reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {(ticket.status === 'queued' || ticket.status === 'processing') && (
        <p className="muted">Waiting for the worker…</p>
      )}

      {reviewing && (
        <>
          <h3>
            Draft reply{' '}
            {ticket.used_fallback && (
              <span className="chip">safe template: no generated draft passed</span>
            )}
          </h3>
          <textarea
            className="draft"
            aria-label="Draft reply"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={7}
          />
          {ticket.proposed_action && (
            <p className="action-box">
              <strong>Proposed action:</strong> {ticket.proposed_action.type} on order{' '}
              {ticket.proposed_action.order_id}
              {ticket.proposed_action.amount_cents
                ? ` for ${money(ticket.proposed_action.amount_cents)}`
                : ''}
              . Approving the reply approves this action.
            </p>
          )}
          <label className="note">
            Note (optional)
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="actions">
            {edited ? (
              <button className="primary" onClick={() => decide('edit')}>
                Send edited reply
              </button>
            ) : (
              <button className="primary" onClick={() => decide('approve')}>
                Approve and send
              </button>
            )}
            <button className="danger" onClick={() => decide('reject')}>
              Reject
            </button>
          </div>
        </>
      )}

      {ticket.final_reply && (
        <>
          <h3>Reply sent</h3>
          <blockquote className="sent">{ticket.final_reply}</blockquote>
        </>
      )}
      {ticket.reviewer_note && <p className="muted small">Reviewer note: {ticket.reviewer_note}</p>}

      <h3>How the draft was checked</h3>
      {ticket.attempt_log.map((a) => (
        <AttemptCard key={a.number} attempt={a} open={a.number === ticket.attempt_log.length} />
      ))}

      <h3>Customer's orders</h3>
      <table>
        <thead>
          <tr>
            <th>Order</th>
            <th>Item</th>
            <th className="num">Amount</th>
            <th>Status</th>
            <th>Delivered</th>
          </tr>
        </thead>
        <tbody>
          {ticket.orders.map((o) => (
            <tr key={o.id}>
              <td>{o.id}</td>
              <td>{o.item}</td>
              <td className="num">{money(o.amount_cents)}</td>
              <td>{o.status}</td>
              <td>{o.delivered_at ? o.delivered_at.slice(0, 10) : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  )
}

function AttemptCard({ attempt, open }: { attempt: Attempt; open: boolean }) {
  return (
    <details className="attempt" open={open}>
      <summary>
        <span>
          Attempt {attempt.number}{' '}
          <span className={attempt.passed ? 'badge sent' : 'badge rejected'}>
            {attempt.passed ? 'passed' : 'rejected'}
          </span>
        </span>
        <span className="muted small">
          {attempt.model} · {attempt.input_tokens + attempt.output_tokens} tokens · $
          {attempt.cost_usd.toFixed(4)}
        </span>
      </summary>
      {attempt.error && <p className="banner error">Generation failed: {attempt.error}</p>}
      {attempt.candidate && <blockquote>{attempt.candidate.reply}</blockquote>}
      <ul className="checks">
        {attempt.checks.map((c) => (
          <li key={c.name} className={c.passed ? 'ok' : 'fail'}>
            <span aria-hidden="true">{c.passed ? '✓' : '✗'}</span>
            <span>
              <strong>{c.name}</strong>
              {c.detail && <span className="check-detail"> — {c.detail}</span>}
            </span>
          </li>
        ))}
      </ul>
    </details>
  )
}

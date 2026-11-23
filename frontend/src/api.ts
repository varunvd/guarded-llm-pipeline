export type Check = { name: string; passed: boolean; detail: string }

export type Candidate = {
  reply: string
  cited_policies: string[]
  proposed_action: { type: string; order_id: string | null; amount_cents: number | null }
  needs_human: boolean
  needs_human_reason: string | null
}

export type Attempt = {
  number: number
  model: string
  passed: boolean
  error: string | null
  candidate: Candidate | null
  checks: Check[]
  input_tokens: number
  output_tokens: number
  cost_usd: number
  latency_ms: number
}

export type TicketSummary = {
  id: number
  customer_name: string
  subject: string
  status: 'queued' | 'processing' | 'awaiting_review' | 'sent' | 'rejected'
  resolution: string | null
  review_reasons: string[]
  used_fallback: boolean
  attempts: number
  created_at: string
}

export type Order = {
  id: string
  item: string
  amount_cents: number
  status: string
  delivered_at: string | null
}

export type TicketDetail = TicketSummary & {
  body: string
  customer: { name: string; email: string; plan: string }
  orders: Order[]
  draft_reply: string | null
  proposed_action: Candidate['proposed_action'] | null
  final_reply: string | null
  reviewer_note: string | null
  attempt_log: Attempt[]
}

export type Stats = {
  tickets: number
  processed: number
  by_status: Record<string, number>
  auto_sent_share: number | null
  first_attempt_pass_share: number | null
  fallback_share: number | null
  avg_attempts: number | null
  failed_checks: Record<string, number>
  cost_usd: number
  queue_depth: number
}

export type Health = { provider: string; auto_send: boolean }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/health'),
  stats: () => request<Stats>('/stats'),
  customers: () => request<{ id: string; name: string }[]>('/customers'),
  tickets: () => request<TicketSummary[]>('/tickets'),
  ticket: (id: number) => request<TicketDetail>(`/tickets/${id}`),
  loadDemo: () => request<{ queued: number[] }>('/demo', { method: 'POST' }),
  create: (ticket: { customer_id: string; subject: string; body: string }) =>
    request<TicketSummary>('/tickets', { method: 'POST', body: JSON.stringify(ticket) }),
  review: (id: number, decision: 'approve' | 'edit' | 'reject', reply?: string, note?: string) =>
    request<TicketDetail>(`/tickets/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision, reply, note: note || null }),
    }),
}

export const money = (cents: number) =>
  (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' })

export const percent = (share: number | null) =>
  share === null ? '–' : `${Math.round(share * 100)}%`

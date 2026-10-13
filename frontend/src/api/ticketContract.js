const TICKET_STATUSES = new Set([
  'PENDING', 'APPROVED', 'BORROWING', 'RETURNING', 'CANCEL_PENDING',
  'REJECTED', 'CANCELLED', 'CLOSED',
]);

export function createIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `ticket-${Date.now()}-${Math.random()}`;
}

export function toTicketQuery({ role, status, search, assetId, offset = 0, limit = 20 } = {}) {
  return {
    perspective: role === 'holder' ? 'CUSTODIAN' : role === 'borrower' ? 'BORROWER' : 'ALL',
    status: status || undefined,
    search: search?.trim() || undefined,
    asset_id: assetId || undefined,
    offset,
    limit,
  };
}

export function mapTicketPage(payload) {
  if (!payload || !Array.isArray(payload.items)) {
    throw new TypeError('Ticket page items must be an array.');
  }
  return {
    items: payload.items.map(mapTicketSummary),
    total: Number(payload.total),
    offset: Number(payload.offset),
    limit: Number(payload.limit),
  };
}

export function mapTicketDetail(payload) {
  if (!payload?.ticket || !Array.isArray(payload.items) || !Array.isArray(payload.events)) {
    throw new TypeError('Ticket detail does not match the canonical contract.');
  }
  const summary = mapTicketSummary(payload.ticket);
  const items = payload.items.map((item) => ({
    id: item.id,
    assetId: item.assetId,
    assetName: item.assetName,
    sourceCustodyId: item.sourceCustodyId,
    quantity: Number(item.quantity),
  }));
  return {
    ...summary,
    items,
    evidence: Array.isArray(payload.evidence) ? payload.evidence : [],
    log: payload.events.map((event) => (
      `${event.occurredAt} · ${event.actorName} · ${event.eventType}`
      + (event.note ? ` · ${event.note}` : '')
    )),
  };
}

export function mapCreateTicketsResult(payload) {
  if (!payload || !Array.isArray(payload.createdTickets) || !Array.isArray(payload.rejectedGroups)) {
    throw new TypeError('Ticket creation result does not match the canonical contract.');
  }
  return {
    createdTickets: payload.createdTickets.map((ticket) => ({
      id: ticket.id,
      status: ticket.status,
    })),
    rejectedGroups: payload.rejectedGroups.map((group) => ({
      sourceCustodyIds: [...group.sourceCustodyIds],
      reasonCode: group.reasonCode,
    })),
  };
}

export function toEvidenceFormData({ photos, note }) {
  const data = new FormData();
  data.append('metadata', JSON.stringify({ note: note || null }));
  photos.forEach((photo) => data.append('files', photo));
  return data;
}

export function mapLegacyTicket(ticket) {
  const statuses = {
    ACTIVE: 'BORROWING',
    AWAITING_HOLDER_CONFIRM: 'RETURNING',
    CANCEL_REQUESTED: 'CANCEL_PENDING',
  };
  return {
    ...ticket,
    status: statuses[ticket.status] || ticket.status,
    items: ticket.items || [{
      id: `${ticket.id}-item`,
      assetId: ticket.assetId || ticket.id,
      assetName: ticket.asset,
      sourceCustodyId: ticket.sourceCustodyId || ticket.holderId,
      quantity: Number(ticket.qty || 1),
    }],
  };
}

function mapTicketSummary(ticket) {
  if (!TICKET_STATUSES.has(ticket.status)) {
    throw new TypeError(`Unsupported Ticket status: ${ticket.status}`);
  }
  return {
    id: ticket.id,
    borrowerId: ticket.borrowerUserId,
    borrower: ticket.borrowerName,
    holderId: ticket.sourceCustodianUserId,
    holder: ticket.sourceCustodianName,
    method: ticket.method,
    status: ticket.status,
    dueDate: ticket.dueDate,
    qty: Number(ticket.totalQuantity),
    asset: ticket.assetNames.join(', '),
    assetNames: [...ticket.assetNames],
    createdAt: ticket.createdAt,
    updatedAt: ticket.updatedAt,
    overdue: Boolean(ticket.dueDate && ticket.status !== 'CLOSED'
      && new Date(`${ticket.dueDate}T23:59:59`) < new Date()),
  };
}

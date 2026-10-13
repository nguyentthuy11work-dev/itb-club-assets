export function getTicketActions(ticket, { isBorrower, isHolder }) {
  if (isBorrower) {
    if (ticket.status === 'PENDING') return ['cancelRequest'];
    if (ticket.status === 'APPROVED') return ['receive', 'cancelRequest'];
    if (ticket.status === 'BORROWING') return ['return'];
    return [];
  }
  if (isHolder) {
    if (ticket.status === 'PENDING') return ['approve', 'reject'];
    if (ticket.status === 'RETURNING') return ['holderReceive'];
    if (ticket.status === 'CANCEL_PENDING') return ['cancelApprove', 'cancelReject'];
  }
  return [];
}

const CLOSED_STATUSES = ['CLOSED', 'REJECTED', 'CANCELLED'];
const NEEDS_ACTION = {
  borrower: ['APPROVED', 'BORROWING'],
  holder: ['PENDING', 'RETURNING', 'CANCEL_PENDING'],
};

// Một nguồn duy nhất cho cả số đếm trên chip lẫn bộ lọc danh sách Ticket.
// perspective: 'borrower' (Tôi mượn) | 'holder' (Tôi cho mượn)
export function ticketBucket(ticket, perspective) {
  if (CLOSED_STATUSES.includes(ticket.status)) return 'closed';
  return NEEDS_ACTION[perspective].includes(ticket.status) ? 'mine' : 'waiting';
}

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

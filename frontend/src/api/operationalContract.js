const TODO_PRESENTATION = {
  OVERDUE_RETURN: { kind: 'overdue', label: 'Quá hạn', action: 'Đã trả' },
  TICKET_APPROVAL: { kind: 'pending', label: 'Chờ duyệt', action: 'approve_reject' },
  RETURN_CONFIRMATION: { kind: 'await_receive', label: 'Chờ nhận lại', action: 'Nhận lại' },
  CUSTODY_CONFIRMATION: { kind: 'confirm', label: 'Chờ xác nhận', action: 'Xác nhận' },
  OVERDUE_CUSTODY_CONFIRMATION: { kind: 'overdue24h', label: 'Quá 24h', action: 'Kiểm tra' },
};

function object(value, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${field} must be an object`);
  }
  return value;
}

function array(value, field) {
  if (!Array.isArray(value)) throw new TypeError(`${field} must be an array`);
  return value;
}

function integer(value, field) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${field} must be a non-negative integer`);
  }
  return value;
}

function todo(value, index) {
  const row = object(value, `todos[${index}]`);
  const presentation = TODO_PRESENTATION[row.kind];
  if (!presentation || typeof row.referenceId !== 'string') {
    throw new TypeError(`todos[${index}] has an invalid typed reference`);
  }
  return {
    id: row.id,
    ...presentation,
    text: `${row.referenceType} · ${row.referenceId}`,
    ticketId: row.referenceType === 'TICKET' ? row.referenceId : null,
    custodyId: row.referenceType === 'CUSTODY' ? row.referenceId : null,
  };
}

export function mapMemberDashboard(payload, assets = []) {
  const data = object(payload, 'dashboard');
  const stats = object(data.stats, 'dashboard.stats');
  return {
    stats: {
      myBorrowCount: integer(stats.activeBorrowCount, 'activeBorrowCount'),
      overdueCount: integer(stats.overdueCount, 'overdueCount'),
      pendingApproval: integer(stats.pendingApprovalCount, 'pendingApprovalCount'),
      awaitingMyReceive: integer(
        stats.awaitingReturnConfirmationCount,
        'awaitingReturnConfirmationCount',
      ),
      heldTotal: integer(stats.heldQuantity, 'heldQuantity'),
      pendingConfirm: integer(
        stats.pendingCustodyConfirmationCount,
        'pendingCustodyConfirmationCount',
      ),
    },
    todos: array(data.todos, 'dashboard.todos').map(todo),
    assets: assets.map((asset) => ({
      id: asset.id,
      name: asset.name,
      type: asset.categoryId,
      total: asset.totalQuantity,
      available: asset.availableQuantity,
      department: asset.responsibleTeamId || '—',
    })),
  };
}

export function mapHrDashboard(payload) {
  const data = object(payload, 'HR dashboard');
  const stats = object(data.stats, 'HR dashboard.stats');
  return {
    stats: {
      totalAssets: integer(stats.totalAssetQuantity, 'totalAssetQuantity'),
      totalTypes: integer(stats.assetTypeCount, 'assetTypeCount'),
      onLoan: integer(stats.temporarilyLoanedQuantity, 'temporarilyLoanedQuantity'),
      onLoanOverdue: integer(stats.overdueTicketCount, 'overdueTicketCount'),
      awaitingCustodian: integer(
        stats.pendingCustodyConfirmationCount,
        'pendingCustodyConfirmationCount',
      ),
      awaitingOver24h: integer(
        stats.overdueCustodyConfirmationCount,
        'overdueCustodyConfirmationCount',
      ),
      openRecons: 0,
      reconDone: 0,
      reconTotal: 0,
    },
    todos: array(data.todos, 'HR dashboard.todos').map(todo),
  };
}

export function mapTransactionPage(payload) {
  const page = object(payload, 'transaction page');
  return array(page.items, 'transaction page.items').map((value, index) => {
    const row = object(value, `items[${index}]`);
    return {
      id: row.id,
      time: row.occurredAt,
      kind: row.eventType,
      qty: integer(row.quantity, `items[${index}].quantity`),
      from: row.fromUserId || '—',
      to: row.toUserId || '—',
      ticketId: row.ticketId,
    };
  });
}

export function toTransactionQuery({ assetId, from, to, offset = 0, limit = 100 } = {}) {
  return {
    assetId: assetId || undefined,
    from: from ? `${from}T00:00:00+07:00` : undefined,
    to: to ? `${to}T23:59:59.999+07:00` : undefined,
    offset,
    limit,
  };
}

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mapHrDashboard,
  mapMemberDashboard,
  mapTransactionPage,
  toTransactionQuery,
} from './operationalContract.js';

test('maps typed member dashboard references for the current UI', () => {
  const result = mapMemberDashboard({
    stats: {
      activeBorrowCount: 1,
      overdueCount: 2,
      pendingApprovalCount: 3,
      awaitingReturnConfirmationCount: 4,
      heldQuantity: 5,
      pendingCustodyConfirmationCount: 6,
    },
    todos: [{ id: 'x', kind: 'TICKET_APPROVAL', referenceType: 'TICKET', referenceId: 't1' }],
  });
  assert.equal(result.stats.myBorrowCount, 1);
  assert.equal(result.todos[0].ticketId, 't1');
  assert.equal(result.todos[0].action, 'approve_reject');
});

test('maps HR counters and immutable transaction pages', () => {
  const dashboard = mapHrDashboard({
    stats: {
      totalAssetQuantity: 10,
      assetTypeCount: 2,
      temporarilyLoanedQuantity: 3,
      overdueTicketCount: 1,
      pendingCustodyConfirmationCount: 4,
      overdueCustodyConfirmationCount: 2,
    },
    todos: [],
  });
  const rows = mapTransactionPage({
    items: [{
      id: 'e1', occurredAt: '2026-10-05T00:00:00Z', eventType: 'APPROVED',
      quantity: 2, fromUserId: 'u1', toUserId: 'u2', ticketId: 't1',
    }],
  });
  assert.equal(dashboard.stats.onLoan, 3);
  assert.equal(rows[0].qty, 2);
  assert.equal(rows[0].ticketId, 't1');
});

test('creates timezone-explicit transaction filters', () => {
  assert.deepEqual(toTransactionQuery({ assetId: 'a1', from: '2026-10-01', to: '2026-10-05' }), {
    assetId: 'a1',
    from: '2026-10-01T00:00:00+07:00',
    to: '2026-10-05T23:59:59.999+07:00',
    offset: 0,
    limit: 100,
  });
});

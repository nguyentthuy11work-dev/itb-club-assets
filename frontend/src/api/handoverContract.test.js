import assert from 'node:assert/strict';
import test from 'node:test';

import { mapHandover } from './handoverContract.js';

const HANDOVER = {
  id: 'handover-id', termId: 'term-id', termCode: '2029-2030', termName: 'Term',
  termStartsOn: '2030-01-01', termEndsOn: '2030-01-31', oldHrUserId: 'old-id',
  newHrUserId: 'new-id', createdByUserId: 'old-id', createdAt: '2030-02-01T00:00:00Z',
  status: 'PENDING_CONFIRMATIONS', oldConfirmedAt: null, newConfirmedAt: null, lockedAt: null,
  items: [{
    id: 'item-id', handoverId: 'handover-id', assetId: 'asset-id', assetName: 'Camera',
    totalQuantity: 2, availableQuantity: 2, condition: 'GOOD', isStale: true,
    lastQualifyingTransactionAt: null, snapshotAt: '2030-02-01T00:00:00Z',
    custodies: [{ custodyId: 'custody-id', custodianUserId: 'user-id', status: 'ACTIVE', onHandQuantity: 2, reservedQuantity: 0, temporarilyLoanedQuantity: 0, condition: 'GOOD', conditionNote: null }],
  }],
};

test('maps immutable handover, stale flag, and independent confirmation timestamps', () => {
  const value = mapHandover(HANDOVER);
  assert.equal(value.items[0].isStale, true);
  assert.equal(value.items[0].custodies[0].onHandQuantity, 2);
  assert.equal(value.oldConfirmedAt, null);
  assert.throws(() => mapHandover({ ...HANDOVER, status: 'OLD_CONFIRMED' }), /unsupported handover/);
});

test('rejects malformed snapshot quantities and custody states', () => {
  assert.throws(
    () => mapHandover({ ...HANDOVER, items: [{ ...HANDOVER.items[0], totalQuantity: -1 }] }),
    /non-negative/,
  );
  assert.throws(
    () => mapHandover({ ...HANDOVER, items: [{ ...HANDOVER.items[0], custodies: [{ ...HANDOVER.items[0].custodies[0], status: 'CLOSED' }] }] }),
    /unsupported custody status/,
  );
});

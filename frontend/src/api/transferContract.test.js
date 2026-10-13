import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mapTransfer,
  mapTransferPage,
  toCreateTransferRequest,
  toTransferEvidenceFormData,
} from './transferContract.js';

const TRANSFER = {
  id: 'transfer-id', sourceCustodyId: 'source-id', assetId: 'asset-id',
  currentCustodianUserId: 'current-id', targetCustodianUserId: 'target-id', quantity: 2,
  conditionSnapshot: 'GOOD', createdByUserId: 'hr-id', createdAt: '2030-02-01T03:00:00Z',
  reconciliationItemId: null, status: 'PENDING_BOTH_CONFIRMATIONS',
  currentConfirmedAt: null, targetConfirmedAt: null, targetCustodyId: null, completedAt: null,
};

test('maps canonical transfer states and pages', () => {
  assert.equal(mapTransfer(TRANSFER).quantity, 2);
  assert.equal(mapTransferPage({ items: [TRANSFER], page: 1, pageSize: 20, total: 1 }).total, 1);
  assert.throws(() => mapTransfer({ ...TRANSFER, status: 'PENDING' }), /unsupported transfer status/);
  assert.throws(() => mapTransfer({ ...TRANSFER, quantity: 0 }), /positive integer/);
});

test('builds strict create and multipart confirmation requests', () => {
  assert.deepEqual(
    toCreateTransferRequest({ sourceCustodyId: ' source ', targetUserId: ' target ', quantity: '2' }),
    { sourceCustodyId: 'source', targetUserId: 'target', quantity: 2 },
  );
  const photo = new Blob(['image'], { type: 'image/jpeg' });
  const form = toTransferEvidenceFormData({ note: ' received ', photos: [photo] });
  assert.deepEqual(JSON.parse(form.get('metadata')), { note: 'received' });
  assert.equal(form.getAll('files').length, 1);
  assert.throws(() => toTransferEvidenceFormData({ photos: [] }), /one to three/);
});

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mapReconciliation,
  mapReconciliationItemPage,
  toReconciliationEvidenceFormData,
} from './reconciliationContract.js';

const ITEM = {
  id: 'item-id', reconciliationId: 'session-id', custodyId: 'custody-id', assetId: 'asset-id',
  custodianUserId: 'user-id', assetName: 'Camera', expectedQuantity: 2,
  expectedCondition: 'GOOD', snapshotAt: '2030-01-02T03:00:00Z', status: 'DISCREPANCY',
  actualQuantity: 1, actualCondition: 'GOOD', conditionNote: null,
  discrepancyReason: 'Missing', confirmedAt: '2030-01-03T03:00:00Z',
  resolutionNote: null, resolvedAt: null, lastRemindedAt: null,
};

test('maps item states and blocks unresolved discrepancies from the close gate', () => {
  const session = mapReconciliation({
    id: 'session-id', reconciliationDate: '2030-01-03', status: 'OPEN',
    openedByUserId: 'hr-id', openedAt: '2030-01-02T03:00:00Z',
    temporaryLoanTicketCount: 1, confirmationRequestedAt: null,
    closedByUserId: null, closedAt: null, items: [ITEM],
  });
  assert.equal(session.unresolvedCount, 1);
  assert.throws(
    () => mapReconciliation({ ...session, items: [{ ...ITEM, status: 'UNCONFIRMED' }] }),
    /unsupported reconciliation item status/,
  );
  assert.equal(mapReconciliationItemPage({ items: [ITEM], page: 1, pageSize: 20, total: 1 }).total, 1);
});

test('builds strict multipart confirmation metadata and evidence', async () => {
  const photo = new Blob(['image'], { type: 'image/jpeg' });
  const form = toReconciliationEvidenceFormData({
    actualQuantity: '2', condition: 'GOOD', conditionNote: ' counted ', photos: [photo],
  });
  assert.equal(JSON.parse(form.get('metadata')).actualQuantity, 2);
  assert.equal(form.getAll('files').length, 1);
  assert.throws(
    () => toReconciliationEvidenceFormData({ actualQuantity: 2, condition: 'GOOD', photos: [] }),
    /one to three/,
  );
});

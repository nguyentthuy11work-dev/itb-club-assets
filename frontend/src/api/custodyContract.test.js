import test from 'node:test';
import assert from 'node:assert/strict';
import {
  mapHeldCustodyPage,
  toCustodyConfirmationFormData,
} from './custodyContract.js';

test('maps canonical held Custodies and derives the 24-hour pending badge', () => {
  const rows = mapHeldCustodyPage({
    items: [{
      custodyId: 'custody-1',
      assetId: 'asset-1',
      name: 'Camera',
      status: 'PENDING',
      onHandQuantity: 3,
      temporarilyLoanedQuantity: 1,
      assignedAt: '2026-10-10T00:00:00Z',
    }],
  }, new Date('2026-10-11T00:00:00Z'));

  assert.deepEqual(rows[0], {
    id: 'custody-1',
    custodyId: 'custody-1',
    assetId: 'asset-1',
    name: 'Camera',
    holding: 3,
    lent: 1,
    status: 'CONFIRM_OVERDUE',
  });
});

test('builds canonical confirmation metadata and repeated file fields', () => {
  const image = new Blob(['image'], { type: 'image/png' });
  const body = toCustodyConfirmationFormData({ photos: [image], note: ' received ' });

  assert.equal(body.get('metadata'), JSON.stringify({ note: 'received' }));
  assert.equal(body.getAll('files').length, 1);
  assert.throws(
    () => toCustodyConfirmationFormData({ photos: [], note: '' }),
    /one to three images/,
  );
});

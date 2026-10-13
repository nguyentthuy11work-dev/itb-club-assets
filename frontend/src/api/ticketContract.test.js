import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mapCreateTicketsResult,
  mapLegacyTicket,
  mapTicketDetail,
  mapTicketPage,
  toEvidenceFormData,
  toTicketQuery,
} from './ticketContract.js';
import { formatTicketActionError } from '../pages/ticketActionError.js';
import { getTicketActions } from '../pages/ticketActions.js';

const TICKET_ID = '00000000-0000-4000-8000-000000000101';
const BORROWER_ID = '00000000-0000-4000-8000-000000000102';
const CUSTODIAN_ID = '00000000-0000-4000-8000-000000000103';
const ASSET_ID = '00000000-0000-4000-8000-000000000104';
const CUSTODY_ID = '00000000-0000-4000-8000-000000000105';

function summary(status = 'PENDING') {
  return {
    id: TICKET_ID,
    borrowerUserId: BORROWER_ID,
    borrowerName: 'Borrower',
    sourceCustodianUserId: CUSTODIAN_ID,
    sourceCustodianName: 'Custodian',
    method: 'TEMPORARY',
    status,
    dueDate: '2099-10-15',
    totalQuantity: 2,
    assetNames: ['Camera'],
    createdAt: '2026-10-05T04:00:00Z',
    updatedAt: '2026-10-05T04:00:00Z',
  };
}

test('maps canonical Ticket page, filters, and UUID ownership fields', () => {
  const page = mapTicketPage({ items: [summary()], total: 1, offset: 0, limit: 20 });
  assert.equal(page.items[0].borrowerId, BORROWER_ID);
  assert.equal(page.items[0].holderId, CUSTODIAN_ID);
  assert.equal(page.items[0].asset, 'Camera');
  assert.deepEqual(toTicketQuery({ role: 'holder', search: ' Camera ', assetId: ASSET_ID }), {
    perspective: 'CUSTODIAN',
    status: undefined,
    search: 'Camera',
    asset_id: ASSET_ID,
    offset: 0,
    limit: 20,
  });
});

test('maps normalized detail and never requires a private storage key', () => {
  const detail = mapTicketDetail({
    ticket: summary('APPROVED'),
    items: [{
      id: 'item-1', assetId: ASSET_ID, assetName: 'Camera',
      sourceCustodyId: CUSTODY_ID, quantity: 2,
    }],
    evidence: [{ id: 'evidence-1', contentType: 'image/png', sizeBytes: 10 }],
    events: [{
      occurredAt: '2026-10-05T04:00:00Z', actorName: 'Custodian',
      eventType: 'APPROVED', note: null,
    }],
  });
  assert.equal(detail.items[0].sourceCustodyId, CUSTODY_ID);
  assert.equal(detail.log[0], '2026-10-05T04:00:00Z · Custodian · APPROVED');
  assert.equal(JSON.stringify(detail).includes('objectKey'), false);
});

test('maps partial creation result and rejects legacy envelopes and statuses', () => {
  const result = mapCreateTicketsResult({
    createdTickets: [{ id: TICKET_ID, status: 'PENDING' }],
    rejectedGroups: [{ sourceCustodyIds: [CUSTODY_ID], reasonCode: 'INSUFFICIENT_LENDABLE_QUANTITY' }],
  });
  assert.equal(result.createdTickets[0].id, TICKET_ID);
  assert.equal(result.rejectedGroups[0].sourceCustodyIds[0], CUSTODY_ID);
  assert.throws(() => mapTicketPage({ tickets: [] }), /items must be an array/);
  assert.throws(
    () => mapTicketPage({ items: [summary('ACTIVE')], total: 1, offset: 0, limit: 20 }),
    /Unsupported Ticket status/,
  );
});

test('builds multipart evidence with JSON metadata and repeated file fields', async () => {
  const first = new Blob(['first'], { type: 'image/png' });
  const second = new Blob(['second'], { type: 'image/jpeg' });
  const form = toEvidenceFormData({ photos: [first, second], note: 'Proof' });
  assert.deepEqual(JSON.parse(form.get('metadata')), { note: 'Proof' });
  assert.equal(form.getAll('files').length, 2);
});

test('exposes every canonical action button by actor side and state', () => {
  assert.deepEqual(getTicketActions({ status: 'PENDING' }, { isBorrower: true }), ['cancelRequest']);
  assert.deepEqual(getTicketActions({ status: 'APPROVED' }, { isBorrower: true }), ['receive', 'cancelRequest']);
  assert.deepEqual(getTicketActions({ status: 'BORROWING' }, { isBorrower: true }), ['return']);
  assert.deepEqual(getTicketActions({ status: 'PENDING' }, { isHolder: true }), ['approve', 'reject']);
  assert.deepEqual(getTicketActions({ status: 'RETURNING' }, { isHolder: true }), ['holderReceive']);
  assert.deepEqual(getTicketActions({ status: 'CANCEL_PENDING' }, { isHolder: true }), ['cancelApprove', 'cancelReject']);
  assert.deepEqual(getTicketActions({ status: 'CLOSED' }, { isBorrower: true }), []);
});

test('shows the stable 409 code and maps legacy mock state only at the adapter edge', () => {
  assert.equal(
    formatTicketActionError({
      message: 'Ticket must be PENDING.',
      status: 409,
      payload: { error: { code: 'INVALID_TICKET_TRANSITION' } },
    }),
    'Ticket must be PENDING. (INVALID_TICKET_TRANSITION)',
  );
  assert.equal(mapLegacyTicket({ id: TICKET_ID, status: 'ACTIVE', asset: 'Camera' }).status, 'BORROWING');
});

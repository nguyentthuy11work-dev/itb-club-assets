import assert from 'node:assert/strict';
import test from 'node:test';

import { mapEventDay, toEventDayRequest } from './eventDayContract.js';

const EVENT_ID = '00000000-0000-4000-8000-000000000001';
const SHIFT_ID = '00000000-0000-4000-8000-000000000002';
const LEAD_ID = '00000000-0000-4000-8000-000000000003';
const ASSET_ID = '00000000-0000-4000-8000-000000000004';

const RESPONSE = {
  id: EVENT_ID,
  name: 'Tet Booth',
  eventDate: '2030-01-02',
  status: 'DRAFT',
  coreStockChanged: false,
  createdByUserId: LEAD_ID,
  shifts: [{
    id: SHIFT_ID,
    sequence: 1,
    name: 'Morning',
    startsAt: '2030-01-02T04:00:00Z',
    endsAt: '2030-01-02T05:00:00Z',
    shiftLeadUserId: LEAD_ID,
    status: 'SCHEDULED',
    assets: [{
      assetId: ASSET_ID,
      quotaQuantity: 3,
      carryOverQuantity: 1,
      expectedOpenQuantity: 4,
    }],
  }],
};

test('maps the complete canonical Event Day response and preserves server IDs', () => {
  const eventDay = mapEventDay(RESPONSE);
  assert.equal(eventDay.id, EVENT_ID);
  assert.equal(eventDay.shifts[0].id, SHIFT_ID);
  assert.equal(eventDay.shifts[0].assets[0].expectedOpenQuantity, 4);
  assert.equal(eventDay.coreStockChanged, false);
});

test('builds a complete replacement request with contiguous sequence values', () => {
  const request = toEventDayRequest({
    ...RESPONSE,
    name: ' Updated Event ',
    shifts: [
      RESPONSE.shifts[0],
      {
        name: 'Evening',
        startsAt: '2030-01-02T13:00',
        endsAt: '2030-01-02T15:00',
        shiftLeadUserId: LEAD_ID,
        assets: [{ assetId: ASSET_ID, quotaQuantity: '2' }],
      },
    ],
  });

  assert.equal(request.name, 'Updated Event');
  assert.equal(request.shifts[0].id, SHIFT_ID);
  assert.equal(request.shifts[0].sequence, 1);
  assert.equal(request.shifts[1].sequence, 2);
  assert.equal(request.shifts[1].quotaQuantity, undefined);
  assert.equal(request.shifts[1].assets[0].quotaQuantity, 2);
  assert.ok(!('id' in request.shifts[1]));
});

test('rejects malformed response status and empty Shift assets', () => {
  assert.throws(() => mapEventDay({ ...RESPONSE, status: 'UNKNOWN' }), /unsupported status/);
  assert.throws(
    () => toEventDayRequest({ ...RESPONSE, shifts: [{ ...RESPONSE.shifts[0], assets: [] }] }),
    /must contain at least one Asset/,
  );
});

import assert from 'node:assert/strict';
import test from 'node:test';

import { mapEventDaySummary, mapShift, mapShiftPage, toShiftQuantities } from './shiftContract.js';

const SHIFT = {
  id: 'shift-id',
  eventDayId: 'event-id',
  eventDayName: 'Tet Booth',
  sequence: 1,
  name: 'Morning',
  startsAt: '2030-01-02T04:00:00Z',
  endsAt: '2030-01-02T05:00:00Z',
  shiftLeadUserId: 'lead-id',
  status: 'CLOSED',
  coreStockChanged: false,
  items: [{
    id: 'item-id',
    assetId: 'asset-id',
    quotaQuantity: 3,
    carryOverQuantity: 2,
    expectedOpenQuantity: 5,
    openingActualQuantity: 4,
    closingActualQuantity: 1,
    consumedQuantity: 4,
    discrepancyReason: 'One missing',
  }],
};

test('maps canonical Shift details and assigned paging without legacy states', () => {
  assert.equal(mapShift(SHIFT).items[0].expectedOpenQuantity, 5);
  assert.deepEqual(
    mapShiftPage({ items: [SHIFT], page: 1, pageSize: 20, total: 1 }).items[0],
    mapShift(SHIFT),
  );
  assert.throws(() => mapShift({ ...SHIFT, status: 'NOT_OPENED' }), /unsupported status/);
});

test('builds complete quantity commands with integer validation', () => {
  assert.deepEqual(toShiftQuantities([{ assetId: 'asset-id', actualQuantity: '2' }]), {
    items: [{ assetId: 'asset-id', actualQuantity: 2 }],
  });
  assert.throws(
    () => toShiftQuantities([{ assetId: 'asset-id', actualQuantity: -1 }]),
    /non-negative integer/,
  );
});

test('keeps final summary items hidden until every Shift closes', () => {
  const progress = mapEventDaySummary({
    eventDayId: 'event-id',
    status: 'IN_PROGRESS',
    complete: false,
    totalShiftCount: 2,
    closedShiftCount: 1,
    coreStockChanged: false,
    items: null,
  });
  assert.equal(progress.items, null);

  const complete = mapEventDaySummary({
    ...progress,
    status: 'COMPLETED',
    complete: true,
    closedShiftCount: 2,
    items: [{
      assetId: 'asset-id',
      totalQuotaQuantity: 5,
      totalConsumedQuantity: 4,
      openingVarianceQuantity: -1,
      finalCarryOverQuantity: 1,
    }],
  });
  assert.equal(complete.items[0].openingVarianceQuantity, -1);
  assert.throws(() => mapEventDaySummary({ ...progress, complete: true }), /only when complete/);
});

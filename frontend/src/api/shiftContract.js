const SHIFT_STATUSES = new Set([
  'SCHEDULED', 'OPENING', 'ACTIVE', 'ACTIVE_WITH_DEVIATION', 'CLOSED',
]);
const EVENT_STATUSES = new Set(['DRAFT', 'IN_PROGRESS', 'COMPLETED']);

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

function string(value, field) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value;
}

function integer(value, field, { nullable = false, signed = false } = {}) {
  if (nullable && value === null) return null;
  const parsed = typeof value === 'string' && value !== '' ? Number(value) : value;
  if (!Number.isInteger(parsed) || (!signed && parsed < 0)) {
    throw new TypeError(`${field} must be ${signed ? 'an' : 'a non-negative'} integer`);
  }
  return parsed;
}

function status(value, allowed, field) {
  if (!allowed.has(value)) throw new TypeError(`${field} has an unsupported status`);
  return value;
}

function nullableString(value, field) {
  return value === null ? null : string(value, field);
}

function mapShiftItem(value, index) {
  const field = `items[${index}]`;
  const row = object(value, field);
  return {
    id: string(row.id, `${field}.id`),
    assetId: string(row.assetId, `${field}.assetId`),
    quotaQuantity: integer(row.quotaQuantity, `${field}.quotaQuantity`),
    carryOverQuantity: integer(row.carryOverQuantity, `${field}.carryOverQuantity`),
    expectedOpenQuantity: integer(
      row.expectedOpenQuantity,
      `${field}.expectedOpenQuantity`,
    ),
    openingActualQuantity: integer(
      row.openingActualQuantity,
      `${field}.openingActualQuantity`,
      { nullable: true },
    ),
    closingActualQuantity: integer(
      row.closingActualQuantity,
      `${field}.closingActualQuantity`,
      { nullable: true },
    ),
    consumedQuantity: integer(
      row.consumedQuantity,
      `${field}.consumedQuantity`,
      { nullable: true, signed: true },
    ),
    discrepancyReason: nullableString(row.discrepancyReason, `${field}.discrepancyReason`),
  };
}

/** Validate and map one complete canonical Shift response. */
export function mapShift(payload) {
  const row = object(payload, 'shift');
  if (typeof row.coreStockChanged !== 'boolean') {
    throw new TypeError('shift.coreStockChanged must be a boolean');
  }
  return {
    id: string(row.id, 'shift.id'),
    eventDayId: string(row.eventDayId, 'shift.eventDayId'),
    eventDayName: string(row.eventDayName, 'shift.eventDayName'),
    sequence: integer(row.sequence, 'shift.sequence'),
    name: string(row.name, 'shift.name'),
    startsAt: string(row.startsAt, 'shift.startsAt'),
    endsAt: string(row.endsAt, 'shift.endsAt'),
    shiftLeadUserId: string(row.shiftLeadUserId, 'shift.shiftLeadUserId'),
    status: status(row.status, SHIFT_STATUSES, 'shift.status'),
    coreStockChanged: row.coreStockChanged,
    items: array(row.items, 'shift.items').map(mapShiftItem),
  };
}

/** Validate and map the assigned Shift page. */
export function mapShiftPage(payload) {
  const row = object(payload, 'shiftPage');
  return {
    items: array(row.items, 'shiftPage.items').map(mapShift),
    page: integer(row.page, 'shiftPage.page'),
    pageSize: integer(row.pageSize, 'shiftPage.pageSize'),
    total: integer(row.total, 'shiftPage.total'),
  };
}

/** Build a complete actual-quantity request from editable rows. */
export function toShiftQuantities(items) {
  return {
    items: array(items, 'items').map((value, index) => {
      const row = object(value, `items[${index}]`);
      return {
        assetId: string(row.assetId, `items[${index}].assetId`),
        actualQuantity: integer(row.actualQuantity, `items[${index}].actualQuantity`),
      };
    }),
  };
}

/** Validate Event Day progress and conditionally available final totals. */
export function mapEventDaySummary(payload) {
  const row = object(payload, 'eventDaySummary');
  if (typeof row.complete !== 'boolean' || typeof row.coreStockChanged !== 'boolean') {
    throw new TypeError('Event Day summary flags must be boolean');
  }
  const items = row.items === null ? null : array(row.items, 'eventDaySummary.items').map(
    (value, index) => {
      const item = object(value, `eventDaySummary.items[${index}]`);
      return {
        assetId: string(item.assetId, `eventDaySummary.items[${index}].assetId`),
        totalQuotaQuantity: integer(
          item.totalQuotaQuantity,
          `eventDaySummary.items[${index}].totalQuotaQuantity`,
        ),
        totalConsumedQuantity: integer(
          item.totalConsumedQuantity,
          `eventDaySummary.items[${index}].totalConsumedQuantity`,
          { signed: true },
        ),
        openingVarianceQuantity: integer(
          item.openingVarianceQuantity,
          `eventDaySummary.items[${index}].openingVarianceQuantity`,
          { signed: true },
        ),
        finalCarryOverQuantity: integer(
          item.finalCarryOverQuantity,
          `eventDaySummary.items[${index}].finalCarryOverQuantity`,
        ),
      };
    },
  );
  if (row.complete !== (items !== null)) {
    throw new TypeError('Final Event Day items must exist only when complete');
  }
  return {
    eventDayId: string(row.eventDayId, 'eventDaySummary.eventDayId'),
    status: status(row.status, EVENT_STATUSES, 'eventDaySummary.status'),
    complete: row.complete,
    totalShiftCount: integer(row.totalShiftCount, 'eventDaySummary.totalShiftCount'),
    closedShiftCount: integer(row.closedShiftCount, 'eventDaySummary.closedShiftCount'),
    coreStockChanged: row.coreStockChanged,
    items,
  };
}

/** Generate one actor-command idempotency key. */
export function createShiftIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `shift-${Date.now()}-${Math.random()}`;
}

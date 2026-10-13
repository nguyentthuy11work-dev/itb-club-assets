const EVENT_DAY_STATUSES = new Set(['DRAFT', 'IN_PROGRESS', 'COMPLETED']);
const SHIFT_STATUSES = new Set(['SCHEDULED', 'OPENING', 'ACTIVE', 'ACTIVE_WITH_DEVIATION', 'CLOSED']);

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

function integer(value, field) {
  const parsed = typeof value === 'string' && value !== '' ? Number(value) : value;
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new TypeError(`${field} must be a non-negative integer`);
  }
  return parsed;
}

function status(value, allowed, field) {
  if (!allowed.has(value)) throw new TypeError(`${field} has an unsupported status`);
  return value;
}

function dateTime(value, field) {
  const text = string(value, field);
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) throw new TypeError(`${field} must be a valid date-time`);
  return parsed.toISOString();
}

function mapShiftAsset(value, field) {
  const row = object(value, field);
  return {
    assetId: string(row.assetId, `${field}.assetId`),
    quotaQuantity: integer(row.quotaQuantity, `${field}.quotaQuantity`),
    carryOverQuantity: integer(row.carryOverQuantity, `${field}.carryOverQuantity`),
    expectedOpenQuantity: integer(row.expectedOpenQuantity, `${field}.expectedOpenQuantity`),
  };
}

function mapShift(value, index) {
  const field = `shifts[${index}]`;
  const row = object(value, field);
  return {
    id: string(row.id, `${field}.id`),
    sequence: integer(row.sequence, `${field}.sequence`),
    name: string(row.name, `${field}.name`),
    startsAt: dateTime(row.startsAt, `${field}.startsAt`),
    endsAt: dateTime(row.endsAt, `${field}.endsAt`),
    shiftLeadUserId: string(row.shiftLeadUserId, `${field}.shiftLeadUserId`),
    status: status(row.status, SHIFT_STATUSES, `${field}.status`),
    assets: array(row.assets, `${field}.assets`).map((asset, assetIndex) => (
      mapShiftAsset(asset, `${field}.assets[${assetIndex}]`)
    )),
  };
}

/** Map and validate the canonical Event Day API response. */
export function mapEventDay(payload) {
  const row = object(payload, 'eventDay');
  if (typeof row.coreStockChanged !== 'boolean') {
    throw new TypeError('eventDay.coreStockChanged must be a boolean');
  }
  return {
    id: string(row.id, 'eventDay.id'),
    name: string(row.name, 'eventDay.name'),
    eventDate: string(row.eventDate, 'eventDay.eventDate'),
    status: status(row.status, EVENT_DAY_STATUSES, 'eventDay.status'),
    coreStockChanged: row.coreStockChanged,
    createdByUserId: string(row.createdByUserId, 'eventDay.createdByUserId'),
    shifts: array(row.shifts, 'eventDay.shifts').map(mapShift),
  };
}

/** Convert editable form state to the complete create/update API request. */
export function toEventDayRequest(draft) {
  const row = object(draft, 'draft');
  const shifts = array(row.shifts, 'draft.shifts');
  if (shifts.length === 0) throw new TypeError('Event Day must contain at least one Shift');

  return {
    name: string(row.name, 'draft.name').trim(),
    eventDate: string(row.eventDate, 'draft.eventDate'),
    shifts: shifts.map((value, index) => {
      const shift = object(value, `draft.shifts[${index}]`);
      const assets = array(shift.assets, `draft.shifts[${index}].assets`);
      if (assets.length === 0) throw new TypeError(`Shift ${index + 1} must contain at least one Asset`);
      const request = {
        sequence: index + 1,
        name: string(shift.name, `draft.shifts[${index}].name`).trim(),
        startsAt: dateTime(shift.startsAt, `draft.shifts[${index}].startsAt`),
        endsAt: dateTime(shift.endsAt, `draft.shifts[${index}].endsAt`),
        shiftLeadUserId: string(
          shift.shiftLeadUserId,
          `draft.shifts[${index}].shiftLeadUserId`,
        ).trim(),
        assets: assets.map((assetValue, assetIndex) => {
          const asset = object(assetValue, `draft.shifts[${index}].assets[${assetIndex}]`);
          return {
            assetId: string(
              asset.assetId,
              `draft.shifts[${index}].assets[${assetIndex}].assetId`,
            ),
            quotaQuantity: integer(
              asset.quotaQuantity,
              `draft.shifts[${index}].assets[${assetIndex}].quotaQuantity`,
            ),
          };
        }),
      };
      if (shift.id) request.id = string(shift.id, `draft.shifts[${index}].id`);
      return request;
    }),
  };
}

/** Generate an idempotency key for one Event Day command attempt. */
export function createEventDayIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `event-day-${Date.now()}-${Math.random()}`;
}

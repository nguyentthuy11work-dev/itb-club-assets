const HANDOVER_STATUSES = new Set(['PENDING_CONFIRMATIONS', 'LOCKED']);
const CUSTODY_STATUSES = new Set(['PENDING', 'ACTIVE']);
const CONDITIONS = new Set(['GOOD', 'DAMAGED', 'LOST']);

function requireObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

function requireString(value, label) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label} is required`);
  return value;
}

function nullableString(value, label) {
  if (value == null) return null;
  if (typeof value !== 'string') throw new Error(`${label} must be a string or null`);
  return value;
}

function nonNegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) throw new Error(`${label} must be non-negative`);
  return value;
}

function mapCustody(raw) {
  const value = requireObject(raw, 'handover custody');
  if (!CUSTODY_STATUSES.has(value.status)) throw new Error('unsupported custody status');
  if (!CONDITIONS.has(value.condition)) throw new Error('unsupported custody condition');
  return {
    custodyId: requireString(value.custodyId, 'custody id'),
    custodianUserId: requireString(value.custodianUserId, 'custodian user id'),
    status: value.status,
    onHandQuantity: nonNegativeInteger(value.onHandQuantity, 'onHandQuantity'),
    reservedQuantity: nonNegativeInteger(value.reservedQuantity, 'reservedQuantity'),
    temporarilyLoanedQuantity: nonNegativeInteger(
      value.temporarilyLoanedQuantity, 'temporarilyLoanedQuantity',
    ),
    condition: value.condition,
    conditionNote: nullableString(value.conditionNote, 'condition note'),
  };
}

function mapItem(raw) {
  const value = requireObject(raw, 'handover item');
  if (!CONDITIONS.has(value.condition)) throw new Error('unsupported item condition');
  if (!Array.isArray(value.custodies)) throw new Error('custodies must be an array');
  return {
    id: requireString(value.id, 'item id'),
    handoverId: requireString(value.handoverId, 'handover id'),
    assetId: requireString(value.assetId, 'asset id'),
    assetName: requireString(value.assetName, 'asset name'),
    totalQuantity: nonNegativeInteger(value.totalQuantity, 'totalQuantity'),
    availableQuantity: nonNegativeInteger(value.availableQuantity, 'availableQuantity'),
    condition: value.condition,
    custodies: value.custodies.map(mapCustody),
    isStale: Boolean(value.isStale),
    lastQualifyingTransactionAt: nullableString(
      value.lastQualifyingTransactionAt, 'last transaction time',
    ),
    snapshotAt: requireString(value.snapshotAt, 'snapshot time'),
  };
}

export function mapHandover(raw) {
  const value = requireObject(raw, 'handover');
  if (!HANDOVER_STATUSES.has(value.status)) throw new Error('unsupported handover status');
  if (!Array.isArray(value.items)) throw new Error('handover items must be an array');
  return {
    id: requireString(value.id, 'handover id'),
    termId: requireString(value.termId, 'term id'),
    termCode: requireString(value.termCode, 'term code'),
    termName: requireString(value.termName, 'term name'),
    termStartsOn: requireString(value.termStartsOn, 'term start'),
    termEndsOn: requireString(value.termEndsOn, 'term end'),
    oldHrUserId: requireString(value.oldHrUserId, 'old HR id'),
    newHrUserId: requireString(value.newHrUserId, 'new HR id'),
    createdByUserId: requireString(value.createdByUserId, 'creator id'),
    createdAt: requireString(value.createdAt, 'creation time'),
    status: value.status,
    oldConfirmedAt: nullableString(value.oldConfirmedAt, 'old confirmation time'),
    newConfirmedAt: nullableString(value.newConfirmedAt, 'new confirmation time'),
    lockedAt: nullableString(value.lockedAt, 'lock time'),
    items: value.items.map(mapItem),
  };
}

export function createHandoverIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `handover-${Date.now()}-${Math.random()}`;
}

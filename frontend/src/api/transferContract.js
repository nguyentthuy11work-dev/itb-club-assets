const TRANSFER_STATUSES = new Set([
  'PENDING_BOTH_CONFIRMATIONS',
  'PENDING_CURRENT_CONFIRMATION',
  'PENDING_TARGET_CONFIRMATION',
  'COMPLETED',
]);
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

export function mapTransfer(raw) {
  const value = requireObject(raw, 'transfer');
  if (!TRANSFER_STATUSES.has(value.status)) throw new Error('unsupported transfer status');
  if (!CONDITIONS.has(value.conditionSnapshot)) throw new Error('unsupported condition snapshot');
  if (!Number.isInteger(value.quantity) || value.quantity < 1) {
    throw new Error('quantity must be a positive integer');
  }
  return {
    id: requireString(value.id, 'transfer id'),
    sourceCustodyId: requireString(value.sourceCustodyId, 'source custody id'),
    assetId: requireString(value.assetId, 'asset id'),
    currentCustodianUserId: requireString(value.currentCustodianUserId, 'current custodian'),
    targetCustodianUserId: requireString(value.targetCustodianUserId, 'target custodian'),
    quantity: value.quantity,
    conditionSnapshot: value.conditionSnapshot,
    createdByUserId: requireString(value.createdByUserId, 'creating actor'),
    createdAt: requireString(value.createdAt, 'creation time'),
    reconciliationItemId: nullableString(value.reconciliationItemId, 'reconciliation item id'),
    status: value.status,
    currentConfirmedAt: nullableString(value.currentConfirmedAt, 'current confirmation time'),
    targetConfirmedAt: nullableString(value.targetConfirmedAt, 'target confirmation time'),
    targetCustodyId: nullableString(value.targetCustodyId, 'target custody id'),
    completedAt: nullableString(value.completedAt, 'completion time'),
  };
}

export function mapTransferPage(raw) {
  const value = requireObject(raw, 'transfer page');
  if (!Array.isArray(value.items)) throw new Error('transfer page items must be an array');
  return {
    items: value.items.map(mapTransfer),
    page: Number(value.page),
    pageSize: Number(value.pageSize),
    total: Number(value.total),
  };
}

export function toCreateTransferRequest({ sourceCustodyId, targetUserId, quantity }) {
  const parsedQuantity = Number(quantity);
  if (!Number.isInteger(parsedQuantity) || parsedQuantity < 1) {
    throw new Error('quantity must be a positive integer');
  }
  return {
    sourceCustodyId: requireString(sourceCustodyId, 'source custody id').trim(),
    targetUserId: requireString(targetUserId, 'target user id').trim(),
    quantity: parsedQuantity,
  };
}

export function toTransferEvidenceFormData({ note, photos }) {
  if (!Array.isArray(photos) || photos.length < 1 || photos.length > 3) {
    throw new Error('one to three evidence images are required');
  }
  const form = new FormData();
  form.append('metadata', JSON.stringify({ note: note?.trim() || null }));
  photos.forEach((photo) => form.append('files', photo));
  return form;
}

export function createTransferIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `transfer-${Date.now()}-${Math.random()}`;
}

const SESSION_STATUSES = new Set(['OPEN', 'CLOSED']);
const ITEM_STATUSES = new Set([
  'PENDING_CONFIRMATION',
  'CONFIRMED',
  'DISCREPANCY',
  'RESOLVED',
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

export function mapReconciliationItem(raw) {
  const value = requireObject(raw, 'reconciliation item');
  if (!ITEM_STATUSES.has(value.status)) throw new Error('unsupported reconciliation item status');
  if (!CONDITIONS.has(value.expectedCondition)) throw new Error('unsupported expected condition');
  if (value.actualCondition != null && !CONDITIONS.has(value.actualCondition)) {
    throw new Error('unsupported actual condition');
  }
  if (!Number.isInteger(value.expectedQuantity) || value.expectedQuantity < 0) {
    throw new Error('expectedQuantity must be a non-negative integer');
  }
  if (value.actualQuantity != null && (!Number.isInteger(value.actualQuantity) || value.actualQuantity < 0)) {
    throw new Error('actualQuantity must be a non-negative integer or null');
  }
  return {
    id: requireString(value.id, 'item id'),
    reconciliationId: requireString(value.reconciliationId, 'reconciliation id'),
    custodyId: requireString(value.custodyId, 'custody id'),
    assetId: requireString(value.assetId, 'asset id'),
    custodianUserId: requireString(value.custodianUserId, 'custodian user id'),
    assetName: requireString(value.assetName, 'asset name'),
    expectedQuantity: value.expectedQuantity,
    expectedCondition: value.expectedCondition,
    snapshotAt: requireString(value.snapshotAt, 'snapshot time'),
    status: value.status,
    actualQuantity: value.actualQuantity ?? null,
    actualCondition: value.actualCondition ?? null,
    conditionNote: nullableString(value.conditionNote, 'condition note'),
    discrepancyReason: nullableString(value.discrepancyReason, 'discrepancy reason'),
    confirmedAt: value.confirmedAt ?? null,
    resolutionNote: nullableString(value.resolutionNote, 'resolution note'),
    resolvedAt: value.resolvedAt ?? null,
    lastRemindedAt: value.lastRemindedAt ?? null,
  };
}

export function mapReconciliation(raw) {
  const value = requireObject(raw, 'reconciliation');
  if (!SESSION_STATUSES.has(value.status)) throw new Error('unsupported reconciliation status');
  if (!Array.isArray(value.items)) throw new Error('reconciliation items must be an array');
  const items = value.items.map(mapReconciliationItem);
  return {
    id: requireString(value.id, 'reconciliation id'),
    reconciliationDate: requireString(value.reconciliationDate, 'reconciliation date'),
    status: value.status,
    openedByUserId: requireString(value.openedByUserId, 'opening actor'),
    openedAt: requireString(value.openedAt, 'opening time'),
    temporaryLoanTicketCount: Number(value.temporaryLoanTicketCount || 0),
    confirmationRequestedAt: value.confirmationRequestedAt ?? null,
    closedByUserId: value.closedByUserId ?? null,
    closedAt: value.closedAt ?? null,
    items,
    unresolvedCount: items.filter((item) => (
      item.status === 'PENDING_CONFIRMATION' || item.status === 'DISCREPANCY'
    )).length,
  };
}

export function mapReconciliationItemPage(raw) {
  const value = requireObject(raw, 'reconciliation page');
  if (!Array.isArray(value.items)) throw new Error('page items must be an array');
  return {
    items: value.items.map(mapReconciliationItem),
    page: Number(value.page),
    pageSize: Number(value.pageSize),
    total: Number(value.total),
  };
}

export function createReconciliationIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `reconciliation-${Date.now()}-${Math.random()}`;
}

export function toReconciliationEvidenceFormData({ actualQuantity, condition, conditionNote, discrepancyReason, photos }) {
  const quantity = Number(actualQuantity);
  if (!Number.isInteger(quantity) || quantity < 0) throw new Error('actual quantity must be a non-negative integer');
  if (!CONDITIONS.has(condition)) throw new Error('unsupported condition');
  if (!Array.isArray(photos) || photos.length < 1 || photos.length > 3) {
    throw new Error('one to three evidence images are required');
  }
  const form = new FormData();
  form.append('metadata', JSON.stringify({
    actualQuantity: quantity,
    condition,
    conditionNote: conditionNote?.trim() || null,
    discrepancyReason: discrepancyReason?.trim() || null,
  }));
  photos.forEach((photo) => form.append('files', photo));
  return form;
}

export function legacyBatchToReconciliation(data) {
  const batch = data.batch;
  const now = new Date().toISOString();
  return mapReconciliation({
    id: batch.id,
    reconciliationDate: batch.openedDate.slice(0, 10),
    status: batch.status,
    openedByUserId: 'mock-hr',
    openedAt: now,
    temporaryLoanTicketCount: 0,
    confirmationRequestedAt: null,
    closedByUserId: null,
    closedAt: null,
    items: batch.rows.map((row) => ({
      id: row.id,
      reconciliationId: batch.id,
      custodyId: `custody-${row.id}`,
      assetId: `asset-${row.id}`,
      custodianUserId: `custodian-${row.id}`,
      assetName: row.asset,
      expectedQuantity: row.expected,
      expectedCondition: 'GOOD',
      snapshotAt: now,
      status: row.result === 'MATCH' ? 'CONFIRMED' : row.result === 'RESOLVED' ? 'RESOLVED' : row.result === 'MISMATCH' ? 'DISCREPANCY' : 'PENDING_CONFIRMATION',
      actualQuantity: row.declared ?? null,
      actualCondition: row.declared == null ? null : 'GOOD',
      conditionNote: null,
      discrepancyReason: row.reason || null,
      confirmedAt: row.declared == null ? null : now,
      resolutionNote: row.resolutionNote || null,
      resolvedAt: row.result === 'RESOLVED' ? now : null,
      lastRemindedAt: null,
    })),
  });
}

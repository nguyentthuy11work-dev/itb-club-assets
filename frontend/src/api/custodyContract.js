function requireNonNegativeInteger(value, field) {
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`Invalid Custody field: ${field}`);
  }
  return value;
}

export function mapHeldCustodyPage(payload, now = new Date()) {
  if (!payload || !Array.isArray(payload.items)) {
    throw new Error('Invalid held Custody page');
  }

  return payload.items.map((item) => {
    if (!item.custodyId || !item.assetId || !item.name) {
      throw new Error('Invalid held Custody identity');
    }
    if (!['PENDING', 'ACTIVE'].includes(item.status)) {
      throw new Error('Invalid held Custody status');
    }
    const assignedAt = new Date(item.assignedAt);
    if (Number.isNaN(assignedAt.getTime())) {
      throw new Error('Invalid held Custody assignment time');
    }
    const pendingOverdue = item.status === 'PENDING'
      && now.getTime() - assignedAt.getTime() >= 24 * 60 * 60 * 1000;

    return {
      id: item.custodyId,
      custodyId: item.custodyId,
      assetId: item.assetId,
      name: item.name,
      holding: requireNonNegativeInteger(item.onHandQuantity, 'onHandQuantity'),
      lent: requireNonNegativeInteger(item.temporarilyLoanedQuantity, 'temporarilyLoanedQuantity'),
      status: item.status === 'ACTIVE'
        ? 'CONFIRMED'
        : pendingOverdue ? 'CONFIRM_OVERDUE' : 'PENDING_CONFIRM',
    };
  });
}

export function toCustodyConfirmationFormData({ photos, note }) {
  if (!Array.isArray(photos) || photos.length < 1 || photos.length > 3) {
    throw new Error('Custody confirmation requires one to three images');
  }
  const body = new FormData();
  body.append('metadata', JSON.stringify({ note: note?.trim() || null }));
  for (const photo of photos) body.append('files', photo);
  return body;
}

export function createCustodyIdempotencyKey() {
  return crypto.randomUUID();
}

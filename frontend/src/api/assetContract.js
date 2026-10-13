function assertObject(value, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${field} must be an object`);
  }
  return value;
}

function assertString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value;
}

function assertNullableString(value, field) {
  if (value === null) return null;
  return assertString(value, field);
}

function assertNonNegativeInteger(value, field) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${field} must be a non-negative integer`);
  }
  return value;
}

function assertPositiveInteger(value, field) {
  if (!Number.isInteger(value) || value < 1) {
    throw new TypeError(`${field} must be a positive integer`);
  }
  return value;
}

function mapEligibleCustody(value, index) {
  const row = assertObject(value, `eligibleCustodies[${index}]`);
  return {
    sourceCustodyId: assertString(
      row.sourceCustodyId,
      `eligibleCustodies[${index}].sourceCustodyId`,
    ),
    lendableQuantity: assertNonNegativeInteger(
      row.lendableQuantity,
      `eligibleCustodies[${index}].lendableQuantity`,
    ),
  };
}

function mapAsset(value, index) {
  const row = assertObject(value, `items[${index}]`);
  if (!Array.isArray(row.eligibleCustodies)) {
    throw new TypeError(`items[${index}].eligibleCustodies must be an array`);
  }
  return {
    id: assertString(row.id, `items[${index}].id`),
    categoryId: assertString(row.categoryId, `items[${index}].categoryId`),
    responsibleTeamId: assertNullableString(
      row.responsibleTeamId,
      `items[${index}].responsibleTeamId`,
    ),
    name: assertString(row.name, `items[${index}].name`),
    description: assertNullableString(row.description, `items[${index}].description`),
    source: assertString(row.source, `items[${index}].source`),
    totalQuantity: assertNonNegativeInteger(
      row.totalQuantity,
      `items[${index}].totalQuantity`,
    ),
    availableQuantity: assertNonNegativeInteger(
      row.availableQuantity,
      `items[${index}].availableQuantity`,
    ),
    eligibleCustodies: row.eligibleCustodies.map(mapEligibleCustody),
  };
}

export function mapAssetPage(payload) {
  const page = assertObject(payload, 'asset page');
  if (!Array.isArray(page.items)) {
    throw new TypeError('items must be an array');
  }
  return {
    items: page.items.map(mapAsset),
    page: assertPositiveInteger(page.page, 'page'),
    pageSize: assertPositiveInteger(page.pageSize, 'pageSize'),
    total: assertNonNegativeInteger(page.total, 'total'),
  };
}

export function toAssetBrowseQuery({ search, categoryId, teamId, page = 1, pageSize = 20 } = {}) {
  return {
    search: search?.trim() || undefined,
    category: categoryId || undefined,
    team: teamId || undefined,
    page,
    page_size: pageSize,
  };
}

import assert from 'node:assert/strict';
import test from 'node:test';

import { mapAssetPage, toAssetBrowseQuery } from './assetContract.js';

const ASSET_ID = '00000000-0000-4000-8000-000000000001';
const CATEGORY_ID = '00000000-0000-4000-8000-000000000002';
const TEAM_ID = '00000000-0000-4000-8000-000000000003';
const CUSTODY_ID = '00000000-0000-4000-8000-000000000004';

test('maps the canonical OpenAPI Asset page without replacing server IDs', () => {
  const page = mapAssetPage({
    items: [{
      id: ASSET_ID,
      categoryId: CATEGORY_ID,
      responsibleTeamId: TEAM_ID,
      name: 'Camera A',
      description: null,
      source: 'PURCHASE',
      totalQuantity: 5,
      availableQuantity: 3,
      eligibleCustodies: [{ sourceCustodyId: CUSTODY_ID, lendableQuantity: 3 }],
    }],
    page: 1,
    pageSize: 20,
    total: 1,
  });

  assert.equal(page.items[0].id, ASSET_ID);
  assert.equal(page.items[0].categoryId, CATEGORY_ID);
  assert.equal(page.items[0].responsibleTeamId, TEAM_ID);
  assert.deepEqual(page.items[0].eligibleCustodies, [
    { sourceCustodyId: CUSTODY_ID, lendableQuantity: 3 },
  ]);
  assert.deepEqual(
    { page: page.page, pageSize: page.pageSize, total: page.total },
    { page: 1, pageSize: 20, total: 1 },
  );
});

test('maps UI filters to canonical query names and trims empty search', () => {
  assert.deepEqual(
    toAssetBrowseQuery({
      search: ' Camera ',
      categoryId: CATEGORY_ID,
      teamId: TEAM_ID,
      page: 2,
      pageSize: 10,
    }),
    {
      search: 'Camera',
      category: CATEGORY_ID,
      team: TEAM_ID,
      page: 2,
      page_size: 10,
    },
  );
  assert.equal(toAssetBrowseQuery({ search: '   ' }).search, undefined);
});

test('rejects the legacy assets envelope', () => {
  assert.throws(() => mapAssetPage({ assets: [] }), /items must be an array/);
});

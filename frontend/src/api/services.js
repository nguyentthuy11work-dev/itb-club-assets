// ============================================================================
// SERVICES — "hợp đồng" API giữa frontend và backend
// ----------------------------------------------------------------------------
// Đây là nơi DUY NHẤT các trang (pages/) gọi tới để lấy/đẩy dữ liệu.
// Mỗi hàm bên dưới mô tả rõ: method + endpoint + payload + response mong đợi.
// Khi nối backend thật, đội backend chỉ cần implement đúng các endpoint này.
//
// Bật/tắt mock: xem src/api/client.js (USE_MOCK) hoặc file .env
// ============================================================================

import { download, http, setToken, USE_MOCK } from './client';
import { mockApi, mockApiExt } from './mockServer';
import { mapAssetPage, toAssetBrowseQuery } from './assetContract';
import {
  createEventDayIdempotencyKey,
  mapEventDay,
  toEventDayRequest,
} from './eventDayContract';
import {
  mapHrDashboard,
  mapMemberDashboard,
  mapTransactionPage,
  toTransactionQuery,
} from './operationalContract';
import {
  createIdempotencyKey,
  mapCreateTicketsResult,
  mapLegacyTicket,
  mapTicketDetail,
  mapTicketPage,
  toEvidenceFormData,
  toTicketQuery,
} from './ticketContract';
import {
  createShiftIdempotencyKey,
  mapEventDaySummary,
  mapShift,
  mapShiftPage,
  toShiftQuantities,
} from './shiftContract';
import {
  createReconciliationIdempotencyKey,
  legacyBatchToReconciliation,
  mapReconciliation,
  mapReconciliationItem,
  mapReconciliationItemPage,
  toReconciliationEvidenceFormData,
} from './reconciliationContract';
import {
  createTransferIdempotencyKey,
  mapTransfer,
  mapTransferPage,
  toCreateTransferRequest,
  toTransferEvidenceFormData,
} from './transferContract';
import {
  createHandoverIdempotencyKey,
  mapHandover,
} from './handoverContract';
import {
  createCustodyIdempotencyKey,
  mapHeldCustodyPage,
  toCustodyConfirmationFormData,
} from './custodyContract';

export const authService = {
  /**
   * POST /auth/login
   * body: { username, password }
   * response: { accessToken, user: { id, name, roles, teamIds } }
   */
  async login(username, password) {
    const data = USE_MOCK ? await mockApi.login(username, password) : await http.post('/auth/login', { username, password });
    setToken(USE_MOCK ? data.token : data.accessToken);
    return data.user;
  },

  async logout() {
    try {
      if (!USE_MOCK) await http.post('/auth/logout');
    } finally {
      setToken(null);
    }
  },

  /**
   * GET /auth/me — trả về user hiện tại dựa theo token, dùng khi F5 lại trang.
   */
  async me() {
    return USE_MOCK ? mockApi.me() : http.get('/auth/me');
  },
};

export const dashboardService = {
  /**
   * GET /dashboard
   * response: {
   *   userName, stats: { myBorrowCount, overdueCount, pendingApproval, awaitingMyReceive, heldTotal, pendingConfirm },
   *   todos: [{ id, kind, label, text, action, ticketId }],
   *   unreadNotifications, assets: [...]
   * }
   */
  async get() {
    if (USE_MOCK) return fetchDashboard();
    const [dashboard, assetPage] = await Promise.all([
      fetchDashboard(),
      http.get('/assets', toAssetBrowseQuery({ pageSize: 100 })),
    ]);
    return mapMemberDashboard(dashboard, mapAssetPage(assetPage).items);
  },

  /** GET /dashboard — chỉ số liệu + việc cần làm, không kèm danh sách tài sản (dùng cho badge menu). */
  async getStats() {
    if (USE_MOCK) return fetchDashboard();
    return mapMemberDashboard(await fetchDashboard());
  },
};

// Trang Dashboard và badge menu thường cùng gọi /dashboard lúc mở trang: dùng chung request
// đang chạy. Xong là bỏ ngay — không cache, lần gọi sau luôn lấy dữ liệu mới.
let dashboardInflight = null;
function fetchDashboard() {
  if (!dashboardInflight) {
    dashboardInflight = (USE_MOCK ? mockApi.getDashboard() : http.get('/dashboard'))
      .finally(() => { dashboardInflight = null; });
  }
  return dashboardInflight;
}

export const assetService = {
  /**
   * GET /assets?search=&category=&team=&page=&page_size=
   * response: { items, page, pageSize, total }
   */
  async list(params) {
    if (USE_MOCK) {
      const data = await mockApi.listAssets({
        search: params?.search,
        type: params?.categoryId,
        department: params?.teamId,
      });
      return {
        items: data.map((asset) => ({
          id: asset.id,
          categoryId: asset.type,
          responsibleTeamId: asset.department,
          name: asset.name,
          description: null,
          source: 'MOCK',
          totalQuantity: asset.total,
          availableQuantity: asset.available,
          eligibleCustodies: (asset.custodians || []).map((custody) => ({
            sourceCustodyId: custody.id,
            lendableQuantity: custody.holding,
          })),
        })),
        page: 1,
        pageSize: data.length || 20,
        total: data.length,
      };
    }
    const data = await http.get('/assets', toAssetBrowseQuery(params));
    return mapAssetPage(data);
  },

  /**
   * GET /assets/held — tài sản mà user hiện tại đang giữ
   * response: { assets: [{ id, name, holding, lent, status }] }
   * status: CONFIRMED | PENDING_CONFIRM | CONFIRM_OVERDUE
   */
  async listHeld() {
    if (USE_MOCK) return (await mockApi.listHeldAssets()).assets;
    return mapHeldCustodyPage(await http.get('/assets/held'));
  },

  /**
   * POST /assets/held/:id/confirm  (multipart trên backend thật: ảnh + note)
   * body: { photos: File[]/base64[], note }
   */
  async confirmHeld(id, payload) {
    if (USE_MOCK) return mockApi.confirmHeldAsset(id, payload);
    return http.postIdempotent(
      `/custodies/${id}/confirm`,
      toCustodyConfirmationFormData(payload),
      createCustodyIdempotencyKey(),
    );
  },
};

export const ticketService = {
  /**
   * POST /tickets/borrow
   * body: { items: [{ assetId, allocations: [{ sourceCustodyId, quantity, method, dueDate }] }] }
   * response: { tickets: [{ id, asset, holder, status, dueDate, ... }] }
   */
  async createBorrowTickets(items) {
    if (USE_MOCK) {
      const data = await mockApi.createBorrowTickets(items);
      return {
        createdTickets: data.tickets.map((ticket) => ({ id: ticket.id, status: ticket.status })),
        rejectedGroups: [],
      };
    }
    const data = await http.postIdempotent(
      '/tickets/borrow', { items }, createIdempotencyKey(),
    );
    return mapCreateTicketsResult(data);
  },

  /**
   * GET /tickets?role=borrower|holder&status=&search=
   * response: { tickets: [{ id, asset, qty, borrower, holder, status, dueDate, ... }] }
   */
  async list(params) {
    if (USE_MOCK) return (await mockApi.listTickets(params)).tickets.map(mapLegacyTicket);
    return mapTicketPage(await http.get('/tickets', toTicketQuery(params))).items;
  },

  /** GET /tickets/:id */
  async get(id) {
    return USE_MOCK
      ? mapLegacyTicket(await mockApi.getTicket(id))
      : mapTicketDetail(await http.get(`/tickets/${id}`));
  },

  /** POST /tickets/:id/approve */
  async approve(id) {
    return USE_MOCK
      ? mockApi.approveTicket(id)
      : http.postIdempotent(`/tickets/${id}/approve`, undefined, createIdempotencyKey());
  },

  /** POST /tickets/:id/reject  body: { reason } */
  async reject(id, reason) {
    return USE_MOCK
      ? mockApi.rejectTicket(id, reason)
      : http.postIdempotent(`/tickets/${id}/reject`, { reason }, createIdempotencyKey());
  },

  /** POST /tickets/:id/confirm-received  body: { photos, note } — bên mượn xác nhận đã nhận */
  async confirmReceived(id, payload) {
    return USE_MOCK
      ? mockApi.confirmReceived(id, payload)
      : http.postMultipart(
        `/tickets/${id}/confirm-received`,
        toEvidenceFormData(payload),
        createIdempotencyKey(),
      );
  },

  /** POST /tickets/:id/mark-returned  body: { photos, note } — bên mượn báo đã trả */
  async markReturned(id, payload) {
    return USE_MOCK
      ? mockApi.markReturned(id, payload)
      : http.postMultipart(
        `/tickets/${id}/mark-returned`,
        toEvidenceFormData(payload),
        createIdempotencyKey(),
      );
  },

  /** POST /tickets/:id/confirm-returned  body: { photos, note } — bên giữ xác nhận đã nhận lại */
  async confirmReturnedByHolder(id, payload) {
    return USE_MOCK
      ? mockApi.confirmReturnedByHolder(id, payload)
      : http.postMultipart(
        `/tickets/${id}/confirm-returned`,
        toEvidenceFormData(payload),
        createIdempotencyKey(),
      );
  },

  /** POST /tickets/:id/cancel-request  body: { reason } */
  async requestCancel(id, reason) {
    return USE_MOCK
      ? mockApi.requestCancel(id, reason)
      : http.postIdempotent(
        `/tickets/${id}/cancel-request`, { reason }, createIdempotencyKey(),
      );
  },

  /** POST /tickets/:id/cancel-resolve  body: { approve: boolean } */
  async resolveCancel(id, approve) {
    return USE_MOCK
      ? mockApi.resolveCancel(id, approve)
      : http.postIdempotent(
        `/tickets/${id}/cancel-resolve`, { approve }, createIdempotencyKey(),
      );
  },
};

export const hrService = {
  /** GET /hr/dashboard — số liệu tổng quan tài sản CLB + việc cần làm của HR */
  async getDashboard() {
    const data = USE_MOCK ? await mockApiExt.getHrDashboard() : await http.get('/hr/dashboard');
    return USE_MOCK ? data : mapHrDashboard(data);
  },

  /** GET /hr/assets — tài sản kèm ban phụ trách + custodian, dùng cho màn quản trị */
  async listAssetsAdmin() {
    const data = USE_MOCK ? await mockApiExt.listAssetsAdmin() : await http.get('/hr/assets');
    return data.assets;
  },

  /** PATCH /hr/assets/:id/responsible-team  body: { responsibleTeamId: UUID|null } */
  async updateAssetResponsibleTeam(assetId, responsibleTeamId) {
    return USE_MOCK
      ? mockApiExt.updateAssetResponsibleTeam(assetId, responsibleTeamId)
      : http.patch(`/hr/assets/${assetId}/responsible-team`, { responsibleTeamId });
  },

  /** POST /hr/assets  body: { name, qty, department, source, allocations: [{custodianId, name, qty}] } */
  async createAsset(payload) {
    return USE_MOCK ? mockApiExt.createAsset(payload) : http.post('/hr/assets', payload);
  },

  /** GET /hr/asset-transactions?assetId=&from=&to= */
  async getTransactionHistory(params) {
    if (USE_MOCK) return (await mockApiExt.getTransactionHistory(params)).transactions;
    const data = await http.get('/hr/asset-transactions', toTransactionQuery(params));
    return mapTransactionPage(data);
  },

};

export const memberService = {
  /**
   * GET /hr/members?page=&page_size=
   * response: { items: [{ id, username, email, name, roles, isActive }], page, pageSize, total }
   */
  async list() {
    return USE_MOCK ? mockApiExt.listMembers() : http.get('/hr/members', { page_size: 100 });
  },

  /**
   * PUT /hr/members/:id/roles  body: { roles: [...] } — thay toàn bộ vai.
   * 403 ROLE_ASSIGNMENT_FORBIDDEN: tự sửa vai mình, hoặc HR đụng tới vai HR/SYSTEM_ADMIN.
   */
  async replaceRoles(memberId, roles) {
    return USE_MOCK
      ? mockApiExt.replaceMemberRoles(memberId, roles)
      : http.put(`/hr/members/${memberId}/roles`, { roles });
  },
};

export const reconciliationService = {
  /** POST /reconciliations — open one immutable Custody snapshot. */
  async open(reconciliationDate) {
    if (USE_MOCK) {
      const data = await mockApiExt.openNewReconciliationBatch();
      return legacyBatchToReconciliation({ batch: data.batch });
    }
    const data = await http.postIdempotent(
      '/reconciliations',
      { reconciliationDate },
      createReconciliationIdempotencyKey(),
    );
    return mapReconciliation(data);
  },

  /** GET /reconciliations/:id — complete HR snapshot. */
  async get(id) {
    if (USE_MOCK) return legacyBatchToReconciliation(await mockApiExt.getReconciliationBatch());
    return mapReconciliation(await http.get(`/reconciliations/${id}`));
  },

  /** GET /reconciliations/me — current actor's snapshot work queue. */
  async listMine({ page = 1, pageSize = 20 } = {}) {
    if (USE_MOCK) {
      const session = legacyBatchToReconciliation(await mockApiExt.getReconciliationBatch());
      return { items: session.items, page, pageSize, total: session.items.length };
    }
    return mapReconciliationItemPage(
      await http.get('/reconciliations/me', { page, page_size: pageSize }),
    );
  },

  async requestConfirmation(id) {
    if (USE_MOCK) return mockApiExt.sendConfirmRequest(id);
    return http.postIdempotent(
      `/reconciliations/${id}/request-confirmation`,
      undefined,
      createReconciliationIdempotencyKey(),
    );
  },

  async confirmItem(id, input) {
    if (USE_MOCK) {
      await mockApiExt.confirmReconciliationRow(id, input);
      const session = legacyBatchToReconciliation(await mockApiExt.getReconciliationBatch());
      return session.items.find((candidate) => candidate.id === id);
    }
    const form = toReconciliationEvidenceFormData(input);
    return mapReconciliationItem(await http.postMultipart(
      `/reconciliation-items/${id}/confirm`,
      form,
      createReconciliationIdempotencyKey(),
    ));
  },

  async resolveItem(id, resolutionNote) {
    if (USE_MOCK) {
      await mockApiExt.resolveReconciliationRow(id, resolutionNote);
      const session = legacyBatchToReconciliation(await mockApiExt.getReconciliationBatch());
      return session.items.find((candidate) => candidate.id === id);
    }
    return mapReconciliationItem(await http.postIdempotent(
      `/reconciliation-items/${id}/resolve`,
      { resolutionNote },
      createReconciliationIdempotencyKey(),
    ));
  },

  async remindItem(id) {
    if (USE_MOCK) return mockApiExt.remindCustodian(id);
    return http.postIdempotent(
      `/reconciliation-items/${id}/remind`,
      undefined,
      createReconciliationIdempotencyKey(),
    );
  },

  async close(id) {
    if (USE_MOCK) {
      await mockApiExt.closeReconciliationBatch(id);
      return legacyBatchToReconciliation(await mockApiExt.getReconciliationBatch());
    }
    return mapReconciliation(await http.postIdempotent(
      `/reconciliations/${id}/close`,
      undefined,
      createReconciliationIdempotencyKey(),
    ));
  },
};

export const transferService = {
  /** POST /transfers — HR reserves source quantity for two-party confirmation. */
  async create(input) {
    const request = toCreateTransferRequest(input);
    const data = USE_MOCK
      ? await mockApiExt.createTransfer(request)
      : await http.postIdempotent('/transfers', request, createTransferIdempotencyKey());
    return mapTransfer(data);
  },

  /** POST /reconciliation-items/:id/transfer — link without resolving discrepancy. */
  async createFromReconciliation(itemId, input) {
    const request = toCreateTransferRequest(input);
    const data = USE_MOCK
      ? await mockApiExt.createTransfer(request, itemId)
      : await http.postIdempotent(
        `/reconciliation-items/${itemId}/transfer`,
        request,
        createTransferIdempotencyKey(),
      );
    return mapTransfer(data);
  },

  /** GET /transfers — backend scopes results to HR or either participant. */
  async list({ status, page = 1, pageSize = 100 } = {}) {
    const data = USE_MOCK
      ? await mockApiExt.listTransfers({ status, page, pageSize })
      : await http.get('/transfers', { status, page, page_size: pageSize });
    return mapTransferPage(data);
  },

  /** Confirm exactly one participant side with private evidence. */
  async confirm(id, side, input) {
    if (side !== 'current' && side !== 'target') throw new Error('unsupported transfer side');
    const data = USE_MOCK
      ? await mockApiExt.confirmTransfer(id, side)
      : await http.postMultipart(
        `/transfers/${id}/confirm-${side}`,
        toTransferEvidenceFormData(input),
        createTransferIdempotencyKey(),
      );
    return mapTransfer(data);
  },
};

export const handoverService = {
  async create(termId) {
    const data = USE_MOCK
      ? await mockApiExt.createHandover(termId)
      : await http.postIdempotent(
        '/handovers', { termId }, createHandoverIdempotencyKey(),
      );
    return mapHandover(data);
  },

  async get(id) {
    const data = USE_MOCK
      ? await mockApiExt.getHandover(id)
      : await http.get(`/handovers/${id}`);
    return mapHandover(data);
  },

  async confirm(id, side) {
    if (side !== 'old' && side !== 'new') throw new Error('unsupported Handover side');
    const data = USE_MOCK
      ? await mockApiExt.confirmHandover(id, side)
      : await http.postIdempotent(
        `/handovers/${id}/confirm-${side}`,
        undefined,
        createHandoverIdempotencyKey(),
      );
    return mapHandover(data);
  },

  async exportCsv(id) {
    if (!USE_MOCK) return download(`/handovers/${id}/export`);
    const value = await mockApiExt.getHandover(id);
    const rows = [
      ['asset_id', 'asset_name', 'total_quantity', 'condition', 'is_stale'],
      ...value.items.map((item) => [
        item.assetId, item.assetName, item.totalQuantity, item.condition, item.isStale,
      ]),
    ];
    return {
      blob: new Blob([rows.map((row) => row.join(',')).join('\n')], { type: 'text/csv' }),
      filename: `handover-${id}.csv`,
    };
  },
};

export const shiftService = {
  /** GET /shifts — canonical assigned Shift page. */
  async list({ page = 1, pageSize = 20 } = {}) {
    const data = USE_MOCK
      ? await mockApiExt.listShifts({ page, pageSize })
      : await http.get('/shifts', { page, page_size: pageSize });
    return mapShiftPage(data);
  },

  /** GET /shifts/:id — complete execution snapshot. */
  async get(id) {
    const data = USE_MOCK
      ? await mockApiExt.getShiftDetail(id)
      : await http.get(`/shifts/${id}`);
    return mapShift(data);
  },

  /** POST /shifts/:id/open — SCHEDULED to OPENING. */
  async open(id) {
    const data = USE_MOCK
      ? await mockApiExt.openShift(id)
      : await http.postIdempotent(`/shifts/${id}/open`, undefined, createShiftIdempotencyKey());
    return mapShift(data);
  },

  /** POST /shifts/:id/confirm-received — exact opening quantities. */
  async confirmReceived(id, items) {
    const request = toShiftQuantities(items);
    const data = USE_MOCK
      ? await mockApiExt.submitShiftReceived(id, request)
      : await http.postIdempotent(
        `/shifts/${id}/confirm-received`,
        request,
        createShiftIdempotencyKey(),
      );
    return mapShift(data);
  },

  /** POST /shifts/:id/report-opening-deviation — mismatch plus explanation. */
  async reportDeviation(id, items, reason) {
    const request = { ...toShiftQuantities(items), reason };
    const data = USE_MOCK
      ? await mockApiExt.reportShiftDeviation(id, request)
      : await http.postIdempotent(
        `/shifts/${id}/report-opening-deviation`,
        request,
        createShiftIdempotencyKey(),
      );
    return mapShift(data);
  },

  /** POST /shifts/:id/close — actual closing quantities and formula outputs. */
  async close(id, items) {
    const request = toShiftQuantities(items);
    const data = USE_MOCK
      ? await mockApiExt.submitShiftClose(id, request)
      : await http.postIdempotent(
        `/shifts/${id}/close`,
        request,
        createShiftIdempotencyKey(),
      );
    return mapShift(data);
  },
};

export const eventDayService = {
  /** POST /event-days — create a complete future Event Day configuration. */
  async create(draft) {
    const request = toEventDayRequest(draft);
    const data = USE_MOCK
      ? await mockApiExt.createEventDay(request)
      : await http.postIdempotent('/event-days', request, createEventDayIdempotencyKey());
    return mapEventDay(data);
  },

  /** GET /event-days/:id — read one complete Event Day configuration. */
  async get(id) {
    const data = USE_MOCK
      ? await mockApiExt.getEventDay(id)
      : await http.get(`/event-days/${id}`);
    return mapEventDay(data);
  },

  /** PATCH /event-days/:id — atomically replace an unstarted configuration. */
  async update(id, draft) {
    const request = toEventDayRequest(draft);
    const data = USE_MOCK
      ? await mockApiExt.updateEventDay(id, request)
      : await http.patchIdempotent(
        `/event-days/${id}`,
        request,
        createEventDayIdempotencyKey(),
      );
    return mapEventDay(data);
  },

  /** GET /event-days/:id/summary — progress or final per-Asset totals. */
  async getSummary(id) {
    const data = USE_MOCK
      ? await mockApiExt.getEventDaySummary(id)
      : await http.get(`/event-days/${id}/summary`);
    return mapEventDaySummary(data);
  },
};

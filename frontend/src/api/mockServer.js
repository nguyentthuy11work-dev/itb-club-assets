// ============================================================================
// MOCK SERVER
// ----------------------------------------------------------------------------
// Chỉ dùng khi chưa nối backend thật (xem client.js -> USE_MOCK).
// Dữ liệu và độ trễ giả lập ở đây bám sát đúng các màn hình wireframe gốc,
// giúp bạn xem toàn bộ luồng chạy được trước khi backend sẵn sàng.
// Toàn bộ hàm mockApi.* có cùng chữ ký (signature) với services.js thật,
// nên khi backend xong, chỉ cần đổi VITE_USE_MOCK=false, không cần sửa code.
// ============================================================================

const delay = (ms = 350) => new Promise((res) => setTimeout(res, ms));

let currentUser = null;

const users = {
  'baotran': { username: 'baotran', password: '123456', id: 'u1', name: 'Bảo Trân', department: 'Ban Hậu cần' },
};

let assets = [
  { id: 'a1', name: 'Loa Bluetooth JBL', type: 'Thiết bị', total: 6, available: 4, department: 'Ban Sự kiện' },
  { id: 'a2', name: 'Bàn gấp 1m2', type: 'Nội thất', total: 12, available: 8, department: 'Ban Hậu cần' },
  { id: 'a3', name: 'Micro không dây', type: 'Thiết bị', total: 4, available: 0, department: 'Ban Truyền thông' },
  { id: 'a4', name: 'Dây nối 10m', type: 'Phụ kiện', total: 8, available: 5, department: 'Ban Hậu cần' },
  { id: 'a5', name: 'Standee A1', type: 'In ấn', total: 2, available: 2, department: 'Ban Truyền thông' },
];

// Người có thể giữ tài sản (để chọn khi mượn)
const custodiansByAsset = {
  a1: [
    { id: 'p1', name: 'Minh Anh', holding: 3 },
    { id: 'p2', name: 'Thu Hà', holding: 1 },
  ],
  a5: [{ id: 'p3', name: 'Quốc Huy', holding: 2 }],
  a4: [],
  a2: [{ id: 'p4', name: 'Gia Hân', holding: 4 }],
  a3: [],
};

let tickets = [
  {
    id: 'TK-0129',
    asset: 'Standee A1',
    qty: 1,
    borrower: 'Bảo Trân',
    borrowerId: 'u1',
    holder: 'Quốc Huy',
    holderId: 'p3',
    status: 'ACTIVE', // Đang mượn
    dueDate: '2026-08-22',
    overdue: true,
    method: 'Mượn tạm',
    log: ['24/08 09:12 — Bạn tạo yêu cầu', '24/08 14:40 — Quốc Huy duyệt'],
  },
  {
    id: 'TK-0141',
    asset: 'Micro không dây',
    qty: 1,
    borrower: 'Bảo Trân',
    borrowerId: 'u1',
    holder: 'Quốc Huy',
    holderId: 'p3',
    status: 'APPROVED', // Đã duyệt, chờ xác nhận nhận
    dueDate: '2026-08-30',
    method: 'Mượn tạm',
    log: ['24/08 09:12 — Bạn tạo yêu cầu', '24/08 14:40 — Quốc Huy duyệt'],
  },
  {
    id: 'TK-0142',
    asset: 'Loa x2, Dây nối x3',
    qty: 5,
    borrower: 'Minh Anh',
    borrowerId: 'p1',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'ACTIVE',
    dueDate: '2026-08-28',
    dueSoon: true,
    method: 'Mượn tạm',
    log: ['20/08 — Minh Anh tạo yêu cầu', '20/08 — Bạn duyệt'],
  },
  {
    id: 'TK-0138',
    asset: 'Dây nối x3',
    qty: 3,
    borrower: 'Minh Anh',
    borrowerId: 'p1',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'PENDING',
    dueDate: null,
    method: 'Mượn tạm',
    log: ['21/08 — Minh Anh tạo yêu cầu'],
  },
  {
    id: 'TK-0146',
    asset: 'Loa JBL x1',
    qty: 1,
    borrower: 'Gia Hân',
    borrowerId: 'p4',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'PENDING',
    dueDate: '2026-08-29',
    method: 'Mượn tạm',
    log: ['24/08 09:30 — Gia Hân tạo yêu cầu'],
  },
  {
    id: 'TK-0133',
    asset: 'Bàn gấp x4',
    qty: 4,
    borrower: 'Gia Hân',
    borrowerId: 'p4',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'AWAITING_HOLDER_CONFIRM',
    dueDate: '2026-08-26',
    returnPhotos: 2,
    method: 'Mượn tạm',
    log: ['25/08 16:20 — Gia Hân bấm Đã trả, gửi 2 ảnh'],
  },
  {
    id: 'TK-0147',
    asset: 'Dây nối x2',
    qty: 2,
    borrower: 'Tuấn Kiệt',
    borrowerId: 'p5',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'CANCEL_REQUESTED',
    dueDate: '2026-08-27',
    cancelReason: 'sự kiện đổi ngày',
    method: 'Mượn tạm',
    log: ['22/08 — Tuấn Kiệt xin huỷ: "sự kiện đổi ngày"'],
  },
  {
    id: 'TK-0140',
    asset: 'Loa JBL x2',
    qty: 2,
    borrower: 'Tuấn Kiệt',
    borrowerId: 'p5',
    holder: 'Bảo Trân',
    holderId: 'u1',
    status: 'ACTIVE',
    dueDate: '2026-09-01',
    method: 'Mượn tạm',
    log: ['18/08 — Tuấn Kiệt tạo yêu cầu', '18/08 — Bạn duyệt'],
  },
];

let heldAssets = [
  { id: 'ha1', name: 'Loa Bluetooth JBL', holding: 3, lent: 2, status: 'CONFIRMED' },
  { id: 'ha2', name: 'Standee A1 (HR mới gán)', holding: 2, lent: 0, status: 'PENDING_CONFIRM' },
  { id: 'ha3', name: 'Dây nối 10m', holding: 5, lent: 3, status: 'CONFIRM_OVERDUE' },
];

let notifications = [
  {
    id: 'n1',
    type: 'overdue',
    title: 'Ticket quá hạn trả',
    detail: 'TK-0129 · bạn mượn Standee A1 của Quốc Huy',
    time: '08:00 hôm nay',
    unread: true,
    ticketId: 'TK-0129',
  },
  {
    id: 'n2',
    type: 'approve_needed',
    title: 'Yêu cầu mượn mới cần bạn duyệt',
    detail: 'TK-0146 · Gia Hân xin Loa JBL x1 bạn đang giữ',
    time: '09:30 hôm nay',
    unread: true,
    ticketId: 'TK-0146',
  },
  {
    id: 'n3',
    type: 'awaiting_receive',
    title: 'Người mượn đã trả, chờ bạn nhận lại',
    detail: 'TK-0133 · Bàn gấp x4 · Gia Hân',
    time: 'hôm qua 16:20',
    unread: true,
    ticketId: 'TK-0133',
  },
  {
    id: 'n4',
    type: 'assigned',
    title: 'Tài sản mới gán cho bạn giữ',
    detail: 'Dây nối 10m x5 · quá 24h chưa xác nhận',
    time: '23/08',
    unread: true,
    ticketId: null,
  },
  {
    id: 'n5',
    type: 'approved',
    title: 'Yêu cầu mượn được duyệt',
    detail: 'TK-0141 · Micro x1 · Quốc Huy duyệt',
    time: '24/08',
    unread: false,
    ticketId: 'TK-0141',
  },
  {
    id: 'n6',
    type: 'cancel_rejected',
    title: 'Yêu cầu huỷ bị từ chối',
    detail: 'TK-0138 · lý do: đã chuẩn bị tài sản',
    time: '23/08',
    unread: false,
    ticketId: 'TK-0138',
  },
];

let ticketSeq = 148;

function findTicket(id) {
  const t = tickets.find((x) => x.id === id);
  if (!t) throw { status: 404, message: 'Không tìm thấy Ticket' };
  return t;
}

export const mockApi = {
  async login(username, password) {
    await delay();
    const u = users[username];
    if (!u || u.password !== password) {
      const err = new Error('Sai tài khoản hoặc mật khẩu');
      err.status = 401;
      throw err;
    }
    currentUser = u;
    return { token: 'mock-token-' + u.id, user: { id: u.id, name: u.name, username: u.username, role: 'MEMBER', department: u.department } };
  },

  async me() {
    await delay(150);
    if (!currentUser) currentUser = users['baotran'];
    return { id: currentUser.id, name: currentUser.name, username: currentUser.username, role: 'MEMBER', department: currentUser.department };
  },

  async getDashboard() {
    await delay();
    const myBorrow = tickets.filter((t) => t.borrowerId === 'u1' && ['ACTIVE', 'APPROVED', 'AWAITING_HOLDER_CONFIRM'].includes(t.status));
    const overdueCount = myBorrow.filter((t) => t.overdue).length;
    const pendingApproval = tickets.filter((t) => t.holderId === 'u1' && t.status === 'PENDING').length;
    const awaitingMyReceive = tickets.filter((t) => t.holderId === 'u1' && t.status === 'AWAITING_HOLDER_CONFIRM').length;
    const heldTotal = heldAssets.reduce((s, a) => s + a.holding, 0);
    const pendingConfirm = heldAssets.filter((a) => a.status !== 'CONFIRMED').length;

    const todos = [];
    tickets.forEach((t) => {
      if (t.overdue && t.borrowerId === 'u1') {
        todos.push({ id: t.id, kind: 'overdue', label: 'Quá hạn', text: `${t.id} · ${t.asset} x1 · bạn mượn của ${t.holder}, hạn ${fmtShort(t.dueDate)}`, action: 'Đã trả', ticketId: t.id });
      }
      if (t.status === 'PENDING' && t.holderId === 'u1') {
        todos.push({ id: t.id, kind: 'pending', label: 'Chờ duyệt', text: `${t.id} · ${t.asset} · ${t.borrower} xin mượn của bạn`, action: 'approve_reject', ticketId: t.id });
      }
      if (t.status === 'AWAITING_HOLDER_CONFIRM' && t.holderId === 'u1') {
        todos.push({ id: t.id, kind: 'await_receive', label: 'Chờ nhận lại', text: `${t.id} · ${t.asset} · ${t.borrower} đã bấm "Đã trả", kèm ${t.returnPhotos || 0} ảnh`, action: 'Nhận lại', ticketId: t.id });
      }
    });
    heldAssets.forEach((a) => {
      if (a.status !== 'CONFIRMED') {
        todos.push({ id: a.id, kind: 'confirm', label: 'Quá 24h', text: `${a.name} · HR gán cho bạn giữ, chưa xác nhận`, action: 'Xác nhận', ticketId: null });
      }
    });

    return {
      userName: currentUser?.name || 'Bảo Trân',
      stats: {
        myBorrowCount: myBorrow.length,
        overdueCount,
        pendingApproval,
        awaitingMyReceive,
        heldTotal,
        pendingConfirm,
      },
      todos,
      unreadNotifications: notifications.filter((n) => n.unread).length,
      assets,
    };
  },

  async listAssets({ search, type, department } = {}) {
    await delay(250);
    return assets
      .filter((a) => !search || a.name.toLowerCase().includes(search.toLowerCase()))
      .filter((a) => !type || type === 'Mọi loại' || a.type === type)
      .filter((a) => !department || department === 'Tất cả' || a.department === department)
      .map((a) => ({ ...a, custodians: custodiansByAsset[a.id] || [] }));
  },

  async createBorrowTickets(items) {
    // items: [{assetId, allocations: [{sourceCustodyId, quantity, method, dueDate}]}]
    await delay(500);
    const created = [];
    items.forEach((item) => {
      item.allocations.forEach((alloc) => {
        const id = `TK-0${ticketSeq++}`;
        const asset = assets.find((candidate) => candidate.id === item.assetId);
        const t = {
          id,
          asset: `${asset?.name || item.assetId} x${alloc.quantity}`,
          qty: alloc.quantity,
          borrower: currentUser?.name || 'Bảo Trân',
          borrowerId: 'u1',
          holder: `Custody ${alloc.sourceCustodyId}`,
          holderId: alloc.sourceCustodyId,
          status: 'PENDING',
          dueDate: alloc.dueDate,
          method: alloc.method,
          log: [`${fmtNow()} — Bạn tạo yêu cầu`],
        };
        tickets.unshift(t);
        created.push(t);
      });
    });
    return { tickets: created };
  },

  async listTickets({ role, status, search } = {}) {
    await delay(300);
    let list = tickets;
    if (role === 'borrower') list = list.filter((t) => t.borrowerId === 'u1');
    if (role === 'holder') list = list.filter((t) => t.holderId === 'u1');
    if (status) list = list.filter((t) => t.status === status);
    if (search) list = list.filter((t) => t.asset.toLowerCase().includes(search.toLowerCase()));
    return { tickets: list };
  },

  async getTicket(id) {
    await delay(250);
    const t = findTicket(id);
    return { ...t };
  },

  async approveTicket(id) {
    await delay(300);
    const t = findTicket(id);
    t.status = 'APPROVED';
    t.log.push(`${fmtNow()} — Bạn duyệt`);
    return { ...t };
  },

  async rejectTicket(id, reason) {
    await delay(300);
    const t = findTicket(id);
    t.status = 'REJECTED';
    t.log.push(`${fmtNow()} — Bạn từ chối: "${reason}"`);
    return { ...t };
  },

  async confirmReceived(id, { photos, note }) {
    await delay(400);
    const t = findTicket(id);
    t.status = 'ACTIVE';
    t.log.push(`${fmtNow()} — Bạn xác nhận đã nhận (${photos.length} ảnh)`);
    return { ...t };
  },

  async markReturned(id, { photos, note }) {
    await delay(400);
    const t = findTicket(id);
    t.status = 'AWAITING_HOLDER_CONFIRM';
    t.returnPhotos = photos.length;
    t.log.push(`${fmtNow()} — Bạn bấm Đã trả (${photos.length} ảnh)`);
    return { ...t };
  },

  async confirmReturnedByHolder(id, { photos, note }) {
    await delay(400);
    const t = findTicket(id);
    t.status = 'CLOSED';
    t.log.push(`${fmtNow()} — ${t.holder} xác nhận đã nhận lại (${photos.length} ảnh)`);
    return { ...t };
  },

  async requestCancel(id, reason) {
    await delay(300);
    const t = findTicket(id);
    t.status = 'CANCEL_REQUESTED';
    t.cancelReason = reason;
    t.log.push(`${fmtNow()} — Bạn gửi yêu cầu huỷ: "${reason || '(không có lý do)'}"`);
    return { ...t };
  },

  async resolveCancel(id, approve) {
    await delay(300);
    const t = findTicket(id);
    t.status = approve ? 'CANCELLED' : t.prevStatus || 'ACTIVE';
    t.log.push(`${fmtNow()} — ${approve ? 'Bạn đồng ý huỷ' : 'Bạn từ chối huỷ'}`);
    return { ...t };
  },

  async listHeldAssets() {
    await delay(300);
    return { assets: heldAssets };
  },

  async confirmHeldAsset(id, { photos, note }) {
    await delay(350);
    const a = heldAssets.find((x) => x.id === id);
    if (a) a.status = 'CONFIRMED';
    return { asset: a };
  },

  async listNotifications({ filter } = {}) {
    await delay(250);
    let list = notifications;
    if (filter === 'unread') list = list.filter((n) => n.unread);
    return { notifications: list, unreadCount: notifications.filter((n) => n.unread).length };
  },

  async markNotificationRead(id) {
    await delay(150);
    const n = notifications.find((x) => x.id === id);
    if (n) n.unread = false;
    return { ok: true };
  },
};

// ============================================================================
// DỮ LIỆU & API GIẢ LẬP CHO VAI HR + TRƯỞNG CA
// ============================================================================

// Danh sách tài sản quản trị (HR) — dùng chung asset id với danh sách assets phía trên
let assetCustodians = {
  a1: [{ id: 'p1', name: 'Minh Anh', qty: 4 }, { id: 'p2', name: 'Thu Hà', qty: 2 }],
  a2: [{ id: 'p2', name: 'Thu Hà', qty: 12 }],
  a3: [{ id: 'p3', name: 'Quốc Huy', qty: 4 }],
  a4: [{ id: 'p2', name: 'Thu Hà', qty: 8 }],
  a5: [{ id: 'p3', name: 'Quốc Huy', qty: 2 }],
};

let transactionLog = [
  { id: 't1', assetId: 'a1', time: '2026-08-25 16:20', kind: 'Trả', qty: 2, from: 'Bảo Trân', to: 'Minh Anh', ticketId: 'TK-0142' },
  { id: 't2', assetId: 'a1', time: '2026-08-24 14:40', kind: 'Mượn', qty: 2, from: 'Minh Anh', to: 'Bảo Trân', ticketId: 'TK-0142' },
  { id: 't3', assetId: 'a1', time: '2026-08-18 10:05', kind: 'Chuyển quyền giữ', qty: 1, from: 'Thu Hà', to: 'Minh Anh', ticketId: 'TRF-0012' },
  { id: 't4', assetId: 'a1', time: '2026-08-05 09:00', kind: 'Ghi nhận mới', qty: 6, from: 'HR', to: 'Minh Anh, Thu Hà', ticketId: null },
];

let reconciliationBatch = {
  id: 'REC-2026-08',
  status: 'OPEN',
  openedDate: '2026-08-20',
  rows: [
    { id: 'r1', custodian: 'Minh Anh', asset: 'Loa Bluetooth JBL', expected: 4, declared: 4, result: 'MATCH' },
    { id: 'r2', custodian: 'Thu Hà', asset: 'Bàn gấp 1m2', expected: 8, declared: 6, result: 'MISMATCH', diff: -2, reason: '2 bàn hỏng chân, để ở kho' },
    { id: 'r3', custodian: 'Quốc Huy', asset: 'Micro không dây', expected: 4, declared: 4, result: 'MATCH' },
    { id: 'r4', custodian: 'Tuấn Kiệt', asset: 'Standee A1', expected: 3, declared: null, result: 'UNCONFIRMED' },
  ],
};

let transferSequence = 2;
let custodyTransfers = [
  {
    id: 'TRF-0001', sourceCustodyId: 'custody-demo-1', assetId: 'a1',
    currentCustodianUserId: 'u1', targetCustodianUserId: 'p2', quantity: 1,
    conditionSnapshot: 'GOOD', createdByUserId: 'u1', createdAt: '2026-08-25T09:00:00Z',
    reconciliationItemId: null, status: 'PENDING_BOTH_CONFIRMATIONS',
    currentConfirmedAt: null, targetConfirmedAt: null, targetCustodyId: null, completedAt: null,
  },
];

let handoverRecord = null;

let shifts = [
  { id: 's1', eventDayId: 'event-1', eventDayName: 'Ngày đặt bàn 24/08', sequence: 1, name: 'Ca sáng', startsAt: '2030-08-24T01:00:00Z', endsAt: '2030-08-24T05:00:00Z', shiftLeadUserId: 'p3', status: 'SCHEDULED' },
  { id: 's2', eventDayId: 'event-1', eventDayName: 'Ngày đặt bàn 24/08', sequence: 2, name: 'Ca chiều', startsAt: '2030-08-24T05:00:00Z', endsAt: '2030-08-24T10:00:00Z', shiftLeadUserId: 'p3', status: 'SCHEDULED' },
  { id: 's3', eventDayId: 'event-2', eventDayName: 'Ngày đặt bàn 10/08', sequence: 1, name: 'Ca đã đóng', startsAt: '2030-08-10T05:00:00Z', endsAt: '2030-08-10T10:00:00Z', shiftLeadUserId: 'p3', status: 'CLOSED' },
];

let shiftItems = {
  s1: [
    { id: 'i1', assetId: 'a1', quotaQuantity: 300, carryOverQuantity: 0, openingActualQuantity: null, closingActualQuantity: null, consumedQuantity: null, discrepancyReason: null },
    { id: 'i2', assetId: 'a2', quotaQuantity: 12, carryOverQuantity: 0, openingActualQuantity: null, closingActualQuantity: null, consumedQuantity: null, discrepancyReason: null },
    { id: 'i3', assetId: 'a4', quotaQuantity: 80, carryOverQuantity: 0, openingActualQuantity: null, closingActualQuantity: null, consumedQuantity: null, discrepancyReason: null },
  ],
  s2: [{ id: 'i4', assetId: 'a1', quotaQuantity: 200, carryOverQuantity: 0, openingActualQuantity: null, closingActualQuantity: null, consumedQuantity: null, discrepancyReason: null }],
  s3: [{ id: 'i5', assetId: 'a5', quotaQuantity: 20, carryOverQuantity: 0, openingActualQuantity: 20, closingActualQuantity: 4, consumedQuantity: 16, discrepancyReason: null }],
};

let eventDaySequence = 1;
const eventDays = new Map();

function mockEventDayResponse(id, request, previous) {
  return {
    id,
    name: request.name,
    eventDate: request.eventDate,
    status: previous?.status || 'DRAFT',
    coreStockChanged: false,
    createdByUserId: previous?.createdByUserId || 'u1',
    shifts: request.shifts.map((shift, shiftIndex) => ({
      id: shift.id || `mock-shift-${eventDaySequence}-${shiftIndex + 1}`,
      sequence: shift.sequence,
      name: shift.name,
      startsAt: shift.startsAt,
      endsAt: shift.endsAt,
      shiftLeadUserId: shift.shiftLeadUserId,
      status: previous?.shifts.find((item) => item.id === shift.id)?.status || 'SCHEDULED',
      assets: shift.assets.map((asset) => ({
        assetId: asset.assetId,
        quotaQuantity: asset.quotaQuantity,
        carryOverQuantity: 0,
        expectedOpenQuantity: asset.quotaQuantity,
      })),
    })),
  };
}

function mockShiftResponse(shift) {
  return {
    ...shift,
    coreStockChanged: false,
    items: (shiftItems[shift.id] || []).map((item) => ({
      ...item,
      expectedOpenQuantity: item.quotaQuantity + item.carryOverQuantity,
    })),
  };
}

export const mockApiExt = {
  // ---------------- HR dashboard ----------------
  async getHrDashboard() {
    await delay();
    const totalAssets = assets.reduce((s, a) => s + a.total, 0);
    const onLoan = tickets.filter((t) => t.status === 'ACTIVE').reduce((s, t) => s + (t.qty || 1), 0);
    const onLoanOverdue = tickets.filter((t) => t.status === 'ACTIVE' && t.overdue).length;
    const awaitingCustodian = heldAssets.filter((a) => a.status !== 'CONFIRMED').length;
    const awaitingOver24h = heldAssets.filter((a) => a.status === 'CONFIRM_OVERDUE').length;

    const todos = [
      { id: 'hd1', kind: 'overdue24h', label: 'Quá 24h', text: 'Minh Anh chưa xác nhận Dây nối 10m ×5 — gán từ 23/08', action: 'Nhắc lại' },
      { id: 'hd2', kind: 'reconciliation', label: 'Đối soát', text: `${reconciliationBatch.id} · còn ${reconciliationBatch.rows.filter((r) => r.result !== 'MATCH').length + 1} Custodian chưa đóng xác nhận`, action: 'Xem đợt' },
      { id: 'hd3', kind: 'mismatch', label: 'Lệch', text: '3 dòng khai sai lệch cần xử lý ngoài hệ thống', action: 'Chi tiết' },
      { id: 'hd4', kind: 'transfer', label: 'Transfer', text: '2 yêu cầu chuyển quyền giữ đang chờ bên nhận xác nhận', action: 'Theo dõi' },
    ];

    return {
      stats: {
        totalAssets,
        totalTypes: new Set(assets.map((a) => a.type)).size,
        onLoan,
        onLoanOverdue,
        awaitingCustodian,
        awaitingOver24h,
        openRecons: reconciliationBatch.status === 'OPEN' ? 1 : 0,
        reconDone: reconciliationBatch.rows.filter((r) => r.result !== 'UNCONFIRMED').length,
        reconTotal: reconciliationBatch.rows.length + 8, // demo số tổng như ảnh (7/12)
      },
      todos,
    };
  },

  // ---------------- Tài sản (quản trị) ----------------
  async listAssetsAdmin() {
    await delay(300);
    return {
      assets: assets.map((a) => ({
        ...a,
        responsibleTeamId: a.responsibleTeamId || null,
        custodians: assetCustodians[a.id] || [],
      })),
    };
  },

  async updateAssetResponsibleTeam(assetId, responsibleTeamId) {
    await delay(250);
    const a = assets.find((x) => x.id === assetId);
    if (a) a.responsibleTeamId = responsibleTeamId;
    return { id: assetId, responsibleTeamId };
  },

  async createAsset({ name, qty, responsibleTeamId, source, allocations }) {
    await delay(400);
    const id = 'a' + (assets.length + 1) + Math.floor(Math.random() * 1000);
    const newAsset = {
      id,
      name,
      type: 'Khác',
      total: qty,
      available: qty,
      responsibleTeamId,
    };
    assets.push(newAsset);
    assetCustodians[id] = allocations.map((al) => ({ id: al.custodianId || al.name, name: al.name, qty: al.qty }));
    transactionLog.unshift({
      id: 't' + (transactionLog.length + 1),
      assetId: id,
      time: fmtNow(),
      kind: 'Ghi nhận mới',
      qty,
      from: source,
      to: allocations.map((a) => a.name).join(', '),
      ticketId: null,
    });
    return { asset: newAsset };
  },

  // ---------------- Lịch sử giao dịch ----------------
  async getTransactionHistory({ assetId, from, to } = {}) {
    await delay(300);
    let list = transactionLog;
    if (assetId) list = list.filter((t) => t.assetId === assetId);
    return { transactions: list };
  },

  // ---------------- Reconciliation ----------------
  async getReconciliationBatch() {
    await delay(300);
    const total = 12;
    const done = 7;
    return { batch: reconciliationBatch, progress: { done, total } };
  },

  async sendConfirmRequest() {
    await delay(300);
    return { ok: true };
  },

  async closeReconciliationBatch() {
    await delay(300);
    const allDone = reconciliationBatch.rows.every((r) => (
      r.result === 'MATCH' || r.result === 'RESOLVED'
    ));
    if (!allDone) {
      const err = new Error('Chưa đủ 100% Custodian đóng xác nhận, không thể đóng đợt.');
      err.status = 409;
      throw err;
    }
    reconciliationBatch.status = 'CLOSED';
    return { batch: reconciliationBatch };
  },

  async openNewReconciliationBatch() {
    await delay(300);
    reconciliationBatch = {
      id: 'REC-2026-09',
      status: 'OPEN',
      openedDate: fmtNow(),
      rows: reconciliationBatch.rows.map((r) => ({ ...r, declared: null, result: 'UNCONFIRMED', diff: undefined, reason: undefined })),
    };
    return { batch: reconciliationBatch };
  },

  async createTransfer(input, reconciliationItemId = null) {
    await delay(300);
    const now = new Date().toISOString();
    const transfer = {
      id: `TRF-${String(transferSequence++).padStart(4, '0')}`,
      sourceCustodyId: input.sourceCustodyId,
      assetId: `asset-${input.sourceCustodyId}`,
      currentCustodianUserId: currentUser?.id || 'u1',
      targetCustodianUserId: input.targetUserId,
      quantity: input.quantity,
      conditionSnapshot: 'GOOD',
      createdByUserId: currentUser?.id || 'u1',
      createdAt: now,
      reconciliationItemId,
      status: 'PENDING_BOTH_CONFIRMATIONS',
      currentConfirmedAt: null,
      targetConfirmedAt: null,
      targetCustodyId: null,
      completedAt: null,
    };
    custodyTransfers = [transfer, ...custodyTransfers];
    return transfer;
  },

  async listTransfers({ status, page = 1, pageSize = 100 } = {}) {
    await delay(200);
    const items = status
      ? custodyTransfers.filter((transfer) => transfer.status === status)
      : custodyTransfers;
    return { items, page, pageSize, total: items.length };
  },

  async confirmTransfer(id, side) {
    await delay(250);
    const transfer = custodyTransfers.find((candidate) => candidate.id === id);
    if (!transfer) throw new Error('Transfer không tồn tại.');
    const field = side === 'current' ? 'currentConfirmedAt' : 'targetConfirmedAt';
    if (transfer[field]) throw new Error('Phía này đã xác nhận.');
    transfer[field] = new Date().toISOString();
    if (transfer.currentConfirmedAt && transfer.targetConfirmedAt) {
      transfer.status = 'COMPLETED';
      transfer.completedAt = new Date().toISOString();
      transfer.targetCustodyId = `custody-${transfer.id}`;
    } else {
      transfer.status = side === 'current'
        ? 'PENDING_TARGET_CONFIRMATION'
        : 'PENDING_CURRENT_CONFIRMATION';
    }
    return transfer;
  },

  async remindCustodian(rowId) {
    await delay(250);
    return { ok: true };
  },

  async confirmReconciliationRow(rowId, input) {
    await delay(250);
    const row = reconciliationBatch.rows.find((item) => item.id === rowId);
    if (!row || row.result !== 'UNCONFIRMED') throw new Error('Item không còn chờ xác nhận.');
    row.declared = Number(input.actualQuantity);
    const matches = row.declared === row.expected && input.condition === 'GOOD';
    row.result = matches ? 'MATCH' : 'MISMATCH';
    row.reason = matches ? undefined : input.discrepancyReason;
    if (!matches && !row.reason?.trim()) throw new Error('Sai lệch bắt buộc có lý do.');
    return { ok: true };
  },

  async resolveReconciliationRow(rowId, resolutionNote) {
    await delay(250);
    const row = reconciliationBatch.rows.find((item) => item.id === rowId);
    if (!row || row.result !== 'MISMATCH') throw new Error('Chỉ xử lý dòng sai lệch chưa giải quyết.');
    row.result = 'RESOLVED';
    row.resolutionNote = resolutionNote;
    return { ok: true };
  },

  // ---------------- Bàn giao nhiệm kỳ ----------------
  async createHandover(termId) {
    await delay(300);
    if (handoverRecord) return handoverRecord;
    const now = new Date().toISOString();
    handoverRecord = {
      id: 'HANDOVER-0001', termId, termCode: '2025-2026', termName: 'Nhiệm kỳ 2025-2026',
      termStartsOn: '2025-09-01', termEndsOn: '2026-08-31', oldHrUserId: 'u1',
      newHrUserId: 'p2', createdByUserId: currentUser?.id || 'u1', createdAt: now,
      status: 'PENDING_CONFIRMATIONS', oldConfirmedAt: null, newConfirmedAt: null,
      lockedAt: null,
      items: assets.map((asset, index) => ({
        id: `handover-item-${index + 1}`, handoverId: 'HANDOVER-0001', assetId: asset.id,
        assetName: asset.name, totalQuantity: asset.total, availableQuantity: asset.available,
        condition: 'GOOD', custodies: [], isStale: asset.id === 'a5',
        lastQualifyingTransactionAt: asset.id === 'a5' ? null : now, snapshotAt: now,
      })),
    };
    return handoverRecord;
  },

  async getHandover(id) {
    await delay(200);
    if (!handoverRecord || handoverRecord.id !== id) throw new Error('Handover không tồn tại.');
    return handoverRecord;
  },

  async confirmHandover(id, side) {
    await delay(250);
    if (!handoverRecord || handoverRecord.id !== id) throw new Error('Handover không tồn tại.');
    const field = side === 'old' ? 'oldConfirmedAt' : 'newConfirmedAt';
    if (handoverRecord[field]) throw new Error('Phía này đã xác nhận.');
    handoverRecord[field] = new Date().toISOString();
    if (handoverRecord.oldConfirmedAt && handoverRecord.newConfirmedAt) {
      handoverRecord.status = 'LOCKED';
      handoverRecord.lockedAt = new Date().toISOString();
    }
    return handoverRecord;
  },

  // ---------------- Teamlead tài sản: cấu hình Event Day ----------------
  async createEventDay(request) {
    await delay(300);
    const id = `mock-event-${eventDaySequence}`;
    const eventDay = mockEventDayResponse(id, request);
    eventDays.set(id, eventDay);
    eventDaySequence += 1;
    return eventDay;
  },

  async getEventDay(id) {
    await delay(250);
    const eventDay = eventDays.get(id);
    if (!eventDay) throw { status: 404, message: 'Không tìm thấy ngày sự kiện' };
    return eventDay;
  },

  async updateEventDay(id, request) {
    await delay(300);
    const previous = eventDays.get(id);
    if (!previous) throw { status: 404, message: 'Không tìm thấy ngày sự kiện' };
    const eventDay = mockEventDayResponse(id, request, previous);
    eventDays.set(id, eventDay);
    return eventDay;
  },

  // ---------------- Trưởng ca ----------------
  async listShifts({ page = 1, pageSize = 20 } = {}) {
    await delay(250);
    const start = (page - 1) * pageSize;
    return {
      items: shifts.slice(start, start + pageSize).map(mockShiftResponse),
      page,
      pageSize,
      total: shifts.length,
    };
  },

  async getShiftDetail(id) {
    await delay(250);
    const shift = shifts.find((s) => s.id === id);
    if (!shift) throw { status: 404, message: 'Không tìm thấy ca' };
    return mockShiftResponse(shift);
  },

  async openShift(id) {
    await delay(300);
    const shift = shifts.find((s) => s.id === id);
    if (!shift) throw { status: 404, message: 'Không tìm thấy ca' };
    shift.status = 'OPENING';
    return mockShiftResponse(shift);
  },

  async submitShiftReceived(id, { items }) {
    await delay(300);
    const shift = shifts.find((s) => s.id === id);
    if (!shift) throw { status: 404, message: 'Không tìm thấy ca' };
    shiftItems[id] = shiftItems[id].map((item) => ({
      ...item,
      openingActualQuantity: items.find((row) => row.assetId === item.assetId)?.actualQuantity,
    }));
    shift.status = 'ACTIVE';
    return mockShiftResponse(shift);
  },

  async reportShiftDeviation(id, { items, reason }) {
    await delay(350);
    const shift = shifts.find((s) => s.id === id);
    if (!shift) throw { status: 404, message: 'Không tìm thấy ca' };
    shiftItems[id] = shiftItems[id].map((item) => {
      const actual = items.find((row) => row.assetId === item.assetId)?.actualQuantity;
      return {
        ...item,
        openingActualQuantity: actual,
        discrepancyReason: actual === item.quotaQuantity + item.carryOverQuantity ? null : reason,
      };
    });
    shift.status = 'ACTIVE_WITH_DEVIATION';
    return mockShiftResponse(shift);
  },

  async submitShiftClose(id, { items }) {
    await delay(350);
    const shift = shifts.find((s) => s.id === id);
    if (!shift) throw { status: 404, message: 'Không tìm thấy ca' };
    shiftItems[id] = shiftItems[id].map((item) => {
      const actual = items.find((row) => row.assetId === item.assetId)?.actualQuantity;
      return {
        ...item,
        closingActualQuantity: actual,
        consumedQuantity: item.quotaQuantity + item.carryOverQuantity - actual,
      };
    });
    shift.status = 'CLOSED';
    return mockShiftResponse(shift);
  },

  async getEventDaySummary(id) {
    await delay(250);
    const eventShifts = shifts.filter((shift) => shift.eventDayId === id);
    const configured = eventDays.get(id);
    if (!eventShifts.length && !configured) {
      throw { status: 404, message: 'Không tìm thấy ngày sự kiện' };
    }
    if (!eventShifts.length) {
      return {
        eventDayId: id,
        status: configured.status,
        complete: false,
        totalShiftCount: configured.shifts.length,
        closedShiftCount: 0,
        coreStockChanged: false,
        items: null,
      };
    }
    const complete = eventShifts.every((shift) => shift.status === 'CLOSED');
    return {
      eventDayId: id,
      status: complete ? 'COMPLETED' : 'IN_PROGRESS',
      complete,
      totalShiftCount: eventShifts.length,
      closedShiftCount: eventShifts.filter((shift) => shift.status === 'CLOSED').length,
      coreStockChanged: false,
      items: complete ? [] : null,
    };
  },
};

function fmtShort(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function fmtNow() {
  const d = new Date();
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

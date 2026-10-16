// Nhãn tiếng Việt cho các mã trạng thái backend trả về. Trang nào hiển thị trạng thái
// thì tra ở đây — không in thẳng mã (OPEN, GOOD…) ra giao diện.

export const CONDITION_LABEL = {
  GOOD: 'Tốt',
  DAMAGED: 'Hư hỏng',
  LOST: 'Thất lạc',
};

export const RECONCILIATION_STATUS = {
  OPEN: ['Đang mở', ''],
  CLOSED: ['Đã đóng', 'green'],
};

export const RECONCILIATION_ITEM_STATUS = {
  PENDING_CONFIRMATION: ['Chưa xác nhận', ''],
  CONFIRMED: ['Đã khớp', 'green'],
  DISCREPANCY: ['Sai lệch chưa xử lý', 'red'],
  RESOLVED: ['Sai lệch đã xử lý', 'gold'],
};

export const HANDOVER_STATUS = {
  PENDING_CONFIRMATIONS: ['Chờ hai HR xác nhận', ''],
  LOCKED: ['Đã khóa', 'green'],
};

export const EVENT_DAY_STATUS = {
  DRAFT: 'Nháp',
  IN_PROGRESS: 'Đang diễn ra',
  COMPLETED: 'Đã hoàn tất',
};

// Mã lạ (backend thêm trạng thái mới) vẫn hiện được thay vì trống.
export function label(map, code) {
  const value = map[code];
  if (value === undefined) return code;
  return Array.isArray(value) ? value[0] : value;
}

export function badgeClass(map, code) {
  const value = map[code];
  return Array.isArray(value) ? value[1] : '';
}

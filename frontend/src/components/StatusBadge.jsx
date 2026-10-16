// Quy ước màu badge:
// Vàng = cảnh báo mềm (sắp đến hạn, quá 24h chưa xác nhận)
// Đỏ   = cảnh báo cứng (quá hạn trả, chênh lệch)
// Cam  = chờ (chờ duyệt, chờ bên còn lại)
// Xanh dương = đang diễn ra (đang mượn)
// Xanh lá = hoàn tất (đã duyệt, đã xác nhận) · Xám = đã đóng/hủy

const STATUS_MAP = {
  PENDING: { text: 'Chờ duyệt', color: '' },
  APPROVED: { text: 'Đã duyệt', color: 'green' },
  BORROWING: { text: 'Đang mượn', color: 'blue' },
  RETURNING: { text: 'Chờ nhận lại', color: '' },
  CANCEL_PENDING: { text: 'Chờ duyệt hủy', color: 'gold' },
  // Mã cũ của mock trước khi qua mapLegacyTicket
  ACTIVE: { text: 'Đang mượn', color: 'blue' },
  AWAITING_HOLDER_CONFIRM: { text: 'Chờ nhận lại', color: '' },
  CANCEL_REQUESTED: { text: 'Chờ duyệt hủy', color: 'gold' },
  REJECTED: { text: 'Từ chối', color: 'red' },
  CANCELLED: { text: 'Đã hủy', color: 'dark' },
  CLOSED: { text: 'Đã đóng', color: 'dark' },
  CONFIRMED: { text: 'Đã xác nhận', color: 'green' },
  PENDING_CONFIRM: { text: 'Chờ tôi xác nhận', color: '' },
  CONFIRM_OVERDUE: { text: 'Quá hạn xác nhận', color: 'gold' },
};

export default function StatusBadge({ status, overdue, dueSoon }) {
  const meta = STATUS_MAP[status] || { text: status, color: '' };
  return (
    <span className="flex-row wrap" style={{ gap: 6 }}>
      <span className={`badge ${meta.color}`}>{meta.text}</span>
      {overdue && <span className="badge red">quá hạn</span>}
      {dueSoon && !overdue && <span className="badge gold">sắp hạn</span>}
    </span>
  );
}

export { STATUS_MAP };

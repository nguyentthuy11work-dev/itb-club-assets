// Quy ước màu badge (đúng theo tham chiếu wireframe 5o):
// Vàng = cảnh báo mềm (sắp đến hạn, quá 24h chưa xác nhận)
// Đỏ   = cảnh báo cứng (quá hạn trả, chênh lệch)
// Xám  = chờ trung tính (chờ duyệt, chờ bên còn lại)
// Xanh = hoàn tất (đã duyệt, đã đóng, đã xác nhận)

const STATUS_MAP = {
  PENDING: { text: 'Chờ duyệt', color: '' },
  APPROVED: { text: 'Đã duyệt', color: 'green' },
  ACTIVE: { text: 'Đang mượn', color: 'green' },
  AWAITING_HOLDER_CONFIRM: { text: 'Chờ Custodian xác nhận', color: '' },
  CANCEL_REQUESTED: { text: 'Người mượn xin huỷ', color: 'gold' },
  REJECTED: { text: 'Từ chối', color: 'red' },
  CANCELLED: { text: 'Đã hủy', color: '' },
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

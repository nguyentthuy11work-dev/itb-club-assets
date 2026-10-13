import { useState } from 'react';

/**
 * Modal nhập lý do dùng chung cho "Từ chối Ticket" (bắt buộc lý do) và
 * "Gửi yêu cầu huỷ" (lý do không bắt buộc).
 *
 * props:
 *  - title, subtitle
 *  - required: lý do có bắt buộc không
 *  - confirmLabel
 *  - confirmDanger: hiển thị nút xác nhận màu đỏ hay đen
 *  - onCancel()
 *  - onSubmit(reason)
 */
export default function ReasonModal({ title, subtitle, required = false, confirmLabel = 'Xác nhận', confirmDanger = false, onCancel, onSubmit }) {
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const disabled = required && reason.trim().length === 0;

  async function handleSubmit() {
    if (disabled) return;
    setSubmitting(true);
    setError('');
    try {
      await onSubmit(reason.trim());
    } catch (err) {
      setError(err.message || 'Có lỗi xảy ra, thử lại.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="modal-panel" style={{ width: 400 }}>
        <h2 className="modal-title" style={{ fontSize: 18 }}>{title}</h2>
        {subtitle && <div className="hint" style={{ marginTop: -6 }}>{subtitle}</div>}

        <div className="field">
          <label htmlFor="reason-text">Lý do{required ? ' *' : ' (không bắt buộc)'}</label>
          <textarea id="reason-text" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>

        {error && <div className="error-box">{error}</div>}

        <div className="flex-row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn outline" onClick={onCancel} disabled={submitting}>Huỷ</button>
          <button
            type="button"
            className={`btn ${confirmDanger ? '' : 'primary'}`}
            style={confirmDanger ? { borderColor: '#c0392b', background: '#c0392b', color: '#fff' } : undefined}
            onClick={handleSubmit}
            disabled={disabled || submitting}
          >
            {submitting ? 'Đang gửi...' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

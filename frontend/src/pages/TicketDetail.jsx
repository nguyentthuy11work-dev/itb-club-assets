import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Layout from '../components/Layout';
import StatusBadge from '../components/StatusBadge';
import PhotoEvidenceModal from '../components/PhotoEvidenceModal';
import ReasonModal from '../components/ReasonModal';
import { ticketService } from '../api/services';
import { useAuth } from '../context/AuthContext';
import { getTicketActions } from './ticketActions';
import { formatTicketActionError } from './ticketActionError';

export default function TicketDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [ticket, setTicket] = useState(null);
  const [error, setError] = useState('');
  const [modal, setModal] = useState(null); // 'receive' | 'return' | 'holderReceive' | 'reject' | 'cancelRequest'
  const [actionError, setActionError] = useState('');
  const [actionBusy, setActionBusy] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const t = await ticketService.get(id);
      setTicket(t);
      return t;
    } catch (err) {
      setError(err.message || 'Không tìm thấy Ticket');
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <Layout title="Ticket">
        <div className="page-error">{error}</div>
      </Layout>
    );
  }
  if (!ticket) {
    return (
      <Layout title="Ticket">
        <div className="loading-block">Đang tải Ticket...</div>
      </Layout>
    );
  }

  const isBorrower = ticket.borrowerId === user?.id;
  const isHolder = ticket.holderId === user?.id;
  const dueOverdue = ticket.overdue;

  async function withReload(fn) {
    await fn();
    const updated = await load();
    setModal(null);
    return updated;
  }

  // Hành động không cần modal (Duyệt / Đồng ý huỷ / Từ chối huỷ / Huỷ yêu cầu khi Chờ duyệt)
  async function handleDirectAction(kind) {
    setActionError('');
    setActionBusy(true);
    try {
      if (kind === 'approve') await withReload(() => ticketService.approve(ticket.id));
      if (kind === 'cancelApprove') await withReload(() => ticketService.resolveCancel(ticket.id, true));
      if (kind === 'cancelReject') await withReload(() => ticketService.resolveCancel(ticket.id, false));
    } catch (err) {
      setActionError(formatTicketActionError(err));
    } finally {
      setActionBusy(false);
    }
  }

  function handleAction(kind) {
    if (['approve', 'cancelApprove', 'cancelReject'].includes(kind)) {
      handleDirectAction(kind);
    } else {
      setModal(kind);
    }
  }

  return (
    <Layout title="Ticket">
      <button className="btn outline" style={{ width: 'fit-content' }} onClick={() => navigate(-1)}>← Quay lại</button>

      <div className="flex-row wrap" style={{ alignItems: 'flex-start', gap: 16 }}>
        <div className="card flex-1" style={{ minWidth: 320, padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="flex-row wrap">
            <div style={{ font: '700 20px var(--font-display)' }}>{ticket.id}</div>
            <StatusBadge status={ticket.status} overdue={dueOverdue} dueSoon={ticket.dueSoon} />
            {ticket.dueDate && ticket.status !== 'CLOSED' && (
              <span className="badge gold">
                Hạn trả {ticket.dueDate}{dueOverdue ? ' — quá hạn' : ''}
              </span>
            )}
          </div>

          <div className="dashed-panel">
            Trong Ticket này bạn là <b>{isBorrower ? 'bên mượn' : isHolder ? 'bên giữ' : 'người xem'}</b> ·
            {isBorrower ? ` bên giữ là ${ticket.holder}` : ` người mượn là ${ticket.borrower}`}
          </div>

          <div className="card" style={{ overflow: 'hidden' }}>
            <table className="table">
              <thead><tr><th>Tài sản</th><th>SL</th><th>Hình thức</th></tr></thead>
              <tbody>{ticket.items.map((item) => (
                <tr key={item.id}>
                  <td>{item.assetName}</td><td>{item.quantity}</td><td>{ticket.method}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>

          <div style={{ font: '700 15px var(--font-display)' }}>Nhật ký</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 14, color: '#444' }}>
            {(ticket.log || []).map((line, i) => <div key={i}>{line}</div>)}
            {ticket.status === 'APPROVED' && <div style={{ color: '#999' }}>chờ bạn xác nhận đã nhận</div>}
          </div>
        </div>

        <div style={{ width: 320, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="card" style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 14 }}>
            <div style={{ font: '600 14px var(--font-display)', color: '#555' }}>Bên giữ</div>
            <div>{ticket.holder}</div>
            <div style={{ font: '600 14px var(--font-display)', color: '#555', marginTop: 4 }}>Ngày trả</div>
            <div>{ticket.dueDate || 'Chuyển custodian — không có hạn'}</div>
          </div>

          <div className="card" style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ font: '600 14px var(--font-display)', color: '#555' }}>Việc của bạn</div>
            <ActionPanel
              ticket={ticket}
              isBorrower={isBorrower}
              isHolder={isHolder}
              onAction={handleAction}
              busy={actionBusy}
            />
            {actionError && <div className="error-box">{actionError}</div>}
          </div>
        </div>
      </div>

      {modal === 'receive' && (
        <PhotoEvidenceModal
          title={`Xác nhận đã nhận — ${ticket.id}`}
          onCancel={() => setModal(null)}
          onSubmit={(payload) => withReload(() => ticketService.confirmReceived(ticket.id, payload))}
        />
      )}
      {modal === 'return' && (
        <PhotoEvidenceModal
          title={`Báo đã trả — ${ticket.id}`}
          onCancel={() => setModal(null)}
          onSubmit={(payload) => withReload(() => ticketService.markReturned(ticket.id, payload))}
        />
      )}
      {modal === 'holderReceive' && (
        <PhotoEvidenceModal
          title={`Xác nhận đã nhận lại — ${ticket.id}`}
          onCancel={() => setModal(null)}
          onSubmit={(payload) => withReload(() => ticketService.confirmReturnedByHolder(ticket.id, payload))}
        />
      )}
      {modal === 'reject' && (
        <ReasonModal
          title={`Từ chối Ticket ${ticket.id}`}
          required
          confirmLabel="Xác nhận từ chối"
          onCancel={() => setModal(null)}
          onSubmit={(reason) => withReload(() => ticketService.reject(ticket.id, reason))}
        />
      )}
      {modal === 'cancelRequest' && (
        <ReasonModal
          title={`Gửi yêu cầu huỷ — ${ticket.id}`}
          subtitle={ticket.status === 'PENDING'
            ? 'Ticket chưa duyệt sẽ được huỷ ngay.'
            : 'Ticket đã được duyệt nên cần bên giữ đồng ý'}
          required
          confirmLabel="Gửi yêu cầu huỷ"
          onCancel={() => setModal(null)}
          onSubmit={(reason) => withReload(() => ticketService.requestCancel(ticket.id, reason))}
        />
      )}
    </Layout>
  );
}

function ActionPanel({ ticket, isBorrower, isHolder, onAction, busy }) {
  const labels = {
    receive: 'Xác nhận đã nhận',
    cancelRequest: ticket.status === 'PENDING' ? 'Huỷ yêu cầu' : 'Gửi yêu cầu huỷ',
    return: 'Đã trả',
    approve: 'Duyệt',
    reject: 'Từ chối',
    holderReceive: 'Xác nhận đã nhận lại',
    cancelApprove: 'Đồng ý huỷ',
    cancelReject: 'Từ chối huỷ',
  };
  const actions = getTicketActions(ticket, { isBorrower, isHolder });
  if (actions.length === 0) {
    return <div className="hint">Chỉ xem — không có thao tác nào ở bước này.</div>;
  }
  return actions.map((action) => (
    <button
      key={action}
      className={`btn ${['reject', 'cancelRequest', 'cancelReject'].includes(action) ? 'danger-outline' : 'primary'}`}
      disabled={busy}
      onClick={() => onAction(action)}
    >
      {labels[action]}
    </button>
  ));
}

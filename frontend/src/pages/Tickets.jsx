import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import StatusBadge from '../components/StatusBadge';
import { ticketService } from '../api/services';
import { ticketBucket } from './ticketActions';

export default function Tickets() {
  const navigate = useNavigate();
  const [role, setRole] = useState('borrower'); // 'borrower' = Tôi mượn, 'holder' = Tôi cho mượn
  const [chip, setChip] = useState('mine'); // mine | waiting | closed
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setTickets([]);
    setError('');
    ticketService
      .list({ role })
      .then((list) => !cancelled && setTickets(list))
      .catch((err) => !cancelled && setError(err.message || 'Không tải được danh sách Ticket'))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [role]);

  const counts = useMemo(() => {
    const tally = { mine: 0, waiting: 0, closed: 0 };
    tickets.forEach((t) => { tally[ticketBucket(t, role)] += 1; });
    return tally;
  }, [tickets, role]);

  const filtered = tickets.filter((t) => ticketBucket(t, role) === chip);

  return (
    <Layout title="Ticket">
      <div className="tabs">
        <button className={`tab-item ${role === 'borrower' ? 'active' : ''}`} onClick={() => { setRole('borrower'); setChip('mine'); }}>
          Tôi mượn{role === 'borrower' ? ` (${tickets.length})` : ''}
        </button>
        <button className={`tab-item ${role === 'holder' ? 'active' : ''}`} onClick={() => { setRole('holder'); setChip('mine'); }}>
          Tôi cho mượn{role === 'holder' ? ` (${tickets.length})` : ''}
        </button>
      </div>

      <div className="chip-row">
        <button className={`chip ${chip === 'mine' ? 'active' : ''}`} onClick={() => setChip('mine')}>Cần tôi xử lý ({counts.mine})</button>
        <button className={`chip ${chip === 'waiting' ? 'active' : ''}`} onClick={() => setChip('waiting')}>Chờ bên kia ({counts.waiting})</button>
        <button className={`chip ${chip === 'closed' ? 'active' : ''}`} onClick={() => setChip('closed')}>Đã đóng ({counts.closed})</button>
      </div>

      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Mã</th>
              <th>Tài sản</th>
              <th>{role === 'borrower' ? 'Người giữ' : 'Người mượn'}</th>
              <th>Ngày trả</th>
              <th>Trạng thái</th>
              <th>Việc của tôi</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={6} className="loading-block">Đang tải Ticket...</td></tr>}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={6} className="hint" style={{ padding: 16 }}>Không có Ticket nào ở mục này.</td></tr>
            )}
            {!loading && filtered.map((t, i) => (
              <tr key={t.id} className={'clickable' + (i % 2 === 1 ? ' alt' : '')} onClick={() => navigate(`/ticket/${t.id}`)}>
                <td><Link to={`/ticket/${t.id}`} onClick={(e) => e.stopPropagation()} style={{ fontWeight: 600 }}>{t.id}</Link></td>
                <td>{t.asset}</td>
                <td>{role === 'borrower' ? t.holder : t.borrower}</td>
                <td>{t.dueDate || '—'}</td>
                <td><StatusBadge status={t.status} overdue={t.overdue} dueSoon={t.dueSoon} /></td>
                <td onClick={(e) => e.stopPropagation()}>
                  <MyTaskCell ticket={t} role={role} onOpen={() => navigate(`/ticket/${t.id}`)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Layout>
  );
}


function MyTaskCell({ ticket: t, role, onOpen }) {
  if (role === 'borrower') {
    if (t.status === 'BORROWING') return <button className="btn small primary" onClick={onOpen}>Đã trả</button>;
    if (t.status === 'APPROVED') return <button className="btn small primary" onClick={onOpen}>Xác nhận đã nhận</button>;
    if (t.status === 'PENDING') return <span className="hint">chờ {t.holder} duyệt</span>;
    if (t.status === 'RETURNING') return <span className="hint">chờ {t.holder} nhận lại</span>;
    return <span className="hint">—</span>;
  }
  if (t.status === 'PENDING') {
    return (
      <span className="flex-row" style={{ gap: 6 }}>
        <button className="btn small" onClick={onOpen}>Duyệt</button>
        <button className="btn small danger-outline" onClick={onOpen}>Từ chối</button>
      </span>
    );
  }
  if (t.status === 'RETURNING') return <button className="btn small primary" onClick={onOpen}>Xác nhận đã nhận lại</button>;
  if (t.status === 'CANCEL_PENDING') {
    return (
      <span className="flex-row" style={{ gap: 6 }}>
        <button className="btn small" onClick={onOpen}>Đồng ý hủy</button>
        <button className="btn small danger-outline" onClick={onOpen}>Từ chối hủy</button>
      </span>
    );
  }
  return <span className="hint">chờ người mượn trả</span>;
}

import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import Layout from '../components/Layout';
import { shiftService } from '../api/services';

export default function ShiftClose() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [shift, setShift] = useState(null);
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const applyShift = useCallback((value) => {
    setShift(value);
    setItems(value.items.map((item) => ({
      ...item,
      actualQuantity: item.closingActualQuantity ?? '',
    })));
  }, []);

  const load = useCallback(async () => {
    setError('');
    try {
      applyShift(await shiftService.get(id));
    } catch (err) {
      setError(err.message || 'Không tải được ca trực');
    }
  }, [applyShift, id]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (shift && ['SCHEDULED', 'OPENING'].includes(shift.status)) {
      navigate(`/ca-truc/${id}`, { replace: true });
    }
  }, [id, navigate, shift]);

  function updateActual(assetId, value) {
    setItems((current) => current.map((item) => (
      item.assetId === assetId ? { ...item, actualQuantity: value } : item
    )));
  }

  async function handleClose() {
    setSubmitting(true);
    setActionError('');
    try {
      applyShift(await shiftService.close(id, items));
    } catch (err) {
      setActionError(err.message || 'Không thể chốt ca.');
    } finally {
      setSubmitting(false);
    }
  }

  if (error) return <Layout title="Chốt ca"><div className="page-error">{error}</div></Layout>;
  if (!shift) return <Layout title="Chốt ca"><div className="loading-block">Đang tải…</div></Layout>;

  const closed = shift.status === 'CLOSED';
  const allEntered = items.every((item) => (
    item.actualQuantity !== '' && Number.isInteger(Number(item.actualQuantity))
    && Number(item.actualQuantity) >= 0
  ));

  return (
    <Layout title={`Chốt ca ${shift.sequence} · ${shift.name}`} subtitle={shift.eventDayName}>
      <Link to="/ca-truc" className="hint" style={{ textDecoration: 'none' }}>← Ca trực của tôi</Link>
      {shift.status === 'ACTIVE_WITH_DEVIATION' && <div className="notice-box warn">Ca bắt đầu với chênh lệch đã báo Teamlead.</div>}

      <div className="card shift-action-card">
        <div><div style={{ font: '700 16px var(--font-display)' }}>Tồn thực tế cuối ca</div><div className="hint">Tiêu hao = định mức + tồn ca trước − tồn thực tế cuối ca.</div></div>
        <table className="table">
          <thead><tr><th>Mã tài sản</th><th>Kỳ vọng đầu ca</th><th>Thực nhận</th><th>Tồn cuối ca</th><th>Tiêu hao</th></tr></thead>
          <tbody>
            {items.map((item) => {
              const actual = item.actualQuantity === '' ? null : Number(item.actualQuantity);
              const consumed = closed ? item.consumedQuantity : actual === null ? null : item.expectedOpenQuantity - actual;
              return (
                <tr key={item.id}>
                  <td><code>{item.assetId}</code></td><td>{item.expectedOpenQuantity}</td><td>{item.openingActualQuantity}</td>
                  <td><input className="quantity-input" type="number" min="0" step="1" value={item.actualQuantity} disabled={closed} onChange={(event) => updateActual(item.assetId, event.target.value)} /></td>
                  <td style={{ fontWeight: 700 }}>{consumed ?? '—'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {actionError && <div className="error-box">{actionError}</div>}
        <button className="btn primary" disabled={!allEntered || closed || submitting} onClick={handleClose}>
          {closed ? 'Đã chốt ca' : submitting ? 'Đang chốt…' : 'Xác nhận chốt ca'}
        </button>
        {closed && <div className="notice-box event-success">Đã chốt ca. Tồn thực tế được tự động đưa vào kỳ vọng của ca kế tiếp; không có thao tác bàn giao thủ công.</div>}
      </div>
    </Layout>
  );
}

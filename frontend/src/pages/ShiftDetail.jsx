import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import Layout from '../components/Layout';
import { shiftService } from '../api/services';

export default function ShiftDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [shift, setShift] = useState(null);
  const [items, setItems] = useState([]);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const applyShift = useCallback((value) => {
    setShift(value);
    setItems(value.items.map((item) => ({
      ...item,
      actualQuantity: item.openingActualQuantity ?? item.expectedOpenQuantity,
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
    if (shift && ['ACTIVE', 'ACTIVE_WITH_DEVIATION', 'CLOSED'].includes(shift.status)) {
      navigate(`/ca-truc/${id}/chot`, { replace: true });
    }
  }, [id, navigate, shift]);

  function updateActual(assetId, value) {
    setItems((current) => current.map((item) => (
      item.assetId === assetId ? { ...item, actualQuantity: value } : item
    )));
  }

  async function run(action) {
    setSubmitting(true);
    setActionError('');
    try {
      const result = await action();
      applyShift(result);
      return result;
    } catch (err) {
      setActionError(err.message || 'Thao tác thất bại, thử lại.');
      return null;
    } finally {
      setSubmitting(false);
    }
  }

  async function handleOpen() {
    await run(() => shiftService.open(id));
  }

  async function handleConfirm() {
    const result = await run(() => shiftService.confirmReceived(id, items));
    if (result) navigate(`/ca-truc/${id}/chot`);
  }

  async function handleDeviation() {
    const result = await run(() => shiftService.reportDeviation(id, items, reason));
    if (result) navigate(`/ca-truc/${id}/chot`);
  }

  if (error) return <Layout title="Ca trực"><div className="page-error">{error}</div></Layout>;
  if (!shift) return <Layout title="Ca trực"><div className="loading-block">Đang tải…</div></Layout>;

  const hasDeviation = items.some(
    (item) => Number(item.actualQuantity) !== item.expectedOpenQuantity,
  );

  return (
    <Layout title={`Ca ${shift.sequence} · ${shift.name}`} subtitle={shift.eventDayName}>
      <Link to="/ca-truc" className="hint" style={{ textDecoration: 'none' }}>← Ca trực của tôi</Link>

      {shift.status === 'SCHEDULED' ? (
        <div className="card shift-action-card">
          <div className="notice-box">
            Hệ thống sẽ kiểm tra thời gian ca và trạng thái ca liền trước trước khi mở.
          </div>
          {actionError && <div className="error-box">{actionError}</div>}
          <button className="btn primary" disabled={submitting} onClick={handleOpen}>
            {submitting ? 'Đang mở…' : 'Mở ca và kiểm tra tài sản'}
          </button>
        </div>
      ) : (
        <div className="card shift-action-card">
          <div>
            <div style={{ font: '700 16px var(--font-display)' }}>Xác nhận số lượng thực nhận</div>
            <div className="hint">Kỳ vọng = định mức ca này + tồn thực tế từ ca trước.</div>
          </div>
          <table className="table">
            <thead><tr><th>Mã tài sản</th><th>Định mức</th><th>Tồn ca trước</th><th>Kỳ vọng</th><th>Thực nhận</th><th>Lệch</th></tr></thead>
            <tbody>
              {items.map((item) => {
                const difference = Number(item.actualQuantity) - item.expectedOpenQuantity;
                return (
                  <tr key={item.id} className={difference !== 0 ? 'alt' : undefined}>
                    <td><code>{item.assetId}</code></td><td>{item.quotaQuantity}</td><td>{item.carryOverQuantity}</td><td>{item.expectedOpenQuantity}</td>
                    <td><input className="quantity-input" type="number" min="0" step="1" value={item.actualQuantity} onChange={(event) => updateActual(item.assetId, event.target.value)} /></td>
                    <td className={difference !== 0 ? 'quantity-difference' : undefined}>{difference > 0 ? `+${difference}` : difference}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {hasDeviation && (
            <div className="field"><label>Lý do chênh lệch gửi Teamlead</label><textarea required maxLength="1000" value={reason} onChange={(event) => setReason(event.target.value)} /></div>
          )}
          {actionError && <div className="error-box">{actionError}</div>}
          <div className="flex-row wrap">
            <button className="btn primary" disabled={hasDeviation || submitting} onClick={handleConfirm}>Xác nhận đã nhận đủ</button>
            <button className="btn danger-solid" disabled={!hasDeviation || !reason.trim() || submitting} onClick={handleDeviation}>Báo chênh lệch và bắt đầu ca</button>
          </div>
          {hasDeviation && <div className="notice-box warn">Ca vẫn có thể bắt đầu với trạng thái “Có chênh lệch”; hệ thống gửi email cho Teamlead tài sản.</div>}
        </div>
      )}
    </Layout>
  );
}

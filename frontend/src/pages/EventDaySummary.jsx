import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { eventDayService } from '../api/services';
import Layout from '../components/Layout';
import { EVENT_DAY_STATUS, label } from '../components/labels';

export default function EventDaySummary() {
  const { eventDayId } = useParams();
  const navigate = useNavigate();
  const [lookupId, setLookupId] = useState(eventDayId || '');
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (id) => {
    setLoading(true);
    setError('');
    try {
      setSummary(await eventDayService.getSummary(id));
    } catch (err) {
      setError(err.message || 'Không tải được báo cáo sự kiện');
      setSummary(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (eventDayId) {
      setLookupId(eventDayId);
      load(eventDayId);
    }
  }, [eventDayId, load]);

  function handleOpen(event) {
    event.preventDefault();
    const id = lookupId.trim();
    if (id) navigate(`/bao-cao-su-kien/${id}`);
  }

  return (
    <Layout title="Báo cáo ngày sự kiện" subtitle="Tiến độ đóng ca và tổng hợp cuối ngày">
      <form className="event-lookup card" onSubmit={handleOpen}>
        <div className="field event-lookup-input">
          <label htmlFor="summary-event-id">Mã ngày sự kiện</label>
          <input id="summary-event-id" value={lookupId} onChange={(event) => setLookupId(event.target.value)} placeholder="Dán mã ngày sự kiện" />
        </div>
        <button className="btn primary" disabled={!lookupId.trim() || loading} type="submit">{loading ? 'Đang tải…' : 'Xem báo cáo'}</button>
      </form>

      {error && <div className="page-error">{error}</div>}
      {summary && (
        <>
          <div className="stat-grid">
            <div className="stat-card"><div className="stat-label">Tiến độ ca</div><div className="stat-value">{summary.closedShiftCount}/{summary.totalShiftCount}</div><div className="stat-sub">ca đã chốt</div></div>
            <div className="stat-card"><div className="stat-label">Trạng thái</div><div className="stat-value" style={{ fontSize: 20 }}>{label(EVENT_DAY_STATUS, summary.status)}</div><div className="stat-sub">{summary.complete ? 'Đã tổng hợp' : 'Chưa đủ điều kiện tổng hợp'}</div></div>
          </div>

          {!summary.complete ? (
            <div className="notice-box warn">Kết quả theo tài sản chỉ xuất hiện sau khi toàn bộ ca trong ngày đã chốt.</div>
          ) : (
            <div className="card" style={{ overflowX: 'auto' }}>
              <div className="card-title-bar">Tổng hợp cuối ngày</div>
              <table className="table">
                <thead><tr><th>Mã tài sản</th><th>Tổng định mức</th><th>Tổng tiêu hao</th><th>Lệch lúc mở</th><th>Tồn cuối</th></tr></thead>
                <tbody>
                  {summary.items.map((item) => (
                    <tr key={item.assetId}>
                      <td><code>{item.assetId}</code></td><td>{item.totalQuotaQuantity}</td><td>{item.totalConsumedQuantity}</td><td>{item.openingVarianceQuantity > 0 ? `+${item.openingVarianceQuantity}` : item.openingVarianceQuantity}</td><td>{item.finalCarryOverQuantity}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Layout>
  );
}

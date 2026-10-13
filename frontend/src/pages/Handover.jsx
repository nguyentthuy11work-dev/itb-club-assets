import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { handoverService } from '../api/services';
import { useAuth } from '../context/AuthContext';

const HANDOVER_KEY = 'ams_current_handover_id';

export default function Handover() {
  const { user } = useAuth();
  const [handoverId, setHandoverId] = useState(() => localStorage.getItem(HANDOVER_KEY) || '');
  const [termId, setTermId] = useState('');
  const [handover, setHandover] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async (id = handoverId) => {
    if (!id) return;
    setError('');
    try {
      const value = await handoverService.get(id);
      setHandover(value);
      setHandoverId(value.id);
      localStorage.setItem(HANDOVER_KEY, value.id);
    } catch (err) {
      setError(err.message || 'Không tải được Handover.');
    }
  }, [handoverId]);

  useEffect(() => { if (handoverId) load(handoverId); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function create(event) {
    event.preventDefault();
    setBusy('create');
    setError('');
    try {
      const value = await handoverService.create(termId);
      setHandover(value);
      setHandoverId(value.id);
      localStorage.setItem(HANDOVER_KEY, value.id);
    } catch (err) {
      setError(err.message || 'Không thể tạo Handover.');
    } finally {
      setBusy('');
    }
  }

  async function confirm(side) {
    if (!handover) return;
    setBusy(side);
    setError('');
    try {
      setHandover(await handoverService.confirm(handover.id, side));
    } catch (err) {
      setError(err.message || 'Không thể xác nhận Handover.');
    } finally {
      setBusy('');
    }
  }

  async function exportCsv() {
    if (!handover) return;
    setBusy('export');
    setError('');
    try {
      const document = await handoverService.exportCsv(handover.id);
      const url = URL.createObjectURL(document.blob);
      const anchor = window.document.createElement('a');
      anchor.href = url;
      anchor.download = document.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message || 'Không thể xuất CSV.');
    } finally {
      setBusy('');
    }
  }

  const mayConfirmOld = handover
    && user?.id === handover.oldHrUserId
    && !handover.oldConfirmedAt
    && handover.status !== 'LOCKED';
  const mayConfirmNew = handover
    && user?.id === handover.newHrUserId
    && !handover.newConfirmedAt
    && handover.status !== 'LOCKED';

  return (
    <Layout title="Bàn giao nhiệm kỳ" subtitle="Snapshot theo nhiệm kỳ, xác nhận hai phía và khóa bất biến.">
      <div className="card" style={{ padding: 18, display: 'grid', gap: 14 }}>
        <form className="flex-row wrap" onSubmit={create}>
          <label className="field flex-1">Term ID đã cấu hình
            <input required value={termId} onChange={(event) => setTermId(event.target.value)} />
          </label>
          <button className="btn primary" disabled={!!busy}>Tạo snapshot</button>
        </form>
        <div className="flex-row wrap">
          <input className="flex-1" placeholder="Handover ID" value={handoverId} onChange={(event) => setHandoverId(event.target.value)} />
          <button className="btn outline" disabled={!handoverId || !!busy} onClick={() => load()}>Tải biên bản</button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </div>

      {handover && (
        <>
          <div className="card" style={{ padding: 18, display: 'grid', gap: 12 }}>
            <div className="flex-row wrap">
              <strong>{handover.termName}</strong>
              <span className={`badge ${handover.status === 'LOCKED' ? 'green' : ''}`}>{handover.status}</span>
              <span className="hint">{handover.termStartsOn} → {handover.termEndsOn}</span>
              <div className="flex-1" />
              <button className="btn outline" disabled={!!busy} onClick={exportCsv}>Xuất CSV</button>
            </div>
            <div className="flex-row wrap">
              <span>HR cũ: <strong>{handover.oldConfirmedAt ? 'Đã xác nhận' : 'Chưa xác nhận'}</strong></span>
              <span>HR mới: <strong>{handover.newConfirmedAt ? 'Đã xác nhận' : 'Chưa xác nhận'}</strong></span>
              {mayConfirmOld && <button className="btn primary" disabled={!!busy} onClick={() => confirm('old')}>Xác nhận phía HR cũ</button>}
              {mayConfirmNew && <button className="btn primary" disabled={!!busy} onClick={() => confirm('new')}>Xác nhận phía HR mới</button>}
            </div>
            {handover.status === 'LOCKED' && <div className="notice-box">Biên bản đã khóa và không thể chỉnh sửa.</div>}
          </div>

          <div className="card" style={{ overflow: 'hidden' }}>
            <table className="table">
              <thead><tr><th>Tài sản</th><th>Tổng</th><th>Khả dụng</th><th>Tình trạng</th><th>Custody</th><th>Cảnh báo</th></tr></thead>
              <tbody>
                {handover.items.map((item) => (
                  <tr key={item.id} className={item.isStale ? 'alt' : undefined}>
                    <td><strong>{item.assetName}</strong><div className="hint">{item.assetId}</div></td>
                    <td>{item.totalQuantity}</td>
                    <td>{item.availableQuantity}</td>
                    <td>{item.condition}</td>
                    <td>{item.custodies.length}<div className="hint">Snapshot: {new Date(item.snapshotAt).toLocaleString('vi-VN')}</div></td>
                    <td>{item.isStale ? <span className="badge red">Không giao dịch trong nhiệm kỳ</span> : <span className="badge green">Có giao dịch</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Layout>
  );
}

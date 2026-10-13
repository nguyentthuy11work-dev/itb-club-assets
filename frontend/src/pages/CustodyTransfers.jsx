import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { transferService } from '../api/services';
import { useAuth } from '../context/AuthContext';
import { ROLES, useRole } from '../context/RoleContext';

const STATUS_LABELS = {
  PENDING_BOTH_CONFIRMATIONS: 'Chờ hai bên',
  PENDING_CURRENT_CONFIRMATION: 'Chờ người đang giữ',
  PENDING_TARGET_CONFIRMATION: 'Chờ người nhận',
  COMPLETED: 'Đã hoàn tất',
};

export default function CustodyTransfers() {
  const { user } = useAuth();
  const { role } = useRole();
  const [items, setItems] = useState([]);
  const [sourceCustodyId, setSourceCustodyId] = useState('');
  const [targetUserId, setTargetUserId] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [selected, setSelected] = useState(null);
  const [note, setNote] = useState('');
  const [photos, setPhotos] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const page = await transferService.list();
      setItems(page.items);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách Transfer.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function create(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await transferService.create({ sourceCustodyId, targetUserId, quantity });
      setSourceCustodyId('');
      setTargetUserId('');
      setQuantity(1);
      await load();
    } catch (err) {
      setError(err.message || 'Không thể tạo Transfer.');
    } finally {
      setBusy(false);
    }
  }

  async function confirm(event) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true);
    setError('');
    try {
      await transferService.confirm(selected.transfer.id, selected.side, { note, photos });
      setSelected(null);
      setNote('');
      setPhotos([]);
      await load();
    } catch (err) {
      setError(err.message || 'Không thể xác nhận Transfer.');
    } finally {
      setBusy(false);
    }
  }

  function availableSide(transfer) {
    if (transfer.status === 'COMPLETED') return null;
    if (transfer.currentCustodianUserId === user?.id && !transfer.currentConfirmedAt) return 'current';
    if (transfer.targetCustodianUserId === user?.id && !transfer.targetConfirmedAt) return 'target';
    return null;
  }

  return (
    <Layout title="Custody Transfer" subtitle="Custody chỉ đổi sau khi người đang giữ và người nhận đều xác nhận.">
      {error && <div className="error-box">{error}</div>}

      {role === ROLES.HR && (
        <form className="card" onSubmit={create} style={{ padding: 18, display: 'grid', gap: 12 }}>
          <strong>Tạo Transfer độc lập</strong>
          <div className="flex-row wrap">
            <label className="field flex-1">Source Custody ID
              <input required value={sourceCustodyId} onChange={(event) => setSourceCustodyId(event.target.value)} />
            </label>
            <label className="field flex-1">Target User ID
              <input required value={targetUserId} onChange={(event) => setTargetUserId(event.target.value)} />
            </label>
            <label className="field">Số lượng
              <input required type="number" min="1" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
            </label>
            <button className="btn primary" disabled={busy}>Tạo yêu cầu</button>
          </div>
        </form>
      )}

      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="table">
          <thead><tr><th>Transfer</th><th>Custody nguồn</th><th>Người nhận</th><th>SL</th><th>Xác nhận</th><th>Trạng thái</th><th /></tr></thead>
          <tbody>
            {items.map((transfer) => {
              const side = availableSide(transfer);
              return (
                <tr key={transfer.id}>
                  <td><strong>{transfer.id}</strong>{transfer.reconciliationItemId && <div className="hint">Recon: {transfer.reconciliationItemId}</div>}</td>
                  <td className="hint">{transfer.sourceCustodyId}<div>{transfer.currentCustodianUserId}</div></td>
                  <td className="hint">{transfer.targetCustodianUserId}</td>
                  <td>{transfer.quantity}</td>
                  <td>
                    <div className="hint">Bên giao: {transfer.currentConfirmedAt ? '✓' : '—'}</div>
                    <div className="hint">Bên nhận: {transfer.targetConfirmedAt ? '✓' : '—'}</div>
                  </td>
                  <td><span className={`badge ${transfer.status === 'COMPLETED' ? 'green' : ''}`}>{STATUS_LABELS[transfer.status]}</span></td>
                  <td>{side && <button className="btn small" onClick={() => setSelected({ transfer, side })}>Xác nhận</button>}</td>
                </tr>
              );
            })}
            {items.length === 0 && <tr><td colSpan={7} className="hint">Không có Transfer nào.</td></tr>}
          </tbody>
        </table>
      </div>

      {selected && (
        <form className="card" onSubmit={confirm} style={{ padding: 18, display: 'grid', gap: 12, maxWidth: 620 }}>
          <strong>Xác nhận phía {selected.side === 'current' ? 'người đang giữ' : 'người nhận'}</strong>
          <label>Ghi chú<input value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <label>Ảnh bằng chứng (1–3)
            <input required type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(event) => setPhotos(Array.from(event.target.files || []).slice(0, 3))} />
          </label>
          <div className="flex-row">
            <button className="btn primary" disabled={busy || photos.length < 1 || photos.length > 3}>Xác nhận</button>
            <button type="button" className="btn outline" onClick={() => setSelected(null)}>Hủy</button>
          </div>
        </form>
      )}
    </Layout>
  );
}

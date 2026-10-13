import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { reconciliationService } from '../api/services';

export default function MyReconciliation() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [actualQuantity, setActualQuantity] = useState(0);
  const [condition, setCondition] = useState('GOOD');
  const [conditionNote, setConditionNote] = useState('');
  const [discrepancyReason, setDiscrepancyReason] = useState('');
  const [photos, setPhotos] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const page = await reconciliationService.listMine({ pageSize: 100 });
      setItems(page.items);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách đối soát.');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function choose(item) {
    setSelected(item);
    setActualQuantity(item.expectedQuantity);
    setCondition(item.expectedCondition);
    setConditionNote('');
    setDiscrepancyReason('');
    setPhotos([]);
  }

  async function submit(event) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true);
    setError('');
    try {
      await reconciliationService.confirmItem(selected.id, {
        item: selected,
        actualQuantity,
        condition,
        conditionNote,
        discrepancyReason,
        photos,
      });
      setSelected(null);
      await load();
    } catch (err) {
      setError(err.message || 'Không thể xác nhận item.');
    } finally {
      setBusy(false);
    }
  }

  const mismatch = selected && (
    Number(actualQuantity) !== selected.expectedQuantity || condition !== selected.expectedCondition
  );

  return (
    <Layout title="Đối soát của tôi" subtitle="Kiểm kê từng Custody theo snapshot được giao cho bạn.">
      {error && <div className="error-box">{error}</div>}
      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="table">
          <thead><tr><th>Tài sản</th><th>Số lượng kỳ vọng</th><th>Tình trạng kỳ vọng</th><th>Trạng thái</th><th /></tr></thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td><strong>{item.assetName}</strong></td>
                <td>{item.expectedQuantity}</td>
                <td>{item.expectedCondition}</td>
                <td><span className={`badge ${item.status === 'CONFIRMED' ? 'green' : item.status === 'DISCREPANCY' ? 'red' : item.status === 'RESOLVED' ? 'gold' : ''}`}>{item.status}</span></td>
                <td>
                  <button className="btn small" disabled={item.status !== 'PENDING_CONFIRMATION'} onClick={() => choose(item)}>
                    Kiểm kê
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <form className="card" onSubmit={submit} style={{ padding: 18, display: 'grid', gap: 12, maxWidth: 620 }}>
          <strong>Xác nhận {selected.assetName}</strong>
          <label>Số lượng thực tế<input type="number" min="0" value={actualQuantity} onChange={(event) => setActualQuantity(event.target.value)} /></label>
          <label>Tình trạng
            <select value={condition} onChange={(event) => setCondition(event.target.value)}>
              <option value="GOOD">GOOD</option><option value="DAMAGED">DAMAGED</option><option value="LOST">LOST</option>
            </select>
          </label>
          <label>Ghi chú<input value={conditionNote} onChange={(event) => setConditionNote(event.target.value)} /></label>
          {mismatch && (
            <label>Lý do sai lệch<textarea required value={discrepancyReason} onChange={(event) => setDiscrepancyReason(event.target.value)} /></label>
          )}
          <label>Ảnh bằng chứng (1–3)<input required type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(event) => setPhotos(Array.from(event.target.files || []).slice(0, 3))} /></label>
          <div className="flex-row">
            <button className="btn primary" disabled={busy || photos.length < 1 || photos.length > 3}>Xác nhận</button>
            <button type="button" className="btn outline" onClick={() => setSelected(null)}>Hủy</button>
          </div>
        </form>
      )}
    </Layout>
  );
}

import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { reconciliationService } from '../api/services';
import { badgeClass, CONDITION_LABEL, label, RECONCILIATION_ITEM_STATUS } from '../components/labels';

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
      setError(err.message || 'Không thể xác nhận tài sản.');
    } finally {
      setBusy(false);
    }
  }

  const mismatch = selected && (
    Number(actualQuantity) !== selected.expectedQuantity || condition !== selected.expectedCondition
  );

  return (
    <Layout title="Đối soát của tôi" subtitle="Kiểm kê từng tài sản bạn đang giữ theo bản chốt của đợt đối soát.">
      {error && <div className="error-box">{error}</div>}
      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>Tài sản</th><th>Số lượng kỳ vọng</th><th>Tình trạng kỳ vọng</th><th>Trạng thái</th><th /></tr></thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td><strong>{item.assetName}</strong></td>
                <td>{item.expectedQuantity}</td>
                <td>{label(CONDITION_LABEL, item.expectedCondition)}</td>
                <td><span className={`badge ${badgeClass(RECONCILIATION_ITEM_STATUS, item.status)}`}>{label(RECONCILIATION_ITEM_STATUS, item.status)}</span></td>
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
        <form className="card recon-form" onSubmit={submit}>
          <strong className="recon-form-title">Kiểm kê: {selected.assetName}</strong>
          <div className="field">
            <label htmlFor="recon-actual">Số lượng thực tế *</label>
            <input id="recon-actual" type="number" min="0" required value={actualQuantity} onChange={(event) => setActualQuantity(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="recon-condition">Tình trạng *</label>
            <select id="recon-condition" value={condition} onChange={(event) => setCondition(event.target.value)}>
              {Object.entries(CONDITION_LABEL).map(([code, text]) => <option key={code} value={code}>{text}</option>)}
            </select>
          </div>
          <div className="field recon-form-wide">
            <label htmlFor="recon-note">Ghi chú</label>
            <input id="recon-note" value={conditionNote} onChange={(event) => setConditionNote(event.target.value)} />
          </div>
          {mismatch && (
            <div className="field recon-form-wide">
              <label htmlFor="recon-reason">Lý do sai lệch *</label>
              <textarea id="recon-reason" required value={discrepancyReason} onChange={(event) => setDiscrepancyReason(event.target.value)} />
            </div>
          )}
          <div className="field recon-form-wide">
            <label htmlFor="recon-photos">Ảnh bằng chứng (1–3 ảnh) *</label>
            <input id="recon-photos" required type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(event) => setPhotos(Array.from(event.target.files || []).slice(0, 3))} />
          </div>
          <div className="flex-row recon-form-wide">
            <button className="btn primary" disabled={busy || photos.length < 1 || photos.length > 3}>Xác nhận</button>
            <button type="button" className="btn outline" onClick={() => setSelected(null)}>Hủy</button>
          </div>
        </form>
      )}
    </Layout>
  );
}

import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { hrService } from '../api/services';

const SOURCES = ['Mua mới', 'Tài trợ', 'Quyên góp', 'Khác'];

let uid = 0;
function nextId() {
  uid += 1;
  return `new-${uid}`;
}

export default function AssetManagement() {
  const [assets, setAssets] = useState([]);
  const [error, setError] = useState('');

  const [name, setName] = useState('');
  const [qty, setQty] = useState('');
  const [responsibleTeamId, setResponsibleTeamId] = useState('');
  const [source, setSource] = useState(SOURCES[0]);
  const [allocations, setAllocations] = useState([{ id: nextId(), custodianName: '', qty: '' }]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const list = await hrService.listAssetsAdmin();
      setAssets(list);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách tài sản');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleResponsibleTeamChange(assetId, value) {
    const teamId = value || null;
    setAssets((prev) => prev.map((a) => (
      a.id === assetId ? { ...a, responsibleTeamId: teamId } : a
    )));
    try {
      await hrService.updateAssetResponsibleTeam(assetId, teamId);
    } catch (err) {
      setError(err.message || 'Cập nhật ban phụ trách thất bại');
      load();
    }
  }

  function addAllocationRow() {
    setAllocations((prev) => [...prev, { id: nextId(), custodianName: '', qty: '' }]);
  }
  function removeAllocationRow(id) {
    setAllocations((prev) => prev.filter((r) => r.id !== id));
  }
  function updateAllocationRow(id, patch) {
    setAllocations((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  const totalQty = Number(qty) || 0;
  const allocatedQty = allocations.reduce((s, r) => s + (Number(r.qty) || 0), 0);
  const allocationValid = allocations.every((r) => r.custodianName.trim() && Number(r.qty) > 0);
  const canSubmit = name.trim() && totalQty > 0 && allocationValid && allocatedQty === totalQty;

  async function handleSubmit() {
    setSubmitting(true);
    setSubmitError('');
    try {
      await hrService.createAsset({
        name: name.trim(),
        qty: totalQty,
        responsibleTeamId: responsibleTeamId || null,
        source,
        allocations: allocations.map((r) => ({ name: r.custodianName.trim(), qty: Number(r.qty) })),
      });
      setName('');
      setQty('');
      setAllocations([{ id: nextId(), custodianName: '', qty: '' }]);
      await load();
    } catch (err) {
      setSubmitError(err.message || 'Tạo tài sản thất bại, thử lại.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Layout title="Tài sản">
      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>Tài sản</th><th>Loại</th><th>SL</th><th>Khả dụng</th><th>Ban phụ trách</th><th>Người giữ</th></tr>
          </thead>
          <tbody>
            {assets.map((a) => (
              <tr key={a.id}>
                <td style={{ fontWeight: 600 }}>{a.name}</td>
                <td>{a.type}</td>
                <td>{a.total}</td>
                <td style={a.available === 0 ? { color: 'var(--red)' } : undefined}>{a.available}</td>
                <td>
                  <input
                    defaultValue={a.responsibleTeamId || ''}
                    placeholder="Mã ban phụ trách (có thể để trống)"
                    onBlur={(e) => handleResponsibleTeamChange(a.id, e.target.value.trim())}
                  />
                </td>
                <td>{(a.custodians || []).map((c) => c.name).join(', ') || '—'}</td>
              </tr>
            ))}
            {assets.length === 0 && !error && (
              <tr><td colSpan={6} className="loading-block">Đang tải...</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ font: '700 16px var(--font-display)' }}>Thêm tài sản mới</div>

        <div className="flex-row wrap">
          <div className="field" style={{ flex: 2, minWidth: 220 }}>
            <label>Tên tài sản *</label>
            <input placeholder="vd. Loa Bluetooth JBL" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field" style={{ width: 120 }}>
            <label>Số lượng *</label>
            <input type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} />
          </div>
          <div className="field" style={{ width: 200 }}>
            <label>Mã ban phụ trách</label>
            <input
              value={responsibleTeamId}
              placeholder="Có thể để trống"
              onChange={(e) => setResponsibleTeamId(e.target.value)}
            />
          </div>
          <div className="field" style={{ width: 180 }}>
            <label>Nguồn phát sinh *</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>

        <div className="field">
          <label>Phân bổ cho người giữ *</label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {allocations.map((row) => (
              <div key={row.id} className="flex-row">
                <input
                  placeholder="Tên người giữ"
                  value={row.custodianName}
                  onChange={(e) => updateAllocationRow(row.id, { custodianName: e.target.value })}
                  style={{ flex: 2, border: '1px solid var(--border-strong)', borderRadius: 8, padding: '8px 10px' }}
                />
                <input
                  type="number"
                  min={1}
                  placeholder="SL"
                  value={row.qty}
                  onChange={(e) => updateAllocationRow(row.id, { qty: e.target.value })}
                  style={{ width: 100, border: '1px solid var(--border-strong)', borderRadius: 8, padding: '8px 10px' }}
                />
                {allocations.length > 1 && (
                  <button type="button" className="btn small outline" onClick={() => removeAllocationRow(row.id)}>×</button>
                )}
              </div>
            ))}
          </div>
          <div className="flex-row" style={{ marginTop: 4 }}>
            <button type="button" className="btn link" onClick={addAllocationRow}>+ Thêm người giữ</button>
            <span className="hint" style={{ color: allocatedQty === totalQty && totalQty > 0 ? 'var(--green)' : 'var(--muted)' }}>
              Tổng phân bổ phải bằng số lượng đã nhập · {allocatedQty}/{totalQty || 0}
            </span>
          </div>
        </div>

        {submitError && <div className="error-box">{submitError}</div>}

        <div className="flex-row">
          <button className="btn primary" disabled={!canSubmit || submitting} onClick={handleSubmit}>
            {submitting ? 'Đang lưu...' : 'Lưu & gán người giữ'}
          </button>
          <span className="hint">Quyền giữ có hiệu lực ngay khi lưu; người giữ sẽ nhận thông báo để xác nhận.</span>
        </div>
      </div>
    </Layout>
  );
}

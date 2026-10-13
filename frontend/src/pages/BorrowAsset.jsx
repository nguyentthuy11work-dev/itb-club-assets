import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import { assetService, ticketService } from '../api/services';

const METHODS = [
  { value: 'TEMPORARY', label: 'Mượn tạm' },
  { value: 'CUSTODY_TRANSFER', label: 'Chuyển custodian' },
];

export default function BorrowAsset() {
  const navigate = useNavigate();
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [search, setSearch] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [teamId, setTeamId] = useState('');

  // { [assetId]: { expanded, rows: { [sourceCustodyId]: { checked, qty, method, dueDate } } } }
  const [selection, setSelection] = useState({});

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [result, setResult] = useState(null); // { tickets: [...] } sau khi tạo thành công

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    assetService
      .list({ search, categoryId, teamId })
      .then((page) => !cancelled && setAssets(page.items))
      .catch((err) => !cancelled && setError(err.message || 'Không tải được danh sách tài sản'))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [search, categoryId, teamId]);

  const categoryIds = useMemo(() => [...new Set(assets.map((a) => a.categoryId))], [assets]);
  const teamIds = useMemo(
    () => [...new Set(assets.map((a) => a.responsibleTeamId).filter(Boolean))],
    [assets],
  );

  function toggleAsset(asset) {
    setSelection((prev) => {
      const cur = prev[asset.id];
      if (cur?.expanded) {
        const { [asset.id]: _, ...rest } = prev;
        return rest;
      }
      const rows = {};
      asset.eligibleCustodies.forEach((custody) => {
        rows[custody.sourceCustodyId] = {
          checked: false,
          qty: 1,
          method: 'TEMPORARY',
          dueDate: '',
        };
      });
      return { ...prev, [asset.id]: { expanded: true, rows } };
    });
  }

  function updateRow(assetId, sourceCustodyId, patch) {
    setSelection((prev) => {
      const assetSel = prev[assetId];
      if (!assetSel) return prev;
      return {
        ...prev,
        [assetId]: {
          ...assetSel,
          rows: {
            ...assetSel.rows,
            [sourceCustodyId]: { ...assetSel.rows[sourceCustodyId], ...patch },
          },
        },
      };
    });
  }

  function clampQty(qty, max) {
    const n = Math.max(1, Math.floor(Number(qty) || 1));
    return Math.min(n, Math.max(1, max));
  }

  // BR-M01: nhập vượt số lượng đang giữ của người đó -> tự kéo về đúng số họ đang giữ
  function handleQtyChange(assetId, custody, value) {
    updateRow(assetId, custody.sourceCustodyId, {
      qty: clampQty(value, custody.lendableQuantity),
    });
  }

  function isAssetValid(asset) {
    const sel = selection[asset.id];
    if (!sel) return false;
    const checkedRows = Object.entries(sel.rows).filter(([, r]) => r.checked);
    if (checkedRows.length === 0) return false;
    return checkedRows.every(
      ([, r]) => (
        r.qty >= 1 && r.method && (r.method === 'CUSTODY_TRANSFER' || r.dueDate)
      ),
    );
  }

  const tickedAssets = assets.filter((a) => selection[a.id]?.expanded);
  const anyChecked = tickedAssets.some((a) =>
    Object.values(selection[a.id].rows).some((r) => r.checked)
  );
  const allValid = tickedAssets.length > 0 && tickedAssets.every(isAssetValid);
  const canSubmit = anyChecked && allValid;

  async function handleSubmit() {
    setSubmitting(true);
    setSubmitError('');
    try {
      const items = tickedAssets
        .map((asset) => {
          const sel = selection[asset.id];
          const allocations = Object.entries(sel.rows)
            .filter(([, r]) => r.checked)
            .map(([sourceCustodyId, r]) => ({
              sourceCustodyId,
              quantity: r.qty,
              method: r.method,
              dueDate: r.method === 'CUSTODY_TRANSFER' ? null : r.dueDate,
            }));
          return { assetId: asset.id, allocations };
        })
        .filter((item) => item.allocations.length > 0);

      const data = await ticketService.createBorrowTickets(items);
      setResult(data);
      setSelection({});
    } catch (err) {
      setSubmitError(err.message || 'Tạo yêu cầu thất bại, thử lại.');
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    return (
      <Layout title="Mượn tài sản">
        <div className="card" style={{ maxWidth: 560, padding: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ font: '700 22px var(--font-display)', color: '#2f7d32' }}>
            Đã tạo {result.createdTickets.length} Ticket
          </div>
          <div className="hint" style={{ marginTop: -6 }}>Mỗi người giữ nhận một thông báo cần duyệt</div>
          {result.createdTickets.map((t) => (
            <div key={t.id} className="flex-row" style={{ border: '1.5px solid #2a2a2a', borderRadius: 6, padding: '8px 11px', fontSize: 14 }}>
              <span className="flex-1">{t.id}</span>
              <span className="badge">Chờ duyệt</span>
            </div>
          ))}
          {result.rejectedGroups.length > 0 && (
            <div className="page-error">
              {result.rejectedGroups.length} nhóm không thể tạo:{' '}
              {result.rejectedGroups.map((group) => group.reasonCode).join(', ')}
            </div>
          )}
          <div className="flex-row" style={{ marginTop: 4 }}>
            <button className="btn primary" onClick={() => navigate('/ticket')}>Xem Ticket</button>
            <button className="btn outline" onClick={() => setResult(null)}>Tạo yêu cầu khác</button>
          </div>
        </div>
      </Layout>
    );
  }

  return (
    <Layout title="Mượn tài sản">
      <div className="hint">
        Tick tài sản ở đầu dòng, phần khai chi tiết sổ ra ngay dưới dòng đó. Không có modal, không có bước riêng.
      </div>

      <div className="flex-row wrap">
        <div className="field">
          <label>Tìm tài sản</label>
          <input placeholder="Nhập tên tài sản..." value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 240 }} />
        </div>
        <div className="field">
          <label>Category ID</label>
          <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)} style={{ width: 220 }}>
            <option value="">Tất cả</option>
            {categoryIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </div>
        <div className="field">
          <label>Responsible Team ID</label>
          <select value={teamId} onChange={(e) => setTeamId(e.target.value)} style={{ width: 220 }}>
            <option value="">Tất cả</option>
            {teamIds.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </div>
      </div>

      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 44 }}></th>
              <th>Tài sản</th>
              <th>Loại</th>
              <th>Khả dụng</th>
              <th>Đã chọn</th>
              <th style={{ width: 30 }}></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={6} className="loading-block">Đang tải tài sản...</td></tr>
            )}
            {!loading && assets.length === 0 && (
              <tr><td colSpan={6} className="hint" style={{ padding: 16 }}>Không tìm thấy tài sản phù hợp.</td></tr>
            )}
            {!loading && assets.map((asset) => {
              const sel = selection[asset.id];
              const disabled = asset.availableQuantity === 0;
              const checkedCount = sel ? Object.values(sel.rows).filter((r) => r.checked).reduce((s, r) => s + Number(r.qty || 0), 0) : 0;
              const checkedPeople = sel ? Object.values(sel.rows).filter((r) => r.checked).length : 0;
              const rowValid = sel ? isAssetValid(asset) : true;

              return (
                <FragmentRow
                  key={asset.id}
                  asset={asset}
                  disabled={disabled}
                  sel={sel}
                  rowValid={rowValid}
                  checkedCount={checkedCount}
                  checkedPeople={checkedPeople}
                  onToggle={() => !disabled && toggleAsset(asset)}
                  onRowChange={(sourceCustodyId, patch) => (
                    updateRow(asset.id, sourceCustodyId, patch)
                  )}
                  onQtyChange={(custody, value) => handleQtyChange(asset.id, custody, value)}
                />
              );
            })}
          </tbody>
        </table>
      </div>

      {submitError && <div className="page-error">{submitError}</div>}

      <div className="flex-row">
        <button className="btn primary" disabled={!canSubmit || submitting} onClick={handleSubmit}>
          {submitting ? 'Đang gửi...' : 'Gửi yêu cầu'}
        </button>
        <button className="btn outline" onClick={() => setSelection({})}>Xoá hết lựa chọn</button>
        <div className="hint flex-1">
          Active khi mọi tài sản đã tick đều có ≥1 người giữ + đủ SL / hình thức / ngày trả
        </div>
      </div>
    </Layout>
  );
}

function FragmentRow({ asset, disabled, sel, rowValid, checkedCount, checkedPeople, onToggle, onRowChange, onQtyChange }) {
  return (
    <>
      <tr className={sel?.expanded ? 'alt' : undefined} style={disabled ? { opacity: 0.45 } : undefined}>
        <td>
          <input type="checkbox" checked={!!sel?.expanded} disabled={disabled} onChange={onToggle} style={{ width: 16, height: 16 }} />
        </td>
        <td style={sel?.expanded ? { fontWeight: 600 } : undefined}>{asset.name}</td>
        <td>{asset.categoryId}</td>
        <td style={disabled ? { color: '#c0392b' } : undefined}>{disabled ? '0 — hết' : asset.availableQuantity}</td>
        <td style={{ color: checkedPeople ? undefined : '#bbb' }}>
          {checkedPeople ? `${checkedCount} · ${checkedPeople} người giữ` : disabled ? 'checkbox disable' : '—'}
        </td>
        <td>
          {!disabled && <button type="button" onClick={onToggle} style={{ border: 'none', background: 'none', cursor: 'pointer' }}>{sel?.expanded ? '^' : 'v'}</button>}
        </td>
      </tr>
      {sel?.expanded && (
        <tr className="alt">
          <td colSpan={6} style={{ padding: '4px 12px 13px 56px' }}>
            {asset.eligibleCustodies.length === 0 ? (
              <div className="hint" style={{ color: '#c0392b' }}>Chưa có người đang giữ tài sản này để mượn.</div>
            ) : (
              <>
                <div style={{ display: 'grid', gridTemplateColumns: '1.6fr .8fr 1.3fr 1.3fr', gap: 8, font: '600 12px var(--font-display)', color: '#666', borderBottom: '1px solid #ddd', paddingBottom: 4 }}>
                  <div>Người giữ</div><div>SL mượn</div><div>Hình thức</div><div>Ngày trả</div>
                </div>
                {asset.eligibleCustodies.map((custody) => {
                  const row = sel.rows[custody.sourceCustodyId];
                  return (
                    <div key={custody.sourceCustodyId} style={{ display: 'grid', gridTemplateColumns: '26px 1.6fr .8fr 1.3fr 1.3fr', gap: 8, alignItems: 'center', padding: '7px 0' }}>
                      <input
                        type="checkbox"
                        checked={row.checked}
                        onChange={(e) => onRowChange(custody.sourceCustodyId, { checked: e.target.checked })}
                        style={{ width: 16, height: 16 }}
                      />
                      <div title={custody.sourceCustodyId}>
                        Custody {custody.sourceCustodyId.slice(0, 8)}
                        <span style={{ color: '#888' }}> (cho mượn {custody.lendableQuantity})</span>
                      </div>
                      <input
                        type="number"
                        min={1}
                        max={custody.lendableQuantity}
                        value={row.qty}
                        disabled={!row.checked}
                        onChange={(e) => onQtyChange(custody, e.target.value)}
                        style={{ height: 30, border: '1.5px solid #2a2a2a', borderRadius: 5, padding: '0 8px', width: '100%' }}
                      />
                      <select
                        value={row.method}
                        disabled={!row.checked}
                        onChange={(e) => onRowChange(custody.sourceCustodyId, { method: e.target.value })}
                        style={{ height: 30, border: '1.5px solid #2a2a2a', borderRadius: 5 }}
                      >
                        {METHODS.map((method) => (
                          <option key={method.value} value={method.value}>{method.label}</option>
                        ))}
                      </select>
                      {row.method === 'CUSTODY_TRANSFER' ? (
                        <div className="hint" style={{ color: '#888' }}>ẩn ô ngày trả</div>
                      ) : (
                        <input
                          type="date"
                          value={row.dueDate}
                          disabled={!row.checked}
                          onChange={(e) => onRowChange(custody.sourceCustodyId, { dueDate: e.target.value })}
                          style={{ height: 30, border: '1.5px solid #2a2a2a', borderRadius: 5, padding: '0 8px' }}
                        />
                      )}
                    </div>
                  );
                })}
              </>
            )}
            {!rowValid && Object.values(sel.rows).some((r) => r.checked) && (
              <div className="hint" style={{ color: '#8a6400', marginTop: 6 }}>
                Đã tick tài sản nhưng còn thiếu SL / hình thức / ngày trả — nút Gửi yêu cầu vẫn disable.
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

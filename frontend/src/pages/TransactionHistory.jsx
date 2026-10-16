import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import Layout from '../components/Layout';
import { assetService, hrService } from '../api/services';

function toCsv(rows) {
  const header = ['Thời gian', 'Loại', 'SL', 'Từ', 'Đến', 'Ticket'];
  const lines = [header.join(',')].concat(
    rows.map((r) => [r.time, r.kind, r.qty, r.from, r.to, r.ticketId || ''].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','))
  );
  return lines.join('\n');
}

export default function TransactionHistory() {
  const [assets, setAssets] = useState([]);
  const [assetId, setAssetId] = useState('');
  const [from, setFrom] = useState('2026-08-01');
  const [to, setTo] = useState('2026-08-25');
  const [rows, setRows] = useState([]);
  const [error, setError] = useState('');

  useEffect(() => {
    assetService.list().then((page) => {
      setAssets(page.items);
      if (page.items.length) setAssetId(page.items[0].id);
    });
  }, []);

  const load = useCallback(async () => {
    if (!assetId) return;
    setError('');
    try {
      const list = await hrService.getTransactionHistory({ assetId, from, to });
      setRows(list);
    } catch (err) {
      setError(err.message || 'Không tải được lịch sử giao dịch');
    }
  }, [assetId, from, to]);

  useEffect(() => {
    load();
  }, [load]);

  function handleExport() {
    const csv = toCsv(rows);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `lich-su-giao-dich-${assetId}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Layout title="Lịch sử giao dịch" subtitle="Tra cứu theo từng tài sản. Đây cũng là căn cứ tính kỳ vọng khi đối soát.">
      <div className="flex-row wrap">
        <div className="field" style={{ width: 220 }}>
          <label>Tài sản</label>
          <select value={assetId} onChange={(e) => setAssetId(e.target.value)}>
            {assets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div className="field">
          <label>Từ ngày</label>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="field">
          <label>Đến ngày</label>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        <div className="flex-1" />
        <button className="btn outline" onClick={handleExport} disabled={rows.length === 0}>Xuất CSV</button>
      </div>

      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>Thời gian</th><th>Loại</th><th>SL</th><th>Từ</th><th>Đến</th><th>Ticket</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.time}</td>
                <td style={{ fontWeight: 600 }}>{r.kind}</td>
                <td>{r.qty}</td>
                <td>{r.from}</td>
                <td>{r.to}</td>
                <td>
                  {r.ticketId ? (
                    <Link to={`/ticket/${r.ticketId}`} style={{ color: 'var(--accent-dark)', fontWeight: 600 }}>{r.ticketId}</Link>
                  ) : '—'}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !error && (
              <tr><td colSpan={6} className="hint" style={{ padding: 16 }}>Không có giao dịch nào trong khoảng đã chọn.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Layout>
  );
}

import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { reconciliationService, transferService } from '../api/services';

const SESSION_KEY = 'ams_current_reconciliation_id';
const STATUS = {
  PENDING_CONFIRMATION: ['Chưa xác nhận', ''],
  CONFIRMED: ['Đã khớp', 'green'],
  DISCREPANCY: ['Sai lệch chưa xử lý', 'red'],
  RESOLVED: ['Sai lệch đã xử lý', 'gold'],
};

export default function Reconciliation() {
  const [session, setSession] = useState(null);
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10));
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(SESSION_KEY) || '');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async (id = sessionId) => {
    if (!id) return;
    setError('');
    try {
      const value = await reconciliationService.get(id);
      setSession(value);
      localStorage.setItem(SESSION_KEY, value.id);
      setSessionId(value.id);
    } catch (err) {
      setError(err.message || 'Không tải được đợt đối soát.');
    }
  }, [sessionId]);

  useEffect(() => { if (sessionId) load(sessionId); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(kind, action) {
    setBusy(kind);
    setError('');
    try {
      const result = await action();
      if (result?.items) {
        setSession(result);
        setSessionId(result.id);
        localStorage.setItem(SESSION_KEY, result.id);
      } else if (sessionId) {
        await load(sessionId);
      }
    } catch (err) {
      setError(err.message || 'Thao tác thất bại.');
    } finally {
      setBusy('');
    }
  }

  async function resolve(item) {
    const note = window.prompt('Nhập cách HR đã xử lý sai lệch:');
    if (!note?.trim()) return;
    await run(item.id, () => reconciliationService.resolveItem(item.id, note));
  }

  async function createTransfer(item) {
    const targetUserId = window.prompt('Nhập User ID của người nhận:');
    if (!targetUserId?.trim()) return;
    const quantity = window.prompt('Nhập số lượng cần chuyển:', String(item.expectedQuantity));
    if (quantity == null) return;
    await run(item.id, async () => {
      await transferService.createFromReconciliation(item.id, {
        sourceCustodyId: item.custodyId,
        targetUserId,
        quantity,
      });
      window.alert('Đã tạo Transfer. Sai lệch vẫn cần được HR xử lý riêng.');
    });
  }

  return (
    <Layout title="Reconciliation" subtitle="Snapshot Custody bất biến, xác nhận trung thực và close gate đầy đủ.">
      <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div className="flex-row wrap">
          <input type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          <button
            className="btn primary"
            disabled={!!busy}
            onClick={() => run('open', () => reconciliationService.open(date))}
          >
            Mở đợt mới
          </button>
          <input
            placeholder="Reconciliation ID"
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
            style={{ minWidth: 300 }}
          />
          <button className="btn outline" disabled={!sessionId || !!busy} onClick={() => load()}>
            Tải đợt
          </button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </div>

      {session && (
        <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div className="flex-row wrap">
            <strong>{session.id}</strong>
            <span className={`badge ${session.status === 'CLOSED' ? 'green' : ''}`}>{session.status}</span>
            <span className="hint">Ngày đối soát: {session.reconciliationDate}</span>
            <span className="hint">Ticket cần trả: {session.temporaryLoanTicketCount}</span>
            <div className="flex-1" />
            <button
              className="btn outline"
              disabled={session.status !== 'OPEN' || !!session.confirmationRequestedAt || !!busy}
              onClick={() => run('request', () => reconciliationService.requestConfirmation(session.id))}
            >
              Gửi yêu cầu xác nhận
            </button>
            <button
              className="btn primary"
              disabled={session.status !== 'OPEN' || session.unresolvedCount > 0 || !!busy}
              onClick={() => run('close', () => reconciliationService.close(session.id))}
            >
              Đóng đợt
            </button>
          </div>

          <div className={session.unresolvedCount ? 'notice-box danger' : 'notice-box'}>
            {session.unresolvedCount
              ? `Còn ${session.unresolvedCount} item chưa xác nhận hoặc sai lệch chưa xử lý.`
              : 'Tất cả item đã đạt trạng thái cho phép đóng đợt.'}
          </div>

          <table className="table">
            <thead>
              <tr><th>Tài sản</th><th>Custodian</th><th>Kỳ vọng</th><th>Thực tế</th><th>Trạng thái</th><th>Thao tác</th></tr>
            </thead>
            <tbody>
              {session.items.map((item) => {
                const [label, badge] = STATUS[item.status];
                return (
                  <tr key={item.id} className={item.status === 'DISCREPANCY' ? 'alt' : undefined}>
                    <td><strong>{item.assetName}</strong><div className="hint">{item.expectedCondition}</div></td>
                    <td className="hint">{item.custodianUserId}</td>
                    <td>{item.expectedQuantity}</td>
                    <td>{item.actualQuantity ?? '—'}</td>
                    <td><span className={`badge ${badge}`}>{label}</span></td>
                    <td>
                      {item.status === 'PENDING_CONFIRMATION' && (
                        <button className="btn small" disabled={!!busy} onClick={() => run(item.id, () => reconciliationService.remindItem(item.id))}>Nhắc lại</button>
                      )}
                      {item.status === 'DISCREPANCY' && (
                        <div className="flex-row wrap">
                          <button className="btn small" disabled={!!busy} onClick={() => resolve(item)}>Ghi nhận xử lý</button>
                          <button className="btn small outline" disabled={!!busy} onClick={() => createTransfer(item)}>Tạo Transfer</button>
                        </div>
                      )}
                      {item.status === 'RESOLVED' && <span className="hint">{item.resolutionNote}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Layout>
  );
}

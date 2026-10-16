import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { reconciliationService, transferService } from '../api/services';
import {
  badgeClass, CONDITION_LABEL, label, RECONCILIATION_ITEM_STATUS, RECONCILIATION_STATUS,
} from '../components/labels';

const SESSION_KEY = 'ams_current_reconciliation_id';

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
    const targetUserId = window.prompt('Nhập mã tài khoản của người nhận:');
    if (!targetUserId?.trim()) return;
    const quantity = window.prompt('Nhập số lượng cần chuyển:', String(item.expectedQuantity));
    if (quantity == null) return;
    await run(item.id, async () => {
      await transferService.createFromReconciliation(item.id, {
        sourceCustodyId: item.custodyId,
        targetUserId,
        quantity,
      });
      window.alert('Đã tạo yêu cầu chuyển quyền giữ. Sai lệch vẫn cần được HR xử lý riêng.');
    });
  }

  return (
    <Layout title="Đối soát" subtitle="Mở đợt kiểm kê, theo dõi xác nhận và xử lý sai lệch.">
      <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div className="field-row">
          <div className="field">
            <label htmlFor="recon-date">Ngày đối soát</label>
            <input id="recon-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          </div>
          <button
            className="btn primary"
            disabled={!!busy}
            onClick={() => run('open', () => reconciliationService.open(date))}
          >
            Mở đợt mới
          </button>
          <div className="field flex-1" style={{ minWidth: 260 }}>
            <label htmlFor="recon-id">Mã đợt đối soát</label>
            <input
              id="recon-id"
              placeholder="Dán mã đợt để mở lại"
              value={sessionId}
              onChange={(event) => setSessionId(event.target.value)}
            />
          </div>
          <button className="btn primary" disabled={!sessionId || !!busy} onClick={() => load()}>
            Tải đợt
          </button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </div>

      {session && (
        <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div className="flex-row wrap">
            <strong>{session.id}</strong>
            <span className={`badge ${badgeClass(RECONCILIATION_STATUS, session.status)}`}>{label(RECONCILIATION_STATUS, session.status)}</span>
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
              ? `Còn ${session.unresolvedCount} tài sản chưa xác nhận hoặc sai lệch chưa xử lý.`
              : 'Tất cả tài sản đã đạt trạng thái cho phép đóng đợt.'}
          </div>

          <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr><th>Tài sản</th><th>Người giữ</th><th>Kỳ vọng</th><th>Thực tế</th><th>Trạng thái</th><th>Thao tác</th></tr>
            </thead>
            <tbody>
              {session.items.map((item) => {
                return (
                  <tr key={item.id} className={item.status === 'DISCREPANCY' ? 'alt' : undefined}>
                    <td><strong>{item.assetName}</strong><div className="hint">{label(CONDITION_LABEL, item.expectedCondition)}</div></td>
                    <td className="hint">{item.custodianUserId}</td>
                    <td>{item.expectedQuantity}</td>
                    <td>{item.actualQuantity ?? '—'}</td>
                    <td><span className={`badge ${badgeClass(RECONCILIATION_ITEM_STATUS, item.status)}`}>{label(RECONCILIATION_ITEM_STATUS, item.status)}</span></td>
                    <td>
                      {item.status === 'PENDING_CONFIRMATION' && (
                        <button className="btn small" disabled={!!busy} onClick={() => run(item.id, () => reconciliationService.remindItem(item.id))}>Nhắc lại</button>
                      )}
                      {item.status === 'DISCREPANCY' && (
                        <div className="flex-row wrap">
                          <button className="btn small" disabled={!!busy} onClick={() => resolve(item)}>Ghi nhận xử lý</button>
                          <button className="btn small outline" disabled={!!busy} onClick={() => createTransfer(item)}>Tạo yêu cầu chuyển</button>
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
        </div>
      )}
    </Layout>
  );
}

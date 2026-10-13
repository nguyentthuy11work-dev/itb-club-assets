import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import { dashboardService, ticketService } from '../api/services';

export default function Dashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState(null);
  const [search, setSearch] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const d = await dashboardService.get();
      setData(d);
    } catch (err) {
      setError(err.message || 'Không tải được dashboard');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleTodoAction(todo) {
    if (todo.kind === 'overdue') {
      navigate(`/ticket/${todo.ticketId}`);
      return;
    }
    if (todo.kind === 'pending') {
      navigate(`/ticket/${todo.ticketId}`);
      return;
    }
    if (todo.kind === 'await_receive') {
      navigate(`/ticket/${todo.ticketId}`);
      return;
    }
    if (todo.kind === 'confirm') {
      navigate('/tai-san-toi-giu');
      return;
    }
  }

  if (error) {
    return (
      <Layout title="Dashboard">
        <div className="page-error">{error}</div>
      </Layout>
    );
  }

  if (!data) {
    return (
      <Layout title="Dashboard">
        <div className="loading-block">Đang tải dashboard...</div>
      </Layout>
    );
  }

  const filteredAssets = data.assets.filter((a) => !search || a.name.toLowerCase().includes(search.toLowerCase()));

  return (
    <Layout title={data.userName ? `Xin chào, ${data.userName}` : 'Dashboard'}>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Tôi đang mượn</div>
          <div className="stat-value">{data.stats.myBorrowCount}</div>
          {data.stats.overdueCount > 0 && <div className="stat-sub warn">{data.stats.overdueCount} quá hạn</div>}
        </div>
        <div className="stat-card">
          <div className="stat-label">Chờ tôi duyệt</div>
          <div className="stat-value">{data.stats.pendingApproval}</div>
          <div className="stat-sub">tài sản tôi giữ</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Chờ tôi nhận lại</div>
          <div className="stat-value">{data.stats.awaitingMyReceive}</div>
          <div className="stat-sub">đã có ảnh trả</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Tài sản tôi giữ</div>
          <div className="stat-value">{data.stats.heldTotal}</div>
          {data.stats.pendingConfirm > 0 && <div className="stat-sub gold">{data.stats.pendingConfirm} chờ xác nhận</div>}
        </div>
      </div>

      <div className="card">
        <div className="card-title-bar">Việc cần làm ({data.todos.length})</div>
        {data.todos.length === 0 ? (
          <div style={{ padding: 16 }} className="hint">Không có việc gì cần xử lý ngay bây giờ 🎉</div>
        ) : (
          data.todos.map((todo, i) => (
            <div
              key={`${todo.id}-${i}`}
              className="flex-row"
              style={{ padding: '9px 12px', borderBottom: i < data.todos.length - 1 ? '1px solid #eee' : 'none', fontSize: 14 }}
            >
              <span className={`badge ${todo.kind === 'overdue' ? 'red' : todo.kind === 'confirm' ? 'gold' : ''}`}>{todo.label}</span>
              <span className="flex-1">{todo.text}</span>
              {todo.action === 'approve_reject' ? (
                <span className="flex-row" style={{ gap: 6 }}>
                  <button className="btn small" onClick={() => navigate(`/ticket/${todo.ticketId}`)}>Duyệt</button>
                  <button className="btn small danger-outline" onClick={() => navigate(`/ticket/${todo.ticketId}`)}>Từ chối</button>
                </span>
              ) : (
                <button className="btn small primary" disabled={busyId === todo.id} onClick={() => handleTodoAction(todo)}>
                  {todo.action}
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <div className="flex-row wrap">
        <div className="field">
          <label>Tìm tài sản</label>
          <input placeholder="Nhập tên tài sản..." value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 220 }} />
        </div>
        <div className="hint flex-1">Bảng cập nhật real-time, thay cho màn "xem phân bổ theo ban" (FR-M06)</div>
        <button className="btn primary" onClick={() => navigate('/muon-tai-san')}>+ Mượn tài sản</button>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Tài sản</th>
              <th>Loại</th>
              <th>Tổng SL</th>
              <th>Khả dụng</th>
              <th>Ban giữ</th>
            </tr>
          </thead>
          <tbody>
            {filteredAssets.map((a) => (
              <tr key={a.id}>
                <td>{a.name}</td>
                <td>{a.type}</td>
                <td>{a.total}</td>
                <td style={a.available === 0 ? { color: '#c0392b' } : undefined}>{a.available}</td>
                <td>{a.department}</td>
              </tr>
            ))}
            {filteredAssets.length === 0 && (
              <tr><td colSpan={5} className="hint" style={{ padding: 16 }}>Không tìm thấy tài sản phù hợp.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Layout>
  );
}

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import { hrService } from '../api/services';

const KIND_STYLE = {
  overdue24h: 'gold',
  reconciliation: '',
  mismatch: 'red',
  transfer: '',
};

const KIND_ROUTE = {
  overdue24h: '/tai-san-toi-giu',
  reconciliation: '/doi-soat',
  mismatch: '/doi-soat',
  transfer: '/lich-su-giao-dich',
};

export default function HRDashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const d = await hrService.getDashboard();
      setData(d);
    } catch (err) {
      setError(err.message || 'Không tải được dashboard HR');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <Layout title="Tổng quan tài sản CLB">
        <div className="page-error">{error}</div>
      </Layout>
    );
  }
  if (!data) {
    return (
      <Layout title="Tổng quan tài sản CLB">
        <div className="loading-block">Đang tải...</div>
      </Layout>
    );
  }

  const { stats, todos } = data;

  return (
    <Layout title="Tổng quan tài sản CLB" subtitle="HR cũng mượn và cũng giữ tài sản — phần đó dùng đúng các màn của vai Thành viên.">
      <div className="flex-row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn primary" onClick={() => navigate('/tai-san')}>+ Thêm tài sản mới</button>
      </div>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Tổng tài sản</div>
          <div className="stat-value">{stats.totalAssets}</div>
          <div className="stat-sub">{stats.totalTypes} loại</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Đang cho mượn</div>
          <div className="stat-value">{stats.onLoan}</div>
          {stats.onLoanOverdue > 0 && <div className="stat-sub warn">{stats.onLoanOverdue} quá hạn</div>}
        </div>
        <div className="stat-card">
          <div className="stat-label">Chờ Custodian xác nhận</div>
          <div className="stat-value">{stats.awaitingCustodian}</div>
          {stats.awaitingOver24h > 0 && <div className="stat-sub gold">{stats.awaitingOver24h} quá 24h</div>}
        </div>
        <div className="stat-card">
          <div className="stat-label">Đợt đối soát mở</div>
          <div className="stat-value">{stats.openRecons}</div>
          <div className="stat-sub">{stats.reconDone}/{stats.reconTotal} đã xác nhận</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title-bar">Việc cần làm <span className="badge">{todos.length}</span></div>
        {todos.map((todo, i) => (
          <div
            key={todo.id}
            className="flex-row"
            style={{ padding: '11px 16px', borderBottom: i < todos.length - 1 ? '1px solid var(--border-hair)' : 'none' }}
          >
            <span className={`badge ${KIND_STYLE[todo.kind] || ''}`}>{todo.label}</span>
            <span className="flex-1">{todo.text}</span>
            <button
              className="btn small"
              disabled={busy === todo.id}
              onClick={() => navigate(KIND_ROUTE[todo.kind] || '/')}
            >
              {todo.action}
            </button>
          </div>
        ))}
      </div>
    </Layout>
  );
}

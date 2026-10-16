import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { HandGrabbing, HourglassMedium, Package, Scales } from '@phosphor-icons/react';
import Layout from '../components/Layout';
import { StatCard } from '../components/ui';
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
    <Layout title="Tổng quan tài sản CLB">
      <div className="flex-row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn primary" onClick={() => navigate('/tai-san')}>+ Thêm tài sản mới</button>
      </div>

      <div className="stat-grid">
        <StatCard icon={Package} tone="primary" label="Tổng tài sản" value={stats.totalAssets} sub={`${stats.totalTypes} mặt hàng`} />
        <StatCard
          icon={HandGrabbing}
          tone="info"
          label="Đang cho mượn"
          value={stats.onLoan}
          sub={stats.onLoanOverdue > 0 ? { text: `${stats.onLoanOverdue} quá hạn`, tone: 'red' } : undefined}
        />
        <StatCard
          icon={HourglassMedium}
          tone="navy"
          label="Chờ người giữ xác nhận"
          value={stats.awaitingCustodian}
          sub={stats.awaitingOver24h > 0 ? { text: `${stats.awaitingOver24h} quá 24h`, tone: 'gold' } : undefined}
        />
        <StatCard icon={Scales} tone="success" label="Đợt đối soát mở" value={stats.openRecons} sub={`${stats.reconDone}/${stats.reconTotal} đã xác nhận`} />
      </div>

      <div className="card">
        <div className="card-title-bar">Việc cần làm <span className="badge">{todos.length}</span></div>
        {todos.map((todo) => (
          <div
            key={todo.id}
            className="todo-row"
          >
            <span className={`badge ${KIND_STYLE[todo.kind] || ''}`}>{todo.label}</span>
            <span className="flex-1">{todo.text}</span>
            <button
              className="btn small"
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

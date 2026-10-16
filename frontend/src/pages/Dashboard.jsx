import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight, HandGrabbing, HourglassMedium, Package, Sparkle, Ticket, Tray } from '@phosphor-icons/react';
import Layout from '../components/Layout';
import StatusBadge from '../components/StatusBadge';
import { Illustration, ProportionBar, SegmentedBarChart, StatCard } from '../components/ui';
import { dashboardService, ticketService } from '../api/services';
import { useNavCounts } from '../context/NavCountsContext';

export default function Dashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [ticketRole, setTicketRole] = useState('borrower');
  const [recent, setRecent] = useState(null);

  const { applyDashboard } = useNavCounts();

  const load = useCallback(async () => {
    setError('');
    try {
      const d = await dashboardService.get();
      setData(d);
      applyDashboard(d);
    } catch (err) {
      setError(err.message || 'Không tải được dashboard');
    }
  }, [applyDashboard]);

  useEffect(() => {
    load();
  }, [load]);

  // Ticket gần đây: cùng API với trang Ticket, chỉ lấy vài dòng đầu.
  useEffect(() => {
    let cancelled = false;
    setRecent(null);
    ticketService
      .list({ role: ticketRole })
      .then((list) => !cancelled && setRecent(list.slice(0, 5)))
      .catch(() => !cancelled && setRecent([]));
    return () => {
      cancelled = true;
    };
  }, [ticketRole]);

  function handleTodoAction(todo) {
    if (['overdue', 'pending', 'await_receive'].includes(todo.kind)) navigate(`/ticket/${todo.ticketId}`);
    else if (todo.kind === 'confirm') navigate('/tai-san-toi-giu');
  }

  if (error) {
    return (
      <Layout title="Trang chủ">
        <div className="page-error">{error}</div>
      </Layout>
    );
  }

  if (!data) {
    return (
      <Layout title="Trang chủ">
        <div className="loading-block">Đang tải dashboard...</div>
      </Layout>
    );
  }

  const filteredAssets = data.assets.filter((a) => !search || a.name.toLowerCase().includes(search.toLowerCase()));
  const { stats, todos } = data;
  const totalUnits = data.assets.reduce((sum, a) => sum + a.total, 0);
  const availableUnits = data.assets.reduce((sum, a) => sum + a.available, 0);
  const outOfStock = data.assets.filter((a) => a.available === 0).length;
  const chartData = [...data.assets]
    .sort((a, b) => b.total - a.total)
    .slice(0, 6)
    .map((a) => ({ label: a.name, values: [a.total - a.available, a.available] }));

  return (
    <Layout
      title={data.userName ? `Chào mừng trở lại, ${data.userName}!` : 'Trang chủ'}
      subtitle="Tình hình mượn – trả tài sản của bạn hôm nay."
    >
      <section className="hero">
        <div className="hero-content">
          <span className="hero-pill">
            <Sparkle size={14} weight="fill" aria-hidden="true" />
            {todos.length ? `${todos.length} việc đang chờ bạn` : 'Mọi thứ đã xong'}
          </span>
          <h2>Mượn tài sản CLB chỉ trong vài bước</h2>
          <div className="hero-actions">
            <Link to="/muon-tai-san" className="btn">Mượn tài sản</Link>
            <Link to="/ticket" className="btn outline">Xem Ticket</Link>
          </div>
        </div>
        <Illustration name="hero" className="hero-art" />
      </section>

      <div className="grid-2">
        <div className="stat-grid dash-stats">
          <StatCard
            icon={HandGrabbing}
            tone="primary"
            label="Tôi đang mượn"
            value={stats.myBorrowCount}
            sub={stats.overdueCount > 0 ? { text: `${stats.overdueCount} quá hạn`, tone: 'red' } : undefined}
          />
          <StatCard icon={HourglassMedium} tone="info" label="Chờ tôi duyệt" value={stats.pendingApproval} sub="tài sản tôi giữ" />
          <StatCard icon={Tray} tone="navy" label="Chờ tôi nhận lại" value={stats.awaitingMyReceive} sub="đã có ảnh trả" />
          <StatCard
            icon={Package}
            tone="success"
            label="Tài sản tôi giữ"
            value={stats.heldTotal}
            sub={stats.pendingConfirm > 0 ? { text: `${stats.pendingConfirm} chờ xác nhận`, tone: 'gold' } : undefined}
          />
        </div>

        <div className="card chart-card">
          <div className="card-head">
            <h2>Đang mượn – khả dụng</h2>
            <div className="legend">
              <span className="legend-item"><span className="legend-dot" style={{ background: 'var(--primary)' }} />Đang mượn</span>
              <span className="legend-item"><span className="legend-dot" style={{ background: 'var(--navy)' }} />Khả dụng</span>
            </div>
          </div>
          <div className="card-pad">
            {chartData.length ? (
              <SegmentedBarChart
                label="Số lượng đang mượn và khả dụng theo tài sản"
                data={chartData}
                series={[{ name: 'Đang mượn', color: 'var(--primary)' }, { name: 'Khả dụng', color: 'var(--navy)' }]}
              />
            ) : (
              <div className="hint">Chưa có tài sản nào.</div>
            )}
          </div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h2>Tình trạng tài sản</h2>
            {outOfStock > 0 && <span className="badge red">{outOfStock} mặt hàng hết</span>}
          </div>
          <div className="card-pad">
            <div className="stat-label">Tổng số lượng</div>
            <div className="big-number" style={{ marginBottom: 16 }}>{totalUnits}</div>
            <ProportionBar
              segments={[
                { name: 'Khả dụng', value: availableUnits, color: 'var(--success)' },
                { name: 'Đang cho mượn', value: totalUnits - availableUnits, color: 'var(--primary)' },
              ]}
            />
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Ticket gần đây</h2>
            <div className="chip-row" role="group" aria-label="Lọc Ticket">
              <button type="button" className={`chip ${ticketRole === 'borrower' ? 'active' : ''}`} aria-pressed={ticketRole === 'borrower'} onClick={() => setTicketRole('borrower')}>Tôi mượn</button>
              <button type="button" className={`chip ${ticketRole === 'holder' ? 'active' : ''}`} aria-pressed={ticketRole === 'holder'} onClick={() => setTicketRole('holder')}>Tôi cho mượn</button>
            </div>
          </div>
          <div className="card-pad" style={{ paddingTop: 10 }}>
            {recent === null && <div className="hint">Đang tải...</div>}
            {recent?.length === 0 && <div className="hint">Chưa có Ticket nào.</div>}
            {recent?.map((t) => (
              <Link key={t.id} to={`/ticket/${t.id}`} className="ticket-row">
                <span className="icon-tile sm tone-primary" aria-hidden="true"><Ticket size={22} weight="duotone" /></span>
                <span className="flex-1">
                  <div className="ticket-title">{t.asset}</div>
                  <div className="ticket-meta">
                    {t.id} · {ticketRole === 'borrower' ? `Người giữ: ${t.holder}` : `Người mượn: ${t.borrower}`}
                    {t.dueDate ? ` · hạn ${t.dueDate}` : ''}
                  </div>
                </span>
                <StatusBadge status={t.status} overdue={t.overdue} dueSoon={t.dueSoon} />
              </Link>
            ))}
            <Link to="/ticket" className="btn link" style={{ marginTop: 6 }}>
              Xem tất cả <ArrowRight size={14} aria-hidden="true" />
            </Link>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-title-bar">Việc cần làm <span className="badge">{todos.length}</span></div>
        {todos.length === 0 ? (
          <div className="empty-state" style={{ border: 'none' }}>
            <Illustration name="empty" />
            <div className="empty-title">Không có việc gì cần xử lý</div>
          </div>
        ) : (
          todos.map((todo, i) => (
            <div key={`${todo.id}-${i}`} className="todo-row">
              <span className={`badge ${todo.kind === 'overdue' ? 'red' : todo.kind === 'confirm' ? 'gold' : ''}`}>{todo.label}</span>
              <span className="flex-1">{todo.text}</span>
              {todo.action === 'approve_reject' ? (
                <span className="flex-row" style={{ gap: 6 }}>
                  <button className="btn small" onClick={() => navigate(`/ticket/${todo.ticketId}`)}>Duyệt</button>
                  <button className="btn small danger-outline" onClick={() => navigate(`/ticket/${todo.ticketId}`)}>Từ chối</button>
                </span>
              ) : (
                <button className="btn small primary" onClick={() => handleTodoAction(todo)}>
                  {todo.action}
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <div className="card" style={{ overflowX: 'auto' }}>
        <div className="card-head" style={{ paddingBottom: 14 }}>
          <h2>Tài sản CLB</h2>
          <label htmlFor="dashboard-search" className="sr-only">Tìm tài sản</label>
          <input id="dashboard-search" type="search" placeholder="Tìm tên tài sản..." value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 240 }} />
        </div>
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
                <td>{a.available === 0 ? <span className="badge red">Hết</span> : a.available}</td>
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
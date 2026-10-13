import { NavLink, useNavigate } from 'react-router-dom';
import { useEffect, useState, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';
import { ROLES, useRole } from '../context/RoleContext';
import { dashboardService, hrService, shiftService } from '../api/services';

const ROLE_TABS = [
  { value: ROLES.MEMBER, label: 'Thành viên' },
  { value: ROLES.HR, label: 'HR' },
  { value: ROLES.SHIFT_LEAD, label: 'Trưởng ca' },
  { value: ROLES.TEAM_LEAD_ASSET, label: 'Teamlead tài sản' },
];

function buildNavItems(role) {
  const base = [
    { to: '/', label: 'Dashboard', end: true },
    { to: '/muon-tai-san', label: 'Mượn tài sản' },
    { to: '/ticket', label: 'Ticket', badgeKey: 'ticketCount' },
    { to: '/tai-san-toi-giu', label: 'Tài sản tôi giữ', badgeKey: 'heldTotal' },
    { to: '/doi-soat-cua-toi', label: 'Đối soát của tôi' },
    { to: '/chuyen-custody', label: 'Chuyển Custody' },
  ];

  if (role === ROLES.SHIFT_LEAD) {
    base.push({ to: '/ca-truc', label: 'Ca trực của tôi', badgeKey: 'openShifts' });
  }

  if (role === ROLES.TEAM_LEAD_ASSET) {
    base.push(
      { to: '/cau-hinh-su-kien', label: 'Cấu hình sự kiện' },
      { to: '/bao-cao-su-kien', label: 'Báo cáo sự kiện' },
    );
  }

  if (role === ROLES.HR) {
    base.push(
      { to: '/tai-san', label: 'Tài sản' },
      { to: '/lich-su-giao-dich', label: 'Lịch sử giao dịch' },
      { to: '/doi-soat', label: 'Reconciliation', badgeKey: 'openRecons' },
      { to: '/ban-giao-nhiem-ky', label: 'Bàn giao nhiệm kỳ' },
      { to: '/bao-cao-su-kien', label: 'Báo cáo sự kiện' },
    );
  }

  return base;
}

function roleTagForUser(role) {
  if (role === ROLES.HR) return 'HR · Thành viên';
  if (role === ROLES.SHIFT_LEAD) return 'Trưởng ca · Thành viên';
  if (role === ROLES.TEAM_LEAD_ASSET) return 'Teamlead tài sản · Thành viên';
  return 'Thành viên';
}

export default function Layout({ children, title, subtitle }) {
  const { user, logout } = useAuth();
  const { role, setRole } = useRole();
  const navigate = useNavigate();
  const [counts, setCounts] = useState({});

  const refreshCounts = useCallback(async () => {
    try {
      const dash = await dashboardService.get();
      const next = {
        ticketCount: dash.todos?.length || 0,
        heldTotal: dash.stats?.heldTotal || 0,
      };
      if (role === ROLES.HR) {
        const hr = await hrService.getDashboard();
        next.openRecons = hr.stats?.openRecons ?? 0;
      }
      if (role === ROLES.SHIFT_LEAD) {
        const page = await shiftService.list({ pageSize: 100 });
        next.openShifts = page.items.filter((shift) => shift.status !== 'CLOSED').length;
      }
      setCounts(next);
    } catch {
      // im lặng — badge không phải dữ liệu tối quan trọng
    }
  }, [role]);

  useEffect(() => {
    refreshCounts();
  }, [refreshCounts, role]);

  const navItems = buildNavItems(role);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand-row">
          <div className="brand-logo">b</div>
          <div>
            <div className="sidebar-brand">ITB Club Assets</div>
            <div className="sidebar-brand-sub">Quản lý tài sản CLB</div>
          </div>
        </div>

        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
          >
            <span className="flex-row" style={{ gap: 8 }}>
              <span className="nav-dot" />
              {item.label}
            </span>
            {item.badgeKey && !!counts[item.badgeKey] && (
              <span className={'nav-badge' + (item.danger ? ' danger' : '')}>{counts[item.badgeKey]}</span>
            )}
          </NavLink>
        ))}

        <div className="sidebar-foot">
          <div className="sidebar-notice">
            <div className="sidebar-notice-title">Nguyên tắc phân quyền</div>
            Menu render theo Role. Mục ngoài quyền được ẩn hẳn, không để trạng thái mở.
          </div>
          <div style={{ marginTop: 10 }}>
            <button
              className="btn outline"
              style={{ width: '100%' }}
              onClick={async () => {
                await logout();
                navigate('/dang-nhap');
              }}
            >
              Đăng xuất
            </button>
          </div>
        </div>
      </aside>

      <div className="main-area">
        <div className="topbar">
          <div className="topbar-left">
            <div className="role-switch">
              <span className="role-switch-label">VAI</span>
              {ROLE_TABS.map((tab) => (
                <button
                  key={tab.value}
                  className={'role-pill' + (role === tab.value ? ' active' : '')}
                  onClick={() => { setRole(tab.value); navigate('/'); }}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
          <div className="avatar-cluster">
            <div className="avatar-chip" title={user?.name}>
              {user?.name ? user.name.charAt(0).toUpperCase() : '?'}
            </div>
            <div className="user-meta">
              <div className="user-name">{user?.name}</div>
              <div className="user-role">{roleTagForUser(role)}</div>
            </div>
          </div>
        </div>
        <div style={{ padding: '18px 28px 0' }}>
          <h1 className="page-title">{title}</h1>
          {subtitle && <div className="page-sub">{subtitle}</div>}
        </div>
        <div className="page-body">{children}</div>
      </div>
    </div>
  );
}

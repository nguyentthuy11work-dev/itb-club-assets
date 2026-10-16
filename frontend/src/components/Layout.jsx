import { Link, NavLink, useNavigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import {
  ArrowsLeftRight, CalendarCheck, CaretDown, ChartBar, ClipboardText, ClockCounterClockwise,
  HandGrabbing, Handshake, House, List, ListChecks, Package, PresentationChart, Scales,
  Ticket, UserGear, X,
} from '@phosphor-icons/react';
import { hasRole, useAuth } from '../context/AuthContext';
import { useNavCounts } from '../context/NavCountsContext';

// Menu hiện mọi mục user có quyền, gom theo nhóm. Mục không có `roles` thì ai cũng thấy;
// nhóm không còn mục nào thì ẩn cả nhóm.
const NAV_GROUPS = [
  { title: '', items: [{ to: '/', label: 'Trang chủ', icon: House, end: true }] },
  {
    title: 'Cá nhân',
    items: [
      { to: '/muon-tai-san', label: 'Mượn tài sản', icon: HandGrabbing },
      { to: '/ticket', label: 'Ticket', icon: Ticket, badgeKey: 'ticketCount' },
      { to: '/tai-san-toi-giu', label: 'Tài sản tôi giữ', icon: Package, badgeKey: 'heldTotal' },
      { to: '/doi-soat-cua-toi', label: 'Đối soát của tôi', icon: ListChecks },
      { to: '/chuyen-custody', label: 'Chuyển quyền giữ', icon: ArrowsLeftRight },
    ],
  },
  {
    title: 'Ca trực',
    items: [{ to: '/ca-truc', label: 'Ca trực của tôi', icon: ClipboardText, badgeKey: 'openShifts', roles: ['SHIFT_LEAD'] }],
  },
  {
    title: 'Sự kiện',
    items: [
      { to: '/cau-hinh-su-kien', label: 'Cấu hình sự kiện', icon: CalendarCheck, roles: ['TEAM_LEAD_ASSET'] },
      { to: '/bao-cao-su-kien', label: 'Báo cáo sự kiện', icon: PresentationChart, roles: ['TEAM_LEAD_ASSET', 'HR'] },
    ],
  },
  {
    title: 'Quản lý',
    items: [
      { to: '/tong-quan', label: 'Tổng quan CLB', icon: ChartBar, roles: ['HR'] },
      { to: '/tai-san', label: 'Tài sản', icon: Package, roles: ['HR'] },
      { to: '/lich-su-giao-dich', label: 'Lịch sử giao dịch', icon: ClockCounterClockwise, roles: ['HR'] },
      { to: '/doi-soat', label: 'Đối soát', icon: Scales, badgeKey: 'openRecons', roles: ['HR'] },
      { to: '/ban-giao-nhiem-ky', label: 'Bàn giao nhiệm kỳ', icon: Handshake, roles: ['HR'] },
    ],
  },
  {
    title: 'Quản trị',
    items: [{ to: '/phan-quyen', label: 'Phân quyền', icon: UserGear, roles: ['SYSTEM_ADMIN', 'HR'] }],
  },
];

function visibleGroups(user) {
  return NAV_GROUPS
    .map((group) => ({ ...group, items: group.items.filter((item) => !item.roles || hasRole(user, ...item.roles)) }))
    .filter((group) => group.items.length > 0);
}

export default function Layout({ children, title, subtitle }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const { counts, refreshIfStale } = useNavCounts();

  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    refreshIfStale();
  }, [refreshIfStale]);

  // Drawer chỉ có tác dụng dưới 1024px; Esc để đóng.
  useEffect(() => {
    if (!drawerOpen) return undefined;
    const onKey = (e) => e.key === 'Escape' && setDrawerOpen(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drawerOpen]);

  return (
    <div className="app-shell">
      <div className={'drawer-backdrop' + (drawerOpen ? ' open' : '')} onClick={() => setDrawerOpen(false)} />
      <div className={'sidebar-frame' + (drawerOpen ? ' open' : '')} id="app-sidebar">
        <aside className="sidebar">
          <div className="flex-row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <Link to="/" className="sidebar-brand-row" aria-label="ITB Club — về trang chủ">
              <img className="brand-logo" src="/logo.svg" alt="" />
              <div>
                <div className="sidebar-brand">ITB Assets</div>
                <div className="sidebar-brand-sub">Quản lý tài sản CLB</div>
              </div>
            </Link>
            <button type="button" className="btn icon ghost menu-toggle" aria-label="Đóng menu" onClick={() => setDrawerOpen(false)}>
              <X size={20} />
            </button>
          </div>

          <nav aria-label="Điều hướng chính">
            {visibleGroups(user).map((group) => (
              <div key={group.title} className="nav-group">
                {group.title && <div className="nav-group-title">{group.title}</div>}
                {group.items.map(({ icon: Icon, ...item }) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    onClick={() => setDrawerOpen(false)}
                    className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
                  >
                    {({ isActive }) => (
                      <>
                        <Icon size={19} weight={isActive ? 'fill' : 'regular'} aria-hidden="true" />
                        <span className="nav-label">{item.label}</span>
                        {item.badgeKey && !!counts[item.badgeKey] && (
                          <span className="nav-badge">{counts[item.badgeKey]}</span>
                        )}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>

          <div className="sidebar-foot">
            <div className="sidebar-slogan">Yes, we&apos;re open</div>
            <div className="sidebar-brand-sub">Information Technology of Business</div>
          </div>
        </aside>
      </div>

      <div className="main-frame">
        <div className="main-area">
          <header className="topbar">
            <button
              type="button"
              className="btn icon menu-toggle"
              aria-label="Mở menu"
              aria-controls="app-sidebar"
              aria-expanded={drawerOpen}
              onClick={() => setDrawerOpen(true)}
            >
              <List size={20} />
            </button>
            <div className="topbar-title">
              <h1 className="page-title">{title}</h1>
              {subtitle && <div className="page-sub">{subtitle}</div>}
            </div>

            <div className="topbar-actions">
              <details className="user-menu">
                <summary aria-label={`Tài khoản: ${user?.name || ''}`}>
                  <span className="avatar-chip" aria-hidden="true">
                    {user?.name ? user.name.charAt(0).toUpperCase() : '?'}
                  </span>
                  <span className="user-meta">
                    <span className="user-name">{user?.name}</span>
                  </span>
                  <span className="user-menu-caret" aria-hidden="true"><CaretDown size={14} /></span>
                </summary>
                <div className="user-menu-panel">
                  <div className="user-menu-info">
                    <div className="user-name">{user?.name}</div>
                  </div>
                  <button
                    type="button"
                    className="btn outline"
                    onClick={async () => {
                      await logout();
                      navigate('/dang-nhap');
                    }}
                  >
                    Đăng xuất
                  </button>
                </div>
              </details>
            </div>
          </header>
          <main className="page-body">{children}</main>
        </div>
      </div>
    </div>
  );
}
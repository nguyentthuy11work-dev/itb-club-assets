import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { dashboardService, hrService, shiftService } from '../api/services';
import { hasRole, useAuth } from './AuthContext';

// Số đếm trên badge menu. Sống một lần cho cả phiên đăng nhập thay vì mỗi trang tự tải lại.
// ponytail: làm mới khi chuyển trang nếu cũ hơn 30s + sau mỗi thao tác; cần realtime thì thay bằng push/SSE.
const STALE_MS = 30_000;

const NavCountsContext = createContext(null);

function fromDashboard(dash) {
  return {
    // Chỉ việc cần làm gắn với Ticket — không tính xác nhận tài sản.
    ticketCount: dash.todos.filter((todo) => todo.ticketId).length,
    heldTotal: dash.stats.heldTotal,
  };
}

export function NavCountsProvider({ children }) {
  const { user } = useAuth();
  const [counts, setCounts] = useState({});
  const loadedAt = useRef(0);
  const inflight = useRef(null);
  const generation = useRef(0); // tăng khi đổi user — bỏ kết quả của phiên trước

  const refresh = useCallback(() => {
    if (!user) return Promise.resolve();
    if (inflight.current) return inflight.current;
    const gen = generation.current;
    inflight.current = Promise.allSettled([
      dashboardService.getStats(),
      hasRole(user, 'HR') ? hrService.getDashboard() : null,
      hasRole(user, 'SHIFT_LEAD') ? shiftService.list({ pageSize: 100 }) : null,
    ]).then(([dash, hr, shifts]) => {
      if (gen !== generation.current) return;
      // Badge không phải dữ liệu tối quan trọng: phần nào lỗi thì bỏ qua phần đó.
      const next = {};
      if (dash.status === 'fulfilled') Object.assign(next, fromDashboard(dash.value));
      if (hr.status === 'fulfilled' && hr.value) next.openRecons = hr.value.stats?.openRecons ?? 0;
      if (shifts.status === 'fulfilled' && shifts.value) {
        next.openShifts = shifts.value.items.filter((shift) => shift.status !== 'CLOSED').length;
      }
      setCounts(next);
      loadedAt.current = Date.now();
    }).finally(() => {
      if (gen === generation.current) inflight.current = null;
    });
    return inflight.current;
  }, [user]);

  useEffect(() => {
    generation.current += 1;
    inflight.current = null;
    setCounts({});
    loadedAt.current = 0;
    refresh();
  }, [refresh]);

  const refreshIfStale = useCallback(() => {
    if (Date.now() - loadedAt.current > STALE_MS) refresh();
  }, [refresh]);

  // Trang Dashboard đã tải sẵn dữ liệu này — dùng lại thay vì gọi API lần nữa.
  const applyDashboard = useCallback((dash) => {
    setCounts((prev) => ({ ...prev, ...fromDashboard(dash) }));
  }, []);

  return (
    <NavCountsContext.Provider value={{ counts, refresh, refreshIfStale, applyDashboard }}>
      {children}
    </NavCountsContext.Provider>
  );
}

export function useNavCounts() {
  const ctx = useContext(NavCountsContext);
  if (!ctx) throw new Error('useNavCounts phải dùng trong NavCountsProvider');
  return ctx;
}

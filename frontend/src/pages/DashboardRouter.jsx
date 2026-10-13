import { ROLES, useRole } from '../context/RoleContext';
import Dashboard from './Dashboard';
import HRDashboard from './HRDashboard';

// Dashboard là màn hình duy nhất có nội dung khác hẳn nhau giữa các vai:
// HR xem "Tổng quan tài sản CLB", còn Thành viên/Trưởng ca xem dashboard cá nhân
// (Trưởng ca dùng chung dashboard Thành viên, chỉ khác ở sidebar có thêm "Ca trực của tôi").
export default function DashboardRouter() {
  const { role } = useRole();
  if (role === ROLES.HR) return <HRDashboard />;
  return <Dashboard />;
}

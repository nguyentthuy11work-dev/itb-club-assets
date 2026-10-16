import { Navigate, useLocation } from 'react-router-dom';
import { hasRole, useAuth } from '../context/AuthContext';

// roles: nếu truyền, user phải có ít nhất một vai trong danh sách, không thì về Dashboard.
export default function RequireAuth({ children, roles }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) return <div className="loading-block">Đang tải...</div>;
  if (!user) return <Navigate to="/dang-nhap" state={{ from: location }} replace />;
  if (roles && !hasRole(user, ...roles)) return <Navigate to="/" replace />;
  return children;
}

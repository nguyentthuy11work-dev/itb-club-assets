import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export default function RequireAuth({ children }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) return <div className="loading-block">Đang tải...</div>;
  if (!user) return <Navigate to="/dang-nhap" state={{ from: location }} replace />;
  return children;
}

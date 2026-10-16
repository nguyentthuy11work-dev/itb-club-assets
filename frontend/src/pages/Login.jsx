import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await login(username, password);
      const redirectTo = location.state?.from?.pathname || '/';
      navigate(redirectTo, { replace: true });
    } catch (err) {
      // Form giữ nguyên dữ liệu đã nhập, chỉ hiện lỗi inline (BR-P01)
      setError(err.message || 'Sai tài khoản hoặc mật khẩu');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="login-frame">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <img src="/logo.svg" alt="" width="56" height="56" />
          <div>
            <div className="login-club">ITB CLUB</div>
            <div className="login-fullname">Information Technology of Business</div>
            <div className="login-slogan">Yes, we&apos;re open</div>
          </div>
        </div>
        <h1 className="login-title">Quản lý tài sản</h1>
        <div className="login-sub">Đăng nhập để tiếp tục</div>

        <div className="field">
          <label htmlFor="username">Tài khoản *</label>
          <input
            id="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">Mật khẩu *</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>

        {error && <div className="error-box">{error}</div>}

        <button type="submit" className="btn primary" disabled={submitting || !username.trim() || !password}>
          {submitting ? 'Đang đăng nhập...' : 'Đăng nhập'}
        </button>
      </form>
      </div>
    </div>
  );
}

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
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-title">Quản lý Tài sản CLB</h1>
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

        <button type="submit" className="btn primary" disabled={submitting}>
          {submitting ? 'Đang đăng nhập...' : 'Đăng nhập'}
        </button>

        <div className="hint" style={{ textAlign: 'center' }}>
          Menu sẽ hiển thị theo vai trò của bạn sau khi đăng nhập.
        </div>
        <div className="hint" style={{ textAlign: 'center', color: '#bbb' }}>
          Demo: tài khoản <b>baotran</b> · mật khẩu <b>123456</b>
        </div>
      </form>
    </div>
  );
}

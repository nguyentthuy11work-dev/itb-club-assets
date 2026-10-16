import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import { memberService } from '../api/services';
import { useAuth } from '../context/AuthContext';

const ROLE_COLUMNS = [
  { code: 'SYSTEM_ADMIN', label: 'Quản trị', elevated: true },
  { code: 'HR', label: 'HR', elevated: true },
  { code: 'TEAM_LEAD_ASSET', label: 'Teamlead tài sản' },
  { code: 'SHIFT_LEAD', label: 'Trưởng ca' },
  { code: 'TEAM_HEAD', label: 'Trưởng ban' },
];
const ELEVATED = ROLE_COLUMNS.filter((c) => c.elevated).map((c) => c.code);

const sameRoles = (a, b) => a.length === b.length && a.every((r) => b.includes(r));

export default function MemberRoles() {
  const { user } = useAuth();
  const isAdmin = user?.roles?.includes('SYSTEM_ADMIN');
  const [members, setMembers] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [error, setError] = useState('');
  const [savingId, setSavingId] = useState(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const page = await memberService.list();
      setMembers(page.items);
      setDrafts(Object.fromEntries(page.items.map((m) => [m.id, m.roles])));
    } catch (err) {
      setError(err.message || 'Không tải được danh sách thành viên');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Cùng luật với backend: không tự sửa mình; HR không đụng vai HR/Quản trị.
  function rowLocked(member) {
    if (member.id === user?.id) return 'Không thể tự sửa vai của mình';
    if (!isAdmin && member.roles.some((r) => ELEVATED.includes(r))) return 'Chỉ Quản trị sửa được thành viên này';
    return '';
  }

  function toggle(memberId, code) {
    setDrafts((prev) => {
      const roles = prev[memberId];
      return { ...prev, [memberId]: roles.includes(code) ? roles.filter((r) => r !== code) : [...roles, code] };
    });
  }

  async function save(member) {
    setSavingId(member.id);
    setError('');
    try {
      const updated = await memberService.replaceRoles(member.id, drafts[member.id]);
      setMembers((prev) => prev.map((m) => (m.id === member.id ? { ...m, roles: updated.roles } : m)));
      setDrafts((prev) => ({ ...prev, [member.id]: updated.roles }));
    } catch (err) {
      setError(err.message || 'Cập nhật vai thất bại');
    } finally {
      setSavingId(null);
    }
  }

  return (
    <Layout
      title="Phân quyền"
      subtitle={isAdmin
        ? 'Gán hoặc gỡ vai cho thành viên. Mọi thay đổi được lưu vết, không xoá lịch sử.'
        : 'HR chỉ gán được vai vận hành. Vai HR / Quản trị do Quản trị gán.'}
    >
      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Thành viên</th>
              {ROLE_COLUMNS.map((c) => <th key={c.code} style={{ textAlign: 'center' }}>{c.label}</th>)}
              <th />
            </tr>
          </thead>
          <tbody>
            {(members || []).map((m) => {
              const locked = rowLocked(m);
              const draft = drafts[m.id] || [];
              const dirty = !sameRoles(draft, m.roles);
              return (
                <tr key={m.id} title={locked || undefined}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{m.name}</div>
                    <div className="hint">{m.username}</div>
                  </td>
                  {ROLE_COLUMNS.map((c) => (
                    <td key={c.code} style={{ textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        aria-label={`${c.label} — ${m.name}`}
                        checked={draft.includes(c.code)}
                        disabled={!!locked || (c.elevated && !isAdmin) || savingId === m.id}
                        onChange={() => toggle(m.id, c.code)}
                      />
                    </td>
                  ))}
                  <td style={{ textAlign: 'right' }}>
                    {locked ? (
                      <span className="hint">{locked}</span>
                    ) : (
                      <button className="btn small primary" disabled={!dirty || savingId === m.id} onClick={() => save(m)}>
                        {savingId === m.id ? 'Đang lưu...' : 'Lưu'}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {!members && !error && (
              <tr><td colSpan={ROLE_COLUMNS.length + 2} className="loading-block">Đang tải...</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="hint">Máy chủ áp quyền mới ngay; menu của thành viên đó cập nhật khi họ tải lại trang.</div>
    </Layout>
  );
}

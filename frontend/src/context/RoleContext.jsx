import { createContext, useContext, useEffect, useState } from 'react';

// "Vai" ở đây là góc nhìn đang được xem trên UI (giống thanh "VAI" trong wireframe),
// KHÔNG phải hệ thống phân quyền thật. Phân quyền thật (được phép làm gì) vẫn phải
// do backend kiểm tra trên từng API — phần này chỉ quyết định sidebar/nội dung nào
// hiển thị ở frontend, để một user có nhiều role (vd Bảo Trân: HR + Thành viên)
// có thể chuyển qua lại xem đúng màn hình của từng vai.

export const ROLES = {
  MEMBER: 'MEMBER',
  HR: 'HR',
  SHIFT_LEAD: 'SHIFT_LEAD',
  TEAM_LEAD_ASSET: 'TEAM_LEAD_ASSET',
};

const ROLE_LABEL = {
  [ROLES.MEMBER]: 'Thành viên',
  [ROLES.HR]: 'HR',
  [ROLES.SHIFT_LEAD]: 'Trưởng ca',
  [ROLES.TEAM_LEAD_ASSET]: 'Teamlead tài sản',
};

const STORAGE_KEY = 'clb_assets_current_role';

const RoleContext = createContext(null);

export function RoleProvider({ children }) {
  const [role, setRole] = useState(() => localStorage.getItem(STORAGE_KEY) || ROLES.MEMBER);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, role);
  }, [role]);

  return (
    <RoleContext.Provider value={{ role, setRole, roleLabel: ROLE_LABEL[role] }}>
      {children}
    </RoleContext.Provider>
  );
}

export function useRole() {
  const ctx = useContext(RoleContext);
  if (!ctx) throw new Error('useRole phải dùng trong RoleProvider');
  return ctx;
}

export { ROLE_LABEL };

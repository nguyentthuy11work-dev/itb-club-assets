import { Routes, Route, Navigate } from 'react-router-dom';
import RequireAuth from './components/RequireAuth';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import HRDashboard from './pages/HRDashboard';
import BorrowAsset from './pages/BorrowAsset';
import Tickets from './pages/Tickets';
import TicketDetail from './pages/TicketDetail';
import MyHeldAssets from './pages/MyHeldAssets';
import AssetManagement from './pages/AssetManagement';
import TransactionHistory from './pages/TransactionHistory';
import Reconciliation from './pages/Reconciliation';
import MyReconciliation from './pages/MyReconciliation';
import ShiftList from './pages/ShiftList';
import ShiftDetail from './pages/ShiftDetail';
import ShiftClose from './pages/ShiftClose';
import EventDayConfiguration from './pages/EventDayConfiguration';
import EventDaySummary from './pages/EventDaySummary';
import CustodyTransfers from './pages/CustodyTransfers';
import Handover from './pages/Handover';
import MemberRoles from './pages/MemberRoles';

const HR = ['HR'];
const SHIFT_LEAD = ['SHIFT_LEAD'];
const TEAM_LEAD = ['TEAM_LEAD_ASSET'];
const EVENT_REPORT = ['TEAM_LEAD_ASSET', 'HR'];
const ROLE_ADMIN = ['SYSTEM_ADMIN', 'HR'];

export default function App() {
  return (
    <Routes>
      <Route path="/dang-nhap" element={<Login />} />

      <Route path="/" element={<RequireAuth><Dashboard /></RequireAuth>} />
      <Route path="/muon-tai-san" element={<RequireAuth><BorrowAsset /></RequireAuth>} />
      <Route path="/ticket" element={<RequireAuth><Tickets /></RequireAuth>} />
      <Route path="/ticket/:id" element={<RequireAuth><TicketDetail /></RequireAuth>} />
      <Route path="/tai-san-toi-giu" element={<RequireAuth><MyHeldAssets /></RequireAuth>} />
      <Route path="/doi-soat-cua-toi" element={<RequireAuth><MyReconciliation /></RequireAuth>} />
      <Route path="/chuyen-custody" element={<RequireAuth><CustodyTransfers /></RequireAuth>} />

      {/* Vai HR */}
      <Route path="/tong-quan" element={<RequireAuth roles={HR}><HRDashboard /></RequireAuth>} />
      <Route path="/tai-san" element={<RequireAuth roles={HR}><AssetManagement /></RequireAuth>} />
      <Route path="/lich-su-giao-dich" element={<RequireAuth roles={HR}><TransactionHistory /></RequireAuth>} />
      <Route path="/doi-soat" element={<RequireAuth roles={HR}><Reconciliation /></RequireAuth>} />
      <Route path="/ban-giao-nhiem-ky" element={<RequireAuth roles={HR}><Handover /></RequireAuth>} />

      {/* Vai Trưởng ca */}
      <Route path="/ca-truc" element={<RequireAuth roles={SHIFT_LEAD}><ShiftList /></RequireAuth>} />
      <Route path="/ca-truc/:id" element={<RequireAuth roles={SHIFT_LEAD}><ShiftDetail /></RequireAuth>} />
      <Route path="/ca-truc/:id/chot" element={<RequireAuth roles={SHIFT_LEAD}><ShiftClose /></RequireAuth>} />

      {/* Vai Teamlead tài sản */}
      <Route path="/cau-hinh-su-kien" element={<RequireAuth roles={TEAM_LEAD}><EventDayConfiguration /></RequireAuth>} />
      <Route path="/cau-hinh-su-kien/:eventDayId" element={<RequireAuth roles={TEAM_LEAD}><EventDayConfiguration /></RequireAuth>} />
      <Route path="/bao-cao-su-kien" element={<RequireAuth roles={EVENT_REPORT}><EventDaySummary /></RequireAuth>} />
      <Route path="/bao-cao-su-kien/:eventDayId" element={<RequireAuth roles={EVENT_REPORT}><EventDaySummary /></RequireAuth>} />

      {/* Quản trị phân quyền */}
      <Route path="/phan-quyen" element={<RequireAuth roles={ROLE_ADMIN}><MemberRoles /></RequireAuth>} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

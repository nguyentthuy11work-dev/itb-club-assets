import { Routes, Route, Navigate } from 'react-router-dom';
import RequireAuth from './components/RequireAuth';
import Login from './pages/Login';
import DashboardRouter from './pages/DashboardRouter';
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

export default function App() {
  return (
    <Routes>
      <Route path="/dang-nhap" element={<Login />} />

      <Route path="/" element={<RequireAuth><DashboardRouter /></RequireAuth>} />
      <Route path="/muon-tai-san" element={<RequireAuth><BorrowAsset /></RequireAuth>} />
      <Route path="/ticket" element={<RequireAuth><Tickets /></RequireAuth>} />
      <Route path="/ticket/:id" element={<RequireAuth><TicketDetail /></RequireAuth>} />
      <Route path="/tai-san-toi-giu" element={<RequireAuth><MyHeldAssets /></RequireAuth>} />
      <Route path="/doi-soat-cua-toi" element={<RequireAuth><MyReconciliation /></RequireAuth>} />
      <Route path="/chuyen-custody" element={<RequireAuth><CustodyTransfers /></RequireAuth>} />

      {/* Vai HR */}
      <Route path="/tai-san" element={<RequireAuth><AssetManagement /></RequireAuth>} />
      <Route path="/lich-su-giao-dich" element={<RequireAuth><TransactionHistory /></RequireAuth>} />
      <Route path="/doi-soat" element={<RequireAuth><Reconciliation /></RequireAuth>} />
      <Route path="/ban-giao-nhiem-ky" element={<RequireAuth><Handover /></RequireAuth>} />

      {/* Vai Trưởng ca */}
      <Route path="/ca-truc" element={<RequireAuth><ShiftList /></RequireAuth>} />
      <Route path="/ca-truc/:id" element={<RequireAuth><ShiftDetail /></RequireAuth>} />
      <Route path="/ca-truc/:id/chot" element={<RequireAuth><ShiftClose /></RequireAuth>} />

      {/* Vai Teamlead tài sản */}
      <Route path="/cau-hinh-su-kien" element={<RequireAuth><EventDayConfiguration /></RequireAuth>} />
      <Route path="/cau-hinh-su-kien/:eventDayId" element={<RequireAuth><EventDayConfiguration /></RequireAuth>} />
      <Route path="/bao-cao-su-kien" element={<RequireAuth><EventDaySummary /></RequireAuth>} />
      <Route path="/bao-cao-su-kien/:eventDayId" element={<RequireAuth><EventDaySummary /></RequireAuth>} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

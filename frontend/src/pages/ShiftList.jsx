import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import { shiftService } from '../api/services';

const STATUS_META = {
  SCHEDULED: { text: 'Đã lên lịch', cls: '' },
  OPENING: { text: 'Chờ xác nhận nhận', cls: 'gold' },
  ACTIVE: { text: 'Đang trực', cls: 'green' },
  ACTIVE_WITH_DEVIATION: { text: 'Đang trực · có lệch', cls: 'red' },
  CLOSED: { text: 'Đã chốt', cls: 'dark' },
};

function shiftTime(shift) {
  const format = new Intl.DateTimeFormat('vi-VN', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  });
  return `${format.format(new Date(shift.startsAt))} – ${format.format(new Date(shift.endsAt))}`;
}

export default function ShiftList() {
  const navigate = useNavigate();
  const [page, setPage] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      setPage(await shiftService.list());
    } catch (err) {
      setError(err.message || 'Không tải được danh sách ca');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <Layout title="Ca trực của tôi" subtitle="Danh sách chỉ gồm các ca được phân công cho bạn">
      <div className="notice-box">
        Mở ca chuyển sang bước kiểm tra tài sản. Ca kế tiếp chỉ mở được sau khi ca liền trước đã chốt.
      </div>
      {error && <div className="page-error">{error}</div>}

      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>Ca</th><th>Lịch</th><th>Sự kiện</th><th>Trạng thái</th><th>Thao tác</th></tr>
          </thead>
          <tbody>
            {!page && !error && <tr><td colSpan={5} className="loading-block">Đang tải…</td></tr>}
            {page?.items.map((shift) => {
              const meta = STATUS_META[shift.status];
              const closing = ['ACTIVE', 'ACTIVE_WITH_DEVIATION', 'CLOSED'].includes(shift.status);
              return (
                <tr key={shift.id} className={shift.status.startsWith('ACTIVE') ? 'alt' : undefined}>
                  <td style={{ fontWeight: 700 }}>Ca {shift.sequence} · {shift.name}</td>
                  <td>{shiftTime(shift)}</td>
                  <td>{shift.eventDayName}</td>
                  <td><span className={`badge ${meta.cls}`}>{meta.text}</span></td>
                  <td>
                    <button
                      className={closing && shift.status !== 'CLOSED' ? 'btn small primary' : 'btn small outline'}
                      onClick={() => navigate(closing ? `/ca-truc/${shift.id}/chot` : `/ca-truc/${shift.id}`)}
                    >
                      {shift.status === 'SCHEDULED' ? 'Mở ca' : shift.status === 'OPENING' ? 'Xác nhận nhận' : shift.status === 'CLOSED' ? 'Xem kết quả' : 'Chốt ca'}
                    </button>
                  </td>
                </tr>
              );
            })}
            {page?.items.length === 0 && (
              <tr><td colSpan={5} className="loading-block">Bạn chưa được phân công ca nào.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Layout>
  );
}

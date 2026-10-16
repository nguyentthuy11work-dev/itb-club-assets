import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import StatusBadge from '../components/StatusBadge';
import PhotoEvidenceModal from '../components/PhotoEvidenceModal';
import { assetService } from '../api/services';
import { useNavCounts } from '../context/NavCountsContext';

export default function MyHeldAssets() {
  const [assets, setAssets] = useState(null);
  const [error, setError] = useState('');
  const [confirmTarget, setConfirmTarget] = useState(null);
  const { refresh: refreshCounts } = useNavCounts();

  const load = useCallback(async () => {
    setError('');
    try {
      const list = await assetService.listHeld();
      setAssets(list);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách tài sản đang giữ');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleConfirm(payload) {
    await assetService.confirmHeld(confirmTarget.id, payload);
    setConfirmTarget(null);
    load();
    refreshCounts();
  }

  return (
    <Layout title="Tài sản tôi giữ">
      {error && <div className="page-error">{error}</div>}

      {assets === null ? (
        <div className="loading-block">Đang tải...</div>
      ) : assets.length === 0 ? (
        <div className="empty-state">
          <div className="empty-title">Bạn chưa giữ tài sản nào</div>
          <div className="empty-desc">Khi HR gán tài sản cho bạn, mục này sẽ xuất hiện kèm thông báo.</div>
        </div>
      ) : (
        <>
          <div className="card" style={{ overflowX: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Tài sản</th>
                  <th>Tôi giữ</th>
                  <th>Cho mượn</th>
                  <th>Trạng thái</th>
                  <th>Việc của tôi</th>
                </tr>
              </thead>
              <tbody>
                {assets.map((a) => (
                  <tr key={a.id}>
                    <td>{a.name}</td>
                    <td>{a.holding}</td>
                    <td>{a.lent}</td>
                    <td><StatusBadge status={a.status} /></td>
                    <td>
                      {a.status === 'CONFIRMED' ? (
                        <span className="hint">không có</span>
                      ) : (
                        <button className="btn small primary" onClick={() => setConfirmTarget(a)}>Xác nhận đã nhận</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="hint">
            Tài sản HR gán thuộc quyền giữ của bạn ngay; hãy xác nhận trong 24h để ghi nhận.
          </div>
        </>
      )}

      {confirmTarget && (
        <PhotoEvidenceModal
          title={`Xác nhận đã nhận — ${confirmTarget.name}`}
          onCancel={() => setConfirmTarget(null)}
          onSubmit={handleConfirm}
        />
      )}
    </Layout>
  );
}

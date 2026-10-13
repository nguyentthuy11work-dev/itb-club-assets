import { useCallback, useEffect, useState } from 'react';
import Layout from '../components/Layout';
import StatusBadge from '../components/StatusBadge';
import PhotoEvidenceModal from '../components/PhotoEvidenceModal';
import { assetService } from '../api/services';

export default function MyHeldAssets() {
  const [assets, setAssets] = useState(null);
  const [error, setError] = useState('');
  const [confirmTarget, setConfirmTarget] = useState(null);

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
  }

  return (
    <Layout title="Tài sản tôi giữ">
      {error && <div className="page-error">{error}</div>}

      {assets === null ? (
        <div className="loading-block">Đang tải...</div>
      ) : assets.length === 0 ? (
        <div className="empty-state">
          <div className="empty-title">Trạng thái rỗng</div>
          <div className="empty-desc">Bạn chưa giữ tài sản nào. Khi HR gán tài sản cho bạn, mục này sẽ xuất hiện kèm thông báo.</div>
        </div>
      ) : (
        <>
          <div className="card" style={{ overflow: 'hidden' }}>
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
                        <span className="hint" style={{ color: '#888' }}>không có</span>
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
            Badge vàng "Quá hạn xác nhận" hiện sau 24h; hệ thống nhắc lại cả HR và bạn (UR-B05). Custody đã có hiệu lực ngay khi HR gán, xác nhận chỉ để ghi nhận (BR-R01).
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

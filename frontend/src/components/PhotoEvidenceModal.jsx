import { useRef, useState } from 'react';

/**
 * Modal ảnh minh chứng dùng chung cho 3 hành động: xác nhận đã nhận,
 * báo đã trả, xác nhận đã nhận lại. Bắt buộc tối thiểu 1 ảnh (BR-M03/M07/M08).
 *
 * props:
 *  - title: tiêu đề modal, ví dụ "Xác nhận đã nhận — TK-0141"
 *  - onCancel(): đóng modal
 *  - onSubmit({ photos, note }): gọi khi bấm Xác nhận, cần async và tự xử lý lỗi
 */
export default function PhotoEvidenceModal({ title, onCancel, onSubmit }) {
  const [photos, setPhotos] = useState([]);
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef(null);

  function addFiles(fileList) {
    const files = Array.from(fileList || []).map((file) => ({
      file,
      url: URL.createObjectURL(file),
      id: `${file.name}-${file.size}-${Math.random()}`,
    }));
    setPhotos((prev) => [...prev, ...files]);
  }

  function removePhoto(id) {
    setPhotos((prev) => prev.filter((p) => p.id !== id));
  }

  async function handleSubmit() {
    if (photos.length === 0) return;
    setSubmitting(true);
    setError('');
    try {
      await onSubmit({ photos: photos.map((p) => p.file), note });
    } catch (err) {
      setError(err.message || 'Có lỗi xảy ra, thử lại.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="modal-panel">
        <h2 className="modal-title">{title}</h2>
        <div className="hint" style={{ marginTop: -6 }}>Bắt buộc tối thiểu 1 ảnh minh chứng</div>

        <label className="dropzone" htmlFor="photo-input">
          Kéo ảnh vào đây hoặc chọn tệp
          <input
            id="photo-input"
            ref={inputRef}
            type="file"
            accept="image/*"
            multiple
            onChange={(e) => addFiles(e.target.files)}
          />
        </label>

        {photos.length > 0 && (
          <div className="photo-thumb-row">
            {photos.map((p) => (
              <div key={p.id} className="photo-thumb" style={{ backgroundImage: `url(${p.url})` }}>
                <button type="button" aria-label="Xoá ảnh" onClick={() => removePhoto(p.id)}>×</button>
              </div>
            ))}
            <button type="button" className="photo-add" onClick={() => inputRef.current?.click()}>+</button>
          </div>
        )}

        <div className="field">
          <label htmlFor="photo-note">Ghi chú (không bắt buộc)</label>
          <textarea id="photo-note" value={note} onChange={(e) => setNote(e.target.value)} />
        </div>

        {error && <div className="error-box">{error}</div>}

        <div className="flex-row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn outline" onClick={onCancel} disabled={submitting}>Huỷ</button>
          <button type="button" className="btn primary" onClick={handleSubmit} disabled={photos.length === 0 || submitting}>
            {submitting ? 'Đang gửi...' : 'Xác nhận'}
          </button>
        </div>
        <div className="hint" style={{ color: '#999' }}>
          Nút Xác nhận disable đến khi có ≥1 ảnh. Dùng chung cho "Đã trả" và "Xác nhận đã nhận lại", chỉ đổi tiêu đề.
        </div>
      </div>
    </div>
  );
}

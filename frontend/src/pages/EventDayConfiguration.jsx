import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { assetService, eventDayService } from '../api/services';
import Layout from '../components/Layout';

let rowSequence = 0;

function rowKey(prefix) {
  rowSequence += 1;
  return `${prefix}-${rowSequence}`;
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function localDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function newAsset() {
  return { key: rowKey('asset'), assetId: '', quotaQuantity: 0 };
}

function newShift(eventDate = today()) {
  return {
    key: rowKey('shift'),
    name: '',
    startsAt: `${eventDate}T08:00`,
    endsAt: `${eventDate}T12:00`,
    shiftLeadUserId: '',
    assets: [newAsset()],
  };
}

function emptyDraft() {
  const eventDate = today();
  return { name: '', eventDate, shifts: [newShift(eventDate)] };
}

function editableEventDay(eventDay) {
  return {
    name: eventDay.name,
    eventDate: eventDay.eventDate,
    shifts: eventDay.shifts.map((shift) => ({
      id: shift.id,
      key: rowKey('shift'),
      name: shift.name,
      startsAt: localDateTime(shift.startsAt),
      endsAt: localDateTime(shift.endsAt),
      shiftLeadUserId: shift.shiftLeadUserId,
      assets: shift.assets.map((asset) => ({
        key: rowKey('asset'),
        assetId: asset.assetId,
        quotaQuantity: asset.quotaQuantity,
      })),
    })),
  };
}

export default function EventDayConfiguration() {
  const { eventDayId } = useParams();
  const navigate = useNavigate();
  const [draft, setDraft] = useState(emptyDraft);
  const [savedEvent, setSavedEvent] = useState(null);
  const [assets, setAssets] = useState(null);
  const [lookupId, setLookupId] = useState(eventDayId || '');
  const [loading, setLoading] = useState(Boolean(eventDayId));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const loadEvent = useCallback(async (id) => {
    if (!id) return;
    setLoading(true);
    setError('');
    setSuccess('');
    try {
      const eventDay = await eventDayService.get(id);
      setSavedEvent(eventDay);
      setDraft(editableEventDay(eventDay));
      setLookupId(eventDay.id);
    } catch (err) {
      setError(err.message || 'Không tải được cấu hình sự kiện');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    assetService.list({ pageSize: 100 })
      .then((page) => setAssets(page.items))
      .catch((err) => setError(err.message || 'Không tải được danh sách tài sản'));
  }, []);

  useEffect(() => {
    if (eventDayId) loadEvent(eventDayId);
  }, [eventDayId, loadEvent]);

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  function updateShift(index, field, value) {
    setDraft((current) => ({
      ...current,
      shifts: current.shifts.map((shift, shiftIndex) => (
        shiftIndex === index ? { ...shift, [field]: value } : shift
      )),
    }));
  }

  function addShift() {
    setDraft((current) => ({
      ...current,
      shifts: [...current.shifts, newShift(current.eventDate)],
    }));
  }

  function removeShift(index) {
    setDraft((current) => ({
      ...current,
      shifts: current.shifts.filter((_, shiftIndex) => shiftIndex !== index),
    }));
  }

  function moveShift(index, direction) {
    setDraft((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.shifts.length) return current;
      const shifts = [...current.shifts];
      [shifts[index], shifts[target]] = [shifts[target], shifts[index]];
      return { ...current, shifts };
    });
  }

  function updateAsset(shiftIndex, assetIndex, field, value) {
    setDraft((current) => ({
      ...current,
      shifts: current.shifts.map((shift, currentShiftIndex) => {
        if (currentShiftIndex !== shiftIndex) return shift;
        return {
          ...shift,
          assets: shift.assets.map((asset, currentAssetIndex) => (
            currentAssetIndex === assetIndex ? { ...asset, [field]: value } : asset
          )),
        };
      }),
    }));
  }

  function addAsset(shiftIndex) {
    setDraft((current) => ({
      ...current,
      shifts: current.shifts.map((shift, index) => (
        index === shiftIndex ? { ...shift, assets: [...shift.assets, newAsset()] } : shift
      )),
    }));
  }

  function removeAsset(shiftIndex, assetIndex) {
    setDraft((current) => ({
      ...current,
      shifts: current.shifts.map((shift, index) => (
        index === shiftIndex
          ? { ...shift, assets: shift.assets.filter((_, rowIndex) => rowIndex !== assetIndex) }
          : shift
      )),
    }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    setSuccess('');
    try {
      const result = savedEvent
        ? await eventDayService.update(savedEvent.id, draft)
        : await eventDayService.create(draft);
      setSavedEvent(result);
      setDraft(editableEventDay(result));
      setLookupId(result.id);
      setSuccess(savedEvent ? 'Đã cập nhật lịch và đồng bộ các nhắc việc chưa gửi.' : 'Đã tạo ngày sự kiện.');
      navigate(`/cau-hinh-su-kien/${result.id}`, { replace: true });
    } catch (err) {
      setError(err.message || 'Không lưu được cấu hình sự kiện');
    } finally {
      setSubmitting(false);
    }
  }

  function startNew() {
    setSavedEvent(null);
    setDraft(emptyDraft());
    setLookupId('');
    setError('');
    setSuccess('');
    navigate('/cau-hinh-su-kien');
  }

  return (
    <Layout
      title="Cấu hình ngày sự kiện"
      subtitle="Tạo lịch ca, phân công Trưởng ca và đặt quota tài sản"
    >
      <div className="notice-box">
        Event Mode chỉ lưu định mức vận hành, không thay đổi Total Quantity hoặc Available Quantity của tài sản.
      </div>

      <div className="event-lookup card">
        <div className="field event-lookup-input">
          <label htmlFor="event-day-id">Mở cấu hình bằng Event Day ID</label>
          <input
            id="event-day-id"
            value={lookupId}
            onChange={(event) => setLookupId(event.target.value)}
            placeholder="UUID của ngày sự kiện"
          />
        </div>
        <button
          className="btn outline"
          type="button"
          disabled={!lookupId.trim() || loading}
          onClick={() => navigate(`/cau-hinh-su-kien/${lookupId.trim()}`)}
        >
          {loading ? 'Đang tải…' : 'Mở'}
        </button>
        <button className="btn link" type="button" onClick={startNew}>Tạo mới</button>
      </div>

      {error && <div className="page-error">{error}</div>}
      {success && <div className="notice-box event-success">{success}</div>}

      {loading ? <div className="loading-block">Đang tải cấu hình…</div> : (
        <form className="event-form" onSubmit={handleSubmit}>
          <section className="card event-section">
            <div className="card-title-bar">
              Thông tin chung
              {savedEvent && <span className="badge">{savedEvent.status}</span>}
            </div>
            <div className="event-fields two-columns">
              <div className="field">
                <label htmlFor="event-name">Tên ngày sự kiện</label>
                <input
                  id="event-name"
                  required
                  maxLength="200"
                  value={draft.name}
                  onChange={(event) => updateDraft('name', event.target.value)}
                  placeholder="Ví dụ: Ngày đặt bàn 24/08"
                />
              </div>
              <div className="field">
                <label htmlFor="event-date">Ngày diễn ra</label>
                <input
                  id="event-date"
                  type="date"
                  required
                  value={draft.eventDate}
                  onChange={(event) => updateDraft('eventDate', event.target.value)}
                />
              </div>
            </div>
          </section>

          {draft.shifts.map((shift, shiftIndex) => (
            <section className="card event-section" key={shift.key}>
              <div className="card-title-bar event-shift-title">
                <span>Ca {shiftIndex + 1}</span>
                <div className="event-row-actions">
                  <button className="btn small outline" type="button" disabled={shiftIndex === 0} onClick={() => moveShift(shiftIndex, -1)}>Lên</button>
                  <button className="btn small outline" type="button" disabled={shiftIndex === draft.shifts.length - 1} onClick={() => moveShift(shiftIndex, 1)}>Xuống</button>
                  <button className="btn small danger-outline" type="button" disabled={draft.shifts.length === 1} onClick={() => removeShift(shiftIndex)}>Xóa ca</button>
                </div>
              </div>

              <div className="event-fields shift-fields">
                <div className="field">
                  <label>Tên ca</label>
                  <input required maxLength="200" value={shift.name} onChange={(event) => updateShift(shiftIndex, 'name', event.target.value)} placeholder="Ca sáng" />
                </div>
                <div className="field">
                  <label>Bắt đầu</label>
                  <input type="datetime-local" required value={shift.startsAt} onChange={(event) => updateShift(shiftIndex, 'startsAt', event.target.value)} />
                </div>
                <div className="field">
                  <label>Kết thúc</label>
                  <input type="datetime-local" required value={shift.endsAt} onChange={(event) => updateShift(shiftIndex, 'endsAt', event.target.value)} />
                </div>
                <div className="field">
                  <label>ID tài khoản Trưởng ca</label>
                  <input required value={shift.shiftLeadUserId} onChange={(event) => updateShift(shiftIndex, 'shiftLeadUserId', event.target.value)} placeholder="UUID có role SHIFT_LEAD" />
                </div>
              </div>

              <div className="event-assets-header">
                <div>
                  <strong>Quota tài sản</strong>
                  <div className="hint">Carry-over do hệ thống tính khi vận hành ca, không cấu hình tại đây.</div>
                </div>
                <button className="btn small outline" type="button" onClick={() => addAsset(shiftIndex)}>Thêm tài sản</button>
              </div>

              <div className="event-asset-list">
                {shift.assets.map((asset, assetIndex) => (
                  <div className="event-asset-row" key={asset.key}>
                    <div className="field">
                      <label>Tài sản</label>
                      <select required value={asset.assetId} onChange={(event) => updateAsset(shiftIndex, assetIndex, 'assetId', event.target.value)}>
                        <option value="">Chọn tài sản</option>
                        {(assets || []).map((item) => (
                          <option key={item.id} value={item.id}>{item.name} · còn {item.availableQuantity}</option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label>Quota</label>
                      <input type="number" min="0" step="1" required value={asset.quotaQuantity} onChange={(event) => updateAsset(shiftIndex, assetIndex, 'quotaQuantity', event.target.value)} />
                    </div>
                    <button className="btn small danger-outline" type="button" disabled={shift.assets.length === 1} onClick={() => removeAsset(shiftIndex, assetIndex)}>Xóa</button>
                  </div>
                ))}
              </div>
            </section>
          ))}

          <div className="event-form-actions">
            <button className="btn outline" type="button" onClick={addShift}>Thêm ca</button>
            <button className="btn primary" type="submit" disabled={submitting || !assets}>
              {submitting ? 'Đang lưu…' : savedEvent ? 'Cập nhật cấu hình' : 'Tạo ngày sự kiện'}
            </button>
          </div>
        </form>
      )}
    </Layout>
  );
}

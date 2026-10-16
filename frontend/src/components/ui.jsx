// Mảnh UI dùng lại giữa các trang. Chỉ hiển thị — không gọi API, không chứa nghiệp vụ.

// Minh hoạ nằm trong /public/illustrations/<name>.svg — thay file là đổi hình, không sửa code.
export function Illustration({ name, className, alt = '' }) {
  return <img src={`/illustrations/${name}.svg`} alt={alt} className={className} />;
}

// tone: primary | info | success | danger | navy. sub: { text, tone: 'red' | 'gold' | '' } hoặc chuỗi.
export function StatCard({ icon: Icon, tone = 'primary', label, value, sub }) {
  const pill = sub && typeof sub === 'object' ? sub : null;
  return (
    <div className="stat-card">
      <div className="stat-top">
        <span className={`icon-tile tone-${tone}`} aria-hidden="true">
          <Icon size={28} weight="duotone" />
        </span>
        {pill && <span className={`badge ${pill.tone || ''}`}>{pill.text}</span>}
      </div>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && !pill && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

function niceMax(n) {
  if (n <= 4) return 4;
  const step = 10 ** Math.floor(Math.log10(n));
  return Math.ceil(n / step) * step;
}

// data: [{ label, values: [a, b] }], series: [{ name, color: 'var(--primary)' }]
export function SegmentedBarChart({ data, series, solid = false, label }) {
  const max = niceMax(Math.max(0, ...data.flatMap((d) => d.values)));
  const summary = data
    .map((d) => `${d.label}: ${series.map((s, i) => `${s.name} ${d.values[i]}`).join(', ')}`)
    .join('; ');
  return (
    <div className={`bar-chart${solid ? ' solid' : ''}`} role="img" aria-label={`${label}. ${summary}`}>
      <div className="bar-axis" aria-hidden="true">
        <span>{max}</span>
        <span>{max / 2}</span>
        <span>0</span>
      </div>
      <div className="bar-plot" aria-hidden="true">
        {data.map((d, gi) => (
          <div key={d.label} className="bar-group">
            <div className="bar-pair">
              {d.values.map((v, i) => (
                <span
                  key={series[i].name}
                  className="bar"
                  title={`${series[i].name}: ${v}`}
                  style={{ height: `${(v / max) * 100}%`, '--bar-color': series[i].color, animationDelay: `${gi * 40}ms` }}
                />
              ))}
            </div>
            <span className="bar-label" title={d.label}>{d.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// segments: [{ name, value, color }]
export function ProportionBar({ segments }) {
  const total = segments.reduce((sum, s) => sum + s.value, 0) || 1;
  return (
    <>
      <div className="proportion" aria-hidden="true">
        {segments.filter((s) => s.value > 0).map((s) => (
          <span key={s.name} className="proportion-seg" style={{ flex: s.value, background: s.color }} />
        ))}
      </div>
      <div className="proportion-legend">
        {segments.map((s) => (
          <div key={s.name} className="proportion-row">
            <span className="legend-dot" style={{ background: s.color }} aria-hidden="true" />
            {s.name}
            <strong>{s.value} · {Math.round((s.value / total) * 100)}%</strong>
          </div>
        ))}
      </div>
    </>
  );
}

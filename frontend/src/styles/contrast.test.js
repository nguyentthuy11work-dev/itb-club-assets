import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// Đọc token từ :root của global.css và kiểm mọi cặp chữ/nền đang dùng đạt WCAG AA (4.5:1).
const css = readFileSync(new URL('./global.css', import.meta.url), 'utf8');
const tokens = Object.fromEntries([...css.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(fg, bg) {
  const [a, b] = [luminance(tokens[fg]), luminance(tokens[bg])].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}

const PAIRS = [
  ['ink', 'primary'], // nav đang chọn, nút chính
  ['ink-soft', 'surface'],
  ['muted', 'surface'],
  ['muted', 'shell'], // chữ phụ trong stat card
  ['subtle', 'surface'],
  ['subtle', 'shell'], // header bảng, nhãn trục
  ['primary-ink', 'surface'],
  ['primary-ink', 'primary-50'],
  ['success-ink', 'success-bg'],
  ['danger-ink', 'danger-bg'],
  ['danger-ink', 'surface'],
  ['warning-ink', 'warning-bg'],
  ['info-ink', 'info-bg'],
  ['muted', 'canvas'], // badge "dark"
];

for (const [fg, bg] of PAIRS) {
  test(`${fg} trên ${bg} ≥ 4.5:1`, () => {
    assert.ok(tokens[fg] && tokens[bg], `thiếu token ${fg} hoặc ${bg}`);
    const ratio = contrast(fg, bg);
    assert.ok(ratio >= 4.5, `${fg}/${bg} = ${ratio.toFixed(2)}:1`);
  });
}

test('vòng focus primary-700 ≥ 3:1 trên nền trắng', () => {
  assert.ok(contrast('primary-700', 'surface') >= 3);
});

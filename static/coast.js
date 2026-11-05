/* AURA's coast page: /api/coast (the nearest coast to her location, or config Coast). */
const $ = id => document.getElementById(id);
const num = (v, d = 1) => (v === null || v === undefined ? '--' : Number(v).toFixed(d));
let data = null;

function cell(k, v, note) {
  const div = document.createElement('div');
  div.innerHTML = '<dt></dt><dd></dd><dd class="note"></dd>';
  div.querySelector('dt').textContent = k;
  div.querySelectorAll('dd')[0].textContent = v;
  div.querySelectorAll('dd')[1].textContent = note || '';
  return div;
}

function show(d) {
  const t = d.tide || {};
  const where = d.direction === 'here' ? `the sea at ${d.where}` : `${d.where}, ${d.km} km ${d.direction}`;
  $('where').textContent = where;
  const going = t.rising === null || t.rising === undefined ? '' : t.rising ? 'The tide is coming in.' : 'The tide is going out.';
  $('says').textContent = `${d.direction === 'here' ? 'Right here' : `Nearest sea: ${where}`}. ` +
    `${d.sea_temp !== null ? `The water's ${num(d.sea_temp, 0)}°. ` : ''}${going}` + (d.surf ? ` The surf's ${d.surf}.` : '');
  const w = d.waves || {}, ww = d.wind_waves || {}, s = d.swell || {}, c = d.current || {};
  $('sea').replaceChildren(
    cell('Sea temperature', `${num(d.sea_temp)}°C`),
    cell('Waves', `${num(w.height_m)} m`, `every ${num(w.period_s, 0)} s from the ${w.from || '?'} · today up to ${num(d.today_max_wave_m)} m`),
    cell('Swell', `${num(s.height_m)} m`, `every ${num(s.period_s, 0)} s from the ${s.from || '?'}`),
    cell('Wind waves', `${num(ww.height_m)} m`, `every ${num(ww.period_s, 0)} s from the ${ww.from || '?'}`),
    cell('Current', `${num(c.kmh)} km/h`, c.kmh ? `towards the ${c.towards}` : ''),
    cell('Surf', d.surf || '--', `tomorrow's waves up to ${num(d.tomorrow_max_wave_m)} m`),
    cell('Tide now', `${num(t.now_m, 2)} m`, `range ${num(t.range_m, 2)} m${going ? ' · ' + (t.rising ? 'rising' : 'falling') : ''}`));
  $('turns').replaceChildren(...(t.turns || []).map(x => {
    const div = document.createElement('div');
    div.className = x.kind;
    div.innerHTML = '<div class="k"></div><div class="v"></div>';
    div.querySelector('.k').textContent = `${x.kind} tide · ${x.day}`;
    div.querySelector('.v').textContent = `${x.at}  ${num(x.m, 2)} m`;
    return div;
  }));
  data = d;
  drawTide();
}

function drawTide() {
  const hours = ((data || {}).tide || {}).hours || [];
  const canvas = $('tide');
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, W, H);
  const pts = hours.filter(h => h[1] !== null);
  if (pts.length < 2) return;
  const vals = pts.map(p => p[1]);
  const lo = Math.min(...vals) - 0.1, hi = Math.max(...vals) + 0.1;
  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const x = i => pad.l + i / (hours.length - 1) * (W - pad.l - pad.r);
  const y = v => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);
  g.font = '12px system-ui, sans-serif';
  g.strokeStyle = '#1E2C45'; g.fillStyle = '#93A3B8';
  for (const v of [lo + 0.1, 0, hi - 0.1]) {
    g.beginPath(); g.moveTo(pad.l, y(v)); g.lineTo(W - pad.r, y(v)); g.stroke();
    g.fillText(`${v.toFixed(1)} m`, 4, y(v) + 4);
  }
  hours.forEach((h, i) => { if (i % 6 === 0) g.fillText(h[0], x(i) - 14, H - 8); });
  const grad = g.createLinearGradient(0, pad.t, 0, H - pad.b);
  grad.addColorStop(0, 'rgba(94, 200, 242, 0.35)'); grad.addColorStop(1, 'rgba(94, 200, 242, 0.02)');
  g.beginPath();
  hours.forEach((h, i) => { if (h[1] !== null) (i ? g.lineTo : g.moveTo).call(g, x(i), y(h[1])); });
  g.lineTo(x(hours.length - 1), H - pad.b); g.lineTo(x(0), H - pad.b); g.closePath();
  g.fillStyle = grad; g.fill();
  g.beginPath(); g.strokeStyle = '#5EC8F2'; g.lineWidth = 2;
  hours.forEach((h, i) => { if (h[1] !== null) (i ? g.lineTo : g.moveTo).call(g, x(i), y(h[1])); });
  g.stroke();
  const nowI = 2;                       // the list starts 2 hours ago
  g.strokeStyle = '#F2A65E'; g.setLineDash([4, 4]); g.lineWidth = 1;
  g.beginPath(); g.moveTo(x(nowI), pad.t); g.lineTo(x(nowI), H - pad.b); g.stroke(); g.setLineDash([]);
  g.fillStyle = '#F2A65E'; g.fillText('now', x(nowI) + 4, pad.t + 12);
}

async function load() {
  try {
    const r = await fetch('/api/coast');
    const d = await r.json();
    if (!r.ok) { $('says').textContent = d.error; return; }
    show(d);
  } catch { $('says').textContent = "AURA didn't answer."; }
}

window.addEventListener('resize', drawTide);
load();
setInterval(load, 15 * 60 * 1000);

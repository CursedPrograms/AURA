/* AURA's page: the weather now, every room's reading, and the graph (a plain canvas, no libraries). */
const $ = id => document.getElementById(id);
const COLOURS = ['#5EC8F2', '#8EDB9C', '#B58CF2', '#F27EA9', '#F2D65E', '#5EF2C8', '#F2875E', '#9FB4FF'];
const OUTSIDE = '#F2A65E';
let units = 'C';
let what = 'temp';
let hours = 24;
let series = {};
const colourOf = {};

function colour(name) {
  if (name === 'Outside') return OUTSIDE;
  if (!colourOf[name]) colourOf[name] = COLOURS[Object.keys(colourOf).length % COLOURS.length];
  return colourOf[name];
}

const deg = t => (t === null || t === undefined) ? '--' : (units === 'F' ? Math.round(t * 9 / 5 + 32) : Math.round(t * 10) / 10);
const unit = () => (units === 'F' ? '°F' : '°C');
const pct = h => (h === null || h === undefined) ? '--' : `${Math.round(h)}%`;

function ago(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

async function refreshNow() {
  let d;
  try { d = await (await fetch('/api/now')).json(); } catch { return; }
  units = d.units || 'C';
  $('says').textContent = d.says;
  if (d.location) $('where').textContent = d.location.name || '';
  const o = d.outside;
  if (o) {
    $('out-temp').textContent = deg(o.temp);
    $('out-unit').textContent = unit();
    $('out-words').textContent = o.words;
    $('out-feels').textContent = `${deg(o.feels)}${unit()}`;
    $('out-hum').textContent = pct(o.hum);
    $('out-wind').textContent = o.wind_kmh === null ? '--' : `${Math.round(o.wind_kmh)} km/h`;
    $('out-range').textContent = `${deg(o.low)}–${deg(o.high)}${unit()}`;
    $('out-rain').textContent = o.rain_chance === null ? '--' : `${Math.round(o.rain_chance)}% chance`;
    $('out-sunrise').textContent = o.sunrise ? o.sunrise.slice(-5) : '--';
    $('out-sunset').textContent = o.sunset ? o.sunset.slice(-5) : '--';
    $('out-uv').textContent = o.uv === null || o.uv === undefined ? '--' : Math.round(o.uv);
    const soon = o.soon || {};
    $('out-soon').textContent = soon.storm_at ? `storm ~${soon.storm_at}` : soon.rain_at ? `rain ~${soon.rain_at}` : 'dry for now';
  }
  if (d.air && d.air.aqi !== null) {
    $('out-air').textContent = `${d.air.words} (${Math.round(d.air.aqi)})` + ((d.air.pollen_high || []).length ? `, ${d.air.pollen_high.join(' + ')} pollen` : '');
  }
  if (d.moon) {
    $('moon-icon').textContent = d.moon.icon;
    $('moon-phase').textContent = `${d.moon.phase} moon`;
    $('moon-more').textContent = d.moon.phase === 'full' ? `${d.moon.lit}% lit: full tonight`
      : `${d.moon.lit}% lit · full in ${Math.round(d.moon.days_to_full)} days`;
  }
  const names = Object.keys(d.inside || {}).sort((a, b) => (a === 'AURA' ? -1 : b === 'AURA' ? 1 : a.localeCompare(b)));
  if (names.length) {
    $('rooms').replaceChildren(...names.map(n => {
      const r = d.inside[n];
      const el = document.createElement('div');
      el.className = 'room' + (Date.now() / 1000 - r.ts > 300 ? ' stale' : '');
      el.style.setProperty('--c', colour(n));
      el.innerHTML = '<div class="who"></div><div class="t"></div><div class="h"></div><div class="via"></div>';
      el.querySelector('.who').textContent = n === 'AURA' ? 'AURA (here)' : n;
      el.querySelector('.t').textContent = `${deg(r.temp)}${unit()}`;
      el.querySelector('.h').textContent = `${pct(r.hum)} humidity`;
      el.querySelector('.via').textContent = `${ago(r.ts)} · ${r.via}`;
      return el;
    }));
  }
  $('problems').replaceChildren(...(d.problems || []).map(p => Object.assign(document.createElement('li'), { textContent: p })));
}

async function refreshHistory() {
  try { series = (await (await fetch(`/api/history?hours=${hours}`)).json()).series || {}; } catch { return; }
  draw();
}

function draw() {
  const canvas = $('chart');
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, W, H);
  const idx = what === 'temp' ? 1 : 2;
  const now = Date.now() / 1000, from = now - hours * 3600;
  const pts = Object.entries(series).map(([name, rows]) =>
    [name, rows.filter(r => r[idx] !== null).map(r => [r[0], what === 'temp' && units === 'F' ? r[idx] * 9 / 5 + 32 : r[idx]])]);
  const all = pts.flatMap(([, p]) => p.map(q => q[1]));
  const pad = { l: 46, r: 14, t: 14, b: 28 };
  g.font = '12px system-ui, sans-serif';
  if (!all.length) {
    g.fillStyle = '#93A3B8';
    g.fillText('Nothing to plot yet: readings are saved once a minute.', pad.l, H / 2);
    $('legend').replaceChildren();
    return;
  }
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 4) { lo -= 2; hi += 2; }
  lo = Math.floor(lo); hi = Math.ceil(hi);
  const x = t => pad.l + (t - from) / (now - from) * (W - pad.l - pad.r);
  const y = v => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);

  g.strokeStyle = '#1E2C45'; g.fillStyle = '#93A3B8'; g.lineWidth = 1;
  const step = Math.max(1, Math.round((hi - lo) / 6));
  for (let v = lo; v <= hi; v += step) {
    g.beginPath(); g.moveTo(pad.l, y(v)); g.lineTo(W - pad.r, y(v)); g.stroke();
    g.fillText(what === 'temp' ? `${v}°` : `${v}%`, 6, y(v) + 4);
  }
  const tick = hours <= 6 ? 3600 : hours <= 24 ? 4 * 3600 : 86400;
  for (let t = Math.ceil(from / tick) * tick; t <= now; t += tick) {
    const d = new Date(t * 1000);
    const label = hours > 24 ? d.toLocaleDateString([], { weekday: 'short' }) : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    g.fillText(label, x(t) - 16, H - 8);
  }

  for (const [name, p] of pts) {
    if (!p.length) continue;
    g.strokeStyle = colour(name); g.lineWidth = name === 'Outside' ? 2.5 : 2;
    g.setLineDash(name === 'Outside' ? [6, 4] : []);
    g.beginPath();
    p.forEach(([t, v], i) => {
      // a gap longer than 15 minutes (her PC was off) breaks the line instead of joining it
      if (i && t - p[i - 1][0] > 900) g.moveTo(x(t), y(v)); else if (i) g.lineTo(x(t), y(v)); else g.moveTo(x(t), y(v));
    });
    g.stroke();
  }
  g.setLineDash([]);
  $('legend').replaceChildren(...pts.filter(([, p]) => p.length).map(([name]) => {
    const s = document.createElement('span');
    s.textContent = name;
    s.style.setProperty('--c', colour(name));
    if (name === 'Outside') s.className = 'dash';
    return s;
  }));
}

document.querySelectorAll('.toggles button').forEach(b => b.addEventListener('click', () => {
  if (b.dataset.what) { what = b.dataset.what; draw(); }
  if (b.dataset.hours) { hours = +b.dataset.hours; refreshHistory(); }
  const group = b.dataset.what ? '[data-what]' : '[data-hours]';
  document.querySelectorAll(`.toggles button${group}`).forEach(o => o.classList.toggle('on', o === b));
}));
window.addEventListener('resize', draw);

$('story-btn').addEventListener('click', async () => {
  const b = $('story-btn');
  b.disabled = true;
  $('story').textContent = 'Looking at the sky...';
  try {
    const d = await (await fetch('/story')).json();
    $('story').textContent = d.story || d.error;
  } catch { $('story').textContent = "The storyteller didn't answer."; }
  b.disabled = false;
});

colour('AURA');
refreshNow();
refreshHistory();
setInterval(refreshNow, 15000);
setInterval(refreshHistory, 60000);

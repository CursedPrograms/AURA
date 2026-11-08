/* AURA's page: the weather now, every room's reading, and the graph (a plain canvas, no libraries). */
const $ = id => document.getElementById(id);
const COLOURS = ['#5EC8F2', '#8EDB9C', '#B58CF2', '#F27EA9', '#F2D65E', '#5EF2C8', '#F2875E', '#9FB4FF'];
const OUTSIDE = '#F2A65E';
let units = 'C';
let what = 'temp';
let hours = 24;
let series = {};
const colourOf = {};

let robotColours = {};            // each robot's own accent (its colour_scheme.xml), from /api/now

function colour(name) {
  if (robotColours[name]) return robotColours[name];
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

/* The city dropdown: home (saved, or your connection's city) first. Picking another city shows its weather;
   the graph's outside line always stays home. "Save as my location" makes it home (location.json). */
let viewing = '';
let homeSaved = false;
const CUSTOM_KEY = 'aura.cities';

async function loadCities() {
  let d;
  try { d = await (await fetch('/api/cities')).json(); } catch { return; }
  homeSaved = d.saved;
  let custom = [];
  try { custom = JSON.parse(localStorage.getItem(CUSTOM_KEY) || '[]'); } catch { /* none */ }
  const pick = $('city-pick');
  const opt = (value, text) => Object.assign(document.createElement('option'), { value, textContent: text });
  pick.replaceChildren(opt('', `${d.home} (${d.saved ? 'saved' : "my connection's city"})`),
    ...[...custom, ...d.cities].filter((c, i, all) => all.indexOf(c) === i).map(c => opt(c, c)),
    opt('*other', 'Another city...'));
  pick.value = viewing;
  showCityButtons();
}

function showCityButtons() {
  $('save-city').hidden = !viewing;
  $('forget-city').hidden = !!viewing || !homeSaved;
  $('city-note').textContent = viewing ? 'Just looking: the graph stays at home.' : '';
}

$('city-pick').addEventListener('change', async e => {
  let city = e.target.value;
  if (city === '*other') {
    city = (prompt('Which city?') || '').trim();
    if (!city) { e.target.value = viewing; return; }
    let custom = [];
    try { custom = JSON.parse(localStorage.getItem(CUSTOM_KEY) || '[]'); } catch { /* none */ }
    localStorage.setItem(CUSTOM_KEY, JSON.stringify([city, ...custom.filter(c => c !== city)].slice(0, 12)));
  }
  viewing = city;
  await loadCities();
  refreshNow();
});

$('save-city').addEventListener('click', async () => {
  const r = await fetch('/api/location', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify({ city: viewing }) });
  const d = await r.json();
  if (!r.ok) { $('city-note').textContent = d.error; return; }
  viewing = '';
  $('city-note').textContent = `Saved: ${d.home} is home now.`;
  await loadCities();
  setTimeout(refreshNow, 2500);
});

$('forget-city').addEventListener('click', async () => {
  await fetch('/api/location', { method: 'DELETE' });
  await loadCities();
  setTimeout(refreshNow, 2500);
});

async function refreshNow() {
  let d;
  try { d = await (await fetch('/api/now' + (viewing ? `?city=${encodeURIComponent(viewing)}` : ''))).json(); } catch { return; }
  if (d.error) { $('city-note').textContent = d.error; return; }
  units = d.units || 'C';
  if (d.colours) robotColours = d.colours;
  showOutlook(d.rain_outlook);
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
  if (o) showDetails(o);
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
      el.innerHTML = '<div class="who"><img alt=""><span></span></div><div class="t"></div><div class="h"></div><div class="via"></div>';
      const face = el.querySelector('.who img');
      face.src = `/robot-avatar/${encodeURIComponent(n)}`;
      face.onerror = () => face.remove();          // a robot without an avatar beside her: just the name
      el.querySelector('.who span').textContent = n === 'AURA' ? 'AURA (here)' : n;
      el.querySelector('.t').textContent = `${deg(r.temp)}${unit()}`;
      el.querySelector('.h').textContent = `${pct(r.hum)} humidity`;
      el.querySelector('.via').textContent = `${ago(r.ts)} · ${r.via}`;
      return el;
    }));
  }
  $('problems').replaceChildren(...(d.problems || []).map(p => Object.assign(document.createElement('li'), { textContent: p })));
}

// Everything else Open-Meteo says: the barometer, wind, clouds, air, storm energy, sun and ground.
function showDetails(o) {
  const num = (v, d = 0) => (v === null || v === undefined ? '--' : Number(v).toFixed(d));
  const c = o.clouds || {}, sun = o.sun || {}, g = o.ground || {}, t = o.today_summary || {}, p = o.precip || {};
  const rows = [
    ['Pressure', `${num(o.pressure_hpa)} hPa`, o.pressure_trend ? `${o.pressure_trend} (${num(o.pressure_change_3h, 1)} in 3 h)` : ''],
    ['Wind', `${num(o.wind_kmh)} km/h ${o.wind_dir || ''}`, `gusts ${num(o.gusts_kmh)} · today max ${num(t.gust_max_kmh)}`],
    ['Clouds', `${num(c.total)}%`, `low ${num(c.low)} · mid ${num(c.mid)} · high ${num(c.high)}`],
    ['Visibility', `${num(o.visibility_km, 1)} km`, o.fog ? 'fog' : ''],
    ['Dew point', `${deg(o.dew_point)}${unit()}`, o.feel_of_air || ''],
    ['Storm energy', o.storm_potential || '--', `CAPE ${num(o.cape)} · lifted ${num(o.lifted_index, 1)}${o.lightning !== null && o.lightning !== undefined ? ` · lightning ${num(o.lightning)}` : ''}`],
    ['Rain today', `${num(t.precip_mm, 1)} mm`, `${num(t.precip_hours)} h · now ${num(p.rain_mm, 1)} rain, ${num(p.showers_mm, 1)} showers`],
    ['Snow', `${num(p.snowfall_cm, 1)} cm`, `depth ${num(p.snow_depth_m, 2)} m · freezes at ${num(o.freezing_level_m)} m`],
    ['Sunshine', `${num(sun.sunshine_h_today, 1)} h today`, `UV clear sky ${num(sun.uv_clear_sky, 1)}`],
    ['Daylight', `${num(sun.daylight_h, 2)} h`, sun.daylight_change_min ? `${sun.daylight_change_min > 0 ? '+' : ''}${num(sun.daylight_change_min, 1)} min since yesterday` : ''],
    ['Sun power', `${num(sun.radiation_wm2)} W/m²`, `direct ${num(sun.direct_wm2)} · diffuse ${num(sun.diffuse_wm2)} · ${num(sun.radiation_today_mj, 1)} MJ today`],
    ['Ground', `${deg(g.soil_temp_surface)}${unit()}`, `6 cm ${deg(g.soil_temp_6cm)}° · moisture ${num((g.soil_moisture_top || 0) * 100)}%`],
    ['Plants drink', `${num(g.et0_today_mm, 1)} mm today`, `now ${num(g.evapotranspiration_mm, 2)} mm/h`],
  ];
  $('details').replaceChildren(...rows.map(([k, v, note]) => {
    const div = document.createElement('div');
    div.innerHTML = '<dt></dt><dd></dd><dd class="note"></dd>';
    div.querySelector('dt').textContent = k;
    div.querySelectorAll('dd')[0].textContent = v;
    div.querySelectorAll('dd')[1].textContent = note;
    return div;
  }));
}

// Her own rain forecast: Open-Meteo, nudged by every sensor in the fleet and the barometer
function showOutlook(o) {
  const box = $('outlook');
  if (!o) { box.hidden = true; return; }
  box.hidden = false;
  $('outlook-chance').textContent = `${o.chance}%`;
  $('outlook-conf').textContent = `${o.confidence} confidence · ${o.sensors} sensor${o.sensors === 1 ? '' : 's'}` +
    (o.sensor_names && o.sensor_names.length ? ` (${o.sensor_names.join(', ')})` : '');
  $('outlook-why').textContent = (o.reasons || []).join(' · ');
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
// Her two talk switches (/talkmute; RIFT has the same): her beeps when she chats, and the chatting itself
const talkBtn = document.getElementById('talk-btn'), chatBtn = document.getElementById('chat-btn');
function showTalk(d) {
  if (d.talk_muted === undefined) return;
  talkBtn.textContent = d.talk_muted ? 'Chat sound: off' : 'Chat sound: on';
  talkBtn.classList.toggle('muted', d.talk_muted);
  chatBtn.textContent = d.chat_off ? 'Fleet chat: off' : 'Fleet chat: on';
  chatBtn.classList.toggle('muted', d.chat_off);
}
async function refreshTalk() {
  try { showTalk(await (await fetch('/talkmute')).json()); } catch {}
}
async function flipTalk(which) {
  try { showTalk(await (await fetch(`/talkmute?${which}=toggle`)).json()); } catch {}
}
talkBtn.addEventListener('click', () => flipTalk('on'));
chatBtn.addEventListener('click', () => flipTalk('chat_off'));
refreshTalk();
setInterval(refreshTalk, 15000);

loadCities();
refreshNow();
refreshHistory();
setInterval(refreshNow, 15000);
setInterval(refreshHistory, 60000);

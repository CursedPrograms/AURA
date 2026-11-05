/* AURA's Europe page: /api/europe for her own place, or ?city= for any European city. */
const $ = id => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined ? '--' : Number(v).toFixed(d));
const cls = w => (w || 'none').replace(/\s+/g, '-');

function tile(key, value, words, extra, bars) {
  const el = document.createElement('div');
  el.className = `tile ${cls(words)}`;
  el.innerHTML = '<div class="k"></div><div class="v"></div><div class="w"></div><div class="peak"></div>';
  el.querySelector('.k').textContent = key;
  el.querySelector('.v').textContent = value;
  el.querySelector('.w').textContent = words || 'no data here';
  el.querySelector('.peak').textContent = extra || '';
  if (bars && bars.some(v => v)) {
    const top = Math.max(...bars.map(v => v || 0), 0.001);
    const b = document.createElement('div');
    b.className = 'bars';
    for (const v of bars) {
      const s = document.createElement('span');
      s.style.height = `${Math.round(((v || 0) / top) * 100)}%`;
      b.appendChild(s);
    }
    el.appendChild(b);
  }
  return el;
}

async function load(city) {
  $('note').textContent = 'Asking Open-Meteo...';
  let d;
  try {
    const r = await fetch('/api/europe' + (city ? `?city=${encodeURIComponent(city)}` : ''));
    d = await r.json();
    if (!r.ok) { $('note').textContent = d.error || 'No answer.'; return; }
  } catch { $('note').textContent = "AURA didn't answer."; return; }
  $('where').textContent = d.where || 'Europe';
  $('note').textContent = d.in_europe ? ''
    : `${d.where || 'Here'} isn't covered: pollen, ammonia and lightning are Europe only. Look at a European city above.`;

  $('pollen').replaceChildren(...Object.entries(d.pollen || {}).map(([k, p]) =>
    tile(k, p.now === null ? '--' : `${fmt(p.now)} /m³`, p.level,
         p.peak_24h === null ? '' : `next 24 h: up to ${fmt(p.peak_24h)} (${p.peak_level})`,
         (p.hours || []).map(h => h[1]))));

  const l = d.lightning || {};
  $('lightning').replaceChildren(
    tile('Lightning now', l.now === null ? '--' : `${fmt(l.now, 2)} J/kg`, l.level,
         l.max_12h === null ? (d.in_europe ? 'not covered here (central Europe only)' : '') : `next 12 h: up to ${fmt(l.max_12h, 2)}`,
         (l.hours || []).map(h => h[1])));

  const a = d.european_aqi || {};
  const names = { overall: 'Overall', pm2_5: 'PM2.5', pm10: 'PM10', nitrogen_dioxide: 'NO₂', ozone: 'Ozone', sulphur_dioxide: 'SO₂' };
  const aqiWords = v => (v === null || v === undefined ? null : v <= 20 ? 'good' : v <= 40 ? 'fair' : v <= 60 ? 'moderate'
    : v <= 80 ? 'poor' : v <= 100 ? 'very poor' : 'extremely poor');
  $('eaqi').replaceChildren(
    ...Object.entries(names).map(([k, label]) => tile(label, fmt(a[k], 0), aqiWords(a[k]))),
    tile('Ammonia', d.ammonia_ugm3 === null ? '--' : `${fmt(d.ammonia_ugm3)} µg/m³`, d.ammonia_ugm3 === null ? null : 'measured'));
}

$('city-form').addEventListener('submit', e => { e.preventDefault(); load($('city').value.trim()); });
$('home').addEventListener('click', () => { $('city').value = ''; load(''); });
load(new URLSearchParams(location.search).get('city') || '');

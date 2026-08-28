/* Shared rendering for the host detail cards (steve / wacky / jeff / bazza):
 * HTML-escape, the half-circle gauges, the load/mem/disk strip-chart, the
 * process rows, and the per-alert acknowledge list. Each page keeps its own
 * load() loop, its distinctive panel, and its ACKS_KEY / API base.
 *
 * Loaded as a plain <script> before the page's inline <script>, so everything
 * here is a global. Depends on Chart.js (/chart.min.js) and the CSS custom
 * properties in hostcard.css / the page's :root. */

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESC_MAP[c]);

const ARC_LEN = 157.08; // half-circumference for r=50

function gaugeSVG() {
  return `
    <div class="gauge-dial">
      <svg viewBox="0 0 120 70">
        <path class="gauge-track" d="M 10 60 A 50 50 0 0 1 110 60" />
        <path class="gauge-fill"  d="M 10 60 A 50 50 0 0 1 110 60" style="stroke-dashoffset:${ARC_LEN}" />
        <circle class="gauge-tick" cx="10"    cy="60"    r="1.6" />
        <circle class="gauge-tick" cx="24.6"  cy="24.6"  r="1.6" />
        <circle class="gauge-tick" cx="60"    cy="10"    r="1.6" />
        <circle class="gauge-tick" cx="95.4"  cy="24.6"  r="1.6" />
        <circle class="gauge-tick" cx="110"   cy="60"    r="1.6" />
      </svg>
      <div class="gauge-readout"></div>
    </div>
    <div class="gauge-cap"></div>
    <div class="gauge-sub"></div>
  `;
}

// withTemp adds a fourth "temp" gauge (jeff / bazza — the Pi hosts).
function ensureGauges(withTemp) {
  const box = document.getElementById('gauges');
  if (box.children.length) return;
  const ids = withTemp ? ['load', 'mem', 'disk', 'temp'] : ['load', 'mem', 'disk'];
  ids.forEach(id => {
    const g = document.createElement('div');
    g.className = 'gauge'; g.id = 'gauge-' + id;
    g.innerHTML = gaugeSVG();
    box.appendChild(g);
  });
}

function setGauge(id, pct, cls, readout, cap, sub) {
  const g = document.getElementById('gauge-' + id);
  const fill = g.querySelector('.gauge-fill');
  fill.classList.remove('ok', 'warn', 'down');
  fill.classList.add(cls);
  const clamped = Math.max(0, Math.min(100, pct));
  fill.style.strokeDashoffset = ARC_LEN * (1 - clamped / 100);
  g.querySelector('.gauge-readout').textContent = readout;
  g.querySelector('.gauge-cap').textContent = cap;
  g.querySelector('.gauge-sub').textContent = sub;
}

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

// ── History chart ── (colors resolved from :root so there's one source of truth)
const rootStyle = getComputedStyle(document.documentElement);
const cssVar = name => rootStyle.getPropertyValue(name).trim();
const CHART_GRID = 'rgba(234,231,223,0.08)';
const CHART_LABEL = cssVar('--ink-dim');
const C_LOAD = cssVar('--chart-load');
const C_MEM  = cssVar('--chart-mem');
const C_DISK = cssVar('--chart-disk');

Chart.defaults.color = CHART_LABEL;
Chart.defaults.borderColor = CHART_GRID;
Chart.defaults.font.family = "ui-monospace, 'DejaVu Sans Mono', monospace";
Chart.defaults.font.size = 10;

const baseOpts = {
  responsive: true,
  animation: { duration: 350 },
  interaction: { mode: 'index', intersect: false },
  plugins: { legend: { display: false },
    tooltip: { backgroundColor: '#141516', borderColor: cssVar('--bezel'), borderWidth: 1,
      titleColor: cssVar('--ink'), bodyColor: cssVar('--ink-dim'), padding: 9,
      usePointStyle: true, boxHeight: 4, boxPadding: 3 } },
  scales: {
    x: { grid: { color: CHART_GRID }, ticks: { color: CHART_LABEL, maxTicksLimit: 10 } },
    y: { grid: { color: CHART_GRID }, ticks: { color: CHART_LABEL }, beginAtZero: true, suggestedMax: 100 },
  },
};

let historyChart = null;
function renderHistory(points, cpus) {
  const labels = points.map(p => {
    const dt = new Date(p.ts * 1000);
    return dt.getHours().toString().padStart(2, '0') + ':' + dt.getMinutes().toString().padStart(2, '0');
  });
  // Overlapping translucent fills on three co-plotted series read as mud, not signal —
  // line + legend + tooltip carries identity instead (see dataviz skill, marks-and-anatomy).
  const series = (label, color, values) => ({
    label, data: values, borderColor: color, borderWidth: 2, fill: false,
    tension: 0.25, pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 12,
    pointStyle: 'line', pointBackgroundColor: color, pointBorderColor: color,
  });
  const data = {
    labels,
    datasets: [
      series('load', C_LOAD, points.map(p => Math.round(p.load1 / cpus * 100))),
      series('mem',  C_MEM,  points.map(p => p.mem_pct)),
      series('disk', C_DISK, points.map(p => p.disk_pct)),
    ],
  };
  if (historyChart) { historyChart.data = data; historyChart.update(); return; }
  historyChart = new Chart(document.getElementById('ch-history'), { type: 'line', data, options: baseOpts });
  document.getElementById('legend').innerHTML = `
    <span><i style="background:${C_LOAD}"></i>load / cpu</span>
    <span><i style="background:${C_MEM}"></i>mem</span>
    <span><i style="background:${C_DISK}"></i>disk</span>
  `;
}

function procRow(p) {
  return `<div class="row"><span>${p.pid}</span><span class="proc-name">${esc(p.name)}</span><span>${p.cpu.toFixed(1)}</span><span>${p.mem.toFixed(1)}</span></div>`;
}

// ── Alerts (each acknowledged individually — an ack is "id -> onset ts",
// so a re-triggered episode with a fresh onset ts un-acks itself) ──
function loadAcks(ackKey) {
  try { return JSON.parse(localStorage.getItem(ackKey) || '{}'); } catch { return {}; }
}
function saveAck(ackKey, id, ts) {
  const acks = loadAcks(ackKey);
  acks[id] = ts;
  localStorage.setItem(ackKey, JSON.stringify(acks));
}

function renderAlerts(allAlerts, ackKey) {
  const acks = loadAcks(ackKey);
  const alerts = allAlerts.filter(a => (acks[a.id] || 0) < a.ts);

  // Prune acks for alerts that no longer exist, so this doesn't grow forever.
  const liveIds = new Set(allAlerts.map(a => a.id));
  const pruned = Object.fromEntries(Object.entries(acks).filter(([id]) => liveIds.has(id)));
  localStorage.setItem(ackKey, JSON.stringify(pruned));

  const section = document.getElementById('alerts-section');
  if (!alerts.length) { section.style.display = 'none'; return { count: 0, critical: 0 }; }

  section.style.display = '';
  document.getElementById('alert-list').innerHTML = alerts.map(a => `
    <div class="alert-item ${a.level === 'critical' ? 'critical' : ''}" data-id="${esc(a.id)}">
      <div class="alert-body">
        <div class="alert-item-top">
          <span class="alert-header">${esc(a.header)}</span>
          <span class="alert-time">${new Date(a.ts * 1000).toLocaleString('en-GB')}</span>
        </div>
        <div class="alert-text">${esc(a.text)}</div>
      </div>
      <button class="ack-btn ack-one" data-id="${esc(a.id)}" data-ts="${a.ts}">Ack</button>
    </div>`).join('');

  document.querySelectorAll('.ack-one').forEach(btn => {
    btn.addEventListener('click', () => {
      saveAck(ackKey, btn.dataset.id, Number(btn.dataset.ts));
      btn.closest('.alert-item').remove();
      if (!document.querySelectorAll('.alert-item').length) section.style.display = 'none';
    });
  });

  return { count: alerts.length, critical: alerts.filter(a => a.level === 'critical').length };
}

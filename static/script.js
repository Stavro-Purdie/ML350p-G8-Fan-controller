// Charts setup
let tempChart, fanChart;
let fanSeriesInit = false;
const maxPoints = 120; // ~10 minutes at 5s
const fanHistory = {};
const fanHistoryLimit = 24; // ~2 minutes of samples at 5s
const trendHorizonSeconds = 30;
let updateInProgress = false;

const setUpdateInProgress = (state) => {
  updateInProgress = state;
  const targets = document.querySelectorAll('[data-disable-during-update]');
  targets.forEach(target => {
    if (state) {
      target.classList.add('update-disabled');
    } else {
      target.classList.remove('update-disabled');
    }
    const controls = target.matches('button, input, select, textarea, fieldset')
      ? [target]
      : target.querySelectorAll('button, input, select, textarea, fieldset');
    controls.forEach(ctrl => {
      if (state) {
        if (!ctrl.dataset.disabledDuringUpdate) {
          ctrl.dataset.disabledDuringUpdate = '1';
          if (ctrl.disabled) {
            ctrl.dataset.disabledBeforeUpdate = '1';
          }
          ctrl.disabled = true;
        }
      } else if (ctrl.dataset.disabledDuringUpdate) {
        delete ctrl.dataset.disabledDuringUpdate;
        if (ctrl.dataset.disabledBeforeUpdate) {
          delete ctrl.dataset.disabledBeforeUpdate;
        } else {
          ctrl.disabled = false;
        }
      }
    });
  });
  const overlay = document.getElementById('updateOverlay');
  if (overlay) {
    overlay.setAttribute('aria-hidden', String(!state));
    overlay.classList.toggle('visible', state);
  }
  document.body.classList.toggle('update-in-progress', state);
};

const ensureHistory = (key) => {
  if (!fanHistory[key]) fanHistory[key] = [];
  return fanHistory[key];
};

const computeTrend = (points, horizonSeconds = trendHorizonSeconds) => {
  const fallback = () => {
    const last = points && points.length ? points[points.length - 1].value : 0;
    return { forecast: Math.round(last), delta: 0, label: 'Stable', icon: '→', className: 'trend-flat', volatile: false };
  };
  if (!points || points.length < 3) return fallback();
  const base = points[0].t;
  let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
  let signFlips = 0;
  let lastDiffSign = 0;
  let maxJump = 0;
  for (let i = 1; i < points.length; i += 1) {
    const diff = points[i].value - points[i - 1].value;
    maxJump = Math.max(maxJump, Math.abs(diff));
    const sign = diff === 0 ? 0 : (diff > 0 ? 1 : -1);
    if (sign !== 0 && lastDiffSign !== 0 && sign !== lastDiffSign) {
      signFlips += 1;
    }
    if (sign !== 0) lastDiffSign = sign;
  }
  for (const p of points) {
    const x = (p.t - base) / 1000;
    const y = p.value;
    sumX += x;
    sumY += y;
    sumXY += x * y;
    sumXX += x * x;
  }
  const n = points.length;
  const denom = n * sumXX - sumX * sumX;
  if (Math.abs(denom) < 1e-6) return fallback();
  const slope = (n * sumXY - sumX * sumY) / denom;
  const meanX = sumX / n;
  const meanY = sumY / n;
  const intercept = meanY - slope * meanX;
  const lastX = (points[n - 1].t - base) / 1000;
  const current = points[n - 1].value;
  let effectiveHorizon = horizonSeconds;
  const recentDiff = points[n - 1].value - points[n - 2].value;
  const velocity = slope;
  const volatility = signFlips >= 2 || maxJump >= 30 || Math.abs(recentDiff) >= 25;
  if (volatility) {
    effectiveHorizon = Math.max(5, Math.min(15, horizonSeconds / 2));
  } else if (Math.abs(velocity) > 2.5) {
    effectiveHorizon = Math.min(horizonSeconds, 20);
  }
  let forecast = intercept + slope * (lastX + effectiveHorizon);
  if (!Number.isFinite(forecast)) return fallback();
  forecast = Math.max(0, Math.min(100, forecast));
  let delta = forecast - current;
  const absDelta = Math.abs(delta);
  let label = 'Stable';
  let icon = '→';
  let className = 'trend-flat';
  let roundDelta = Math.round(delta);
  if (absDelta < 1.2) {
    delta = 0;
    roundDelta = 0;
    forecast = current;
  } else if (delta >= 6) {
    label = 'Spiking';
    icon = '⤴';
    className = 'trend-spike';
  } else if (delta >= 2) {
    label = 'Rising';
    icon = '↑';
    className = 'trend-up';
  } else if (delta <= -6) {
    label = 'Plunging';
    icon = '⤵';
    className = 'trend-plunge';
  } else if (delta <= -2) {
    label = 'Falling';
    icon = '↓';
    className = 'trend-down';
  }
  if (volatility) {
    label = 'Volatile';
    icon = '≈';
    className = 'trend-volatile';
    const damped = Math.max(-10, Math.min(10, delta));
    delta = damped;
    forecast = current + damped;
    roundDelta = Math.round(delta);
  }
  const finalForecast = Math.round(Math.max(0, Math.min(100, forecast)));
  return {
    forecast: finalForecast,
    delta: roundDelta,
    label,
    icon,
    className,
    volatile: volatility
  };
};

function initCharts() {
  if (typeof Chart === 'undefined') return;
  const tctx = document.getElementById('tempChart').getContext('2d');
  const fctx = document.getElementById('fanChart').getContext('2d');
  tempChart = new Chart(tctx, {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'CPU (°C)', data: [], borderColor: '#e74c3c', fill: false },
      { label: 'GPU (°C)', data: [], borderColor: '#3498db', fill: false },
    ]},
    options: { animation: false, responsive: true, scales: { y: { beginAtZero: true } } }
  });
  fanChart = new Chart(fctx, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: { animation: false, responsive: true, scales: { y: { beginAtZero: true, max: 100 } } }
  });
}

function pushData(chart, values) {
  const ts = new Date().toLocaleTimeString();
  chart.data.labels.push(ts);
  chart.data.labels.length > maxPoints && chart.data.labels.shift();
  chart.data.datasets.forEach((ds, idx) => {
    ds.data.push(values[idx] ?? null);
    ds.data.length > maxPoints && ds.data.shift();
  });
  chart.update('none');
}

function poll() {
  fetch('/status')
    .then(res => res.json())
    .then(data => {
      const ids = Array.isArray(data.fans_ids) ? data.fans_ids : [];
      const labels = Array.isArray(data.fan_labels) ? data.fan_labels : [];
      const groups = Array.isArray(data.fan_groups) ? data.fan_groups : [];
      const categories = Array.isArray(data.fan_categories) ? data.fan_categories : null;
      const rawFans = Array.isArray(data.fans) ? data.fans : [];
      const speeds = rawFans.map(v => {
        const num = parseInt(v, 10);
        if (Number.isNaN(num)) return 0;
        return Math.max(0, Math.min(100, num));
      });
      const nowTs = Date.now();
      speeds.forEach((value, idx) => {
        const key = ids[idx] || `fan${idx + 1}`;
        const history = ensureHistory(key);
        history.push({ t: nowTs, value });
        if (history.length > fanHistoryLimit) {
          history.splice(0, history.length - fanHistoryLimit);
        }
      });
      const cpu = parseInt(data.cpu || '0', 10) || 0;
      const gpu = parseInt(data.gpu || '0', 10) || 0;
      document.getElementById('cpu').innerText = data.cpu;
      document.getElementById('gpu').innerText = data.gpu;
      if (data.gpu_name) document.getElementById('gpu_name').innerText = `(${data.gpu_name})`;
      if (data.gpu_power) document.getElementById('gpu_power').innerText = `- ${data.gpu_power} W`;
      const health = document.getElementById('health');
      if (health && data.ok) {
        health.style.display = 'block';
        const summary = speeds.map((val, idx) => {
          const name = labels[idx] || ids[idx] || `Fan ${idx + 1}`;
          const group = groups[idx] ? `${groups[idx]} ` : '';
          return `${group}${name} ${val}%`;
        }).join(', ');
        health.innerText = `Status OK • Fans: ${summary}`;
      }
      const fanList = document.getElementById('fan-speeds');
      fanList.innerHTML = '';
      const ilo = data.ilo_fan_percents || {};
      const bits = data.fan_bits || [];
      const pickHeatClass = (pct) => {
        if (pct >= 90) return { bar: 'bar-hot', item: 'fan-hot' };
        if (pct >= 70) return { bar: 'bar-warm', item: 'fan-warm' };
        if (pct >= 45) return { bar: 'bar-cool', item: 'fan-cool' };
        return { bar: 'bar-chill', item: 'fan-chill' };
      };

      const buildFanRow = (idx, options = {}) => {
        if (idx == null || idx < 0 || idx >= speeds.length) {
          return null;
        }
        const { showGroup = true, overrideSpeed } = options;
        const li = document.createElement('li');
        li.className = 'fan-item';
        const fanId = ids[idx] || `fan${idx + 1}`;
        const label = labels[idx] || fanId;
        const group = groups[idx] || '';
        const header = document.createElement('div');
        header.className = 'fan-header';
        const labelWrap = document.createElement('div');
        labelWrap.className = 'fan-label';
        if (showGroup && group) {
          const badge = document.createElement('span');
          badge.className = 'fan-badge';
          badge.textContent = group;
          labelWrap.appendChild(badge);
        }
        const nameSpan = document.createElement('span');
        nameSpan.className = 'fan-name';
        nameSpan.textContent = label;
        labelWrap.appendChild(nameSpan);
        const idSpan = document.createElement('span');
        idSpan.className = 'fan-id';
        idSpan.textContent = fanId;
        labelWrap.appendChild(idSpan);
        header.appendChild(labelWrap);
        const valueSpan = document.createElement('div');
        valueSpan.className = 'fan-value';
        let displayValue = speeds[idx];
        if (displayValue === undefined || displayValue === null || displayValue === '') {
          displayValue = overrideSpeed ?? 0;
        }
        const pctNum = parseInt(displayValue, 10);
        const pctWidth = Number.isNaN(pctNum) ? 0 : Math.max(0, Math.min(100, pctNum));
        valueSpan.textContent = `${pctWidth}%`;
        header.appendChild(valueSpan);
        li.appendChild(header);
        li.dataset.percent = pctWidth;
        const heatClass = pickHeatClass(pctWidth);
        li.classList.add(heatClass.item);
        const meter = document.createElement('div');
        meter.className = 'meter';
        meter.setAttribute('role', 'progressbar');
        meter.setAttribute('aria-valuemin', '0');
        meter.setAttribute('aria-valuemax', '100');
        meter.setAttribute('aria-valuenow', pctWidth);
        const bar = document.createElement('div');
        bar.className = `bar ${heatClass.bar}`;
        bar.style.width = `${pctWidth}%`;
        bar.style.setProperty('--initial-width', `${pctWidth}%`);
        bar.dataset.percent = pctWidth;
        meter.appendChild(bar);
        li.appendChild(meter);
        const history = fanHistory[fanId] || [];
        const trend = computeTrend(history);
        const trendWrap = document.createElement('div');
        trendWrap.className = 'fan-trend';
        const iconSpan = document.createElement('span');
        iconSpan.className = `trend-icon ${trend.className}`;
        iconSpan.textContent = trend.icon;
        const labelSpan = document.createElement('span');
        labelSpan.className = 'trend-label';
        labelSpan.textContent = trend.label;
        const forecastSpan = document.createElement('span');
        forecastSpan.className = 'trend-forecast';
        const deltaText = trend.delta === 0 ? '' : (trend.delta > 0 ? ` (+${trend.delta}%)` : ` (${trend.delta}%)`);
        forecastSpan.textContent = `${trendHorizonSeconds}s ≈ ${trend.forecast}%${deltaText}`;
        trendWrap.appendChild(iconSpan);
        trendWrap.appendChild(labelSpan);
        trendWrap.appendChild(forecastSpan);
        li.appendChild(trendWrap);
        li.dataset.forecast = trend.forecast;
        li.dataset.trend = trend.className.replace('trend-', '');
        if (trend.volatile) {
          li.dataset.volatility = 'high';
        } else {
          delete li.dataset.volatility;
        }
        const detailParts = [];
        detailParts.push(`Now ${pctWidth}%`);
        detailParts.push(`Forecast ${trend.forecast}%`);
        if (trend.delta !== 0) {
          detailParts.push(`Δ${trendHorizonSeconds}s ${trend.delta > 0 ? '+' : ''}${trend.delta}%`);
        }
        const raw = bits[idx] ?? '';
        if (raw !== undefined && raw !== null && raw !== '') {
          detailParts.push(`raw ${raw}/255`);
        }
        if (ilo && Object.prototype.hasOwnProperty.call(ilo, fanId)) {
          detailParts.push(`iLO ${ilo[fanId]}%`);
        }
        const detail = document.createElement('div');
        detail.className = 'fan-detail';
        detail.textContent = detailParts.join(' • ');
        li.appendChild(detail);
        const shouldStrobe = pctWidth >= 92 || trend.forecast >= 95 || Math.abs(trend.delta) >= 12 || trend.volatile;
        if (shouldStrobe) {
          li.classList.add('strobe-active');
        } else {
          li.classList.remove('strobe-active');
        }
        return li;
      };

      let appended = false;
      if (categories && categories.length) {
        categories.forEach(cat => {
          if (!cat || !Array.isArray(cat.items) || cat.items.length === 0) return;
          const catLi = document.createElement('li');
          catLi.className = 'fan-category';
          const title = document.createElement('div');
          title.className = 'fan-category-title';
          title.textContent = cat.name || 'Fans';
          catLi.appendChild(title);
          const inner = document.createElement('ul');
          inner.className = 'fan-category-items';
          cat.items.forEach(item => {
            const idx = typeof item.index === 'number' ? item.index : ids.indexOf(item.id);
            const row = buildFanRow(idx, { showGroup: false, overrideSpeed: item.speed });
            if (row) {
              inner.appendChild(row);
            }
          });
          if (inner.children.length) {
            catLi.appendChild(inner);
            fanList.appendChild(catLi);
            appended = true;
          }
        });
      }
      if (!appended) {
        speeds.forEach((_, i) => {
          const row = buildFanRow(i, { showGroup: true });
          if (row) fanList.appendChild(row);
        });
      }
      // Sensors table
      const tbody = document.querySelector('#sensors tbody');
      if (tbody && Array.isArray(data.sensors)) {
        tbody.innerHTML = '';
        data.sensors.forEach(s => {
          const tr = document.createElement('tr');
          tr.innerHTML = `<td>${s.label}</td><td>${s.value}</td>`;
          tbody.appendChild(tr);
        });
      }
      // GPU info
      const gtbody = document.querySelector('#gpuinfo tbody');
      if (gtbody && Array.isArray(data.gpu_info)) {
        gtbody.innerHTML = '';
        data.gpu_info.forEach(g => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${g.index}</td>
            <td>${g.name}</td>
            <td>${g.bus_id}</td>
            <td>${g.temperature} °C</td>
            <td>${g.util_gpu}% / ${g.util_mem}%</td>
            <td>${g.util_enc || ''}% / ${g.util_dec || ''}%</td>
            <td>${g.mem_used}/${g.mem_total} MiB</td>
            <td>${g.power_draw}/${g.power_limit} W</td>
            <td>${g.pstate}</td>
            <td>GR ${g.clocks_gr} / MEM ${g.clocks_mem} / VID ${g.clocks_video}</td>
            <td>Gen ${g.pcie_gen_cur}/${g.pcie_gen_max} x${g.pcie_width_cur || ''}/${g.pcie_width_max || ''}</td>
            <td>${g.encoder_sessions ?? ''}</td>
          `;
          gtbody.appendChild(tr);
        });
      }
      if (window.Chart && tempChart && fanChart) {
        pushData(tempChart, [cpu, gpu]);
        // Initialize fan series to match fan count once
        if (!fanSeriesInit) {
          const colors = ['#2ecc71','#1abc9c','#9b59b6','#f1c40f','#e67e22','#34495e','#16a085','#8e44ad'];
          fanChart.data.datasets = speeds.map((_, i) => ({
            label: labels[i] || ids[i] || `Fan ${i+1}`, data: [], borderColor: colors[i % colors.length], fill: false
          }));
          fanSeriesInit = true;
        }
        if (fanChart.data.datasets.length === speeds.length) {
          fanChart.data.datasets.forEach((ds, idx) => {
            const lbl = labels[idx] || ids[idx] || ds.label;
            if (lbl && ds.label !== lbl) ds.label = lbl;
          });
        }
        const fans = speeds.map(v => v ?? null);
        pushData(fanChart, fans);
        // Persist last few points to localStorage (lightweight)
        try {
          const payload = {
            tLabels: tempChart.data.labels.slice(-60),
            tCPU: tempChart.data.datasets[0].data.slice(-60),
            tGPU: tempChart.data.datasets[1].data.slice(-60),
            fLabels: fanChart.data.labels.slice(-60),
            fSeries: fanChart.data.datasets.map(d => d.data.slice(-60)),
            fNames: labels,
          };
          localStorage.setItem('dfc_history', JSON.stringify(payload));
        } catch (_) {}
      }

      // Queue depth + last command indicators
      const qdiv = document.getElementById('queueInfo');
      if (qdiv) {
        const depth = data.ilo_queue_depth ?? 0;
        const waiting = data.ilo_queue_waiting ?? depth;
        const active = data.ilo_queue_active ?? 0;
        const last = data.ilo_last_cmd || null;
        if (depth || last || active) {
          qdiv.style.display = 'block';
          const queueText = `pending ${depth} (waiting ${waiting}, active ${active})`;
          const lastText = last ? ` • last [${last.time}] rc=${last.rc ?? ''} ${last.ms ?? ''}ms :: ${last.cmd || ''}` : '';
          qdiv.textContent = `iLO queue: ${queueText}${lastText}`;
        } else {
          qdiv.style.display = 'none';
        }
      }
    })
    .catch(() => {});
}

window.addEventListener('load', () => {
  if (window.Chart) initCharts();
  // restore history if present
  try {
    const raw = localStorage.getItem('dfc_history');
    if (raw && tempChart && fanChart) {
      const h = JSON.parse(raw);
      tempChart.data.labels = h.tLabels || [];
      tempChart.data.datasets[0].data = h.tCPU || [];
      tempChart.data.datasets[1].data = h.tGPU || [];
      tempChart.update('none');
      // Rebuild fan datasets to the stored count if needed
      const colors = ['#2ecc71','#1abc9c','#9b59b6','#f1c40f','#e67e22','#34495e','#16a085','#8e44ad'];
      const seriesCount = (h.fSeries && h.fSeries.length) || 0;
      if (fanChart.data.datasets.length !== seriesCount) {
        fanChart.data.datasets = Array.from({length: seriesCount}, (_, i) => ({
          label: (h.fNames && h.fNames[i]) || `Fan ${i+1}`, data: [], borderColor: colors[i % colors.length], fill: false
        }));
      }
      fanChart.data.labels = h.fLabels || [];
      fanChart.data.datasets.forEach((d, i) => { d.data = (h.fSeries && h.fSeries[i]) || []; });
      if (h.fNames) {
        fanChart.data.datasets.forEach((d, i) => {
          if (h.fNames[i]) d.label = h.fNames[i];
        });
      }
      fanChart.update('none');
      fanSeriesInit = fanChart.data.datasets.length > 0;
    }
  } catch (_) {}
  poll();
  setInterval(poll, 5000);
});

function loadILORecent() {
  fetch('/ilo_recent').then(r => r.json()).then(arr => {
    const pre = document.getElementById('iloRecent');
    if (!pre) return;
    const lines = (arr || []).map(x => {
      try {
        return `[${x.time}] rc=${x.rc} ${x.ms}ms :: ${x.cmd}\n${(x.out||'').trim()}`;
      } catch (_) {
        return JSON.stringify(x);
      }
    });
    pre.textContent = lines.join('\n\n');
  }).catch(() => {});
}

function loadLogs() {
  fetch('/logs').then(r => r.json()).then(obj => {
    const pre = document.getElementById('logs');
    if (!pre) return;
    if (obj.ok) pre.textContent = obj.text || ''; else pre.textContent = obj.error || 'Error fetching logs';
  }).catch(() => {});
}

function triggerSelfUpdate() {
  const statusEl = document.getElementById('selfUpdateStatus');
  const btn = document.getElementById('selfUpdateButton');
  if (btn) btn.disabled = true;
  setUpdateInProgress(true);
  if (statusEl) {
    statusEl.textContent = 'Checking for updates…';
    statusEl.className = 'status-text pending';
  }
  const shortCommit = (rev) => (rev && typeof rev === 'string') ? rev.substring(0, 8) : 'unknown';
  fetch('/self_update', { method: 'POST' })
    .then(async res => {
      let payload = {};
      try {
        payload = await res.json();
      } catch (_) {
        payload = {};
      }
      if (!res.ok || !payload.ok) {
        const msg = payload.error || `HTTP ${res.status}`;
        if (statusEl) {
          statusEl.textContent = `Update failed: ${msg}`;
          statusEl.className = 'status-text error';
        }
        return;
      }
      const before = shortCommit(payload.before);
      const after = shortCommit(payload.after);
      const changed = !!payload.changed;
      const summary = changed ? `${before} → ${after}` : `${after} (up-to-date)`;
      const info = (payload.output || '').split('\n').map(s => s.trim()).filter(Boolean).slice(-2).join(' | ');
      const preserved = Array.isArray(payload.preserved) ? payload.preserved.filter(Boolean) : [];
      const restart = payload.restart || {};
      if (statusEl) {
        const parts = [];
        parts.push(changed ? `Update complete: ${summary}` : `Already up to date: ${summary}`);
        if (info) parts.push(info);
        if (preserved.length) parts.push(`Preserved ${preserved.join(', ')}`);
        if (restart && restart.ran) {
          parts.push(restart.output ? `Restarted (${restart.output})` : 'Restarted service');
        } else if (restart && restart.output && changed) {
          parts.push(restart.output);
        }
        statusEl.textContent = parts.join(' • ');
        statusEl.className = 'status-text success';
      }
    })
    .catch(() => {
      if (statusEl) {
        statusEl.textContent = 'Update failed: network error';
        statusEl.className = 'status-text error';
      }
    })
    .finally(() => {
      setUpdateInProgress(false);
      setTimeout(() => {
        if (btn) btn.disabled = false;
      }, 400);
    });
}

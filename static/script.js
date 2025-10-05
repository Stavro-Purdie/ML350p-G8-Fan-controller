// Charts setup
let tempChart, fanChart;
let fanSeriesInit = false;
const maxPoints = 120; // ~10 minutes at 5s

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
      const cpu = parseInt(data.cpu || '0', 10) || 0;
      const gpu = parseInt(data.gpu || '0', 10) || 0;
      document.getElementById('cpu').innerText = data.cpu;
      document.getElementById('gpu').innerText = data.gpu;
      if (data.gpu_name) document.getElementById('gpu_name').innerText = `(${data.gpu_name})`;
      if (data.gpu_power) document.getElementById('gpu_power').innerText = `- ${data.gpu_power} W`;
      const health = document.getElementById('health');
      if (health && data.ok) {
        health.style.display = 'block';
        const summary = data.fans.map((val, idx) => {
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
      const buildFanRow = (idx, options = {}) => {
        if (idx == null || idx < 0 || idx >= data.fans.length) {
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
        const prefix = showGroup && group ? `${group} • ` : '';
        header.textContent = `${prefix}${label} (${fanId})`;
        li.appendChild(header);
        const meter = document.createElement('div');
        meter.className = 'meter';
        const bar = document.createElement('div');
        bar.className = 'bar';
        let displayValue = data.fans[idx];
        if (displayValue === undefined || displayValue === null || displayValue === '') {
          displayValue = overrideSpeed ?? 0;
        }
        const pctNum = parseInt(displayValue, 10);
        const pctWidth = Number.isNaN(pctNum) ? 0 : Math.max(0, Math.min(100, pctNum));
        bar.style.width = `${pctWidth}%`;
        meter.appendChild(bar);
        li.appendChild(meter);
        const raw = bits[idx] ?? '';
        const iloPct = (ilo && Object.prototype.hasOwnProperty.call(ilo, fanId)) ? ` | iLO ${ilo[fanId]}%` : '';
        const detail = document.createElement('div');
        detail.className = 'fan-detail';
        detail.textContent = `${displayValue}% (raw ${raw || ''}/255)${iloPct}`;
        li.appendChild(detail);
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
        data.fans.forEach((_, i) => {
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
          fanChart.data.datasets = data.fans.map((_, i) => ({
            label: labels[i] || ids[i] || `Fan ${i+1}`, data: [], borderColor: colors[i % colors.length], fill: false
          }));
          fanSeriesInit = true;
        }
        if (fanChart.data.datasets.length === data.fans.length) {
          fanChart.data.datasets.forEach((ds, idx) => {
            const lbl = labels[idx] || ids[idx] || ds.label;
            if (lbl && ds.label !== lbl) ds.label = lbl;
          });
        }
        const fans = data.fans.map(v => v ?? null);
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

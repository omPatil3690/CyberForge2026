/**
 * CyberForge Executive Analytics Dashboard Controller
 * Powered by DuckDB Aggregation Layer & FastAPI
 */

// Application State
const state = {
  mode: 'company', // 'company' or 'store'
  companyId: 1,
  tenantId: 1,
  datePreset: 'all', // '7d', '30d', '90d', 'all'
  autoRefreshInterval: 5000,
  refreshTimer: null,
  isRefreshing: false,
  charts: {
    trend: null,
    status: null,
    topProducts: null
  }
};

// Date range calculation
function getDateRange() {
  const now = new Date();
  let fromDate = new Date();

  if (state.datePreset === '7d') {
    fromDate.setDate(now.getDate() - 7);
  } else if (state.datePreset === '30d') {
    fromDate.setDate(now.getDate() - 30);
  } else if (state.datePreset === '90d') {
    fromDate.setDate(now.getDate() - 90);
  } else {
    // 'all' covers the full synthetic dataset year
    return {
      from: '2026-01-01T00:00:00',
      to: '2026-12-31T23:59:59'
    };
  }

  return {
    from: fromDate.toISOString().split('.')[0],
    to: now.toISOString().split('.')[0]
  };
}

// Formatting helpers
function formatCurrency(val) {
  if (val === undefined || val === null) return '$0.00';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(val);
}

function formatNumber(val) {
  if (val === undefined || val === null) return '0';
  return new Intl.NumberFormat('en-US').format(val);
}

// Initialize Dashboard
document.addEventListener('DOMContentLoaded', () => {
  initUIEvents();
  initCharts();
  refreshAllData();
  setupAutoRefresh();
});

// UI Event Listeners
function initUIEvents() {
  // Store / Company Selector
  const selector = document.getElementById('store-selector');
  if (selector) {
    selector.addEventListener('change', (e) => {
      const val = e.target.value;
      if (val === 'company') {
        state.mode = 'company';
      } else {
        state.mode = 'store';
        state.tenantId = parseInt(val, 10);
      }
      updateContextBadge();
      refreshAllData();
    });
  }

  // Date Range Presets
  const presetButtons = document.querySelectorAll('.date-preset-btn');
  presetButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      presetButtons.forEach(b => b.classList.remove('bg-cyan-500/20', 'text-cyan-400', 'border-cyan-500/50'));
      btn.classList.add('bg-cyan-500/20', 'text-cyan-400', 'border-cyan-500/50');
      state.datePreset = btn.getAttribute('data-preset');
      refreshAllData();
    });
  });

  // Auto Refresh Interval Selector
  const refreshSelect = document.getElementById('refresh-interval-select');
  if (refreshSelect) {
    refreshSelect.addEventListener('change', (e) => {
      const val = parseInt(e.target.value, 10);
      state.autoRefreshInterval = val;
      setupAutoRefresh();
    });
  }

  // Manual Refresh Button
  const refreshBtn = document.getElementById('btn-manual-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      refreshAllData();
    });
  }
}

function updateContextBadge() {
  const badge = document.getElementById('context-badge');
  const isolationBadge = document.getElementById('isolation-badge');
  if (!badge) return;

  if (state.mode === 'company') {
    badge.innerText = 'Company Owner Rollup (All 10 Stores / 1M Orders)';
    badge.className = 'text-xs font-semibold px-2.5 py-1 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/30';
    if (isolationBadge) {
      isolationBadge.innerText = 'Cross-Tenant OLAP Rollup (10 DuckDB Replicas)';
    }
  } else {
    badge.innerText = `Store #${state.tenantId} (Dedicated Physical DuckDB)`;
    badge.className = 'text-xs font-semibold px-2.5 py-1 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
    if (isolationBadge) {
      isolationBadge.innerText = `Air-Gapped: data/tenants/tenant_${state.tenantId}.duckdb`;
    }
  }
}

// Chart Initialization
function initCharts() {
  // 1. Revenue & Orders Trend Chart (Dual Axis)
  const trendOptions = {
    chart: {
      type: 'line',
      height: 320,
      background: 'transparent',
      toolbar: { show: false },
      animations: { enabled: true, easing: 'easeinout', speed: 600 }
    },
    theme: { mode: 'dark' },
    colors: ['#06b6d4', '#8b5cf6'],
    stroke: { curve: 'smooth', width: [3, 0] },
    fill: {
      type: ['gradient', 'solid'],
      gradient: {
        shade: 'dark',
        type: 'vertical',
        shadeIntensity: 0.5,
        opacityFrom: 0.35,
        opacityTo: 0.05
      }
    },
    series: [
      { name: 'Revenue ($)', type: 'area', data: [] },
      { name: 'Orders Count', type: 'column', data: [] }
    ],
    xaxis: {
      type: 'category',
      categories: [],
      axisBorder: { color: '#374151' },
      axisTicks: { color: '#374151' },
      labels: { style: { colors: '#9ca3af', fontSize: '11px' } }
    },
    yaxis: [
      {
        title: { text: 'Revenue ($)', style: { color: '#06b6d4' } },
        labels: {
          style: { colors: '#9ca3af' },
          formatter: (v) => `$${(v / 1000).toFixed(0)}k`
        }
      },
      {
        opposite: true,
        title: { text: 'Orders', style: { color: '#8b5cf6' } },
        labels: {
          style: { colors: '#9ca3af' },
          formatter: (v) => v ? v.toLocaleString() : '0'
        }
      }
    ],
    grid: { borderColor: '#1f2937', strokeDashArray: 3 },
    tooltip: {
      shared: true,
      theme: 'dark',
      y: {
        formatter: (val, opts) => {
          if (opts.seriesIndex === 0) return formatCurrency(val);
          return `${formatNumber(val)} orders`;
        }
      }
    },
    legend: { labels: { colors: '#9ca3af' }, position: 'top' }
  };
  state.charts.trend = new ApexCharts(document.querySelector("#chart-trend"), trendOptions);
  state.charts.trend.render();

  // 2. Status Breakdown Donut Chart
  const statusOptions = {
    chart: {
      type: 'donut',
      height: 300,
      background: 'transparent'
    },
    theme: { mode: 'dark' },
    colors: ['#10b981', '#06b6d4', '#f59e0b', '#f43f5e'],
    labels: ['Delivered', 'Shipped', 'Placed', 'Cancelled'],
    series: [0, 0, 0, 0],
    dataLabels: { enabled: false },
    stroke: { colors: ['#111827'], width: 2 },
    plotOptions: {
      pie: {
        donut: {
          size: '72%',
          labels: {
            show: true,
            total: {
              show: true,
              label: 'Total Orders',
              color: '#9ca3af',
              fontSize: '12px',
              formatter: () => state.totalOrdersCount ? formatNumber(state.totalOrdersCount) : '0'
            },
            value: {
              color: '#f3f4f6',
              fontSize: '20px',
              fontWeight: 700,
              formatter: (v) => formatNumber(v)
            }
          }
        }
      }
    },
    legend: { position: 'bottom', labels: { colors: '#9ca3af' } },
    tooltip: {
      theme: 'dark',
      y: { formatter: (v) => `${formatNumber(v)} orders` }
    }
  };
  state.charts.status = new ApexCharts(document.querySelector("#chart-status"), statusOptions);
  state.charts.status.render();

  // 3. Top Products Horizontal Bar Chart
  const topProductsOptions = {
    chart: {
      type: 'bar',
      height: 290,
      background: 'transparent',
      toolbar: { show: false }
    },
    theme: { mode: 'dark' },
    colors: ['#06b6d4'],
    plotOptions: {
      bar: {
        horizontal: true,
        borderRadius: 4,
        barHeight: '65%'
      }
    },
    dataLabels: {
      enabled: true,
      textAnchor: 'start',
      style: { colors: ['#f3f4f6'], fontSize: '11px' },
      formatter: (val, opt) => `$${(val / 1000).toFixed(1)}k`,
      offsetX: 10
    },
    series: [{ name: 'Revenue', data: [] }],
    xaxis: {
      categories: [],
      labels: {
        style: { colors: '#9ca3af' },
        formatter: (v) => `$${(v / 1000).toFixed(0)}k`
      },
      axisBorder: { color: '#374151' }
    },
    yaxis: {
      labels: {
        style: { colors: '#d1d5db', fontSize: '12px' },
        maxWidth: 180
      }
    },
    grid: { borderColor: '#1f2937', strokeDashArray: 3 },
    tooltip: {
      theme: 'dark',
      y: { formatter: (v) => formatCurrency(v) }
    }
  };
  state.charts.topProducts = new ApexCharts(document.querySelector("#chart-top-products"), topProductsOptions);
  state.charts.topProducts.render();
}

// Main Refresh Function
async function refreshAllData() {
  if (state.isRefreshing) return;
  state.isRefreshing = true;

  const refreshIcon = document.getElementById('refresh-icon');
  if (refreshIcon) refreshIcon.classList.add('animate-spin');

  const t0 = performance.now();

  try {
    await Promise.all([
      fetchSummary(),
      fetchTrend(),
      fetchTopProducts(),
      fetchStatusBreakdown(),
      fetchCustomMetrics(),
      fetchObservability()
    ]);
  } catch (err) {
    console.error('Error fetching dashboard metrics:', err);
  } finally {
    const elapsed = (performance.now() - t0).toFixed(1);
    const renderTimeBadge = document.getElementById('render-latency-badge');
    if (renderTimeBadge) renderTimeBadge.innerText = `${elapsed} ms full render`;

    if (refreshIcon) refreshIcon.classList.remove('animate-spin');
    state.isRefreshing = false;
  }
}

// 1. Fetch Summary
async function fetchSummary() {
  const dates = getDateRange();
  let url = '';
  if (state.mode === 'company') {
    url = `/companies/${state.companyId}/metrics/summary?from_date=${dates.from}&to_date=${dates.to}`;
  } else {
    url = `/tenants/${state.tenantId}/metrics/summary?from_date=${dates.from}&to_date=${dates.to}`;
  }

  const t0 = performance.now();
  const res = await fetch(url);
  const latency = (performance.now() - t0).toFixed(2);
  const data = await res.json();

  state.totalOrdersCount = data.order_count;

  // Update DOM
  document.getElementById('metric-revenue').innerText = formatCurrency(data.total_revenue);
  document.getElementById('metric-orders').innerText = formatNumber(data.order_count);
  document.getElementById('metric-aov').innerText = formatCurrency(data.avg_order_value);
  
  const latencyEl = document.getElementById('metric-query-latency');
  if (latencyEl) {
    latencyEl.innerText = `${latency} ms`;
  }

  const freshnessEl = document.getElementById('metric-freshness');
  if (freshnessEl && data.data_freshness_seconds !== undefined) {
    freshnessEl.innerText = `${data.data_freshness_seconds.toFixed(1)}s lag`;
  }
}

// 2. Fetch Trend
async function fetchTrend() {
  const dates = getDateRange();
  let url = '';
  if (state.mode === 'company') {
    url = `/companies/${state.companyId}/metrics/trend?interval=day&from_date=${dates.from}&to_date=${dates.to}`;
  } else {
    url = `/tenants/${state.tenantId}/metrics/trend?interval=day&from_date=${dates.from}&to_date=${dates.to}`;
  }

  const res = await fetch(url);
  const data = await res.json();
  const series = data.data || [];

  const categories = series.map(p => p.period.split('T')[0]);
  const revenues = series.map(p => p.revenue);
  const counts = series.map(p => p.order_count);

  if (state.charts.trend) {
    state.charts.trend.updateOptions({
      xaxis: { categories: categories }
    });
    state.charts.trend.updateSeries([
      { name: 'Revenue ($)', data: revenues },
      { name: 'Orders Count', data: counts }
    ]);
  }
}

// 3. Fetch Top Products
async function fetchTopProducts() {
  const dates = getDateRange();
  let url = '';
  if (state.mode === 'company') {
    url = `/companies/${state.companyId}/metrics/top-products?limit=6&from_date=${dates.from}&to_date=${dates.to}`;
  } else {
    url = `/tenants/${state.tenantId}/metrics/top-products?limit=6&from_date=${dates.from}&to_date=${dates.to}`;
  }

  const res = await fetch(url);
  const data = await res.json();
  const products = data.data || [];

  const names = products.map(p => p.name.length > 22 ? p.name.substring(0, 20) + '…' : p.name);
  const revenues = products.map(p => p.total_revenue);

  if (state.charts.topProducts) {
    state.charts.topProducts.updateOptions({
      xaxis: { categories: names }
    });
    state.charts.topProducts.updateSeries([{ name: 'Revenue', data: revenues }]);
  }

  // Populate leaderboard table
  const tbody = document.getElementById('table-top-products-body');
  if (tbody) {
    tbody.innerHTML = products.map((p, idx) => `
      <tr class="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
        <td class="py-2.5 px-3 font-mono text-cyan-400 font-medium">#${idx + 1}</td>
        <td class="py-2.5 px-3">
          <div class="font-medium text-gray-200 text-sm truncate max-w-[200px]" title="${p.name}">${p.name}</div>
          <div class="text-xs text-gray-400">${p.category || 'General'}</div>
        </td>
        <td class="py-2.5 px-3 text-right font-mono text-gray-300">${formatNumber(p.quantity_sold)}</td>
        <td class="py-2.5 px-3 text-right font-mono font-semibold text-emerald-400">${formatCurrency(p.total_revenue)}</td>
      </tr>
    `).join('');
  }
}

// 4. Fetch Status Breakdown
async function fetchStatusBreakdown() {
  const dates = getDateRange();
  let url = '';
  if (state.mode === 'company') {
    url = `/companies/${state.companyId}/metrics/breakdown/status?from_date=${dates.from}&to_date=${dates.to}`;
  } else {
    url = `/tenants/${state.tenantId}/metrics/breakdown/status?from_date=${dates.from}&to_date=${dates.to}`;
  }

  const res = await fetch(url);
  const data = await res.json();
  const list = data.data || [];

  // Map to Delivered, Shipped, Placed, Cancelled
  const statusMap = { delivered: 0, shipped: 0, placed: 0, cancelled: 0 };
  const revMap = { delivered: 0, shipped: 0, placed: 0, cancelled: 0 };
  list.forEach(item => {
    const s = item.status.toLowerCase();
    if (statusMap[s] !== undefined) {
      statusMap[s] = item.count;
      revMap[s] = item.revenue;
    }
  });

  if (state.charts.status) {
    state.charts.status.updateSeries([
      statusMap.delivered,
      statusMap.shipped,
      statusMap.placed,
      statusMap.cancelled
    ]);
  }

  // Update status summary badges
  const elDelivered = document.getElementById('status-val-delivered');
  if (elDelivered) elDelivered.innerText = `${formatNumber(statusMap.delivered)} (${formatCurrency(revMap.delivered)})`;

  const elShipped = document.getElementById('status-val-shipped');
  if (elShipped) elShipped.innerText = `${formatNumber(statusMap.shipped)} (${formatCurrency(revMap.shipped)})`;

  const elPlaced = document.getElementById('status-val-placed');
  if (elPlaced) elPlaced.innerText = `${formatNumber(statusMap.placed)} (${formatCurrency(revMap.placed)})`;

  const elCancelled = document.getElementById('status-val-cancelled');
  if (elCancelled) elCancelled.innerText = `${formatNumber(statusMap.cancelled)} (${formatCurrency(revMap.cancelled)})`;
}

// 5. Fetch Custom Dynamic Metrics (Bonus B-4)
async function fetchCustomMetrics() {
  const dates = getDateRange();
  const tenantId = state.mode === 'store' ? state.tenantId : 1;

  try {
    const [resCanc, resFulf] = await Promise.all([
      fetch(`/tenants/${tenantId}/metrics/custom/cancellation_rate?from_date=${dates.from}&to_date=${dates.to}`),
      fetch(`/tenants/${tenantId}/metrics/custom/fulfillment_rate?from_date=${dates.from}&to_date=${dates.to}`)
    ]);

    if (resCanc.ok) {
      const dataCanc = await resCanc.json();
      const elCanc = document.getElementById('custom-val-cancellation');
      if (elCanc) elCanc.innerText = `${dataCanc.value.toFixed(2)}%`;
    }

    if (resFulf.ok) {
      const dataFulf = await resFulf.json();
      const elFulf = document.getElementById('custom-val-fulfillment');
      if (elFulf) elFulf.innerText = `${dataFulf.value.toFixed(2)}%`;
    }
  } catch (e) {
    console.error('Error fetching custom dynamic metrics:', e);
  }
}

// 6. Fetch System Observability & Health (Bonus B-5)
async function fetchObservability() {
  try {
    const [healthRes, metricsRes] = await Promise.all([
      fetch('/admin/health'),
      fetch('/admin/metrics')
    ]);

    if (healthRes.ok) {
      const health = await healthRes.json();
      const elDuckOrders = document.getElementById('obs-duck-orders');
      if (elDuckOrders) elDuckOrders.innerText = formatNumber(health.duckdb_order_count);

      const elPgOrders = document.getElementById('obs-pg-orders');
      if (elPgOrders) elPgOrders.innerText = formatNumber(health.pg_order_count_estimate);

      const elLag = document.getElementById('obs-freshness-lag');
      if (elLag) elLag.innerText = `${health.data_freshness_seconds.toFixed(2)}s`;
    }

    if (metricsRes.ok) {
      const metrics = await metricsRes.json();
      const p50El = document.getElementById('obs-latency-p50');
      if (p50El) p50El.innerText = `${metrics.query_latency_p50_ms.toFixed(2)} ms`;

      const p95El = document.getElementById('obs-latency-p95');
      if (p95El) p95El.innerText = `${metrics.query_latency_p95_ms.toFixed(2)} ms`;

      const p99El = document.getElementById('obs-latency-p99');
      if (p99El) p99El.innerText = `${metrics.query_latency_p99_ms.toFixed(2)} ms`;

      const cacheEl = document.getElementById('obs-cache-ratio');
      if (cacheEl) cacheEl.innerText = `${(metrics.cache_hit_ratio * 100).toFixed(0)}% DuckDB`;
    }
  } catch (e) {
    console.error('Error fetching admin observability:', e);
  }
}

// Auto-Refresh Loop
function setupAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }

  if (state.autoRefreshInterval > 0) {
    state.refreshTimer = setInterval(() => {
      refreshAllData();
    }, state.autoRefreshInterval);
  }
}

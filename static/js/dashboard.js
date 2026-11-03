(() => {
  const data = window.smartFactDashboard;
  if (!data) return;

  const colors = ['#3478f6', '#20a78a', '#8b66e8', '#f2a93b', '#e46a83', '#9aa9bd'];
  const money = new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', maximumFractionDigits: 0 });
  const compactMoney = new Intl.NumberFormat('es-CO', { notation: 'compact', maximumFractionDigits: 1 });
  const svg = (tag, attrs = {}) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  };

  const renderPurchasesChart = () => {
    const container = document.getElementById('purchases-chart');
    if (!container) return;
    const series = Array.isArray(data.months) ? data.months : [];
    if (!series.length || series.every(point => Number(point.value) === 0)) {
      container.textContent = container.dataset.empty;
      container.classList.add('chart-empty');
      return;
    }

    const width = 720;
    const height = 254;
    const pad = { top: 14, right: 14, bottom: 42, left: 60 };
    const chartWidth = width - pad.left - pad.right;
    const chartHeight = height - pad.top - pad.bottom;
    const max = Math.max(...series.map(point => Number(point.value) || 0), 1);
    const slot = chartWidth / series.length;
    const barWidth = Math.max(4, Math.min(32, slot * .56));
    const chart = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'presentation', preserveAspectRatio: 'none' });
    const defs = svg('defs');
    const gradient = svg('linearGradient', { id: 'purchases-gradient', x1: '0', y1: '0', x2: '0', y2: '1' });
    const stopTop = svg('stop', { offset: '0%', 'stop-color': '#4c8df7' });
    const stopBottom = svg('stop', { offset: '100%', 'stop-color': '#9fc1ff' });
    gradient.append(stopTop, stopBottom);
    defs.append(gradient);
    chart.append(defs);

    for (let tick = 0; tick <= 4; tick += 1) {
      const y = pad.top + chartHeight * tick / 4;
      chart.append(svg('line', { x1: pad.left, y1: y, x2: width - pad.right, y2: y, class: 'chart-grid-line' }));
      const label = svg('text', { x: pad.left - 9, y: y + 4, 'text-anchor': 'end', class: 'chart-axis-label' });
      label.textContent = compactMoney.format(max * (4 - tick) / 4);
      chart.append(label);
    }

    const labelStep = Math.max(1, Math.ceil(series.length / 8));
    series.forEach((point, index) => {
      const value = Math.max(0, Number(point.value) || 0);
      const barHeight = value ? Math.max(2, chartHeight * value / max) : 0;
      const x = pad.left + slot * index + (slot - barWidth) / 2;
      const y = pad.top + chartHeight - barHeight;
      const bar = svg('rect', { x, y, width: barWidth, height: barHeight, rx: Math.min(5, barWidth / 2), class: 'purchase-bar' });
      bar.setAttribute('fill', 'url(#purchases-gradient)');
      const title = svg('title');
      title.textContent = `${point.label}: ${money.format(value)}`;
      bar.append(title);
      chart.append(bar);

      if (index % labelStep === 0 || index === series.length - 1) {
        const label = svg('text', { x: pad.left + slot * index + slot / 2, y: height - 12, 'text-anchor': 'middle', class: 'chart-axis-label chart-month-label' });
        label.textContent = point.label;
        chart.append(label);
      }
    });
    container.replaceChildren(chart);
  };

  const renderSupplierChart = () => {
    const donut = document.getElementById('supplier-donut');
    const legend = document.getElementById('supplier-legend');
    if (!donut || !legend) return;
    const rows = (Array.isArray(data.suppliers) ? data.suppliers : [])
      .map(row => ({ name: String(row.name || 'Proveedor'), value: Math.max(0, Number(row.total) || 0) }))
      .filter(row => row.value > 0);
    const total = Math.max(0, Number(data.totalSpent) || 0);
    const topTotal = rows.reduce((sum, row) => sum + row.value, 0);
    if (total > topTotal + .5) rows.push({ name: 'Otros proveedores', value: total - topTotal });
    const sum = rows.reduce((acc, row) => acc + row.value, 0);

    if (!sum) {
      donut.classList.add('donut-empty');
      donut.setAttribute('aria-label', 'No hay compras confirmadas para distribuir');
      donut.replaceChildren();
      legend.textContent = 'Sin compras confirmadas en este período.';
      legend.classList.add('legend-empty');
      return;
    }

    let cursor = 0;
    const segments = rows.map((row, index) => {
      const start = cursor;
      cursor += row.value / sum * 100;
      return `${colors[index % colors.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
    });
    donut.style.background = `conic-gradient(${segments.join(', ')})`;
    donut.setAttribute('aria-label', `Distribución de compras entre ${rows.length} proveedores`);
    legend.replaceChildren();
    rows.forEach((row, index) => {
      const entry = document.createElement('div');
      entry.className = 'supplier-legend-row';
      const swatch = document.createElement('span');
      swatch.className = 'supplier-legend-swatch';
      swatch.style.backgroundColor = colors[index % colors.length];
      const name = document.createElement('span');
      name.className = 'supplier-legend-name';
      name.textContent = row.name;
      name.title = row.name;
      const amount = document.createElement('strong');
      amount.textContent = money.format(row.value);
      entry.append(swatch, name, amount);
      legend.append(entry);
    });
  };

  renderPurchasesChart();
  renderSupplierChart();
})();

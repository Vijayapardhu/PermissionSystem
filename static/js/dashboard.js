document.addEventListener('DOMContentLoaded', () => {
  if (typeof Chart === 'undefined') return;

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = '#475569';

  const css = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };

  const palette = [
    css('--au-primary', '#004e92'),
    css('--au-success', '#10b981'),
    css('--au-warning', '#f59e0b'),
    css('--au-danger', '#ef4444'),
    '#6366f1',
    '#0ea5e9',
  ];

  const readData = (canvas) => ({
    labels: JSON.parse(canvas.dataset.labels || '[]'),
    values: JSON.parse(canvas.dataset.values || '[]'),
  });

  const baseOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { position: 'bottom', labels: { usePointStyle: true, padding: 16 } },
    },
  };

  // Approval / rejection / pending split
  const statusCanvas = document.getElementById('statusChart');
  if (statusCanvas) {
    const { labels, values } = readData(statusCanvas);
    new Chart(statusCanvas, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: [css('--au-success', '#10b981'), css('--au-danger', '#ef4444'), css('--au-warning', '#f59e0b')],
          borderWidth: 0,
          hoverOffset: 8,
        }],
      },
      options: {
        ...baseOptions,
        cutout: '58%',
        plugins: {
          ...baseOptions.plugins,
          tooltip: {
            callbacks: {
              label: (item) => `${item.label}: ${item.raw}%`,
            },
          },
        },
      },
    });
  }

  // Leave vs classroom
  const typeCanvas = document.getElementById('typeChart');
  if (typeCanvas) {
    const { labels, values } = readData(typeCanvas);
    new Chart(typeCanvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Requests',
          data: values,
          backgroundColor: [css('--au-primary', '#004e92'), '#0ea5e9'],
          borderRadius: 6,
          maxBarThickness: 70,
        }],
      },
      options: {
        ...baseOptions,
        indexAxis: 'y',
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0 } },
          y: { grid: { display: false } },
        },
      },
    });
  }

  // Reason categories
  const reasonCanvas = document.getElementById('reasonChart');
  if (reasonCanvas) {
    const { labels, values } = readData(reasonCanvas);
    new Chart(reasonCanvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Requests',
          data: values,
          backgroundColor: labels.map((_, i) => palette[i % palette.length]),
          borderRadius: 6,
        }],
      },
      options: {
        ...baseOptions,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0 } },
          x: { grid: { display: false } },
        },
      },
    });
  }

  // Seven day trend
  const trendCanvas = document.getElementById('trendChart');
  if (trendCanvas) {
    const { labels, values } = readData(trendCanvas);
    new Chart(trendCanvas, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Requests',
          data: values,
          borderColor: css('--au-primary', '#004e92'),
          backgroundColor: 'rgba(0, 78, 146, 0.10)',
          fill: true,
          tension: 0.35,
          pointRadius: 3,
        }],
      },
      options: {
        ...baseOptions,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0 } },
          x: { grid: { display: false } },
        },
      },
    });
  }
});
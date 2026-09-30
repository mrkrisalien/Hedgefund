(function (root) {
  const chartViews = {};
  const deltaChartViews = {};

  function formatNseTime(time) {
    const sec = typeof time === "number" ? time : Number(time && time.timestamp);
    if (!Number.isFinite(sec)) return "";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(sec * 1000));
  }

  function nseTimeScaleOptions() {
    return {
      borderColor: "#1c2738",
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time) => formatNseTime(time),
    };
  }

  function marketPrice(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return number.toLocaleString(undefined, {
      minimumFractionDigits: number < 100 ? 2 : 0,
      maximumFractionDigits: number < 100 ? 4 : 2,
    });
  }

  function watchLevels(rows) {
    const map = {};
    (rows || []).forEach((row) => {
      [row.symbol, row.name, row.input_symbol].forEach((value) => {
        const key = String(value || "").trim().toUpperCase();
        if (key) map[key] = row;
      });
    });
    return map;
  }

  function chartDomId(item) {
    return "chart-" + String(item.symbol || item.name || "").replace(/[^A-Za-z0-9]/g, "");
  }

  function deltaChartDomId(item) {
    return "delta-chart-" + String(item.symbol || "").replace(/[^A-Za-z0-9]/g, "");
  }

  function ensureChartCard(item, gridId) {
    const id = chartDomId(item);
    let col = document.getElementById(id + "-col");
    if (!col) {
      col = document.createElement("div");
      col.className = "col-12 col-md-6 col-xl-3";
      col.id = id + "-col";
      col.innerHTML = `<div class="card chart-card h-100">
        <div class="d-flex justify-content-between align-items-center">
          <div class="label mb-0">${item.rank || ""}. ${item.symbol || item.name || ""}</div>
          <div class="chart-last" id="${id}-last">--</div>
        </div>
        <div class="small text-secondary mb-1">${item.name || ""}</div>
        <div class="small mb-1" id="${id}-levels">ENTRY -- · SL -- · TARGET --</div>
        <div class="chart-shell" id="${id}"></div>
      </div>`;
      const grid = document.getElementById(gridId);
      if (grid) grid.appendChild(col);
    }
    return id;
  }

  function upsertChart(item, levels) {
    const id = ensureChartCard(item, "chartGrid");
    const lastEl = document.getElementById(id + "-last");
    if (lastEl) lastEl.textContent = item.last != null ? Number(item.last).toFixed(2) : (item.error || "no data");
    const host = document.getElementById(id);
    if (!host || typeof LightweightCharts === "undefined") return;
    const candles = item.candles || [];
    if (!chartViews[id]) {
      const chart = LightweightCharts.createChart(host, {
        layout: { background: { color: "#101826" }, textColor: "#ffffff" },
        grid: { vertLines: { color: "#1c2738" }, horzLines: { color: "#1c2738" } },
        rightPriceScale: { borderColor: "#1c2738" },
        localization: { timeFormatter: (time) => formatNseTime(time) },
        timeScale: nseTimeScaleOptions(),
        width: host.clientWidth,
        height: host.clientHeight || 280,
      });
      const series = chart.addCandlestickSeries({
        upColor: "#3dd68c",
        downColor: "#ff5c7a",
        borderVisible: false,
        wickUpColor: "#3dd68c",
        wickDownColor: "#ff5c7a",
      });
      const hidden = {
        color: "rgba(0,0,0,0)",
        lineVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      };
      chartViews[id] = {
        chart,
        series,
        lowerBound: chart.addLineSeries(hidden),
        upperBound: chart.addLineSeries(hidden),
        lines: [],
      };
      new ResizeObserver(() => {
        chart.applyOptions({ width: host.clientWidth, height: host.clientHeight || 280 });
      }).observe(host);
    }
    const view = chartViews[id];
    if (candles.length) view.series.setData(candles);
    const lvl =
      levels[String(item.symbol || "").toUpperCase()] ||
      levels[String(item.name || "").toUpperCase()] ||
      {};
    const levelEl = document.getElementById(id + "-levels");
    if (levelEl) {
      const exitBit = lvl.exit != null
        ? ` · <span style="color:#f5c542">EXIT ${marketPrice(lvl.exit)}</span>`
        : "";
      levelEl.innerHTML =
        `<span style="color:#4ea1ff">ENTRY ${lvl.entry != null ? marketPrice(lvl.entry) : "--"}</span>` +
        ` · <span style="color:#ff5c7a">SL ${lvl.sl != null ? marketPrice(lvl.sl) : "--"}</span>` +
        ` · <span style="color:#3dd68c">TARGET ${lvl.tp != null ? marketPrice(lvl.tp) : "--"}</span>` +
        exitBit;
    }
    (view.lines || []).forEach((line) => {
      try { view.series.removePriceLine(line); } catch (error) {}
    });
    view.lines = [];
    [
      [lvl.trigger, "#ffb547", "RANGE BREAK"],
      [lvl.entry, "#4ea1ff", "ENTRY"],
      [lvl.sl, "#ff5c7a", "SL"],
      [lvl.tp, "#3dd68c", "TP"],
      [lvl.exit, "#f5c542", "EXIT"],
    ].forEach(([price, color, title]) => {
      const value = Number(price);
      if (!Number.isFinite(value) || value <= 0) return;
      view.lines.push(view.series.createPriceLine({
        price: value, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
      }));
    });
    const protectedLevels = [lvl.trigger, lvl.entry, lvl.sl, lvl.tp, lvl.exit]
      .map(Number)
      .filter((price) => Number.isFinite(price) && price > 0);
    if (candles.length && protectedLevels.length) {
      const visiblePrices = protectedLevels.concat(
        candles.flatMap((bar) => [Number(bar.low), Number(bar.high)])
          .filter((price) => Number.isFinite(price) && price > 0)
      );
      const low = Math.min(...visiblePrices);
      const high = Math.max(...visiblePrices);
      const padding = Math.max((high - low) * 0.06, high * 0.001);
      const boundTime = candles[candles.length - 1].time;
      view.lowerBound.setData([{ time: boundTime, value: low - padding }]);
      view.upperBound.setData([{ time: boundTime, value: high + padding }]);
    }
  }

  function upsertDeltaChart(item) {
    const id = deltaChartDomId(item);
    let col = document.getElementById(id + "-col");
    if (!col) {
      col = document.createElement("div");
      col.className = "col-12 col-md-6 col-xl-3";
      col.id = id + "-col";
      col.innerHTML = `<div class="card chart-card h-100">
        <div class="d-flex justify-content-between align-items-center">
          <div class="label mb-0">${item.symbol || ""}</div>
          <div class="chart-last" id="${id}-last">--</div>
        </div>
        <div class="small mb-1">${item.market || ""} · ${item.strategy || ""}</div>
        <div class="chart-shell" id="${id}"></div>
      </div>`;
      const grid = document.getElementById("deltaChartGrid");
      if (grid) grid.appendChild(col);
    }
    const last = document.getElementById(id + "-last");
    if (last) {
      last.textContent = item.last != null
        ? Number(item.last).toLocaleString(undefined, { maximumFractionDigits: 8 })
        : (item.error || "unsupported");
    }
    const host = document.getElementById(id);
    if (!host || typeof LightweightCharts === "undefined") return;
    if (!deltaChartViews[id]) {
      const chart = LightweightCharts.createChart(host, {
        layout: { background: { color: "#101826" }, textColor: "#ffffff" },
        grid: { vertLines: { color: "#1c2738" }, horzLines: { color: "#1c2738" } },
        rightPriceScale: { borderColor: "#1c2738" },
        localization: { timeFormatter: (time) => formatNseTime(time) },
        timeScale: nseTimeScaleOptions(),
        width: host.clientWidth,
        height: host.clientHeight || 280,
      });
      deltaChartViews[id] = {
        chart,
        series: chart.addCandlestickSeries({
          upColor: "#3dd68c",
          downColor: "#ff5c7a",
          borderVisible: false,
          wickUpColor: "#3dd68c",
          wickDownColor: "#ff5c7a",
        }),
      };
      new ResizeObserver(() => {
        chart.applyOptions({ width: host.clientWidth, height: host.clientHeight || 280 });
      }).observe(host);
    }
    if ((item.candles || []).length) deltaChartViews[id].series.setData(item.candles);
  }

  async function loadDhanCharts(levels) {
    const meta = document.getElementById("chartMeta");
    if (!meta) return;
    try {
      const response = await fetch("/api/charts");
      const data = await response.json();
      const charts = data.charts || [];
      meta.textContent = (data.ok ? "LIVE 1-min · NSE 09:15–15:30 IST · " : "Chart error · ") + charts.length + " names";
      const visibleIds = new Set(charts.map(chartDomId));
      Object.keys(chartViews).forEach((id) => {
        if (visibleIds.has(id)) return;
        try { chartViews[id].chart.remove(); } catch (error) {}
        delete chartViews[id];
        const col = document.getElementById(id + "-col");
        if (col) col.remove();
      });
      const grid = document.getElementById("chartGrid");
      if (grid) {
        Array.from(grid.children).forEach((col) => {
          const id = String(col.id || "").replace(/-col$/, "");
          if (!visibleIds.has(id)) col.remove();
        });
      }
      charts.forEach((item) => upsertChart(item, levels || {}));
      const hash = (location.hash || "").replace(/^#/, "");
      if (hash) document.getElementById(hash + "-col")?.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      meta.textContent = "Charts failed: " + error;
    }
  }

  async function loadDeltaCharts() {
    const meta = document.getElementById("deltaChartMeta");
    if (!meta) return;
    try {
      const response = await fetch("/api/delta/charts");
      const data = await response.json();
      const charts = data.charts || [];
      meta.textContent = `${data.ok ? "LIVE" : "Chart error"} · 5-minute · 24/7 IST · ${charts.length} markets`;
      const visible = new Set(charts.map(deltaChartDomId));
      Object.keys(deltaChartViews).forEach((id) => {
        if (visible.has(id)) return;
        try { deltaChartViews[id].chart.remove(); } catch (error) {}
        delete deltaChartViews[id];
        const col = document.getElementById(id + "-col");
        if (col) col.remove();
      });
      charts.forEach(upsertDeltaChart);
    } catch (error) {
      meta.textContent = "Delta charts failed: " + error;
    }
  }

  root.DeskCharts = {
    chartDomId,
    watchLevels,
    loadDhanCharts,
    loadDeltaCharts,
  };
})(window);

(function () {
  const palette = {
    blue: "#2563eb",
    teal: "#0f766e",
    rose: "#b83f58",
    gold: "#b7791f",
    line: "#e6ebf0",
    axis: "#475569"
  };
  const chartColors = [palette.blue, palette.teal, palette.gold, palette.rose, "#64748b", "#7c3aed"];

  function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "null");
    } catch (error) {
      console.warn("Invalid JSON block", id, error);
      return null;
    }
  }

  function pct(value) {
    return Number(value).toFixed(1) + "%";
  }

  function grid() {
    return { left: 58, right: 48, top: 52, bottom: 58, containLabel: true };
  }

  function marketOption(rows, type) {
    if (type === "bar") {
      return {
        animation: false,
        color: [palette.blue, palette.gold],
        tooltip: { trigger: "axis" },
        legend: { top: 8 },
        grid: grid(),
        xAxis: { type: "category", data: rows.map((row) => row.year) },
        yAxis: [
          { type: "value", name: "销量 百万辆", splitLine: { lineStyle: { color: palette.line } } },
          { type: "value", name: "渗透率", axisLabel: { formatter: (value) => value + "%" } }
        ],
        series: [
          { name: "销量", type: "bar", data: rows.map((row) => row.sales_m) },
          {
            name: "渗透率",
            type: "line",
            yAxisIndex: 1,
            data: rows.map((row) => row.penetration),
            lineStyle: { width: 3 }
          }
        ]
      };
    }

    return {
      animation: false,
      color: [palette.blue, palette.gold],
      tooltip: { trigger: "axis" },
      legend: { top: 8 },
      grid: grid(),
      xAxis: { type: "category", data: rows.map((row) => row.year) },
      yAxis: [
        { type: "value", name: "销量 百万辆", splitLine: { lineStyle: { color: palette.line } } },
        { type: "value", name: "渗透率", axisLabel: { formatter: (value) => value + "%" } }
      ],
      series: [
        {
          name: "销量",
          type: "line",
          smooth: true,
          data: rows.map((row) => row.sales_m),
          lineStyle: { width: 3 }
        },
        {
          name: "渗透率",
          type: "line",
          yAxisIndex: 1,
          smooth: true,
          data: rows.map((row) => row.penetration),
          lineStyle: { width: 3, type: "dashed" }
        }
      ]
    };
  }

  function shareOption(rows, type) {
    if (type === "pie") {
      return {
        animation: false,
        color: chartColors,
        tooltip: { trigger: "item", valueFormatter: (value) => pct(value) },
        legend: { bottom: 8, type: "scroll" },
        series: [
          {
            type: "pie",
            radius: ["42%", "70%"],
            center: ["50%", "42%"],
            data: rows.map((row) => ({ name: row.brand, value: row.share })),
            label: { formatter: (params) => params.name + "\n" + pct(params.value) }
          }
        ]
      };
    }

    const reversed = rows.slice().reverse();
    return {
      animation: false,
      color: [palette.teal],
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: (value) => pct(value)
      },
      grid: { left: 92, right: 42, top: 28, bottom: 24, containLabel: true },
      xAxis: {
        type: "value",
        axisLabel: { formatter: (value) => value + "%" },
        splitLine: { lineStyle: { color: palette.line } }
      },
      yAxis: { type: "category", data: reversed.map((row) => row.brand) },
      series: [
        {
          name: "份额",
          type: "bar",
          data: reversed.map((row) => ({
            value: row.share,
            itemStyle: {
              color:
                row.brand === "比亚迪"
                  ? palette.teal
                  : row.brand === "特斯拉中国"
                    ? palette.rose
                    : palette.blue
            }
          })),
          label: { show: true, position: "right", formatter: (params) => pct(params.value) }
        }
      ]
    };
  }

  function marketGrowthOption(rows, type) {
    if (type === "bar") {
      return {
        animation: false,
        color: [palette.blue],
        tooltip: { trigger: "axis" },
        grid: grid(),
        xAxis: { type: "category", data: rows.map((r) => r.year), axisLabel: { rotate: 0 } },
        yAxis: {
          type: "value",
          name: "市场规模（亿美元）",
          splitLine: { lineStyle: { color: palette.line } }
        },
        series: [
          {
            name: "市场规模",
            type: "bar",
            data: rows.map((r) => ({
              value: r.market_size,
              itemStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "#60a5fa" },
                  { offset: 1, color: palette.blue }
                ])
              }
            })),
            label: { show: true, position: "top", formatter: "${c}B" }
          }
        ]
      };
    }
    return {
      animation: false,
      color: [palette.blue],
      tooltip: { trigger: "axis" },
      grid: grid(),
      xAxis: { type: "category", data: rows.map((r) => r.year) },
      yAxis: {
        type: "value",
        name: "市场规模（亿美元）",
        splitLine: { lineStyle: { color: palette.line } }
      },
      series: [
        {
          name: "市场规模",
          type: "line",
          smooth: true,
          data: rows.map((r) => r.market_size),
          lineStyle: { width: 3, color: palette.blue },
          itemStyle: { color: palette.blue },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(37,99,235,0.25)" },
              { offset: 1, color: "rgba(37,99,235,0.02)" }
            ])
          },
          label: { show: true, position: "top", formatter: "${c}B" }
        }
      ]
    };
  }

  function demandRankingOption(rows, type) {
    const reversed = rows.slice().reverse();
    const colors = [palette.teal, palette.blue, palette.gold, palette.rose, "#7c3aed", "#64748b"];
    return {
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      grid: { left: 110, right: 56, top: 20, bottom: 24, containLabel: true },
      xAxis: {
        type: "value",
        name: "需求优先级评分",
        splitLine: { lineStyle: { color: palette.line } }
      },
      yAxis: { type: "category", data: reversed.map((r) => r.feature) },
      series: [
        {
          name: "优先级",
          type: "bar",
          data: reversed.map((r, i) => ({
            value: r.score,
            itemStyle: { color: colors[rows.length - 1 - i] || palette.blue }
          })),
          label: { show: true, position: "right", formatter: "{c}" }
        }
      ]
    };
  }

  function platformComparisonOption(rows, type) {
    const categories = rows.map((r) => r.platform);
    return {
      animation: false,
      color: [palette.blue, palette.teal, palette.gold],
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      legend: { top: 6, data: ["开源生态", "企业功能", "价格竞争力"] },
      grid: grid(),
      xAxis: { type: "category", data: categories },
      yAxis: {
        type: "value",
        name: "评分（满分10）",
        max: 10,
        splitLine: { lineStyle: { color: palette.line } }
      },
      series: [
        { name: "开源生态", type: "bar", data: rows.map((r) => r.open_source), barGap: "0%" },
        { name: "企业功能", type: "bar", data: rows.map((r) => r.enterprise) },
        { name: "价格竞争力", type: "bar", data: rows.map((r) => r.pricing) }
      ]
    };
  }

  function techStackOption(rows, type) {
    if (type === "pie") {
      return {
        animation: false,
        color: chartColors,
        tooltip: { trigger: "item" },
        legend: { bottom: 8, type: "scroll" },
        series: [
          {
            type: "pie",
            radius: ["40%", "68%"],
            center: ["50%", "44%"],
            data: rows.map((r) => ({ name: r.layer, value: r.count })),
            label: { formatter: "{b}\n{c}家 ({d}%)" }
          }
        ]
      };
    }
    const reversed = rows.slice().reverse();
    return {
      animation: false,
      color: [palette.blue],
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      grid: { left: 110, right: 48, top: 24, bottom: 24, containLabel: true },
      xAxis: {
        type: "value",
        name: "采用厂商数",
        splitLine: { lineStyle: { color: palette.line } }
      },
      yAxis: { type: "category", data: reversed.map((r) => r.layer) },
      series: [
        {
          name: "采用数",
          type: "bar",
          data: reversed.map((r, i) => ({
            value: r.count,
            itemStyle: { color: chartColors[rows.length - 1 - i] || palette.blue }
          })),
          label: { show: true, position: "right" }
        }
      ]
    };
  }

  function setupToc() {
    const nav = document.querySelector(".toc-rail");
    const page = document.querySelector(".doc-page");
    if (!nav || !page) return;

    const targets = Array.from(
      page.querySelectorAll("header[id], section[id]")
    ).filter((block) => block.querySelector("h1, h2"));

    const lines = document.createElement("div");
    const popup = document.createElement("div");
    lines.className = "toc-lines";
    popup.className = "toc-popup";

    targets.forEach((target) => {
      const heading = target.querySelector("h1, h2");
      const label = heading.textContent.trim();
      const line = document.createElement("a");
      const link = document.createElement("a");
      line.className = "toc-line";
      line.href = "#" + target.id;
      line.dataset.target = target.id;
      line.setAttribute("aria-label", label);
      link.href = "#" + target.id;
      link.dataset.target = target.id;
      link.textContent = label;
      lines.appendChild(line);
      popup.appendChild(link);
    });

    nav.replaceChildren(lines, popup);
    const links = Array.from(nav.querySelectorAll("a[data-target]"));
    function setActive(id) {
      links.forEach((link) => link.classList.toggle("active", link.dataset.target === id));
    }

    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(
        (entries) => {
          const visible = entries
            .filter((entry) => entry.isIntersecting)
            .sort((a, b) => Math.abs(a.boundingClientRect.top) - Math.abs(b.boundingClientRect.top))[0];
          if (visible) setActive(visible.target.id);
        },
        { rootMargin: "-22% 0px -62% 0px", threshold: 0.01 }
      );
      targets.forEach((target) => observer.observe(target));
    } else if (links[0]) {
      links[0].classList.add("active");
    }
  }

  function setupCharts() {
    if (!window.echarts) return;
    const instances = [];
    document.querySelectorAll("[data-chart]").forEach((block) => {
      // data-chart 既可能挂在 figure 外层，也可能直接挂在 .chart-canvas 上
      const canvas = block.classList.contains("chart-canvas")
        ? block
        : block.querySelector(".chart-canvas");
      const dataId = block.getAttribute("data-data");
      const chartKind = block.getAttribute("data-chart");
      const initialType = block.getAttribute("data-type") || "line";
      const rows = readJson(dataId);
      if (!canvas || !rows) return;

      const chart = echarts.init(canvas, null, { renderer: "canvas" });
      function optionFor(type) {
        if (chartKind === "market-trend") return marketOption(rows, type);
        if (chartKind === "share-ranking") return shareOption(rows, type);
        if (chartKind === "market-growth") return marketGrowthOption(rows, type);
        if (chartKind === "demand-ranking") return demandRankingOption(rows, type);
        if (chartKind === "platform-comparison") return platformComparisonOption(rows, type);
        if (chartKind === "tech-stack") return techStackOption(rows, type);
        return {};
      }
      chart.setOption(optionFor(initialType), true);
      instances.push(chart);

      block.querySelectorAll("[data-chart-type]").forEach((button) => {
        button.classList.toggle("active", button.dataset.chartType === initialType);
        button.addEventListener("click", () => {
          block
            .querySelectorAll("[data-chart-type]")
            .forEach((item) => item.classList.toggle("active", item === button));
          chart.setOption(optionFor(button.dataset.chartType), true);
        });
      });
    });

    window.addEventListener("resize", () => {
      instances.forEach((chart) => chart.resize());
    });
  }

  function setupMermaid() {
    if (!window.mermaid || !document.querySelector(".mermaid")) return;
    window.mermaid.initialize({
      startOnLoad: true,
      securityLevel: "loose",
      theme: "base",
      themeVariables: {
        fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica Neue, Arial, sans-serif",
        primaryColor: "#eef4ff",
        primaryBorderColor: "#b9cdfd",
        primaryTextColor: "#2f302d",
        lineColor: "#9aa1ad",
        textColor: "#2f302d",
        mainBkg: "#ffffff",
        clusterBkg: "#f7f8fa",
        clusterBorder: "#e6e8ec"
      },
      flowchart: {
        htmlLabels: true,
        curve: "basis",
        padding: 14,
        nodeSpacing: 42,
        rankSpacing: 58
      }
    });
  }

  const lucideIcons = {
    maximize2: [
      ["path", { d: "M15 3h6v6" }],
      ["path", { d: "m21 3-7 7" }],
      ["path", { d: "m3 21 7-7" }],
      ["path", { d: "M9 21H3v-6" }]
    ],
    x: [
      ["path", { d: "M18 6 6 18" }],
      ["path", { d: "m6 6 12 12" }]
    ]
  };

  function lucideIcon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "lucide-icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    (lucideIcons[name] || []).forEach(([tag, attrs]) => {
      const child = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.entries(attrs).forEach(([key, value]) => child.setAttribute(key, value));
      svg.appendChild(child);
    });
    return svg;
  }

  function setupDiagramZoom() {
    const frames = Array.from(document.querySelectorAll(".mermaid-frame"));
    if (!frames.length) return;

    const lightbox = document.createElement("div");
    const panel = document.createElement("div");
    const close = document.createElement("button");
    const content = document.createElement("div");
    lightbox.className = "diagram-lightbox";
    panel.className = "diagram-lightbox-panel";
    close.className = "diagram-lightbox-close";
    close.type = "button";
    close.title = "Close";
    close.setAttribute("aria-label", "Close enlarged diagram");
    close.appendChild(lucideIcon("x"));
    content.className = "diagram-lightbox-content";
    panel.append(close, content);
    lightbox.appendChild(panel);
    document.body.appendChild(lightbox);

    function closeLightbox() {
      lightbox.classList.remove("open");
      content.replaceChildren();
      document.body.style.overflow = "";
    }

    function openLightbox(frame) {
      const svg = frame.querySelector("svg");
      const mermaidSource = frame.querySelector(".mermaid");
      const clone = svg ? svg.cloneNode(true) : mermaidSource ? mermaidSource.cloneNode(true) : null;
      if (!clone) return;
      content.replaceChildren(clone);
      lightbox.classList.add("open");
      document.body.style.overflow = "hidden";
      close.focus();
    }

    close.addEventListener("click", closeLightbox);
    lightbox.addEventListener("click", (event) => {
      if (event.target === lightbox) closeLightbox();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && lightbox.classList.contains("open")) closeLightbox();
    });

    frames.forEach((frame) => {
      if (frame.querySelector(".diagram-zoom-trigger")) return;
      const button = document.createElement("button");
      button.className = "diagram-zoom-trigger";
      button.type = "button";
      button.title = "Enlarge";
      button.setAttribute("aria-label", "Enlarge diagram");
      button.appendChild(lucideIcon("maximize2"));
      button.addEventListener("click", () => openLightbox(frame));
      frame.appendChild(button);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupToc();
    setupMermaid();
    setupDiagramZoom();
    setupCharts();
  });
})();

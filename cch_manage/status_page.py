import json
import re
from html import escape


ROOT_RELATIVE_ATTR_RE = re.compile(r'(?P<prefix>\b(?:href|src|action)=["\'])(?P<url>/[^"\']*)')


def model_price_client_script() -> str:
    return """
  const modelPricesEndpoint = window.location.origin + "/cch/status/model-prices";
  const rankingsEndpoint = window.location.origin + "/cch/status/rankings";
  const widePriceQuery = window.matchMedia("(min-width: 960px)");
  const wideOpenColumns = "minmax(0, 6fr) minmax(0, 4fr)";
  const wideClosedColumns = "minmax(0, 6fr) minmax(0, 0fr)";
  const priceColumns = [
    {key: "model", label: "模型", numeric: false},
    {key: "inputPerM", label: "输入", unit: "/M", numeric: true},
    {key: "outputPerM", label: "输出", unit: "/M", numeric: true},
    {key: "cacheReadPerM", label: "缓存读", unit: "/M", numeric: true},
    {key: "cacheWritePerM", label: "缓存写", unit: "/M", numeric: true},
    {key: "perCall", label: "每次价格", numeric: true},
  ];
  let priceOpen = false;
  let priceAnimating = false;
  let showPriceCoeff = false;
  let pricePayload = null;
  let priceFailed = false;
  let priceSort = {key: "", dir: "asc"};
  let priceObserver = null;
  let priceLayoutGeneration = 0;
  let rankOpen = false;
  let rankShowUsers = true;
  let rankPayload = null;
  let rankFailed = false;
  let rankSort = {key: "costUsd", dir: "desc"};

  const findSortButton = () => {
    const icon = document.querySelector("svg.lucide-arrow-down-a-z");
    const fromIcon = icon && icon.closest("button");
    if (fromIcon) return fromIcon;
    for (const button of document.querySelectorAll("button")) {
      if ((button.textContent || "").trim() === "自定义排序") return button;
    }
    return null;
  };
  const findPriceShell = (sortButton) =>
    sortButton.closest("div.mx-auto.flex.w-full.max-w-7xl.flex-col");
  const formatPriceCell = (cell) => {
    if (cell == null || cell === "") return "—";
    if (typeof cell === "object") {
      if (cell.min == null || cell.max == null || cell.min === "" || cell.max === "") return "—";
      if (String(cell.min) === String(cell.max)) return "$" + escapeHtml(cell.min);
      return "$" + escapeHtml(cell.min) + "–$" + escapeHtml(cell.max);
    }
    return "$" + escapeHtml(cell);
  };
  const priceSyncedLabel = (value) => {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "";
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    return `更新于 ${date.getMonth() + 1}月${date.getDate()}日 ${hours}:${minutes}`;
  };
  const priceSortValue = (model, key) => {
    if (key === "model") return String(model.model || "");
    const source = showPriceCoeff ? model.adjusted : model.prices;
    const cell = source ? source[key] : null;
    if (showPriceCoeff) {
      if (!cell || cell.min == null || cell.min === "") return null;
      const adjusted = Number(cell.min);
      return Number.isFinite(adjusted) ? adjusted : null;
    }
    if (cell == null || cell === "") return null;
    const amount = Number(cell);
    return Number.isFinite(amount) ? amount : null;
  };
  const comparePriceRows = (left, right) => {
    if (!priceSort.key) return 0;
    const leftValue = priceSortValue(left, priceSort.key);
    const rightValue = priceSortValue(right, priceSort.key);
    const leftEmpty = leftValue == null || leftValue === "";
    const rightEmpty = rightValue == null || rightValue === "";
    if (leftEmpty && rightEmpty) return 0;
    if (leftEmpty) return 1;
    if (rightEmpty) return -1;
    const compared = typeof leftValue === "string"
      ? leftValue.localeCompare(rightValue)
      : leftValue - rightValue;
    return priceSort.dir === "desc" ? -compared : compared;
  };
  const priceSwitchSelectedStyle = "display:inline-block;border-radius:999px;padding:3px 10px;background:#2563eb;color:#ffffff;font-weight:600;line-height:1.25;white-space:nowrap;";
  const priceSwitchIdleStyle = "display:inline-block;border-radius:999px;padding:3px 10px;background:#e5e7eb;color:#3f3f46;font-weight:500;line-height:1.25;white-space:nowrap;";
  const paintPriceSwitch = () => {
    const button = document.getElementById("cch-model-price-coeff");
    if (!button) return;
    button.setAttribute("aria-checked", showPriceCoeff ? "true" : "false");
    const off = button.querySelector("[data-price-side='off']");
    const on = button.querySelector("[data-price-side='on']");
    if (off) off.style.cssText = showPriceCoeff ? priceSwitchIdleStyle : priceSwitchSelectedStyle;
    if (on) on.style.cssText = showPriceCoeff ? priceSwitchSelectedStyle : priceSwitchIdleStyle;
  };
  const renderPriceTable = () => {
    const body = document.getElementById("cch-model-price-body");
    const synced = document.getElementById("cch-model-price-synced");
    if (!body || !pricePayload) return;
    if (synced) synced.textContent = priceSyncedLabel(pricePayload.syncedAt);
    const models = Array.isArray(pricePayload.models) ? pricePayload.models.slice() : [];
    models.sort(comparePriceRows);
    const head = priceColumns.map((column) => {
      const arrow = priceSort.key === column.key ? (priceSort.dir === "asc" ? " ▲" : " ▼") : "";
      const unit = column.unit
        ? `<span style="color:var(--muted-foreground);font-weight:500">${column.unit}</span>`
        : "";
      const align = column.numeric ? "right" : "left";
      return `<th style="text-align:${align};padding:10px 12px;white-space:nowrap;font-weight:600;border-bottom:1px solid hsl(var(--border) / .7)"><button type="button" data-price-sort="${column.key}" style="all:unset;cursor:pointer">${escapeHtml(column.label)}${unit}${arrow}</button></th>`;
    }).join("");
    const rows = models.map((model) => {
      const cells = priceColumns.map((column) => {
        if (column.key === "model") {
          const groups = Array.isArray(model.groups)
            ? model.groups.map((slug) => escapeHtml(slug)).join(" · ")
            : "";
          return `<td style="padding:10px 12px;vertical-align:top"><div style="font-weight:600">${escapeHtml(model.model)}</div><div style="color:var(--muted-foreground);font-size:0.75rem">${groups}</div></td>`;
        }
        const source = showPriceCoeff ? model.adjusted : model.prices;
        const cell = source ? source[column.key] : null;
        return `<td style="padding:10px 12px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums">${formatPriceCell(cell)}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    body.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:0.875rem;line-height:1.4;color:var(--foreground)"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
    body.querySelectorAll("[data-price-sort]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.getAttribute("data-price-sort") || "";
        if (priceSort.key === key) priceSort.dir = priceSort.dir === "asc" ? "desc" : "asc";
        else priceSort = {key, dir: "asc"};
        renderPriceTable();
      });
    });
  };
  const loadModelPrices = () => {
    const body = document.getElementById("cch-model-price-body");
    if (body) body.textContent = "加载中";
    fetch(modelPricesEndpoint, {cache: "no-store"})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("model prices unavailable")))
      .then((payload) => {
        const data = payload && payload.ok ? payload.data : null;
        if (!data || !Array.isArray(data.models) || (data.models.length === 0 && !data.syncedAt)) {
          throw new Error("model prices unavailable");
        }
        pricePayload = data;
        priceFailed = false;
        renderPriceTable();
      })
      .catch(() => {
        pricePayload = null;
        priceFailed = true;
        const failedBody = document.getElementById("cch-model-price-body");
        if (failedBody) failedBody.textContent = "价格暂时不可用";
        const synced = document.getElementById("cch-model-price-synced");
        if (synced) synced.textContent = "";
      });
  };
  const rankColumns = () => [
    {key: "name", label: rankShowUsers ? "用户" : "模型", numeric: false},
    {key: "requestCount", label: "请求数", numeric: true},
    {key: "tokenCount", label: "Token数", numeric: true},
    {key: "costUsd", label: "消耗金额", numeric: true},
  ];
  const rankRangeLabel = (payload) => {
    if (!payload) return "";
    const start = dateLabel(payload.weekStart);
    const endDate = payload.weekEnd ? new Date(payload.weekEnd) : null;
    if (endDate && !Number.isNaN(endDate.getTime())) endDate.setDate(endDate.getDate() - 1);
    const end = dateLabel(endDate);
    const range = start && end ? `${start}–${end}` : "";
    const synced = priceSyncedLabel(payload.syncedAt);
    if (range && synced) return `${range} · ${synced}`;
    return range || synced;
  };
  const rankSortValue = (row, key) => {
    if (key === "name") return String(row.name || "");
    const amount = Number(row[key]);
    return Number.isFinite(amount) ? amount : null;
  };
  const compareRankRows = (left, right) => {
    const leftValue = rankSortValue(left, rankSort.key);
    const rightValue = rankSortValue(right, rankSort.key);
    const leftEmpty = leftValue == null || leftValue === "";
    const rightEmpty = rightValue == null || rightValue === "";
    if (leftEmpty && rightEmpty) return 0;
    if (leftEmpty) return 1;
    if (rightEmpty) return -1;
    const compared = typeof leftValue === "string"
      ? leftValue.localeCompare(rightValue)
      : leftValue - rightValue;
    return rankSort.dir === "desc" ? -compared : compared;
  };
  const formatRankCell = (row, key) => {
    if (key === "name") return escapeHtml(row.name || "");
    if (key === "costUsd") return money(row.costUsd);
    if (key === "tokenCount") return tokensM(row.tokenCount);
    return compactNumber(row[key]);
  };
  const paintRankSwitch = () => {
    const button = document.getElementById("cch-rank-scope");
    if (!button) return;
    button.setAttribute("aria-checked", rankShowUsers ? "false" : "true");
    const userSide = button.querySelector("[data-rank-side='user']");
    const modelSide = button.querySelector("[data-rank-side='model']");
    if (userSide) userSide.style.cssText = rankShowUsers ? priceSwitchSelectedStyle : priceSwitchIdleStyle;
    if (modelSide) modelSide.style.cssText = rankShowUsers ? priceSwitchIdleStyle : priceSwitchSelectedStyle;
  };
  const renderRankTable = () => {
    const body = document.getElementById("cch-rank-body");
    const synced = document.getElementById("cch-rank-synced");
    if (!body || !rankPayload) return;
    if (synced) synced.textContent = rankRangeLabel(rankPayload);
    const source = rankShowUsers ? rankPayload.users : rankPayload.models;
    const entries = Array.isArray(source) ? source.slice() : [];
    entries.sort(compareRankRows);
    const columns = rankColumns();
    const head = columns.map((column) => {
      const arrow = rankSort.key === column.key ? (rankSort.dir === "asc" ? " ▲" : " ▼") : "";
      const align = column.numeric ? "right" : "left";
      return `<th style="text-align:${align};padding:10px 12px;white-space:nowrap;font-weight:600;border-bottom:1px solid hsl(var(--border) / .7)"><button type="button" data-rank-sort="${column.key}" style="all:unset;cursor:pointer">${escapeHtml(column.label)}${arrow}</button></th>`;
    }).join("");
    const rows = entries.map((entry) => {
      const cells = columns.map((column) => {
        const align = column.numeric ? "right" : "left";
        const weight = column.key === "name" ? "font-weight:600;" : "font-variant-numeric:tabular-nums;";
        return `<td style="padding:10px 12px;text-align:${align};white-space:nowrap;${weight}">${formatRankCell(entry, column.key)}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    const empty = entries.length === 0
      ? `<tr><td colspan="${columns.length}" style="padding:10px 12px;color:var(--muted-foreground)">本周暂无数据</td></tr>`
      : "";
    body.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:0.875rem;line-height:1.4;color:var(--foreground)"><thead><tr>${head}</tr></thead><tbody>${rows}${empty}</tbody></table>`;
    body.querySelectorAll("[data-rank-sort]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.getAttribute("data-rank-sort") || "";
        if (rankSort.key === key) rankSort.dir = rankSort.dir === "asc" ? "desc" : "asc";
        else rankSort = {key, dir: "asc"};
        renderRankTable();
      });
    });
  };
  const loadRanks = () => {
    const body = document.getElementById("cch-rank-body");
    if (body) body.textContent = "加载中";
    fetch(rankingsEndpoint, {cache: "no-store"})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("rankings unavailable")))
      .then((payload) => {
        const data = payload && payload.ok ? payload.data : null;
        if (!data || !data.syncedAt) throw new Error("rankings unavailable");
        rankPayload = data;
        rankFailed = false;
        renderRankTable();
      })
      .catch(() => {
        rankPayload = null;
        rankFailed = true;
        const failedBody = document.getElementById("cch-rank-body");
        if (failedBody) failedBody.textContent = "排行榜暂时不可用";
        const synced = document.getElementById("cch-rank-synced");
        if (synced) synced.textContent = "";
      });
  };
  const clearWidePriceLayout = (shell) => {
    shell.style.display = "";
    shell.style.gridTemplateColumns = "";
    shell.style.gridTemplateRows = "";
    shell.style.columnGap = "";
    shell.style.alignItems = "";
    shell.style.transition = "";
    shell.style.maxWidth = "";
    shell.style.removeProperty("--cch-status-width");
    for (const child of shell.children) {
      child.style.gridColumn = "";
      child.style.gridRow = "";
    }
  };
  const setPricePanelHidden = (panel, hidden) => {
    panel.hidden = hidden;
    panel.style.display = hidden ? "none" : "flex";
    if (!hidden) panel.style.flexDirection = "column";
  };
  const releasePricePanelPin = (panel) => {
    panel.style.gridColumn = "";
    panel.style.gridRow = "";
    panel.style.position = "";
    panel.style.top = "";
    panel.style.height = "";
    panel.style.maxHeight = "";
    panel.style.minHeight = "";
    panel.style.marginTop = "";
    panel.style.alignSelf = "";
    panel.style.justifySelf = "";
    panel.style.width = "";
  };
  const findPriceAnchor = () => {
    const board = document.getElementById("cch-weekly-leaderboard");
    const card = board && board.querySelector("article");
    if (card) return card;
    if (typeof findFirstGroupCard === "function") return findFirstGroupCard();
    return null;
  };
  let priceAnchorFrame = 0;
  const syncPricePanelToAnchor = () => {
    const panel = document.getElementById(priceOpen ? "cch-model-price-panel" : "cch-rank-panel");
    const sortButton = findSortButton();
    const shell = sortButton ? findPriceShell(sortButton) : null;
    if (!panel || !shell || !(priceOpen || rankOpen) || !widePriceQuery.matches || panel.hidden) return;
    const groups = shell.querySelector(":scope > div.space-y-4");
    const anchor = findPriceAnchor();
    const anchorRect = anchor ? anchor.getBoundingClientRect() : null;
    const groupsRect = groups ? groups.getBoundingClientRect() : null;
    const delta = anchorRect && groupsRect ? Math.max(0, anchorRect.top - groupsRect.top) : 0;
    // margin-top drops the in-flow panel from the groups row onto the cards.
    // Sticky top stays 0 so once the page scrolls, the panel rests on the
    // viewport top instead of keeping that offset.
    panel.style.marginTop = delta + "px";
    panel.style.top = "0px";
    const visibleTop = anchorRect ? Math.max(0, anchorRect.top) : 0;
    const height = Math.max(window.innerHeight - visibleTop, 280);
    panel.style.height = height + "px";
    panel.style.maxHeight = height + "px";
  };
  const requestPricePanelSync = () => {
    if (priceAnchorFrame) return;
    priceAnchorFrame = window.requestAnimationFrame(() => {
      priceAnchorFrame = 0;
      syncPricePanelToAnchor();
    });
  };
  const pinPricePanel = (shell, panel) => {
    panel.style.position = "sticky";
    panel.style.alignSelf = "start";
    panel.style.justifySelf = "stretch";
    panel.style.width = "100%";
    panel.style.minHeight = "0";
    panel.style.overflow = "hidden";
    syncPricePanelToAnchor();
  };
  const applyWidePriceLayout = (shell, panel, columns, withTransition) => {
    const shellStyle = window.getComputedStyle(shell);
    const padX = (parseFloat(shellStyle.paddingLeft) || 0) + (parseFloat(shellStyle.paddingRight) || 0);
    shell.style.setProperty("--cch-status-width", `calc(80rem - ${padX}px)`);
    shell.style.display = "grid";
    shell.style.alignItems = "start";
    shell.style.gridTemplateRows = "auto auto auto";
    const open = columns === wideOpenColumns;
    shell.style.columnGap = open ? "20px" : "0px";
    shell.style.transition = withTransition ? "grid-template-columns 320ms ease, max-width 320ms ease" : "none";
    shell.style.gridTemplateColumns = columns;
    shell.style.maxWidth = open
      ? `calc(var(--cch-status-width) + 20px + var(--cch-status-width) * 4 / 6 + ${padX}px)`
      : `calc(var(--cch-status-width) + ${padX}px)`;
    const header = shell.querySelector(":scope > header");
    const sortButton = findSortButton();
    const toolbar = sortButton ? sortButton.parentElement : null;
    const groups = shell.querySelector(":scope > div.space-y-4");
    if (header) {
      header.style.gridColumn = "1";
      header.style.gridRow = "1";
    }
    if (toolbar && toolbar.parentElement === shell) {
      toolbar.style.gridColumn = "1";
      toolbar.style.gridRow = "2";
    }
    if (groups) {
      groups.style.gridColumn = "1";
      groups.style.gridRow = "3";
    }
    panel.style.gridColumn = "2";
    // Sit in the groups row so the panel starts beside the leaderboard,
    // not beside the page title. syncPricePanelToAnchor nudges it down to
    // the cards themselves.
    panel.style.gridRow = "3";
    pinPricePanel(shell, panel);
  };
  const ensurePriceToggle = (sortButton) => {
    let toggle = document.getElementById("cch-model-price-toggle");
    if (!toggle) {
      toggle = document.createElement("button");
      toggle.id = "cch-model-price-toggle";
      toggle.type = "button";
      toggle.textContent = "模型价格表";
      toggle.addEventListener("click", () => setPriceOpenState(!priceOpen));
    }
    if (toggle.className !== sortButton.className) toggle.className = sortButton.className;
    toggle.setAttribute("aria-pressed", priceOpen ? "true" : "false");
    toggle.style.flex = "0 0 auto";
    toggle.style.boxShadow = priceOpen ? "inset 0 0 0 1px currentColor" : "";
    if (toggle.previousElementSibling !== sortButton) {
      sortButton.insertAdjacentElement("afterend", toggle);
    }
  };
  const ensureRankToggle = (sortButton) => {
    let toggle = document.getElementById("cch-rank-toggle");
    if (!toggle) {
      toggle = document.createElement("button");
      toggle.id = "cch-rank-toggle";
      toggle.type = "button";
      toggle.textContent = "排行榜";
      toggle.addEventListener("click", () => setRankOpenState(!rankOpen));
    }
    if (toggle.className !== sortButton.className) toggle.className = sortButton.className;
    toggle.setAttribute("aria-pressed", rankOpen ? "true" : "false");
    toggle.style.flex = "0 0 auto";
    toggle.style.boxShadow = rankOpen ? "inset 0 0 0 1px currentColor" : "";
    const priceToggle = document.getElementById("cch-model-price-toggle");
    const anchor = priceToggle || sortButton;
    if (toggle.previousElementSibling !== anchor) {
      anchor.insertAdjacentElement("afterend", toggle);
    }
  };
  const ensurePriceToolbar = (sortButton) => {
    const toolbar = sortButton.parentElement;
    if (!toolbar) return;
    toolbar.style.justifyContent = "flex-start";
    toolbar.style.columnGap = "8px";
    const search = toolbar.querySelector(":scope > div.relative");
    if (search) search.style.marginRight = "auto";
  };
  const ensurePricePanel = (shell) => {
    let panel = document.getElementById("cch-model-price-panel");
    if (!panel) {
      panel = document.createElement("aside");
      panel.id = "cch-model-price-panel";
      panel.hidden = true;
      panel.style.cssText = "min-width:0;box-sizing:border-box;overflow:hidden;border:1px solid hsl(var(--border) / .6);border-radius:16px;background:hsl(var(--card) / .72);color:var(--foreground);box-shadow:0 8px 24px rgb(0 0 0 / .06);backdrop-filter:blur(8px);";
      panel.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex:0 0 auto;padding:14px 14px 10px;background:hsl(var(--card) / .94);border-bottom:1px solid hsl(var(--border) / .45)"><div style="min-width:0"><div style="font-size:1.125rem;font-weight:600">模型价格表</div><div id="cch-model-price-synced" style="margin-top:2px;font-size:0.75rem;color:var(--muted-foreground)"></div></div><button type="button" id="cch-model-price-coeff" role="switch" aria-checked="false" aria-label="在原价和实际价之间切换" style="display:inline-flex;flex:0 0 auto;align-items:center;gap:2px;border:1px solid #d4d4d8;border-radius:999px;padding:2px;background:#f3f4f6;color:#18181b;font-size:0.875rem;line-height:1.25;cursor:pointer;white-space:nowrap"><span data-price-side="off" style="${priceSwitchSelectedStyle}">原价</span><span data-price-side="on" style="${priceSwitchIdleStyle}">实际</span></button></div><div id="cch-model-price-body" style="flex:1;min-height:0;overflow:auto;padding:10px 12px 16px;font-size:0.875rem;color:var(--muted-foreground)"></div>`;
      const coeff = panel.querySelector("#cch-model-price-coeff");
      if (coeff) {
        coeff.addEventListener("click", () => {
          showPriceCoeff = !showPriceCoeff;
          paintPriceSwitch();
          if (pricePayload) renderPriceTable();
        });
      }
    }
    if (panel.parentElement !== shell) shell.appendChild(panel);
    // getElementById only finds a connected node, so the first paint has to
    // happen after the panel is in the document. The markup colors above
    // cover the frame before this runs.
    paintPriceSwitch();
    return panel;
  };
  const ensureRankPanel = (shell) => {
    let panel = document.getElementById("cch-rank-panel");
    if (!panel) {
      panel = document.createElement("aside");
      panel.id = "cch-rank-panel";
      panel.hidden = true;
      panel.style.cssText = "min-width:0;box-sizing:border-box;overflow:hidden;border:1px solid hsl(var(--border) / .6);border-radius:16px;background:hsl(var(--card) / .72);color:var(--foreground);box-shadow:0 8px 24px rgb(0 0 0 / .06);backdrop-filter:blur(8px);";
      panel.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex:0 0 auto;padding:14px 14px 10px;background:hsl(var(--card) / .94);border-bottom:1px solid hsl(var(--border) / .45)"><div style="min-width:0"><div style="font-size:1.125rem;font-weight:600">排行榜</div><div id="cch-rank-synced" style="margin-top:2px;font-size:0.75rem;color:var(--muted-foreground)"></div></div><button type="button" id="cch-rank-scope" role="switch" aria-checked="false" aria-label="在用户和模型之间切换" style="display:inline-flex;flex:0 0 auto;align-items:center;gap:2px;border:1px solid #d4d4d8;border-radius:999px;padding:2px;background:#f3f4f6;color:#18181b;font-size:0.875rem;line-height:1.25;cursor:pointer;white-space:nowrap"><span data-rank-side="user" style="${priceSwitchSelectedStyle}">用户</span><span data-rank-side="model" style="${priceSwitchIdleStyle}">模型</span></button></div><div id="cch-rank-body" style="flex:1;min-height:0;overflow:auto;padding:10px 12px 16px;font-size:0.875rem;color:var(--muted-foreground)"></div>`;
      const scope = panel.querySelector("#cch-rank-scope");
      if (scope) {
        scope.addEventListener("click", () => {
          rankShowUsers = !rankShowUsers;
          paintRankSwitch();
          if (rankPayload) renderRankTable();
        });
      }
    }
    if (panel.parentElement !== shell) shell.appendChild(panel);
    paintRankSwitch();
    return panel;
  };
  const activeSidePanel = (pricePanel, rankPanel) => priceOpen ? pricePanel : rankPanel;
  const ensurePriceChrome = (restore) => {
    const sortButton = findSortButton();
    if (!sortButton) return;
    const shell = findPriceShell(sortButton);
    if (!shell) return;
    ensurePriceToggle(sortButton);
    ensureRankToggle(sortButton);
    ensurePriceToolbar(sortButton);
    const pricePanel = ensurePricePanel(shell);
    const rankPanel = ensureRankPanel(shell);
    if (!restore || priceAnimating || (!priceOpen && !rankOpen)) return;
    const panel = activeSidePanel(pricePanel, rankPanel);
    const other = panel === pricePanel ? rankPanel : pricePanel;
    setPricePanelHidden(other, true);
    setPricePanelHidden(panel, false);
    if (widePriceQuery.matches) {
      applyWidePriceLayout(shell, panel, wideOpenColumns, false);
      panel.style.opacity = "1";
      panel.style.overflow = "hidden";
      return;
    }
    clearWidePriceLayout(shell);
    releasePricePanelPin(panel);
    panel.style.maxHeight = "70vh";
    panel.style.opacity = "1";
    panel.style.overflow = "hidden";
  };
  const animateSideLayout = (kind) => {
    const generation = ++priceLayoutGeneration;
    const stillCurrent = () => generation === priceLayoutGeneration;
    const isOpen = () => kind === "rank" ? rankOpen : priceOpen;
    priceAnimating = true;
    ensurePriceChrome(false);
    const sortButton = findSortButton();
    const shell = sortButton ? findPriceShell(sortButton) : null;
    const panel = document.getElementById(kind === "rank" ? "cch-rank-panel" : "cch-model-price-panel");
    if (!shell || !panel) {
      priceAnimating = false;
      return;
    }
    const finishAnimation = (apply) => {
      window.setTimeout(() => {
        if (!stillCurrent()) return;
        priceAnimating = false;
        apply();
      }, 320);
    };
    if (!isOpen()) {
      panel.style.overflow = "hidden";
      panel.style.opacity = "0";
      if (widePriceQuery.matches && shell.style.display === "grid") {
        applyWidePriceLayout(shell, panel, wideClosedColumns, true);
      } else {
        panel.style.transition = "max-height 320ms ease, opacity 250ms ease";
        panel.style.maxHeight = "0px";
      }
      finishAnimation(() => {
        if (isOpen() || priceOpen || rankOpen) return;
        setPricePanelHidden(panel, true);
        clearWidePriceLayout(shell);
        releasePricePanelPin(panel);
        panel.style.opacity = "";
        panel.style.overflow = "hidden";
        panel.style.transition = "";
      });
      return;
    }
    const other = document.getElementById(kind === "rank" ? "cch-model-price-panel" : "cch-rank-panel");
    if (other) setPricePanelHidden(other, true);
    setPricePanelHidden(panel, false);
    panel.style.opacity = "0";
    panel.style.overflow = "hidden";
    if (!widePriceQuery.matches) {
      clearWidePriceLayout(shell);
      releasePricePanelPin(panel);
      panel.style.transition = "max-height 320ms ease, opacity 250ms ease";
      panel.style.maxHeight = "0px";
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (!stillCurrent() || !isOpen()) return;
        panel.style.maxHeight = "70vh";
        panel.style.opacity = "1";
        finishAnimation(() => {
          if (isOpen()) panel.style.overflow = "hidden";
        });
      }));
      return;
    }
    panel.style.transition = "opacity 250ms ease";
    applyWidePriceLayout(shell, panel, wideClosedColumns, false);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!stillCurrent() || !isOpen()) return;
      applyWidePriceLayout(shell, panel, wideOpenColumns, true);
      panel.style.opacity = "1";
      finishAnimation(() => {
        if (isOpen() && widePriceQuery.matches) pinPricePanel(shell, panel);
      });
    }));
  };
  const showSideImmediately = (kind) => {
    priceLayoutGeneration += 1;
    priceAnimating = false;
    priceOpen = kind === "price";
    rankOpen = kind === "rank";
    ensurePriceChrome(false);
    const sortButton = findSortButton();
    const shell = sortButton ? findPriceShell(sortButton) : null;
    const pricePanel = document.getElementById("cch-model-price-panel");
    const rankPanel = document.getElementById("cch-rank-panel");
    const panel = kind === "rank" ? rankPanel : pricePanel;
    const other = kind === "rank" ? pricePanel : rankPanel;
    if (!shell || !panel) return;
    if (other) setPricePanelHidden(other, true);
    setPricePanelHidden(panel, false);
    panel.style.opacity = "1";
    panel.style.overflow = "hidden";
    if (widePriceQuery.matches) {
      applyWidePriceLayout(shell, panel, wideOpenColumns, false);
      return;
    }
    clearWidePriceLayout(shell);
    releasePricePanelPin(panel);
    panel.style.maxHeight = "70vh";
  };
  const setPriceOpenState = (open) => {
    if (open && rankOpen) {
      showSideImmediately("price");
      if (!pricePayload || priceFailed) loadModelPrices();
      return;
    }
    priceOpen = open;
    const toggle = document.getElementById("cch-model-price-toggle");
    if (toggle) toggle.setAttribute("aria-pressed", open ? "true" : "false");
    animateSideLayout("price");
    if (open && (!pricePayload || priceFailed)) loadModelPrices();
  };
  const setRankOpenState = (open) => {
    if (open && priceOpen) {
      showSideImmediately("rank");
      if (!rankPayload || rankFailed) loadRanks();
      return;
    }
    rankOpen = open;
    const toggle = document.getElementById("cch-rank-toggle");
    if (toggle) toggle.setAttribute("aria-pressed", open ? "true" : "false");
    animateSideLayout("rank");
    if (open && (!rankPayload || rankFailed)) loadRanks();
  };
  const startModelPrices = () => {
    ensurePriceChrome(false);
    if (!priceObserver && document.body) {
      priceObserver = new MutationObserver(() => {
        if (priceObserver) priceObserver.disconnect();
        try {
          ensurePriceChrome(true);
        } finally {
          if (priceObserver && document.body) {
            priceObserver.observe(document.body, {childList: true, subtree: true});
          }
        }
      });
      priceObserver.observe(document.body, {childList: true, subtree: true});
    }
    if (typeof widePriceQuery.addEventListener === "function") {
      widePriceQuery.addEventListener("change", () => {
        if ((priceOpen || rankOpen) && !priceAnimating) {
          animateSideLayout(priceOpen ? "price" : "rank");
        }
      });
    }
    window.addEventListener("scroll", requestPricePanelSync, {passive: true});
    window.addEventListener("resize", requestPricePanelSync);
  };
"""


def inject_status_script(
    html: str,
    quota_status_slugs: list[str],
    cache_ttl_seconds: int,
) -> str:
    script = f"""
<script>
(() => {{
  const quotaStatusSlugs = {json.dumps(list(quota_status_slugs))};
  const quotaSlugSet = new Set(quotaStatusSlugs);
  const refreshMs = {max(1, cache_ttl_seconds) * 1000};
  const consumptionEndpoint = window.location.origin + "/cch/status/weekly-consumption";
  const leaderboardEndpoint = window.location.origin + "/cch/status/weekly-leaderboard";
  const priceCoefficientsEndpoint = window.location.origin + "/cch/status/group-price-coefficients";
  const groupUsageEndpoint = window.location.origin + "/cch/status/group-usage";
  const providerUsageEndpoint = window.location.origin + "/cch/status/provider-usage";
  const money = (value) => typeof value === "number"
    ? "$" + value.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})
    : "—";
  const dateLabel = (value) => {{
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "";
    return `${{date.getMonth() + 1}}月${{date.getDate()}}日`;
  }};
  const compactNumber = (value) => typeof value === "number"
    ? value.toLocaleString(undefined, {{ maximumFractionDigits: 0 }})
    : "0";
  const tokensM = (value) => {{
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    const millions = value / 1e6;
    const digits = millions >= 100 ? 0 : 1;
    return millions.toLocaleString(undefined, {{
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }}) + " M";
  }};
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }}[char]));
  const percent = (value) => typeof value === "number" ? value.toFixed(2) + "%" : "—";
  const alpha = (token, pct) => `color-mix(in oklab, var(${{token}}) ${{pct}}%, transparent)`;
  const palette = (value) => {{
    if (typeof value !== "number") {{
      return {{
        border: "hsl(var(--border))",
        background: "hsl(var(--background) / .6)",
        color: "var(--muted-foreground)",
      }};
    }}
    if (value < 70) {{
      return {{
        border: "rgb(16 185 129 / .55)",
        background: "rgb(16 185 129 / .12)",
        color: "rgb(5 150 105)",
      }};
    }}
    if (value < 90) {{
      return {{
        border: "rgb(249 115 22 / .6)",
        background: "rgb(249 115 22 / .14)",
        color: "rgb(194 65 12)",
      }};
    }}
    return {{
      border: "rgb(239 68 68 / .6)",
      background: "rgb(239 68 68 / .14)",
      color: "rgb(220 38 38)",
    }};
  }};
  const pillStyle = (colors) =>
    `border:1px solid ${{colors.border}};border-radius:6px;padding:2px 8px;background:${{colors.background}};color:${{colors.color}};`;
  const countdownStr = (resetsAt) => {{
    if (!resetsAt) return null;
    const diffMs = new Date(resetsAt).getTime() - Date.now();
    if (!Number.isFinite(diffMs) || diffMs <= 0) return null;
    const hours = Math.floor(diffMs / (1000 * 60 * 60));
    const minutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
    if (hours >= 24) {{
      const days = Math.floor(hours / 24);
      return `${{days}}d${{hours % 24}}h`;
    }}
    if (hours > 0) return `${{hours}}h${{minutes}}m`;
    return `${{minutes}}m`;
  }};
  const tierLabel = (name) => {{
    if (name === "five_hour") return "5h";
    if (name === "weekly_limit" || name === "seven_day") return "周";
    return String(name || "");
  }};
  const usageTierPills = (tiers) => (Array.isArray(tiers) ? tiers : []).map((tier) => {{
    const util = typeof tier.utilization === "number" ? tier.utilization : null;
    const style = pillStyle(palette(util));
    const label = tierLabel(tier.name);
    const utilText = typeof util === "number" ? Math.round(util) + "%" : "—";
    const countdown = countdownStr(tier.resetsAt);
    const countdownText = countdown ? ` · ${{countdown}}` : "";
    return `<span style="${{style}}">${{escapeHtml(label)}} ${{utilText}}${{countdownText}}</span>`;
  }}).join("");
  let usageTooltipSlug = null;
  const ensureUsageTooltip = () => {{
    let tip = document.getElementById("cch-provider-usage-tooltip");
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = "cch-provider-usage-tooltip";
    tip.style.cssText = `position:fixed;z-index:2147483647;display:none;pointer-events:none;min-width:140px;max-width:min(360px,calc(100vw - 16px));padding:10px;border:1px solid ${{alpha("--border", 60)}};border-radius:10px;background:hsl(var(--card) / .96);color:var(--foreground);box-shadow:0 8px 24px rgb(0 0 0 / .18);backdrop-filter:blur(8px);font-size:12px;line-height:1.25;`;
    document.body.appendChild(tip);
    return tip;
  }};
  const hideUsageTooltip = () => {{
    usageTooltipSlug = null;
    const tip = document.getElementById("cch-provider-usage-tooltip");
    if (tip) tip.style.display = "none";
  }};
  const positionUsageTooltip = (anchor, tip) => {{
    const rect = anchor.getBoundingClientRect();
    const gap = 8;
    const tipRect = tip.getBoundingClientRect();
    let left = rect.left;
    let top = rect.bottom + gap;
    if (left + tipRect.width > window.innerWidth - 8) {{
      left = window.innerWidth - tipRect.width - 8;
    }}
    if (left < 8) left = 8;
    if (top + tipRect.height > window.innerHeight - 8) {{
      top = rect.top - tipRect.height - gap;
    }}
    if (top < 8) top = 8;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }};
  const showUsageTooltip = (anchor, slug, group) => {{
    const keyUsages = Array.isArray(group && group.keyUsages) ? group.keyUsages : [];
    if (keyUsages.length === 0) {{
      hideUsageTooltip();
      return;
    }}
    const tip = ensureUsageTooltip();
    usageTooltipSlug = slug;
    tip.innerHTML = keyUsages.map((entry) => {{
      if (!entry || !entry.success) {{
        const style = pillStyle(palette(null));
        return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;"><span style="${{style}}">用量查询失败</span></div>`;
      }}
      const opacity = entry.stale ? "0.55" : "1";
      return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;opacity:${{opacity}};">${{usageTierPills(entry.tiers)}}</div>`;
    }}).join(`<div style="height:8px;"></div>`);
    tip.style.display = "block";
    positionUsageTooltip(anchor, tip);
  }};
  const findFirstGroupCard = () => {{
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      if (!String(link.textContent || "").trim()) continue;
      const header = link.closest("div.flex.items-center.gap-2") || link.parentElement;
      if (!header) continue;
      return header.closest("section") || header.closest('[class*="rounded"][class*="border"]');
    }}
    return null;
  }};
  const scheduleRenderQuota = (data, attempts = 20) => {{
    if (renderQuota(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderQuota(data, attempts - 1), 250);
  }};
  const scheduleRenderLeaderboard = (data, attempts = 20) => {{
    if (renderLeaderboard(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderLeaderboard(data, attempts - 1), 250);
  }};
  const scheduleRenderPriceCoefficients = (data, attempts = 20) => {{
    if (renderPriceCoefficients(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderPriceCoefficients(data, attempts - 1), 250);
  }};
  const scheduleRenderProviderUsage = (data, attempts = 20) => {{
    if (renderProviderUsage(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderProviderUsage(data, attempts - 1), 250);
  }};
  const scheduleRenderGroupUsage = (data, attempts = 20) => {{
    if (renderGroupUsage(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderGroupUsage(data, attempts - 1), 250);
  }};
  const trophyStyle = (rank) => {{
    if (rank === 1) return "filter:none;background:rgb(250 204 21 / .16);border-color:rgb(250 204 21 / .55);";
    if (rank === 2) return "filter:grayscale(1) brightness(1.18);background:rgb(148 163 184 / .16);border-color:rgb(148 163 184 / .55);";
    return "filter:sepia(1) saturate(1.6) hue-rotate(330deg) brightness(.88);background:rgb(180 83 9 / .14);border-color:rgb(180 83 9 / .45);";
  }};
  const renderLeaderboard = (data) => {{
    if (!data.weeklyLeaderboardEnabled) return true;
    const entries = Array.isArray(data.leaderboard) ? data.leaderboard.slice(0, 3) : [];
    const existing = document.getElementById("cch-weekly-leaderboard");
    if (entries.length === 0) {{
      if (existing) existing.remove();
      return true;
    }}

    const firstGroupCard = findFirstGroupCard();
    if (!firstGroupCard || !firstGroupCard.parentElement) return false;

    let board = document.getElementById("cch-weekly-leaderboard");
    if (!board) {{
      board = document.createElement("section");
      board.id = "cch-weekly-leaderboard";
      board.style.cssText = "margin:0 0 16px 0;";
      firstGroupCard.parentElement.insertBefore(board, firstGroupCard);
    }}

    const cards = entries.map((entry) => `
      <article style="min-width:0;border:1px solid ${{alpha("--border", 60)}};border-radius:12px;background:${{alpha("--card", 40)}};padding:16px;display:flex;align-items:center;gap:12px;backdrop-filter:blur(4px);box-shadow:0 1px 2px rgb(0 0 0 / .05);">
        <div style="width:32px;height:32px;flex:0 0 auto;border:1px solid ${{alpha("--border", 60)}};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px;line-height:1;background:${{alpha("--muted", 30)}};${{trophyStyle(entry.rank)}}">🏆</div>
        <div style="min-width:0;flex:1 1 auto;display:flex;align-items:center;justify-content:space-between;gap:10px;">
          <div style="min-width:0;">
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:.025em;color:var(--muted-foreground);">第${{entry.rank}}名</div>
            <div style="margin-top:2px;font-size:14px;font-weight:600;color:var(--foreground);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${{escapeHtml(entry.name || "未知用户")}}</div>
          </div>
          <div style="flex:0 0 auto;text-align:right;">
            <div style="font-size:14px;font-weight:700;color:var(--foreground);">${{money(entry.usedUsd)}}</div>
            <div style="margin-top:2px;font-size:12px;color:var(--muted-foreground);">${{compactNumber(entry.requestCount)}} 次请求</div>
          </div>
        </div>
      </article>
    `).join("");

    const rangeStart = dateLabel(data.weekStart);
    const rangeEndDate = data.weekEnd ? new Date(data.weekEnd) : null;
    if (rangeEndDate && !Number.isNaN(rangeEndDate.getTime())) {{
      rangeEndDate.setDate(rangeEndDate.getDate() - 1);
    }}
    const rangeEnd = dateLabel(rangeEndDate);
    const totalCostNote = typeof data.weeklyTotalUsedUsd === "number"
      ? `, 总消费：${{money(data.weeklyTotalUsedUsd)}}`
      : "";
    const totalRequestsNote = typeof data.weeklyTotalRequests === "number"
      ? `, 总请求：${{compactNumber(data.weeklyTotalRequests)}}`
      : "";
    const rangeNote = rangeStart && rangeEnd ? `（${{rangeStart}}-${{rangeEnd}}${{totalCostNote}}${{totalRequestsNote}}）` : "";
    board.innerHTML = `
      <div style="margin-bottom:10px;font-size:14px;font-weight:600;color:var(--foreground);">本周消费排行榜${{rangeNote}}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;">${{cards}}</div>
    `;
    return true;
  }};
  const renderQuota = (data) => {{
    const groups = Array.isArray(data && data.groups) ? data.groups : [];
    if (groups.length === 0) {{
      document.querySelectorAll("[data-cch-weekly-quota]").forEach((el) => el.remove());
      return true;
    }}

    const renderedSlugs = new Set();
    let missing = false;
    for (const group of groups) {{
      const slug = group && typeof group.statusSlug === "string" ? group.statusSlug : "";
      if (!slug) continue;
      const header = findHeaderForSlug(slug);
      if (!header) {{
        missing = true;
        continue;
      }}

      document.querySelectorAll(`span[data-cch-weekly-quota="${{slug}}"]`).forEach((el) => {{
        if (el.parentElement !== header) el.remove();
      }});

      let badges = header.querySelector(`:scope > span[data-cch-weekly-quota="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchWeeklyQuota = slug;
        badges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
        header.appendChild(badges);
      }}
      const colors = palette(group.usagePct);
      const style = pillStyle(colors);
      badges.innerHTML = [
        `<span style="${{style}}">本周消费 ${{money(group.usedUsd)}} / ${{money(group.quotaUsd)}}</span>`,
        `<span style="${{style}}">${{percent(group.usagePct)}}</span>`,
        `<span style="${{style}}">剩余 ${{money(group.remainingUsd)}}</span>`,
      ].join("");
      renderedSlugs.add(slug);
    }}

    document.querySelectorAll("[data-cch-weekly-quota]").forEach((el) => {{
      if (!renderedSlugs.has(el.getAttribute("data-cch-weekly-quota"))) el.remove();
    }});
    return !missing;
  }};
  const groupSlugFromHref = (href) => {{
    const match = String(href || "").match(/\\/status\\/([^/?#]+)(?:[?#].*)?$/);
    return match ? decodeURIComponent(match[1]) : "";
  }};
  const coefficientBadgeStyle = () =>
    "border:1px solid rgb(16 185 129 / .55);border-radius:6px;background:rgb(16 185 129 / .12);padding:2px 8px;font-size:12px;line-height:1.25;color:rgb(5 150 105);";
  const unavailableBadgeStyle = () =>
    "flex:0 0 auto;margin-left:6px;border:1px solid rgb(239 68 68 / .6);border-radius:6px;background:rgb(239 68 68 / .14);padding:2px 8px;font-size:12px;line-height:1.25;font-weight:600;color:rgb(220 38 38);white-space:nowrap;";
  const usageBadgeColors = {{
    border: "rgb(59 130 246 / .55)",
    background: "rgb(59 130 246 / .12)",
    color: "rgb(37 99 235)",
  }};
  const renderPriceCoefficients = (data) => {{
    const groups = data && data.groups && typeof data.groups === "object" ? data.groups : null;
    if (!groups) return true;

    const unavailable = new Set(
      Array.isArray(data && data.unavailable) ? data.unavailable.filter((slug) => typeof slug === "string" && slug) : []
    );
    let renderedAny = false;
    let missingUnavailable = false;
    const renderedUnavailable = new Set();
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      const slug = groupSlugFromHref(link.getAttribute("href"));
      if (!slug || !String(link.textContent || "").trim()) continue;

      const header = link.closest("div.flex.items-center.gap-2") || link.parentElement;
      if (!header) continue;

      if (unavailable.has(slug)) {{
        link.style.flex = "0 1 auto";
        let marker = header.querySelector(`:scope > span[data-cch-unavailable="${{slug}}"]`);
        if (!marker) {{
          marker = document.createElement("span");
          marker.dataset.cchUnavailable = slug;
          marker.textContent = "Unavailable";
          marker.style.cssText = unavailableBadgeStyle();
          link.insertAdjacentElement("afterend", marker);
        }}
        renderedUnavailable.add(slug);
      }}

      if (!Array.isArray(groups[slug]) || groups[slug].length === 0) continue;
      link.style.flex = "0 1 auto";

      let badges = header.querySelector(`span[data-cch-price-coefficients="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchPriceCoefficients = slug;
        badges.style.cssText = "display:inline-flex;flex:0 0 auto;align-items:center;gap:4px;margin-left:6px;";
        link.insertAdjacentElement("afterend", badges);
      }}

      badges.innerHTML = groups[slug].map((value) =>
        `<span style="${{coefficientBadgeStyle()}}">${{escapeHtml(value)}}x</span>`
      ).join("");
      renderedAny = true;
    }}

    for (const slug of unavailable) {{
      if (!renderedUnavailable.has(slug)) missingUnavailable = true;
    }}
    document.querySelectorAll("[data-cch-unavailable]").forEach((el) => {{
      if (!unavailable.has(el.getAttribute("data-cch-unavailable"))) el.remove();
    }});
    if (missingUnavailable) return false;
    return renderedAny || unavailable.size > 0 || document.querySelector('a[href*="/status/"]') !== null;
  }};
  let lastProviderUsageData = null;
  // Same mount strategy as weekly consumption (renderQuota): find the group
  // header row via the title link, then appendChild a badge strip as the last
  // flex child. Do not nest into span.ml-auto or walk Collapsible siblings.
  const findHeaderForSlug = (slug) => {{
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      if (groupSlugFromHref(link.getAttribute("href")) !== slug) continue;
      // Prefer the titled group link (skip icon-only external-link anchors).
      if (!String(link.textContent || "").trim()) continue;
      return link.closest("div.flex.items-center.gap-2") || link.parentElement;
    }}
    return null;
  }};
  const renderProviderUsage = (data) => {{
    const groups = Array.isArray(data && data.groups) ? data.groups : [];
    if (groups.length === 0) {{
      // Drop the cached payload too, otherwise the countdown refresh below
      // re-adds badges the server has since stopped reporting.
      lastProviderUsageData = null;
      hideUsageTooltip();
      document.querySelectorAll("[data-cch-provider-usage]").forEach((el) => el.remove());
      return true;
    }}
    lastProviderUsageData = data;

    const bySlug = new Map();
    for (const group of groups) {{
      const slugs = Array.isArray(group.statusGroupSlugs) ? group.statusGroupSlugs : [];
      for (const slug of slugs) {{
        if (!slug) continue;
        if (!bySlug.has(slug)) bySlug.set(slug, group);
      }}
    }}

    let renderedAny = false;
    for (const [slug, group] of bySlug.entries()) {{
      const header = findHeaderForSlug(slug);
      if (!header) continue;

      // Drop any stale node left in the wrong parent (e.g. model grid).
      document.querySelectorAll(`span[data-cch-provider-usage="${{slug}}"]`).forEach((el) => {{
        if (el.parentElement !== header) el.remove();
      }});

      let badges = header.querySelector(`:scope > span[data-cch-provider-usage="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchProviderUsage = slug;
        // Match weekly consumption strip: flex row of pills after header controls.
        badges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
        header.appendChild(badges);
      }}

      // The server keeps serving the last good reading through a transient
      // query failure; dim it so a frozen number is not mistaken for a live one.
      badges.style.opacity = group.stale ? "0.55" : "1";
      if (group.stale) {{
        badges.title = "上次成功查询的数据，最新一次查询失败";
      }} else {{
        badges.removeAttribute("title");
      }}

      if (!group.success) {{
        const style = pillStyle(palette(null));
        badges.innerHTML = `<span style="${{style}}">用量查询失败</span>`;
      }} else {{
        const tiers = Array.isArray(group.tiers) ? group.tiers : [];
        badges.innerHTML = usageTierPills(tiers);
      }}
      if (Array.isArray(group.keyUsages) && group.keyUsages.length > 0 && badges.innerHTML) {{
        badges.style.cursor = "help";
        badges.onmouseenter = () => showUsageTooltip(badges, slug, group);
        badges.onmouseleave = hideUsageTooltip;
        if (usageTooltipSlug === slug) showUsageTooltip(badges, slug, group);
      }} else {{
        badges.style.cursor = "";
        badges.onmouseenter = null;
        badges.onmouseleave = null;
        if (usageTooltipSlug === slug) hideUsageTooltip();
      }}
      renderedAny = true;
    }}
    return renderedAny || bySlug.size === 0;
  }};
  const renderGroupUsage = (data) => {{
    const groups = data && data.groups && typeof data.groups === "object" ? data.groups : null;
    if (!groups) {{
      document.querySelectorAll("[data-cch-group-usage]").forEach((el) => el.remove());
      return true;
    }}

    const renderedSlugs = new Set();
    let renderedAny = false;
    for (const [slug, entry] of Object.entries(groups)) {{
      if (!slug || !entry || typeof entry !== "object") continue;
      const header = findHeaderForSlug(slug);
      if (!header) continue;

      document.querySelectorAll(`span[data-cch-group-usage="${{slug}}"]`).forEach((el) => {{
        if (el.parentElement !== header) el.remove();
      }});

      let badges = header.querySelector(`:scope > span[data-cch-group-usage="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchGroupUsage = slug;
        badges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
        header.appendChild(badges);
      }}

      const style = pillStyle(usageBadgeColors);
      const pills = [
        `<span style="${{style}}">${{compactNumber(entry.requestCount)}} 次</span>`,
        `<span style="${{style}}">${{tokensM(entry.totalTokens)}}</span>`,
      ];
      // A card with a weekly cap already shows used/quota/remaining. Skip the
      // extra dollar pill there so the same spend is not shown twice.
      if (!quotaSlugSet.has(slug)) {{
        pills.push(`<span style="${{style}}">${{money(entry.costUsd)}}</span>`);
      }}
      badges.innerHTML = pills.join("");
      renderedSlugs.add(slug);
      renderedAny = true;
    }}

    document.querySelectorAll("[data-cch-group-usage]").forEach((el) => {{
      if (!renderedSlugs.has(el.getAttribute("data-cch-group-usage"))) el.remove();
    }});
    return renderedAny || document.querySelector('a[href*="/status/"]') !== null;
  }};
  const loadConsumption = () => {{
    fetch(consumptionEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly consumption unavailable")))
      .then((body) => body && body.ok && scheduleRenderQuota(body.data))
      .catch(() => {{}});
  }};
  const loadLeaderboard = () => {{
    fetch(leaderboardEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly leaderboard unavailable")))
      .then((body) => body && body.ok && scheduleRenderLeaderboard(body.data))
      .catch(() => {{}});
  }};
  const loadPriceCoefficients = () => {{
    fetch(priceCoefficientsEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("group price coefficients unavailable")))
      .then((body) => body && body.ok && scheduleRenderPriceCoefficients(body.data))
      .catch(() => {{}});
  }};
  const loadProviderUsage = () => {{
    fetch(providerUsageEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("provider usage unavailable")))
      .then((body) => body && body.ok && scheduleRenderProviderUsage(body.data))
      .catch(() => {{}});
  }};
  const loadGroupUsage = () => {{
    fetch(groupUsageEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("group usage unavailable")))
      .then((body) => body && body.ok && scheduleRenderGroupUsage(body.data))
      .catch(() => {{}});
  }};
{model_price_client_script()}
  const startEnhancements = () => {{
    loadConsumption();
    loadLeaderboard();
    loadPriceCoefficients();
    loadProviderUsage();
    loadGroupUsage();
    startModelPrices();
    window.setInterval(loadConsumption, refreshMs);
    window.setInterval(loadLeaderboard, refreshMs);
    window.setInterval(loadPriceCoefficients, refreshMs);
    window.setInterval(loadProviderUsage, Math.min(refreshMs, 30000));
    window.setInterval(loadGroupUsage, refreshMs);
    window.setInterval(() => {{
      if (lastProviderUsageData) renderProviderUsage(lastProviderUsageData);
    }}, 30000);
  }};
  const waitForHydration = (attempts = 80) => {{
    if (window.next && document.querySelector("next-route-announcer")) {{
      startEnhancements();
      return;
    }}
    if (attempts <= 0) {{
      startEnhancements();
      return;
    }}
    window.setTimeout(() => waitForHydration(attempts - 1), 100);
  }};
  waitForHydration();
}})();
</script>
"""
    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script


def rewrite_root_relative_urls(html: str, *, base_url: str, locale: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if (
            url.startswith("//")
            or url.startswith("/_next/")
            or url.startswith(f"/{locale}/status")
        ):
            return match.group(0)
        return match.group("prefix") + escape(base_url + url, quote=True)

    return ROOT_RELATIVE_ATTR_RE.sub(replace, html)

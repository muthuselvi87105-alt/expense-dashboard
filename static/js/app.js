/* Company Expense Management – front end (vanilla JS, Chart.js) */
(() => {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || $("[name=csrfmiddlewaretoken]")?.value || "";
  const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const short = (n) => {
    const a = Math.abs(n);
    if (a >= 1e7) return (n / 1e7).toFixed(a >= 1e8 ? 0 : 1) + " Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(a >= 1e6 ? 0 : 1) + " L";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(n));
  };
  const money = (v, cur = "INR") => (cur === "INR" ? "₹" : cur + " ") + inr.format(Number(v || 0));
  const moneyShort = (v) => "₹" + short(Number(v || 0));
  const fmtDate = (d) => (d ? new Date(d.length === 10 ? d + "T00:00:00" : d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "");
  const fmtDateTime = (d) => (d ? new Date(d).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  const store = {
    get(k, def) { try { const v = localStorage.getItem("exp." + k); return v ? JSON.parse(v) : def; } catch { return def; } },
    set(k, v) { try { localStorage.setItem("exp." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  class ApiError extends Error {
    constructor(msg, data) { super(msg); this.data = data; }
  }

  async function api(path, { method = "GET", body, form, raw } = {}) {
    const opts = { method, headers: { "X-CSRFToken": csrf(), Accept: "application/json" }, credentials: "same-origin" };
    if (form) opts.body = form;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    const res = await fetch(path, opts);
    if (res.status === 403 && res.redirected) location.href = "/login/";
    if (raw) return res;
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text.slice(0, 200) }; }
    if (!res.ok) throw new ApiError(errorText(data) || `Request failed (${res.status})`, data);
    return data;
  }

  function errorText(data) {
    if (!data) return "";
    if (typeof data === "string") return data;
    if (data.detail) return data.detail;
    if (Array.isArray(data)) return data.map(errorText).filter(Boolean).join(" ");
    return Object.entries(data).map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.map(errorText).join(" ") : errorText(v)}`).join(" · ");
  }

  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isError ? " error" : "");
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), isError ? 6000 : 3000);
  }

  function promptComment(title, label = "Comment", required = true) {
    return new Promise((resolve) => {
      const dlg = $("#promptDialog");
      $("#promptTitle").textContent = title;
      $("#promptLabel").textContent = label + (required ? " (required)" : " (optional)");
      $("#promptText").value = "";
      dlg.returnValue = "";
      dlg.showModal();
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        const text = $("#promptText").value.trim();
        if (dlg.returnValue !== "ok") return resolve(null);
        if (required && !text) { toast("A comment is required.", true); return resolve(null); }
        resolve(text);
      });
    });
  }

  const pill = (status, label) => `<span class="pill s-${esc(status)}">${esc(label || status)}</span>`;

  // -------------------------------------------------------------- state
  const S = {
    me: null,
    masters: {},
    filters: store.get("filters", { status_scope: "active" }),
    page: 1,
    ordering: "-expense_date",
    queue: "manager",
    charts: {},
    dash: null,
  };

  const VIEWS = { dashboard: "Dashboard", expenses: "Expenses", approvals: "Approvals", import: "Import", reports: "Reports", audit: "Audit log" };
  const ACTION_LABELS = {
    submit: ["Submit for approval", "btn-primary"], start_review: ["Start review", ""], approve: ["Approve", "btn-primary"],
    start_verification: ["Start finance check", ""], verify: ["Finance verify", "btn-primary"], return: ["Return for correction", ""],
    reject: ["Reject", "btn-danger"], cancel: ["Cancel expense", "btn-danger"], reopen: ["Reopen for amendment", ""],
  };
  const COMMENT_REQUIRED = new Set(["return", "reject", "cancel", "reopen"]);

  // --------------------------------------------------------------- boot
  async function boot() {
    applyTheme(store.get("theme", null));
    try {
      S.me = await api("/api/me/");
    } catch (e) { location.href = "/login/"; return; }
    const m = S.me;
    $("#userName").textContent = "Muthu";
    $("#menuName").textContent = "Muthu";
    $("#avatar").textContent = "M";
    $("#menuRoles").textContent = m.role_labels.join(", ") || "No role assigned";
    $("#menuScope").textContent = "Entities: " + m.entity_scope;
    $$("[data-requires]").forEach((el) => { el.hidden = !m[el.dataset.requires]; });
    $("#notifDot").hidden = !m.unread_notifications;

    const [entities, categories, departments, projects, vendors, employees, payments] = await Promise.all(
      ["entities", "categories", "departments", "projects", "vendors", "employees", "payment-methods"].map((p) => api(`/api/${p}/`)));
    Object.assign(S.masters, { entities, categories, departments, projects, vendors, employees, payments });
    fillFilters();
    bindGlobal();
    window.addEventListener("hashchange", route);
    route();
  }

  function fillFilters() {
    const f = $("#filters");
    const opt = (list, label = (x) => x.name) => list.map((x) => `<option value="${esc(x.id)}">${esc(label(x))}</option>`).join("");
    f.querySelector("[name=entity]").insertAdjacentHTML("beforeend", opt(S.masters.entities, (x) => `${x.code} – ${x.name}`));
    f.querySelector("[name=category]").insertAdjacentHTML("beforeend", opt(S.masters.categories));
    f.querySelector("[name=department]").insertAdjacentHTML("beforeend", opt(S.masters.departments));
    f.querySelector("[name=project]").insertAdjacentHTML("beforeend", opt(S.masters.projects));
    f.querySelector("[name=vendor]").insertAdjacentHTML("beforeend", opt(S.masters.vendors));
    f.querySelector("[name=employee]").insertAdjacentHTML("beforeend", opt(S.masters.employees));
    f.querySelector("[name=status]").insertAdjacentHTML("beforeend", S.me.statuses.map((s) => `<option value="${s.value}">${esc(s.label)}</option>`).join(""));
    f.querySelector("[name=itc_status]").insertAdjacentHTML("beforeend", S.me.itc_statuses.map((s) => `<option value="${s.value}">${esc(s.label)}</option>`).join(""));
    f.querySelector("[name=status_scope]").innerHTML = Object.entries(S.me.status_scopes)
      .map(([k, v]) => `<option value="${k}" title="${esc(v)}">${esc({ active: "All except rejected/cancelled", submitted: "Submitted & later", manager_approved: "Manager approved+", finance_verified: "Finance verified", all: "Everything" }[k] || k)}</option>`).join("");
    writeFilters();
  }

  function writeFilters() {
    $$("#filters [name]").forEach((el) => { el.value = S.filters[el.name] ?? (el.name === "status_scope" ? "active" : ""); });
    const extra = ["department", "project", "vendor", "employee", "status", "itc_status", "search"].some((k) => S.filters[k]);
    if (extra) { $("#moreFilterRow").hidden = false; $("#moreFilters").setAttribute("aria-expanded", "true"); }
  }

  function filterQuery(extra = {}) {
    const p = new URLSearchParams();
    Object.entries({ ...S.filters, ...extra }).forEach(([k, v]) => { if (v !== "" && v != null) p.set(k, v); });
    return p.toString();
  }

  function setFilters(patch, goTo) {
    Object.assign(S.filters, patch);
    Object.keys(S.filters).forEach((k) => { if (S.filters[k] === "") delete S.filters[k]; });
    store.set("filters", S.filters);
    writeFilters();
    S.page = 1;
    if (goTo && location.hash !== "#" + goTo) location.hash = goTo;
    else refresh();
  }

  function bindGlobal() {
    let t;
    $("#filters").addEventListener("input", (e) => {
      if (!e.target.name) return;
      clearTimeout(t);
      t = setTimeout(() => setFilters({ [e.target.name]: e.target.value }), e.target.type === "search" ? 350 : 0);
    });
    $("#moreFilters").addEventListener("click", () => {
      const row = $("#moreFilterRow");
      row.hidden = !row.hidden;
      $("#moreFilters").setAttribute("aria-expanded", String(!row.hidden));
    });
    $("#clearFilters").addEventListener("click", () => { S.filters = { status_scope: "active" }; setFilters({}); });
    $("#newExpenseBtn").addEventListener("click", () => openForm(null));
    $("#drawerClose").addEventListener("click", closeDrawer);
    $("#drawerBackdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });
    $("#userChip").addEventListener("click", (e) => { e.stopPropagation(); $("#userMenu").hidden = !$("#userMenu").hidden; $("#notifPanel").hidden = true; });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".user-menu")) $("#userMenu").hidden = true;
      if (!e.target.closest("#notifPanel") && !e.target.closest("#notifBtn")) $("#notifPanel").hidden = true;
    });
    $("#themeToggle").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
      applyTheme(dark ? "light" : "dark");
      store.set("theme", dark ? "light" : "dark");
      if (currentView() === "dashboard" && S.dash) renderCharts(S.dash);
    });
    $("#notifBtn").addEventListener("click", toggleNotifications);
    $$("[data-export]").forEach((b) => b.addEventListener("click", () => download(`/api/exports/expenses.${b.dataset.export}?${filterQuery()}`)));
    $("#icExport").addEventListener("click", () => download(`/api/exports/intercompany.csv?${filterQuery()}`));
    $("#ordering").addEventListener("change", (e) => { S.ordering = e.target.value; S.page = 1; loadExpenses(); });
    $$(".tab[data-queue]").forEach((b) => b.addEventListener("click", () => {
      $$(".tab[data-queue]").forEach((x) => x.classList.toggle("active", x === b));
      S.queue = b.dataset.queue;
      loadQueue();
    }));
    $$("[data-table]").forEach((b) => b.addEventListener("click", () => showTable(b.dataset.table, b.closest("figure").querySelector("h2").textContent)));
    bindImport();
  }

  function applyTheme(theme) {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }

  function download(url) {
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("Preparing download…");
  }

  // ------------------------------------------------------------- router
  const currentView = () => (location.hash.slice(1).split("/")[0] || "dashboard");

  function route() {
    let view = currentView();
    if (!VIEWS[view]) view = "dashboard";
    if (view === "import" && !S.me.can_import) view = "dashboard";
    if (view === "audit" && !S.me.can_view_audit) view = "dashboard";
    $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
    $$("[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
    $("#viewTitle").textContent = VIEWS[view];
    $("#filters").hidden = ["import", "audit"].includes(view) || view === "approvals";
    refresh();
    const sub = location.hash.split("/")[1];
    if (sub) openDetail(sub);
    else if (!$("#drawer").hidden) closeDrawer();
  }

  function refresh() {
    const v = currentView();
    if (v === "dashboard") loadDashboard();
    else if (v === "expenses") loadExpenses();
    else if (v === "approvals") loadQueue();
    else if (v === "import") loadRuns();
    else if (v === "reports") loadGST();
    else if (v === "audit") loadAudit();
    updateApprovalBadge();
  }

  function scopeNote(rule, entityScope) {
    $("#scopeNote").innerHTML = `Showing: <b>${esc(rule)}</b> · Entity access: <b>${esc(entityScope || S.me.entity_scope)}</b> · Amounts in INR unless stated`;
  }

  // ---------------------------------------------------------- dashboard
  async function loadDashboard() {
    let d;
    try { d = await api("/api/dashboard/?" + filterQuery()); } catch (e) { toast(e.message, true); return; }
    S.dash = d;
    scopeNote(d.status_rule, d.entity_scope);
    const k = d.kpis;
    const tiles = [
      ["Gross expenses", money(k.gross), `${k.records} records`, {}],
      ["Taxable value", money(k.taxable), "Excludes GST", {}],
      ["Recorded GST", money(k.recorded_gst), "As invoiced – not ITC", { gst_type: "" }],
      ["Verified eligible ITC", money(k.verified_itc), `Potential (unverified): ${moneyShort(k.potential_itc)}`, { itc_status: "VERIFIED_ELIGIBLE" }],
      ["Net cost after verified ITC", money(k.net_after_itc), "Gross − verified ITC", {}],
      ["Pending manager approval", k.pending_manager.count, moneyShort(k.pending_manager.amount), { status: "SUBMITTED,MANAGER_REVIEW" }, k.pending_manager.count > 0],
      ["Pending finance verification", k.pending_finance.count, moneyShort(k.pending_finance.amount), { status: "MANAGER_APPROVED,FINANCE_VERIFICATION" }, k.pending_finance.count > 0],
      ["Returned / rejected", `${k.returned.count} / ${k.rejected.count}`, "Need correction / declined", { status: "RETURNED,REJECTED" }],
      ["Missing receipts", k.missing_receipts, "Submitted without a receipt", { missing_receipt: "1" }, k.missing_receipts > 0],
      ["Duplicate suspects", k.duplicate_suspects, `Tax variances: ${k.tax_variances}`, { duplicate: "1" }, k.duplicate_suspects > 0],
    ];
    $("#kpis").innerHTML = tiles.map(([label, value, sub, drill, attention], i) =>
      `<button class="kpi${attention ? " attention" : ""}" data-i="${i}" title="Open matching records"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></button>`).join("");
    $$("#kpis .kpi").forEach((el) => el.addEventListener("click", () => {
      const drill = tiles[el.dataset.i][3];
      if (Object.keys(drill).length) setFilters(drill, "expenses"); else location.hash = "expenses";
    }));
    if (k.currencies.length > 1) $("#scopeNote").insertAdjacentHTML("beforeend", ` · <b>Note:</b> mixed currencies (${esc(k.currencies.join(", "))}) are summed at face value`);

    const sync = d.sync;
    $("#syncStrip").hidden = !sync;
    if (sync) {
      $("#syncStrip").innerHTML = `<span>Last successful import: <b>${sync.last_success ? fmtDateTime(sync.last_success) : "never"}</b></span>
        <span>Failed/partial imports (30 days): <b>${sync.failed_30d}</b></span>
        <span>Awaiting review: <b>${sync.awaiting_review}</b></span>
        <span>Google Sheets sync: <b>pending</b></span>${S.me.can_import ? '<a href="#import">Open imports</a>' : ""}`;
    }
    renderCharts(d);
  }

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function renderCharts(d) {
    if (!window.Chart) { setTimeout(() => renderCharts(d), 150); return; }
    const color = cssVar("--series-1"), grid = cssVar("--grid"), ink = cssVar("--text-2"), surface = cssVar("--surface");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = ink;
    const specs = [
      ["chMonth", d.by_month, "x", (r) => ({ month: r.id })],
      ["chCategory", d.by_category, "y", (r) => ({ category: r.id })],
      ["chEntity", d.by_entity, "y", (r) => ({ entity: r.id })],
      ["chAlloc", d.by_allocated_entity, "y", (r) => ({ entity: r.id })],
      ["chVendor", d.by_vendor, "y", (r) => ({ search: r.id || "" })],
      ["chStatus", d.by_status, "y", (r) => ({ status: r.id })],
      ["chEmployee", d.by_employee, "y", (r) => ({ employee: r.id })],
      ["chProject", d.by_project, "y", (r) => ({ project: r.id })],
    ];
    for (const [id, rows, axis, drill] of specs) {
      const canvas = document.getElementById(id);
      const box = canvas.parentElement;
      if (S.charts[id]) { S.charts[id].destroy(); delete S.charts[id]; }
      box.querySelector(".empty")?.remove();
      if (!rows || !rows.length) { canvas.hidden = true; box.insertAdjacentHTML("beforeend", '<div class="empty">No records for these filters</div>'); continue; }
      canvas.hidden = false;
      const horizontal = axis === "y";
      const valueAxis = { grid: { color: grid }, border: { display: false }, ticks: { callback: (v) => moneyShort(v), maxTicksLimit: 6 } };
      const catAxis = { grid: { display: false }, border: { color: grid }, ticks: { autoSkip: !horizontal, callback(v) { const l = this.getLabelForValue(v); return l.length > 22 ? l.slice(0, 21) + "…" : l; } } };
      S.charts[id] = new Chart(canvas, {
        type: "bar",
        data: { labels: rows.map((r) => r.label), datasets: [{ data: rows.map((r) => Number(r.value)), backgroundColor: color, hoverBackgroundColor: color, borderRadius: 4, borderSkipped: "start", borderColor: surface, borderWidth: 0, maxBarThickness: horizontal ? 18 : 28, categoryPercentage: 0.8 }] },
        options: {
          indexAxis: axis, responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: (c) => { const r = rows[c.dataIndex]; return ` ${money(r.value)}` + (r.count != null ? ` · ${r.count} records` : ""); }, footer: () => "Click to see records" } },
          },
          scales: horizontal ? { x: valueAxis, y: catAxis } : { y: valueAxis, x: catAxis },
          onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
          onClick: (e, els) => {
            if (!els.length) return;
            const r = rows[els[0].index];
            const patch = drill(r);
            if (id === "chStatus") patch.status_scope = "all";
            setFilters(patch, "expenses");
          },
        },
      });
    }
  }

  function showTable(key, title) {
    const rows = (S.dash && S.dash[key]) || [];
    $("#tableDialogTitle").textContent = title;
    $("#tableDialogBody").innerHTML = `<table class="data"><thead><tr><th>Item</th><th class="num">Records</th><th class="num">Amount</th></tr></thead><tbody>${
      rows.map((r) => `<tr class="nohover"><td>${esc(r.label)}</td><td class="num">${r.count ?? "—"}</td><td class="num">${money(r.value)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">No data</td></tr>'}</tbody></table>`;
    $("#tableDialog").showModal();
  }

  // ----------------------------------------------------------- expenses
  async function loadExpenses() {
    const tbody = $("#expenseTable tbody");
    tbody.innerHTML = '<tr class="nohover"><td colspan="10" class="muted">Loading…</td></tr>';
    let d;
    try { d = await api(`/api/expenses/?${filterQuery({ page: S.page, ordering: S.ordering })}`); } catch (e) { tbody.innerHTML = ""; toast(e.message, true); return; }
    scopeNote(d.status_rule);
    tbody.innerHTML = d.results.map(rowHtml).join("") || '<tr class="nohover"><td colspan="10" class="muted">No expenses match these filters.</td></tr>';
    bindRows(tbody);
    const pages = Math.max(1, Math.ceil(d.count / 25));
    $("#pager").innerHTML = `<span>${d.count} records · page ${S.page} of ${pages}</span>
      <span class="row-gap"><button class="btn btn-ghost" ${S.page <= 1 ? "disabled" : ""} data-p="-1">Previous</button><button class="btn btn-ghost" ${S.page >= pages ? "disabled" : ""} data-p="1">Next</button></span>`;
    $$("#pager [data-p]").forEach((b) => b.addEventListener("click", () => { S.page += Number(b.dataset.p); loadExpenses(); }));
  }

  function flags(e) {
    return (e.duplicate_suspect ? '<span class="flag">Duplicate?</span>' : "") + (e.tax_variance ? '<span class="flag">Tax variance</span>' : "") + (!e.attachment_count && e.status !== "DRAFT" ? '<span class="flag">No receipt</span>' : "");
  }

  function rowHtml(e) {
    return `<tr data-id="${e.id}">
      <td class="ref">${esc(e.ref)}</td><td class="date">${fmtDate(e.expense_date)}</td>
      <td class="title">${esc(e.title)}<div>${flags(e)}</div></td><td>${esc(e.paying_entity_code)}</td><td>${esc(e.category_name)}</td>
      <td>${esc(e.vendor_name_snapshot)}</td><td class="num">${money(e.gross_amount, e.currency)}</td><td class="st">${pill(e.status, e.status_label)}</td>
      <td>${esc(e.submitter_name)}</td><td class="notes" title="${esc(e.notes)}">${esc(e.notes)}</td></tr>`;
  }

  function bindRows(tbody) {
    $$("tr[data-id]", tbody).forEach((tr) => tr.addEventListener("click", () => openDetail(tr.dataset.id)));
  }

  async function loadQueue() {
    const params = { manager: { status: "SUBMITTED,MANAGER_REVIEW" }, finance: { status: "MANAGER_APPROVED,FINANCE_VERIFICATION" }, mine: { status: "DRAFT,RETURNED", employee: S.me.user.id } }[S.queue];
    const q = new URLSearchParams({ ...params, ordering: "expense_date" });
    const tbody = $("#queueTable tbody");
    let d;
    try { d = await api("/api/expenses/?" + q); } catch (e) { toast(e.message, true); return; }
    tbody.innerHTML = d.results.map((e) => `<tr data-id="${e.id}"><td class="ref">${esc(e.ref)}</td><td class="date">${fmtDate(e.expense_date)}</td><td class="title">${esc(e.title)}</td><td>${esc(e.paying_entity_code)}</td><td class="num">${money(e.gross_amount, e.currency)}</td><td class="st">${pill(e.status, e.status_label)}</td><td>${esc(e.submitter_name)}</td><td>${flags(e)}</td></tr>`).join("")
      || '<tr class="nohover"><td colspan="8" class="muted">Nothing waiting here.</td></tr>';
    bindRows(tbody);
  }

  async function updateApprovalBadge() {
    const r = S.me.roles;
    const statuses = [];
    if (r.includes("MANAGER") || r.includes("SUPER_ADMIN")) statuses.push("SUBMITTED", "MANAGER_REVIEW");
    if (r.some((x) => ["FINANCE_ADMIN", "ACCOUNTANT", "SUPER_ADMIN"].includes(x))) statuses.push("MANAGER_APPROVED", "FINANCE_VERIFICATION");
    if (!statuses.length) return;
    try {
      const d = await api("/api/expenses/?" + new URLSearchParams({ status: statuses.join(","), page_size: 1 }));
      const b = $("#approvalsBadge");
      b.textContent = d.count;
      b.hidden = !d.count;
    } catch { /* badge is best-effort */ }
  }

  // ------------------------------------------------------------- drawer
  function openDrawer(title) {
    $("#drawerTitle").textContent = title;
    $("#drawer").hidden = false;
    $("#drawerBackdrop").hidden = false;
    $("#drawerBody").innerHTML = '<p class="muted">Loading…</p>';
    $("#drawerBody").scrollTop = 0;
  }

  function closeDrawer() {
    $("#drawer").hidden = true;
    $("#drawerBackdrop").hidden = true;
    if (location.hash.includes("/")) history.replaceState(null, "", "#" + currentView());
  }

  const nameOf = (list, id, fmt = (x) => x.name) => { const x = (S.masters[list] || []).find((o) => String(o.id) === String(id)); return x ? fmt(x) : ""; };

  async function openDetail(id) {
    openDrawer("Expense");
    let e;
    try { e = await api(`/api/expenses/${id}/`); } catch (err) { $("#drawerBody").innerHTML = `<p class="alert alert-error">${esc(err.message)}</p>`; return; }
    renderDetail(e);
  }

  function renderDetail(e) {
    $("#drawerTitle").innerHTML = `${esc(e.ref)} · ${esc(e.title)} ${pill(e.status, e.status_label)}`;
    const ent = (id) => nameOf("entities", id, (x) => `${x.code} – ${x.name}`);
    const calc = e.calculated_tax_values || {};
    const d = (label, value) => `<div><span>${esc(label)}</span><b>${value === "" || value == null ? "—" : value}</b></div>`;
    const warn = [];
    if (e.duplicate_suspect) warn.push("Possible duplicate of another expense – Finance should review before verifying.");
    if (e.tax_variance) warn.push(`Declared tax differs from the system calculation (calculated gross ${money(calc.gross_amount)}).`);
    if (e.status === "RETURNED") { const last = [...e.history].reverse().find((h) => h.action === "return"); if (last) warn.push(`Returned by ${last.actor.name}: “${last.comment}”`); }
    if (e.status === "REJECTED") { const last = [...e.history].reverse().find((h) => h.action === "reject"); if (last) warn.push(`Rejected by ${last.actor.name}: “${last.comment}”`); }

    const actions = e.actions.map((a) => `<button class="btn ${ACTION_LABELS[a]?.[1] || ""}" data-action="${a}">${esc(ACTION_LABELS[a]?.[0] || a)}</button>`).join("");
    const allocTotal = e.allocations.reduce((s, a) => s + Number(a.amount), 0);

    $("#drawerBody").innerHTML = `
      ${warn.map((w) => `<p class="alert alert-warn">${esc(w)}</p>`).join("")}
      <div class="detail-grid">
        ${d("Expense date", fmtDate(e.expense_date))}
        ${d("Gross amount", `<span style="font-size:18px;font-weight:700">${money(e.gross_amount, e.currency)}</span>`)}
        ${d("Category", esc(nameOf("categories", e.category)))}
        ${d("Paying entity", esc(ent(e.paying_entity)))}
        ${d("Beneficiary entity", esc(ent(e.beneficiary_entity)))}
        ${d("Submitter", esc(e.submitter.name))}
        ${d("Department", esc(nameOf("departments", e.department)))}
        ${d("Project / cost centre", esc(nameOf("projects", e.project)))}
        ${d("Payment method", esc(nameOf("payments", e.payment_method)))}
        ${d("Source", esc(e.source_type === "MANUAL" ? "Manual entry" : e.source_type + " import") + (e.external_record_id ? ` · ${esc(e.external_record_id)}` : ""))}
        ${d("Version", e.version_number)}
        ${d("Updated", fmtDateTime(e.updated_at))}
      </div>
      ${e.description ? `<div class="section"><h3>Business purpose</h3><p>${esc(e.description)}</p></div>` : ""}
      ${e.notes ? `<div class="section"><h3>Notes</h3><p>${esc(e.notes)}</p></div>` : ""}

      <div class="section"><h3>Invoice &amp; GST</h3>
        <div class="detail-grid">
          ${d("Vendor", esc(e.vendor_name_snapshot))}${d("Invoice no.", esc(e.invoice_number))}${d("Invoice date", fmtDate(e.invoice_date))}
          ${d("Supplier GSTIN", esc(e.supplier_gstin))}${d("Recipient GSTIN", esc(e.recipient_gstin))}${d("Supplier state → place of supply", esc(`${e.supplier_state_code || "?"} → ${e.place_of_supply_state_code || "?"}`))}
          ${d("GST type", esc(e.gst_type))}${d("Rate", e.gst_rate != null ? esc(Number(e.gst_rate)) + "%" : "")}${d("Reconciliation", esc(e.reconciliation_status))}
        </div>
        <div class="calc-box">
          <div><span>Taxable value</span><b>${money(e.taxable_value)}</b></div>
          <div><span>CGST</span><b>${money(e.cgst_amount)}</b></div><div><span>SGST/UTGST</span><b>${money(e.sgst_utgst_amount)}</b></div>
          <div><span>IGST</span><b>${money(e.igst_amount)}</b></div><div><span>Cess</span><b>${money(e.cess_amount)}</b></div>
          <div><span>Other charges</span><b>${money(e.other_charges)}</b></div><div><span>Recorded GST</span><b>${money(e.total_gst)}</b></div>
          ${calc.gross_amount ? `<div><span>System-calculated gross</span><b>${money(calc.gross_amount)}</b></div>` : ""}
          ${calc.rcm_liability ? `<div><span>RCM liability (not in gross)</span><b>${money(calc.rcm_liability)}</b></div>` : ""}
        </div>
      </div>

      <div class="section"><h3>Input tax credit review</h3>
        <div class="detail-grid">
          ${d("ITC status", esc(e.itc_status_label))}${d("Verified eligible ITC", money(e.verified_itc_amount))}${d("Potential ITC (info only)", money(e.potential_itc_amount))}
          ${e.itc_reviewed_at ? d("Reviewed", fmtDateTime(e.itc_reviewed_at)) + d("Reason", esc(e.itc_reason)) + d("Evidence / reference", esc(e.itc_reference)) : ""}
        </div>
        ${e.can_review_itc && !["DRAFT", "CANCELLED"].includes(e.status) && Number(e.total_gst) > 0 ? `
        <form id="itcForm" class="form-grid">
          <label class="field"><span>ITC status</span><select name="itc_status">${S.me.itc_statuses.map((s) => `<option value="${s.value}" ${s.value === e.itc_status ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select></label>
          <label class="field"><span>Eligible ITC amount</span><input name="verified_itc_amount" type="number" step="0.01" min="0" max="${e.total_gst}" value="${Number(e.verified_itc_amount) || e.total_gst}"><small class="hint">Only counted when status is “Verified eligible”.</small></label>
          <label class="field"><span>Reason (required)</span><input name="reason" required value=""></label>
          <label class="field"><span>Evidence / reference</span><input name="reference"></label>
          <div class="span-2"><button class="btn" type="submit">Save ITC review</button>${e.status === "FINANCE_VERIFIED" ? ' <span class="muted small">Recorded as an amendment after verification.</span>' : ""}</div>
        </form>` : ""}
      </div>

      <div class="section"><h3>Intercompany allocation</h3>
        ${e.allocations.length ? `<table class="data"><thead><tr><th>Beneficiary</th><th class="num">%</th><th class="num">Allocated</th><th class="num">Recharge</th><th>Recharge status</th><th>Reference</th></tr></thead><tbody>
          ${e.allocations.map((a) => `<tr class="nohover"><td>${esc(a.beneficiary_entity_code)}</td><td class="num">${a.percent != null ? Number(a.percent) : "—"}</td><td class="num">${money(a.amount)}</td><td class="num">${money(a.recharge_amount)}</td><td>${esc(a.recharge_status)}</td><td>${esc(a.reference)}</td></tr>`).join("")}
          </tbody><tfoot><tr><td>Unallocated</td><td></td><td class="num">${money(Number(e.gross_amount) - allocTotal)}</td><td colspan="3"></td></tr></tfoot></table>`
          : '<p class="muted small">No allocation. The full cost sits with the beneficiary (or paying) entity.</p>'}
        <p class="muted small">The supplier invoice is counted once in group spend; allocations only redistribute cost between entities.</p>
        ${e.can_edit || e.can_review_itc ? '<button class="btn btn-ghost" id="editAlloc">Edit allocation</button>' : ""}
        <div id="allocEditor"></div>
      </div>

      <div class="section"><h3>Receipts &amp; attachments</h3>
        <div class="att-list">${e.attachments.map((a) => `<div class="att"><a href="/attachments/${a.id}/?inline=1" target="_blank" rel="noopener">${esc(a.file_name)}</a><span class="muted small">${(a.size / 1024).toFixed(0)} KB · ${fmtDateTime(a.uploaded_at)}</span></div>`).join("") || '<p class="muted small">No attachments yet.</p>'}</div>
        ${e.can_edit || (e.can_review_itc && e.status !== "FINANCE_VERIFIED") ? `<form id="attForm" class="row-gap" style="margin-top:8px"><input type="file" name="file" accept=".pdf,.jpg,.jpeg,.png" required style="max-width:300px"><button class="btn" type="submit">Upload</button><span class="muted small">PDF, JPG or PNG · max ${S.me.limits.ATTACHMENT_MAX_BYTES / 1048576} MB · up to ${S.me.limits.ATTACHMENT_MAX_COUNT} files</span></form>` : ""}
      </div>

      <div class="section"><h3>History</h3>
        <ul class="timeline">${e.history.map((h) => `<li><b>${esc(ACTION_LABELS[h.action]?.[0] || h.action)}</b> by ${esc(h.actor.name)} <span class="when">${fmtDateTime(h.event_time)}</span>${h.comment ? `<br>“${esc(h.comment)}”` : ""}</li>`).join("") || '<li class="muted">Created, not yet submitted.</li>'}</ul>
      </div>

      <div class="sticky-actions">
        ${e.can_edit ? '<button class="btn" id="editBtn">Edit</button>' : ""}
        ${e.can_edit && e.status === "DRAFT" && !e.history.length ? '<button class="btn btn-ghost btn-danger" id="deleteBtn">Delete draft</button>' : ""}
        ${actions || (!e.can_edit ? '<span class="muted small">No actions available to you for this record.</span>' : "")}
      </div>`;

    $$("[data-action]", $("#drawerBody")).forEach((b) => b.addEventListener("click", () => doAction(e, b.dataset.action)));
    $("#editBtn")?.addEventListener("click", () => openForm(e));
    $("#deleteBtn")?.addEventListener("click", async () => {
      if (!confirm("Delete this draft permanently?")) return;
      try { await api(`/api/expenses/${e.id}/`, { method: "DELETE" }); toast("Draft deleted"); closeDrawer(); refresh(); } catch (err) { toast(err.message, true); }
    });
    $("#itcForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = Object.fromEntries(new FormData(ev.target));
      try { renderDetail(await api(`/api/expenses/${e.id}/itc-review/`, { method: "POST", body })); toast("ITC review saved"); refresh(); } catch (err) { toast(err.message, true); }
    });
    $("#attForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try { renderDetail(await api(`/api/expenses/${e.id}/attachments/`, { method: "POST", form: new FormData(ev.target) })); toast("Attachment uploaded"); } catch (err) { toast(err.message, true); }
    });
    $("#editAlloc")?.addEventListener("click", () => allocEditor(e));
  }

  async function doAction(e, action) {
    let comment = "";
    if (COMMENT_REQUIRED.has(action)) {
      comment = await promptComment(ACTION_LABELS[action][0], "Reason", true);
      if (comment === null) return;
    } else if (["approve", "verify"].includes(action)) {
      comment = await promptComment(ACTION_LABELS[action][0], "Comment", false);
      if (comment === null) return;
    }
    try {
      const updated = await api(`/api/expenses/${e.id}/transition/`, { method: "POST", body: { action, comment } });
      toast(`${updated.ref}: ${updated.status_label}`);
      renderDetail(updated);
      refresh();
    } catch (err) { toast(err.message, true); }
  }

  function allocEditor(e) {
    const box = $("#allocEditor");
    const lines = e.allocations.length ? e.allocations.map((a) => ({ ...a })) : [
      { beneficiary_entity: e.paying_entity, method: "PERCENT", percent: 100, amount: "", recharge_amount: 0, recharge_status: "DRAFT", reference: "" }];
    const entOpts = (sel) => S.masters.entities.map((x) => `<option value="${x.id}" ${String(x.id) === String(sel) ? "selected" : ""}>${esc(x.code)}</option>`).join("");
    const statuses = ["DRAFT", "PENDING_REVIEW", "APPROVED", "INVOICED", "SETTLED", "CANCELLED"];
    function draw() {
      const method = lines[0]?.method || "PERCENT";
      box.innerHTML = `<div class="card pad" style="margin-top:8px">
        <label class="f inline"><span>Basis</span><select id="allocMethod"><option value="PERCENT" ${method === "PERCENT" ? "selected" : ""}>Percentage of gross</option><option value="FIXED" ${method === "FIXED" ? "selected" : ""}>Fixed amount</option></select></label>
        <div style="margin-top:10px">${lines.map((l, i) => `<div class="alloc-row" data-i="${i}">
          <label class="field"><span>Entity</span><select data-k="beneficiary_entity">${entOpts(l.beneficiary_entity)}</select></label>
          ${method === "PERCENT" ? `<label class="field"><span>%</span><input type="number" step="0.01" min="0" max="100" data-k="percent" value="${l.percent ?? ""}"></label>` : `<label class="field"><span>Amount</span><input type="number" step="0.01" min="0" data-k="amount" value="${l.amount ?? ""}"></label>`}
          <label class="field"><span>Recharge amt</span><input type="number" step="0.01" min="0" data-k="recharge_amount" value="${l.recharge_amount ?? 0}"></label>
          <label class="field"><span>Recharge status</span><select data-k="recharge_status">${statuses.map((s) => `<option ${s === l.recharge_status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
          <button class="icon-btn" data-del="${i}" aria-label="Remove line">✕</button></div>`).join("")}</div>
        <div class="row-gap"><button class="btn btn-ghost" id="allocAdd">Add line</button><button class="btn btn-primary" id="allocSave">Save allocation</button><button class="btn btn-ghost" id="allocClear">Remove all</button><span class="muted small" id="allocSum"></span></div></div>`;
      $("#allocMethod").addEventListener("change", (ev) => { lines.forEach((l) => (l.method = ev.target.value)); draw(); });
      $$(".alloc-row [data-k]", box).forEach((inp) => inp.addEventListener("input", () => { lines[inp.closest(".alloc-row").dataset.i][inp.dataset.k] = inp.value; sum(); }));
      $$("[data-del]", box).forEach((b) => b.addEventListener("click", () => { lines.splice(Number(b.dataset.del), 1); draw(); }));
      $("#allocAdd").addEventListener("click", () => { lines.push({ beneficiary_entity: S.masters.entities[0].id, method, percent: "", amount: "", recharge_amount: 0, recharge_status: "DRAFT" }); draw(); });
      $("#allocClear").addEventListener("click", () => { lines.length = 0; save(); });
      $("#allocSave").addEventListener("click", save);
      sum();
    }
    function sum() {
      const m = lines[0]?.method;
      const t = lines.reduce((s, l) => s + Number((m === "PERCENT" ? l.percent : l.amount) || 0), 0);
      const el = $("#allocSum");
      if (el) el.textContent = m === "PERCENT" ? `Total ${t}% (${100 - t}% unallocated)` : `Total ${money(t)} of ${money(e.gross_amount)}`;
    }
    async function save() {
      const payload = lines.map((l) => ({ beneficiary_entity: l.beneficiary_entity, method: l.method, percent: l.method === "PERCENT" ? l.percent || 0 : null, amount: l.method === "FIXED" ? l.amount || 0 : null, recharge_amount: l.recharge_amount || 0, recharge_status: l.recharge_status, reference: l.reference || "" }));
      try { const r = await api(`/api/expenses/${e.id}/allocations/`, { method: "PUT", body: { lines: payload } }); toast("Allocation saved"); renderDetail(r); } catch (err) { toast(err.message, true); }
    }
    draw();
  }

  // ---------------------------------------------------------------- form
  function openForm(e) {
    openDrawer(e ? `Edit ${e.ref}` : "New expense");
    const prof = S.me;
    const v = (k, def = "") => (e ? e[k] ?? def : def);
    const opt = (list, sel, fmt = (x) => x.name, blank = true) => (blank ? '<option value="">—</option>' : "") + S.masters[list].map((x) => `<option value="${x.id}" ${String(x.id) === String(sel) ? "selected" : ""}>${esc(fmt(x))}</option>`).join("");
    const today = new Date().toISOString().slice(0, 10);
    const gstTypes = [["", "No GST / not an invoice"], ["CGST_SGST", "CGST + SGST (intra-state)"], ["IGST", "IGST (inter-state)"], ["EXEMPT", "Exempt"], ["NIL_RATED", "Nil rated"], ["ZERO_RATED", "Zero rated"], ["NON_GST", "Non-GST"], ["RCM", "Reverse charge"], ["CESS", "Cess only"]];
    const hasOverride = e && e.source_tax_values && Object.keys(e.source_tax_values).some((k) => k !== "gross_amount");
    $("#drawerBody").innerHTML = `<form id="expForm" novalidate>
      <div id="formErrors"></div>
      <fieldset><legend>Expense</legend><div class="form-grid">
        <label class="field span-2"><span>Expense name *</span><input name="title" required maxlength="200" value="${esc(v("title"))}" placeholder="e.g. Client lunch"></label>
        <label class="field"><span>Date *</span><input type="date" name="expense_date" required max="${today}" value="${esc(v("expense_date", today))}"></label>
        <label class="field"><span>Category *</span><select name="category" required>${opt("categories", v("category"))}</select></label>
        <label class="field"><span>Paying entity *</span><select name="paying_entity" required>${opt("entities", v("paying_entity", S.me.defaults.entity || (S.masters.entities.length === 1 ? S.masters.entities[0].id : "")), (x) => `${x.code} – ${x.name}`)}</select></label>
        <label class="field"><span>Beneficiary entity</span><select name="beneficiary_entity">${opt("entities", v("beneficiary_entity"), (x) => `${x.code} – ${x.name}`)}</select><small class="hint">Only if a different group company benefits.</small></label>
        <label class="field"><span>Department</span><select name="department">${opt("departments", v("department", S.me.defaults.department || ""))}</select></label>
        <label class="field"><span>Project / cost centre</span><select name="project">${opt("projects", v("project"))}</select></label>
        <label class="field"><span>Payment method</span><select name="payment_method">${opt("payments", v("payment_method"))}</select></label>
        <label class="field"><span>Currency</span><input name="currency" maxlength="3" value="${esc(v("currency", "INR"))}"></label>
        <label class="field span-2"><span>Business purpose</span><textarea name="description" rows="2">${esc(v("description"))}</textarea></label>
        <label class="field span-2"><span>Notes</span><textarea name="notes" rows="2">${esc(v("notes"))}</textarea></label>
      </div></fieldset>

      <fieldset><legend>Amount, invoice &amp; GST</legend><div class="form-grid">
        <label class="field"><span>GST treatment</span><select name="gst_type">${gstTypes.map(([k, l]) => `<option value="${k}" ${k === v("gst_type") ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="field gst-only"><span>GST rate %</span><input type="number" name="gst_rate" step="0.001" min="0" value="${esc(v("gst_rate") ?? "")}"></label>
        <label class="field gst-only"><span>Taxable value</span><input type="number" name="taxable_value" step="0.01" min="0" value="${esc(e && Number(e.taxable_value) ? e.taxable_value : "")}"></label>
        <label class="field"><span id="grossLabel">Gross amount *</span><input type="number" name="gross_amount" step="0.01" min="0" value="${esc(e ? e.gross_amount : "")}"><small class="hint" id="grossHint"></small></label>
        <label class="field"><span>Other charges</span><input type="number" name="other_charges" step="0.01" min="0" value="${esc(e && Number(e.other_charges) ? e.other_charges : "")}"></label>
        <label class="field"><span>Vendor</span><select name="vendor">${opt("vendors", v("vendor"))}</select></label>
        <label class="field"><span>Vendor name (if not listed)</span><input name="vendor_name_snapshot" value="${esc(v("vendor_name_snapshot"))}"></label>
        <label class="field"><span>Invoice number</span><input name="invoice_number" value="${esc(v("invoice_number"))}"></label>
        <label class="field"><span>Invoice date</span><input type="date" name="invoice_date" max="${today}" value="${esc(v("invoice_date") || "")}"></label>
        <label class="field gst-only"><span>Supplier GSTIN</span><input name="supplier_gstin" maxlength="15" value="${esc(v("supplier_gstin"))}" style="text-transform:uppercase"></label>
        <label class="field gst-only"><span>Supplier state code</span><input name="supplier_state_code" maxlength="2" value="${esc(v("supplier_state_code"))}" placeholder="e.g. 33"></label>
        <label class="field gst-only"><span>Place of supply state code</span><input name="place_of_supply_state_code" maxlength="2" value="${esc(v("place_of_supply_state_code"))}"></label>
        <div class="span-2 gst-only"><div class="calc-box" id="calcBox"></div></div>
        <label class="check span-2 gst-only"><input type="checkbox" id="overrideTax" ${hasOverride ? "checked" : ""}> Enter tax amounts exactly as on the invoice (system will flag a variance if they differ)</label>
        <div class="span-2 form-grid" id="overrideBox" ${hasOverride ? "" : "hidden"}>
          <label class="field"><span>CGST</span><input type="number" step="0.01" min="0" name="cgst_amount" value="${esc(e ? e.cgst_amount : "")}"></label>
          <label class="field"><span>SGST / UTGST</span><input type="number" step="0.01" min="0" name="sgst_utgst_amount" value="${esc(e ? e.sgst_utgst_amount : "")}"></label>
          <label class="field"><span>IGST</span><input type="number" step="0.01" min="0" name="igst_amount" value="${esc(e ? e.igst_amount : "")}"></label>
          <label class="field"><span>Cess</span><input type="number" step="0.01" min="0" name="cess_amount" value="${esc(e ? e.cess_amount : "")}"></label>
        </div>
      </div></fieldset>
      ${!e ? `<fieldset><legend>Receipt</legend><input type="file" name="receipt" accept=".pdf,.jpg,.jpeg,.png"><p class="muted small">PDF, JPG or PNG, max ${S.me.limits.ATTACHMENT_MAX_BYTES / 1048576} MB. You can add more later.</p></fieldset>` : ""}
      <div class="sticky-actions">
        <button class="btn btn-ghost" type="button" id="cancelForm">Cancel</button>
        <button class="btn" type="submit" data-then="">Save draft</button>
        <button class="btn btn-primary" type="submit" data-then="submit">Save &amp; submit</button>
      </div></form>`;

    const form = $("#expForm");
    const val = (n) => form.elements[n]?.value ?? "";
    function updateCalc() {
      const type = val("gst_type");
      const gstOn = !!type;
      $$(".gst-only", form).forEach((el) => (el.hidden = !gstOn));
      if (!gstOn) { $("#grossLabel").textContent = "Amount *"; $("#grossHint").textContent = ""; return; }
      const taxable = Number(val("taxable_value") || 0), rate = Number(val("gst_rate") || 0), other = Number(val("other_charges") || 0);
      let c = 0, s = 0, i = 0;
      const sup = val("supplier_state_code"), pos = val("place_of_supply_state_code");
      let t = type;
      if (t === "RCM") t = "RCM";
      if (t === "CGST_SGST") { c = s = Math.round(taxable * rate / 2) / 100; }
      else if (t === "IGST") { i = Math.round(taxable * rate) / 100; }
      const noTax = ["EXEMPT", "NIL_RATED", "ZERO_RATED", "NON_GST"].includes(t);
      const gross = noTax && !taxable ? Number(val("gross_amount") || 0) : taxable + c + s + i + other;
      const stateHint = sup && pos ? (sup === pos ? "States match → intra-state (CGST+SGST) is typical." : "States differ → inter-state (IGST) is typical.") : "";
      $("#calcBox").innerHTML = `<div><span>CGST</span><b>${money(c)}</b></div><div><span>SGST/UTGST</span><b>${money(s)}</b></div><div><span>IGST</span><b>${money(i)}</b></div><div><span>Calculated gross</span><b>${money(gross)}</b></div>${t === "RCM" ? `<div><span>RCM liability</span><b>${money(Math.round(taxable * rate) / 100)}</b></div>` : ""}${stateHint ? `<div style="grid-column:1/-1"><span>Hint</span>${esc(stateHint)} Finance confirms the treatment.</div>` : ""}`;
      $("#grossLabel").textContent = "Gross amount";
      $("#grossHint").textContent = noTax ? "Enter the amount paid." : "Leave blank to use the calculated gross. If entered, it is checked against the calculation.";
    }
    form.addEventListener("input", updateCalc);
    $("#overrideTax").addEventListener("change", (ev) => ($("#overrideBox").hidden = !ev.target.checked));
    $("#cancelForm").addEventListener("click", () => (e ? renderDetail(e) : closeDrawer()));
    form.elements.vendor.addEventListener("change", () => {
      const vd = S.masters.vendors.find((x) => String(x.id) === form.elements.vendor.value);
      if (vd) {
        if (!form.elements.supplier_gstin.value) form.elements.supplier_gstin.value = vd.gstin;
        if (!form.elements.supplier_state_code.value) form.elements.supplier_state_code.value = vd.state_code;
      }
    });
    form.elements.paying_entity.addEventListener("change", () => {
      const en = S.masters.entities.find((x) => String(x.id) === form.elements.paying_entity.value);
      if (en && !form.elements.place_of_supply_state_code.value) form.elements.place_of_supply_state_code.value = en.state_code;
    });
    updateCalc();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const then = ev.submitter?.dataset.then;
      const body = {};
      const fields = ["title", "expense_date", "category", "paying_entity", "beneficiary_entity", "department", "project", "payment_method", "currency", "description", "notes", "gst_type", "vendor", "vendor_name_snapshot", "invoice_number", "invoice_date"];
      fields.forEach((f) => (body[f] = val(f) || (["title", "description", "notes", "vendor_name_snapshot", "invoice_number", "gst_type"].includes(f) ? "" : null)));
      body.currency = (body.currency || "INR").toUpperCase();
      const gstOn = !!val("gst_type");
      body.other_charges = val("other_charges") || "0";
      if (gstOn) {
        Object.assign(body, { gst_rate: val("gst_rate") || null, taxable_value: val("taxable_value") || "0", supplier_gstin: val("supplier_gstin").toUpperCase(), supplier_state_code: val("supplier_state_code"), place_of_supply_state_code: val("place_of_supply_state_code") });
        if ($("#overrideTax").checked) ["cgst_amount", "sgst_utgst_amount", "igst_amount", "cess_amount"].forEach((f) => (body[f] = val(f) || "0"));
        if (val("gross_amount")) body.gross_amount = val("gross_amount");
      } else {
        Object.assign(body, { gst_rate: null, taxable_value: "0", supplier_gstin: "", supplier_state_code: "", place_of_supply_state_code: "", gross_amount: val("gross_amount") || null });
      }
      // client-side checks (server re-validates)
      const missing = [["title", "Expense name"], ["expense_date", "Date"], ["category", "Category"], ["paying_entity", "Paying entity"]].filter(([k]) => !body[k]).map(([, l]) => l);
      if (!gstOn && !body.gross_amount) missing.push("Amount");
      if (gstOn && !Number(body.taxable_value) && !body.gross_amount) missing.push("Taxable value or gross amount");
      if (missing.length) { $("#formErrors").innerHTML = `<p class="alert alert-error">Please fill: ${esc(missing.join(", "))}</p>`; $("#drawerBody").scrollTop = 0; return; }
      if (e) body.version_number = e.version_number;
      try {
        let saved = await api(e ? `/api/expenses/${e.id}/` : "/api/expenses/", { method: e ? "PATCH" : "POST", body });
        const file = form.elements.receipt?.files?.[0];
        if (file) {
          const fd = new FormData();
          fd.append("file", file);
          try { saved = await api(`/api/expenses/${saved.id}/attachments/`, { method: "POST", form: fd }); } catch (err) { toast("Saved, but receipt upload failed: " + err.message, true); }
        }
        if (then === "submit") {
          try { saved = await api(`/api/expenses/${saved.id}/transition/`, { method: "POST", body: { action: "submit" } }); toast(`${saved.ref} submitted for approval`); }
          catch (err) { toast(`Saved as draft. Not submitted: ${err.message}`, true); saved = await api(`/api/expenses/${saved.id}/`); }
        } else toast(`${saved.ref} saved`);
        renderDetail(saved);
        refresh();
      } catch (err) {
        $("#formErrors").innerHTML = `<p class="alert alert-error">${esc(err.message)}</p>`;
        $("#drawerBody").scrollTop = 0;
      }
    });
  }

  // -------------------------------------------------------------- import
  function bindImport() {
    const form = $("#importForm"), drop = $("#drop"), input = form.elements.file;
    input.addEventListener("change", () => ($("#dropLabel").textContent = input.files[0]?.name || "Choose a file"));
    ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (e) => { e.preventDefault(); input.files = e.dataTransfer.files; $("#dropLabel").textContent = input.files[0]?.name; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!input.files.length) { toast("Choose a file first", true); return; }
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      btn.textContent = "Checking file…";
      try {
        const run = await api("/api/imports/", { method: "POST", form: new FormData(form) });
        form.reset();
        $("#dropLabel").textContent = "Choose a .xlsx or .csv file, or drop it here";
        await showRun(run);
        loadRuns();
      } catch (err) { toast(err.message, true); }
      finally { btn.disabled = false; btn.textContent = "Upload and preview"; }
    });
  }

  async function showRun(run) {
    const card = $("#previewCard");
    card.hidden = false;
    const c = run.row_counts || {};
    const rows = c.total ? await api(`/api/imports/${run.id}/rows/`) : [];
    const count = (label, n) => `<div class="count"><b>${n ?? 0}</b>${label}</div>`;
    const done = run.status !== "AWAITING_REVIEW";
    card.innerHTML = `<div class="row-gap" style="justify-content:space-between"><h2>${esc(run.file_name)} ${pill(run.status === "SUCCESS" ? "FINANCE_VERIFIED" : run.status === "FAILED" ? "REJECTED" : run.status === "PARTIAL" ? "RETURNED" : "SUBMITTED", run.status_label)}</h2>
        <span class="muted small">${fmtDateTime(run.started_at)}</span></div>
      ${run.error_message ? `<p class="alert ${run.status === "SKIPPED" ? "alert-info" : "alert-error"}">${esc(run.error_message)}</p>` : ""}
      ${c.total != null ? `<div class="counts">${count("rows", c.total)}${done ? count("created", c.created) + count("updated", c.updated) + count("unchanged", c.skipped) : count("new", c.NEW) + count("updates", c.UPDATE) + count("duplicate candidates", c.DUPLICATE) + count("unchanged", c.UNCHANGED)}${count("rejected", done ? c.rejected : c.REJECTED)}</div>` : ""}
      ${c.unknown_columns && c.unknown_columns.length ? `<p class="alert alert-warn">Ignored unknown columns: ${esc(c.unknown_columns.join(", "))}</p>` : ""}
      ${rows.length ? `<div class="table-wrap" style="max-height:360px;overflow:auto"><table class="data"><thead><tr><th>Row</th><th>Result</th><th>Expense</th><th>Date</th><th>Ref / ID</th><th>Messages</th></tr></thead><tbody>
        ${rows.map((r) => `<tr class="nohover row-${r.status}"><td>${r.row_number}</td><td>${esc(r.status)}</td><td>${esc(r.title)}</td><td>${esc(r.expense_date)}</td><td>${esc(r.expense_ref || r.external_record_id)}</td><td>${r.errors.map((x) => `<div style="color:var(--bad)">${esc(x.message)}</div>`).join("")}${r.warnings.map((w) => `<div style="color:var(--warn)">${esc(w)}</div>`).join("")}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
      <div class="row-gap" style="margin-top:12px">
        ${!done ? `<button class="btn btn-primary" id="commitRun" ${(c.NEW || 0) + (c.UPDATE || 0) + (c.DUPLICATE || 0) ? "" : "disabled"}>Import ${(c.NEW || 0) + (c.DUPLICATE || 0)} new and ${c.UPDATE || 0} updated rows</button><button class="btn btn-ghost" id="discardRun">Discard</button>` : ""}
        ${(c.REJECTED || c.rejected) ? `<a class="btn" href="/api/imports/${run.id}/errors.csv/">Download error report</a>` : ""}
        ${done && (c.created || c.updated) ? '<a class="btn btn-ghost" href="#expenses" id="viewImported">View expenses</a>' : ""}
      </div>`;
    $("#commitRun")?.addEventListener("click", async (ev) => {
      ev.target.disabled = true;
      ev.target.textContent = "Importing…";
      try { const r = await api(`/api/imports/${run.id}/commit/`, { method: "POST" }); toast(`Import ${r.status_label.toLowerCase()}`); showRun(r); loadRuns(); }
      catch (err) { toast(err.message, true); ev.target.disabled = false; }
    });
    $("#discardRun")?.addEventListener("click", async () => {
      try { await api(`/api/imports/${run.id}/discard/`, { method: "POST" }); card.hidden = true; loadRuns(); } catch (err) { toast(err.message, true); }
    });
    $("#viewImported")?.addEventListener("click", () => setFilters({ status_scope: "all" }));
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function loadRuns() {
    if (!S.me.can_import) return;
    let d;
    try { d = await api("/api/imports/"); } catch (e) { toast(e.message, true); return; }
    const tbody = $("#runTable tbody");
    tbody.innerHTML = d.results.map((r) => `<tr data-run="${r.id}"><td>${fmtDateTime(r.started_at)}</td><td>${esc(r.file_name)}</td><td>${esc(r.started_by.name)}</td><td>${esc(r.status_label)}</td><td>${r.row_counts.total ?? "—"}</td><td><button class="link-btn">Open</button></td></tr>`).join("")
      || '<tr class="nohover"><td colspan="6" class="muted">No imports yet.</td></tr>';
    $$("tr[data-run]", tbody).forEach((tr) => tr.addEventListener("click", async () => showRun(await api(`/api/imports/${tr.dataset.run}/`))));
  }

  // ------------------------------------------------------------- reports
  async function loadGST() {
    let d;
    try { d = await api("/api/reports/gst-summary/?" + filterQuery()); } catch (e) { $("#gstTable tbody").innerHTML = `<tr class="nohover"><td colspan="11" class="muted">${esc(e.message)}</td></tr>`; return; }
    scopeNote(d.status_rule);
    const itcLabel = Object.fromEntries(S.me.itc_statuses.map((s) => [s.value, s.label]));
    const tot = { n: 0, taxable: 0, cgst: 0, sgst: 0, igst: 0, cess: 0, itc: 0 };
    $("#gstTable tbody").innerHTML = d.rows.map((r) => {
      Object.keys(tot).forEach((k) => (tot[k] += Number(r[k] || 0)));
      return `<tr class="nohover"><td>${esc(r.entity)}</td><td>${esc(r.period)}</td><td>${esc(r.gst_type)}</td><td>${esc(itcLabel[r.itc_status] || r.itc_status)}</td><td class="num">${r.n}</td><td class="num">${inr.format(r.taxable)}</td><td class="num">${inr.format(r.cgst)}</td><td class="num">${inr.format(r.sgst)}</td><td class="num">${inr.format(r.igst)}</td><td class="num">${inr.format(r.cess)}</td><td class="num">${inr.format(r.itc)}</td></tr>`;
    }).join("") || '<tr class="nohover"><td colspan="11" class="muted">No records.</td></tr>';
    $("#gstTable tfoot")?.remove();
    if (d.rows.length) $("#gstTable").insertAdjacentHTML("beforeend", `<tfoot><tr><td colspan="4">Total</td><td class="num">${tot.n}</td>${["taxable", "cgst", "sgst", "igst", "cess", "itc"].map((k) => `<td class="num">${inr.format(tot[k])}</td>`).join("")}</tr></tfoot>`);
  }

  async function loadAudit() {
    let rows;
    try { rows = await api("/api/audit/"); } catch (e) { toast(e.message, true); return; }
    $("#auditTable tbody").innerHTML = rows.map((a) => `<tr class="nohover"><td>${fmtDateTime(a.timestamp)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.record_type)} ${esc(String(a.record_id).slice(0, 8))}</td><td>${esc(a.result)}</td><td class="notes" title="${esc(JSON.stringify(a.changes))}">${esc(JSON.stringify(a.changes)).slice(0, 140)}</td></tr>`).join("");
  }

  // ------------------------------------------------------- notifications
  async function toggleNotifications(e) {
    e.stopPropagation();
    const panel = $("#notifPanel");
    if (!panel.hidden) { panel.hidden = true; return; }
    $("#userMenu").hidden = true;
    const list = await api("/api/notifications/");
    panel.innerHTML = list.map((n) => `<div class="notif${n.is_read ? "" : " unread"}" data-exp="${n.expense || ""}"><div>${esc(n.message)}</div><div class="muted small">${fmtDateTime(n.created_at)}</div></div>`).join("") || '<div class="notif muted">No notifications</div>';
    panel.hidden = false;
    $$(".notif[data-exp]", panel).forEach((el) => el.addEventListener("click", () => { if (el.dataset.exp) { panel.hidden = true; openDetail(el.dataset.exp); } }));
    if (list.some((n) => !n.is_read)) { api("/api/notifications/mark-read/", { method: "POST" }); $("#notifDot").hidden = true; }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

/* Dossier front end — vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) =>
    String(v ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  const fmtMoney = (v, d = 1) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    const sign = n < 0 ? "−" : "";
    const a = Math.abs(n);
    if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(d)}M`;
    if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(0)}K`;
    return `${sign}$${a.toFixed(0)}`;
  };
  const fmtPct = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : `${(Number(v) * 100).toFixed(d)}%`);
  const fmtNum = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US"));
  const fmtDate = (s) => {
    if (!s) return "—";
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(s);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  };
  const fmtTime = (s) => {
    if (!s) return "";
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  };
  const ago = (s) => {
    if (!s) return "";
    const d = new Date(s);
    const secs = Math.max(1, (Date.now() - d.getTime()) / 1000);
    if (secs < 60) return "just now";
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  };
  const rating = (score) => (score >= 16 ? "critical" : score >= 12 ? "high" : score >= 6 ? "moderate" : "low");
  const ratingLabel = (s) => ({ critical: "Critical", high: "High", moderate: "Moderate", low: "Low" }[rating(s)]);
  const titleCase = (s) => String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const stripCites = (s) => String(s || "").replace(/\s*\[\[EV-\d+\]\]/g, "").trim();
  const statusMeta = {
    draft: ["Draft", ""],
    researching: ["Researching", "live"],
    analysed: ["Analysed", "live"],
    in_review: ["In review", "warn"],
    approved: ["Approved", "ok"],
    error: ["Failed", "bad"],
    cancelled: ["Cancelled", "warn"],
  };

  // Optional access token: supplied once as ?token=… then kept for the tab.
  const TOKEN_KEY = "dossier.token";
  (function captureToken() {
    try {
      const url = new URL(window.location.href);
      const token = url.searchParams.get("token");
      if (token) {
        localStorage.setItem(TOKEN_KEY, token);
        url.searchParams.delete("token");
        history.replaceState({}, "", url.pathname + url.search + url.hash);
      }
    } catch (_) {}
  })();
  const authToken = () => {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  };

  const STAGES = [
    ["resolve", "Resolve"],
    ["filings", "Filings"],
    ["financials", "Financials"],
    ["news", "News"],
    ["evidence", "Evidence"],
    ["index", "Index"],
    ["analyse", "Analyse"],
    ["write", "Write"],
    ["verify", "Verify"],
  ];

  const state = {
    deals: [],
    deal: null,
    tab: "overview",
    doc: null,
    risks: [],
    evidence: [],
    sources: [],
    health: null,
    run: null,
    log: [],
    stream: null,
    streamRun: null,
    researchMode: "sources",
    selectedSource: null,
    evQuery: "",
    evKind: "",
    srcQuery: "",
    srcKind: "",
    riskFilter: null,
    exporting: false,
  };

  // ------------------------------------------------------------------ api
  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json" };
    const token = authToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(path, {
      headers,
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const j = await res.json();
        msg = j.detail || j.error || msg;
      } catch (_) {}
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity .3s";
      setTimeout(() => el.remove(), 320);
    }, 4200);
  }

  // --------------------------------------------------------------- routing
  function parseHash() {
    const m = location.hash.match(/^#\/deal\/([^/]+)(?:\/([a-z]+))?/);
    return m ? { dealId: decodeURIComponent(m[1]), tab: m[2] || "overview" } : null;
  }
  function go(dealId, tab = "overview") {
    location.hash = `#/deal/${encodeURIComponent(dealId)}/${tab}`;
  }
  async function route() {
    const r = parseHash();
    if (!r) {
      showWelcome();
      return;
    }
    if (!state.deal || state.deal.id !== r.dealId) {
      await loadDeal(r.dealId);
    }
    state.tab = ["overview", "research", "risk", "memo"].includes(r.tab) ? r.tab : "overview";
    showWorkspace();
    renderTabs();
    renderView();
    renderRail();
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    bindChrome();
    try {
      const [health, deals] = await Promise.all([api("/api/health"), api("/api/deals")]);
      state.health = health;
      state.deals = deals.deals || [];
      renderHealth();
      renderRail();
    } catch (e) {
      toast(`Cannot reach the API: ${e.message}`, "err");
    }
    await route();
  }

  function renderHealth() {
    const h = state.health;
    if (!h) return;
    const llm = h.llm || {};
    const pulse = $(".pulse");
    const text = $("#local-badge-text");
    const note = $("#rail-note");
    if (llm.available) {
      const provider = llm.provider === "openrouter" ? "OpenRouter" : (llm.provider === "ollama" ? "Local model" : (llm.provider || "Model"));
      const model = String(llm.model || "").replace(/:free$/, "");
      const short = model.length > 26 ? model.slice(0, 24) + "…" : model;
      text.textContent = `${provider} · ${short}`;
      pulse.classList.remove("off");
      if (note) note.textContent = llm.local
        ? "Filings stay on this machine. No deal data is sent to a third-party model."
        : `Completions use ${provider}. Sources and the evidence ledger stay local.`;
    } else {
      text.textContent = "Deterministic engine · no model needed";
      pulse.classList.add("off");
      if (note) note.textContent = "Filings stay on this machine. Drafting runs without a model.";
    }
    $("#local-badge").title = `API ${h.version} · ${llm.provider || "deterministic"} · database ${h.database}`;
  }

  function renderRail() {
    const list = $("#deal-list");
    if (!state.deals.length) {
      list.innerHTML = `<div class="tiny" style="color:#7c837e;padding:6px 8px;line-height:1.6">No deals yet. Create one or load the demo.</div>`;
      return;
    }
    list.innerHTML = state.deals
      .map((d) => {
        const [, cls] = statusMeta[d.status] || ["Draft", ""];
        const running = d.running ? "live" : cls;
        const active = state.deal && state.deal.id === d.id ? "active" : "";
        const cnt = d.counts?.risks ? `${d.counts.risks} risks` : d.ticker || "No analysis";
        return `<button class="deal-item ${active}" data-deal="${esc(d.id)}">
          <div class="di-top"><span class="dot ${running}"></span><span class="di-name">${esc(d.company || d.name)}</span></div>
          <div class="di-sub"><span>${esc(d.ticker || "—")}</span><span>·</span><span>${esc(cnt)}</span></div>
        </button>`;
      })
      .join("");
    $$(".deal-item", list).forEach((el) =>
      el.addEventListener("click", () => {
        closeDrawer();
        go(el.dataset.deal, "overview");
      })
    );
  }

  function showWelcome() {
    if (state.stream) stopStream();
    state.deal = null;
    state.doc = null;
    state.risks = [];
    state.evidence = [];
    state.sources = [];
    $("#welcome").classList.remove("hidden");
    $("#workspace").classList.add("hidden");
    renderRail();
  }

  function showWorkspace() {
    $("#welcome").classList.add("hidden");
    $("#workspace").classList.remove("hidden");
  }

  // ------------------------------------------------------------- loading
  async function loadDeal(id, { quiet = false } = {}) {
    try {
      const deal = await api(`/api/deals/${id}`);
      if (state.deal?.id !== id) {
        state.riskFilter = null;
        state.researchMode = "sources";
        state.srcQuery = state.evQuery = state.srcKind = state.evKind = "";
      }
      state.deal = deal;
      const [sources, evidence, risks, doc] = await Promise.all([
        api(`/api/deals/${id}/sources`).catch(() => ({ sources: [] })),
        api(`/api/deals/${id}/evidence`).catch(() => ({ evidence: [] })),
        api(`/api/deals/${id}/risks`).catch(() => ({ risks: [] })),
        api(`/api/deals/${id}/document`).catch(() => null),
      ]);
      state.sources = sources.sources || [];
      state.evidence = evidence.evidence || [];
      state.risks = risks.risks || [];
      state.doc = doc;
      state.selectedSource = state.sources[0]?.id || null;
      state.run = deal.latest_run || null;
      state.log = [];
      if (state.run && !["complete", "failed", "cancelled"].includes(state.run.status)) {
        startStream(state.run.id, { quiet });
      } else if (state.run) {
        loadRecentLog(state.run.id);
      }
      if (!quiet) updateHeader();
    } catch (e) {
      toast(`Could not open deal: ${e.message}`, "err");
      location.hash = "";
    }
  }

  async function loadRecentLog(runId) {
    try {
      const { events } = await api(`/api/runs/${runId}/events?after=0`);
      state.log = (events || []).slice(-120);
      if (state.tab === "overview") renderView();
    } catch (_) {}
  }

  // ------------------------------------------------------------- streaming
  function stopStream() {
    if (state.stream) {
      state.stream.close();
      state.stream = null;
      state.streamRun = null;
    }
  }

  function startStream(runId, { quiet = false } = {}) {
    stopStream();
    state.streamRun = runId;
    state.run = { ...(state.run || {}), id: runId, status: "running", stage: state.run?.stage || "queued" };
    const token = authToken();
    const streamUrl = `/api/runs/${runId}/stream?after=0${token ? `&token=${encodeURIComponent(token)}` : ""}`;
    const es = new EventSource(streamUrl);
    state.stream = es;
    es.addEventListener("log", (ev) => {
      const e = JSON.parse(ev.data);
      state.log.push(e);
      if (state.log.length > 250) state.log = state.log.slice(-250);
      if (e.stage && e.stage !== "queued") state.run.stage = e.stage;
      if (state.tab === "overview") {
        renderPipeline();
        renderLog();
      }
    });
    es.addEventListener("state", async (ev) => {
      const run = JSON.parse(ev.data);
      state.run = { ...run, stats: run.stats || {} };
      stopStream();
      const ok = run.status === "complete";
      toast(
        ok
          ? "Run complete — memorandum and risk matrix refreshed."
          : run.status === "cancelled"
          ? "Run cancelled."
          : `Run failed: ${run.error || "unknown error"}`,
        ok ? "ok" : run.status === "cancelled" ? "warn" : "err"
      );
      const keepTab = state.tab;
      await loadDeal(state.deal.id, { quiet: true });
      await refreshDeals();
      updateHeader();
      if (keepTab === state.tab) renderView();
      renderRail();
    });
    es.onerror = () => {
      if (!state.stream) return;
      stopStream();
      setTimeout(() => pollRun(runId), 1800);
    };
  }

  async function pollRun(runId) {
    try {
      const run = await api(`/api/runs/${runId}`);
      state.run = run;
      if (["complete", "failed", "cancelled"].includes(run.status)) {
        const keepTab = state.tab;
        await loadDeal(state.deal.id, { quiet: true });
        await refreshDeals();
        updateHeader();
        renderView();
        renderTabs();
        toast(run.status === "complete" ? "Run complete." : run.status === "cancelled" ? "Run cancelled." : `Run failed: ${run.error}`, run.status === "complete" ? "ok" : run.status === "cancelled" ? "warn" : "err");
      } else {
        setTimeout(() => pollRun(runId), 1500);
        if (state.tab === "overview") renderPipeline();
      }
    } catch (_) {
      setTimeout(() => pollRun(runId), 3000);
    }
  }

  async function refreshDeals() {
    try {
      const d = await api("/api/deals");
      state.deals = d.deals || [];
    } catch (_) {}
  }

  // ---------------------------------------------------------------- header
  function updateHeader() {
    const d = state.deal;
    if (!d) return;
    $("#company-name").textContent = d.company || d.name;
    $("#company-ticker").textContent = d.ticker || "";
    $("#company-ticker").style.display = d.ticker ? "" : "none";
    const [label, cls] = statusMeta[d.status] || [titleCase(d.status), ""];
    const pill = $("#deal-status");
    pill.textContent = d.running ? "Running" : label;
    pill.className = `pill ${d.running ? "live" : cls}`;
    $("#crumbs").innerHTML = `${esc(d.client || "Advisory engagement")} &nbsp;/&nbsp; <b>${esc(titleCase(d.deal_type || "sell side"))}</b>`;

    const runBtn = $("#run-btn");
    runBtn.classList.toggle("running", !!d.running);
    runBtn.disabled = !!d.running;
    runBtn.innerHTML = d.running
      ? `<span class="spin"></span> Running due diligence`
      : `<span class="run-dot"></span> ${state.doc ? "Re-run due diligence" : "Run due diligence"}`;
    $("#export-btn").disabled = !state.doc;
    $("#tab-sources").textContent = d.counts?.sources ?? "";
    $("#tab-risks").textContent = d.counts?.risks ?? "";
    $("#tab-trust").textContent = d.trust?.score ? `${d.trust.score}` : "";
  }

  function renderTabs() {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === state.tab));
    ["overview", "research", "risk", "memo"].forEach((t) => {
      $(`#view-${t}`).classList.toggle("hidden", t !== state.tab);
    });
  }

  function renderView() {
    if (!state.deal) return;
    updateHeader();
    if (state.tab === "overview") renderOverview();
    if (state.tab === "research") renderResearch();
    if (state.tab === "risk") renderRisk();
    if (state.tab === "memo") renderMemo();
  }

  // -------------------------------------------------------------- overview
  function renderOverview() {
    const d = state.deal;
    const a = d.summary?.analysis || {};
    const trust = d.trust || {};
    const fyLabel = a.fy_label || "Latest FY";
    const cards = [
      ["Revenue", fmtMoney(a.revenue), a.revenue_growth != null ? `${(a.revenue_growth * 100).toFixed(1)}% y/y` : `${fyLabel}`, a.revenue_growth],
      ["Gross margin", fmtPct(a.gross_margin), `Gross profit ${fmtMoney(a.gross_profit)}`, null],
      ["Operating margin", fmtPct(a.operating_margin), `Operating income ${fmtMoney(a.operating_income)}`, null],
      ["Free cash flow", fmtMoney(a.fcf), `Operating cash flow ${fmtMoney(a.ocf)}`, null],
    ];
    const mix = sourceMix();
    const topRisks = [...state.risks].sort((x, y) => y.score - x.score).slice(0, 4);
    const doc = state.doc;

    $("#view-overview").innerHTML = `
      <div class="grid g4">
        ${cards
          .map(
            ([label, value, sub, growth]) => `<div class="card">
              <div class="metric-label">${esc(label)}</div>
              <div class="metric-value">${esc(value)}</div>
              <div class="metric-sub">${growth != null ? `<span class="${growth >= 0 ? "up" : "down"}">${esc(sub)}</span>` : esc(sub)}</div>
            </div>`
          )
          .join("")}
      </div>

      <div class="grid g2" style="margin-top:14px;grid-template-columns:minmax(0,1.65fr) minmax(280px,.85fr)">
        <div class="card" id="pipeline-card"></div>
        <div class="card">
          <div class="kicker">Evidence coverage</div>
          <div style="display:flex;align-items:baseline;gap:8px;margin:10px 0 2px">
            <div class="metric-value" style="font-size:34px">${trust.score ?? "—"}<span style="font-size:16px;color:var(--muted)">/100</span></div>
          </div>
          <div class="small muted">${doc ? `Trust score on the current draft` : "Run the pipeline to generate a draft"}</div>
          <div class="divider"></div>
          <div class="grid" style="grid-template-columns:1fr 1fr;gap:10px">
            <div><div class="metric-label">Ledger</div><div style="font-family:var(--serif);font-size:22px">${fmtNum(d.counts?.evidence)}</div><div class="tiny muted">evidence items</div></div>
            <div><div class="metric-label">Citation coverage</div><div style="font-family:var(--serif);font-size:22px">${trust.citation_coverage != null ? trust.citation_coverage + "%" : "—"}</div><div class="tiny muted">${trust.claims ? `${trust.sourced_claims}/${trust.claims} claims` : "sentences sourced"}</div></div>
          </div>
          <div class="divider"></div>
          <div class="legend">
            <span><em style="background:var(--teal)"></em>cited claim</span>
            <span><em style="background:#c6c0b1"></em>analytical</span>
            <span><em style="background:var(--red)"></em>conflict</span>
          </div>
          <button class="btn btn-ghost btn-sm" style="margin-top:12px;width:100%" data-goto="memo">${doc ? "Read the memorandum →" : "No draft yet"}</button>
        </div>
      </div>

      <div class="section-title"><h2>The risk picture</h2><button class="link" data-goto="risk">Open matrix →</button></div>
      <div class="grid g2" style="grid-template-columns:minmax(0,1.15fr) minmax(280px,.85fr)">
        <div class="card flush">
          ${
            topRisks.length
              ? topRisks
                  .map(
                    (r) => `<button class="risk-row" data-risk="${esc(r.id)}">
                        <div class="score-badge ${rating(r.score)}"><b>${r.score}</b><span>${rating(r.score)}</span></div>
                        <div class="risk-main">
                          <h4>${esc(r.title)}</h4>
                          <p>${esc(stripCites(r.description))}</p>
                          <div class="risk-meta"><span class="cat">${esc(r.category)}</span><span class="chip">S${r.severity} × L${r.likelihood}</span><span class="chip">${(r.evidence_ids || []).length} sources</span></div>
                        </div>
                      </button>`
                  )
                  .join("")
              : `<div class="empty">No risk matrix yet. Run due diligence to build one.</div>`
          }
        </div>
        <div style="display:flex;flex-direction:column;gap:14px">
          <div class="card">
            <div class="kicker">Research base</div>
            <div style="display:flex;align-items:baseline;gap:8px;margin:9px 0 4px">
              <div class="metric-value" style="font-size:30px">${fmtNum(d.counts?.sources)}</div>
              <div class="small muted">sources · ${fmtNum(d.counts?.chunks)} passages</div>
            </div>
            <div class="source-mix">${mix.bars}</div>
            <div class="legend">${mix.legend}</div>
          </div>
          <div class="card">
            <div class="kicker">Transaction</div>
            <dl class="dl" style="display:grid;grid-template-columns:96px 1fr;gap:6px 10px;margin:10px 0 0;font-size:12px">
              <dt class="muted">Type</dt><dd style="margin:0">${esc(titleCase(d.deal_type || "sell side"))}</dd>
              <dt class="muted">Client</dt><dd style="margin:0">${esc(d.client || "—")}</dd>
              <dt class="muted">Entity</dt><dd style="margin:0">${esc(d.company || d.name)}</dd>
              <dt class="muted">CIK</dt><dd style="margin:0" class="mono">${esc(d.cik || "—")}</dd>
              <dt class="muted">Sector</dt><dd style="margin:0">${esc(d.sector || "—")}</dd>
            </dl>
          </div>
        </div>
      </div>

      <div class="section-title"><h2>What the agents produced</h2><span class="tiny muted">${doc ? `Draft v${doc.version} · ${fmtDate(doc.updated_at)} · ${doc.stats?.words || 0} words` : ""}</span></div>
      <div class="grid g3">
        <div class="card">
          <div class="kicker">01 · Research</div>
          <p class="small" style="margin:9px 0 0;line-height:1.6">Primary filings and news are read and converted into <b>${fmtNum(d.counts?.evidence)}</b> evidence items, each with a locator and a confidence score.</p>
          <button class="btn btn-ghost btn-sm" style="margin-top:12px" data-goto="research">Inspect the ledger</button>
        </div>
        <div class="card">
          <div class="kicker">02 · Analysis</div>
          <p class="small" style="margin:9px 0 0;line-height:1.6"><b>${fmtNum(d.counts?.risks)}</b> risks scored severity × likelihood, including <b>${fmtNum(d.counts?.critical_risks)}</b> critical, each mapped to the evidence that triggered it.</p>
          <button class="btn btn-ghost btn-sm" style="margin-top:12px" data-goto="risk">Open the matrix</button>
        </div>
        <div class="card">
          <div class="kicker">03 · Draft</div>
          <p class="small" style="margin:9px 0 0;line-height:1.6">${doc ? `A ${doc.stats?.sections || 13}-section memorandum with <b>${trust.citation_coverage ?? "—"}%</b> citation coverage and <b>${(trust.numeric_conflicts || []).length}</b> numeric conflicts.` : "No memorandum has been drafted yet."}</p>
          <button class="btn btn-ghost btn-sm" style="margin-top:12px" data-goto="memo">${doc ? "Read the draft" : "Generate a draft"}</button>
        </div>
      </div>
    `;
    renderPipeline();
    $$("[data-goto]", $("#view-overview")).forEach((b) => b.addEventListener("click", () => go(state.deal.id, b.dataset.goto)));
    $$("[data-risk]", $("#view-overview")).forEach((b) => b.addEventListener("click", () => showRisk(b.dataset.risk)));
  }

  function sourceMix() {
    const colors = { sec: "#0f766e", news: "#d9a441", web: "#8b9dc3", data: "#708a7d", other: "#b6b0a2" };
    const counts = {};
    state.sources.forEach((s) => {
      const k = s.kind === "sec_filing" ? "sec" : s.kind === "news" ? "news" : s.kind || "other";
      counts[k] = (counts[k] || 0) + 1;
    });
    const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
    const order = ["sec", "news", "web", "data", "other"].filter((k) => counts[k]);
    const bars = order
      .map((k) => `<i style="width:${(counts[k] / total) * 100}%;background:${colors[k] || "#b6b0a2"}" title="${counts[k]} ${k}"></i>`)
      .join("");
    const legend = order
      .map((k) => `<span><em style="background:${colors[k] || "#b6b0a2"}"></em>${k === "sec" ? "Filings" : titleCase(k)} · ${counts[k]}</span>`)
      .join("");
    return { bars, legend };
  }

  function renderPipeline() {
    const host = $("#pipeline-card");
    if (!host) return;
    const run = state.run;
    const running = run && !["complete", "failed", "cancelled"].includes(run.status);
    const idx = run ? STAGES.findIndex(([k]) => k === run.stage) : -1;
    const done = run?.status === "complete";
    const steps = STAGES.map(([key, label], i) => {
      const cls = done || (idx > i && idx !== -1) ? "done" : running && (i === idx || (run.stage === "queued" && i === 0)) ? "live" : "";
      return `<div class="pipe-step ${cls}"><div class="pipe-bar"><i></i></div><div class="pipe-label">${label}</div></div>`;
    }).join("");
    const statusText = !run
      ? "Not started"
      : run.status === "complete"
      ? `Complete · ${run.stats?.words ? `${run.stats.words.toLocaleString()} words` : ""}${run.stats?.trust ? ` · trust ${run.stats.trust}` : ""}`
      : run.status === "failed"
      ? `Failed · ${esc(run.error || "")}`
      : run.status === "cancelled"
      ? "Cancelled"
      : `${STAGES.find(([k]) => k === run.stage)?.[1] || "Starting"}…`;
    const pct = done ? 100 : run?.progress != null ? Math.round(run.progress * 100) : 0;
    host.innerHTML = `
      <div class="run-head">
        <h3>Due diligence pipeline</h3>
        <span class="pill ${done ? "ok" : running ? "live" : run?.status === "failed" ? "bad" : ""}">${running ? '<span class="spin" style="width:9px;height:9px;border-width:1.5px;border-top-color:#0f766e"></span> ' : ""}${esc(statusText)}</span>
        <span class="spacer"></span>
        <span class="tiny muted">${run?.started_at ? `Started ${ago(run.started_at)}` : "Three agents · research → analyse → write"}</span>
      </div>
      <div class="pipe" style="margin-top:16px">${steps}</div>
      <div class="meter" style="margin:14px 0 14px"><i style="width:${pct}%"></i></div>
      <div class="log" id="run-log">${logHtml()}</div>
      <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
        <button class="btn btn-ghost btn-sm" id="pipe-run">${running ? "Running…" : done ? "Re-run" : "Start run"}</button>
        ${running ? '<button class="btn btn-ghost btn-sm" id="pipe-cancel">Cancel</button>' : ""}
        <span class="tiny muted">${running ? "You can leave this page — the run continues in the background." : (state.health?.llm?.available ? `${state.health.llm.provider === "openrouter" ? "OpenRouter" : "Model"} polish · verified against the ledger` : "Deterministic engine · local model optional")}</span>
      </div>`;
    const log = $("#run-log");
    if (log) log.scrollTop = log.scrollHeight;
    const b = $("#pipe-run");
    if (b) {
      b.disabled = running;
      b.addEventListener("click", triggerRun);
    }
    const cancel = $("#pipe-cancel");
    if (cancel) {
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try {
          await api(`/api/runs/${state.run.id}/cancel`, { method: "POST", body: {} });
          toast("Cancellation requested.", "warn");
        } catch (e) {
          toast(`Could not cancel: ${e.message}`, "err");
        }
      });
    }
  }

  function logHtml() {
    if (!state.log.length) return `<div><span class="m" style="color:#8b948f">No run log yet.</span></div>`;
    return state.log
      .slice(-80)
      .map(
        (e) => `<div><span class="t">${esc(fmtTime(e.ts))}</span><span class="s">${esc(e.stage || "")}</span><span class="m ${e.level === "error" ? "err" : ""}">${esc(e.message)}</span></div>`
      )
      .join("");
  }

  function renderLog() {
    const log = $("#run-log");
    if (!log) return;
    log.innerHTML = logHtml();
    log.scrollTop = log.scrollHeight;
  }

  // -------------------------------------------------------------- research
  function renderResearch() {
    const host = $("#view-research");
    const modes = `<div class="tabs-sm">
      <button class="${state.researchMode === "sources" ? "active" : ""}" data-mode="sources">Sources</button>
      <button class="${state.researchMode === "ledger" ? "active" : ""}" data-mode="ledger">Evidence ledger</button>
    </div>`;
    const kinds = [...new Set(state.sources.map((s) => s.kind))].sort();
    const kindOpts = `<option value="">All source types</option>${kinds.map((k) => `<option value="${esc(k)}">${esc(titleCase(k))}</option>`).join("")}`;
    const evKinds = [...new Set(state.evidence.map((e) => e.kind))].sort();
    const evOpts = `<option value="">All evidence types</option>${evKinds.map((k) => `<option value="${esc(k)}">${esc(titleCase(k))}</option>`).join("")}`;

    host.innerHTML = `
      <div class="toolbar">
        ${modes}
        <input class="input search" id="research-q" placeholder="${state.researchMode === "sources" ? "Search titles, forms, publishers…" : "Search labels, values, quotes…"}" value="${esc(state.researchMode === "sources" ? state.srcQuery : state.evQuery)}">
        <select class="input select" id="research-kind">${state.researchMode === "sources" ? kindOpts : evOpts}</select>
        <span class="tiny muted">${state.researchMode === "sources" ? `${state.sources.length} sources` : `${state.evidence.length} evidence items`}</span>
      </div>
      <div class="research-grid">
        <div class="card flush">
          <div class="card-head"><h3>${state.researchMode === "sources" ? "Source documents" : "Evidence ledger"}</h3><span class="sub" id="research-count"></span></div>
          <div class="${state.researchMode === "sources" ? "source-list" : "ev-list"}" id="research-list"></div>
        </div>
        <div class="card flush" id="research-detail"></div>
      </div>`;

    $$("[data-mode]").forEach((b) =>
      b.addEventListener("click", () => {
        state.researchMode = b.dataset.mode;
        renderResearch();
      })
    );
    const q = $("#research-q");
    const kind = $("#research-kind");
    q.addEventListener("input", () => {
      if (state.researchMode === "sources") state.srcQuery = q.value;
      else state.evQuery = q.value;
      renderResearchList();
    });
    kind.addEventListener("change", () => {
      if (state.researchMode === "sources") state.srcKind = kind.value;
      else state.evKind = kind.value;
      renderResearchList();
    });
    renderResearchList();
    renderResearchDetail();
  }

  function filteredSources() {
    const q = state.srcQuery.toLowerCase().trim();
    return state.sources.filter((s) => {
      if (state.srcKind && s.kind !== state.srcKind) return false;
      if (!q) return true;
      return `${s.title} ${s.publisher || ""} ${s.form || ""} ${s.locator || ""}`.toLowerCase().includes(q);
    });
  }
  function filteredEvidence() {
    const q = state.evQuery.toLowerCase().trim();
    return state.evidence.filter((e) => {
      if (state.evKind && e.kind !== state.evKind) return false;
      if (!q) return true;
      return `${e.id} ${e.label} ${e.value || ""} ${e.quote || ""} ${e.period || ""}`.toLowerCase().includes(q);
    });
  }

  function renderResearchList() {
    const list = $("#research-list");
    const count = $("#research-count");
    if (!list) return;
    if (state.researchMode === "sources") {
      const items = filteredSources();
      count.textContent = `${items.length} shown`;
      list.innerHTML = items.length
        ? items
            .map((s) => {
              const active = state.selectedSource === s.id ? "active" : "";
              return `<button class="source-item ${active}" data-source="${esc(s.id)}">
                <div class="si-top"><span class="badge ${s.form ? "form" : ""}">${esc(s.kind === "sec_filing" ? s.form || "filing" : s.kind)}</span><b>${esc(s.title)}</b></div>
                <div class="si-sub">${esc(s.publisher || "")}${s.published_at ? ` · ${esc(fmtDate(s.published_at))}` : ""}</div>
              </button>`;
            })
            .join("")
        : `<div class="empty">No sources match.</div>`;
      $$("[data-source]", list).forEach((b) =>
        b.addEventListener("click", () => {
          state.selectedSource = b.dataset.source;
          renderResearchList();
          renderResearchDetail();
        })
      );
    } else {
      const items = filteredEvidence();
      count.textContent = `${items.length} shown`;
      list.innerHTML = items.length
        ? items
            .map(
              (e) => `<button class="ev-row" data-ev="${esc(e.id)}">
                <span class="ev-id">${esc(e.id)}</span>
                <span class="ev-main"><b>${esc(e.label)}</b><span class="q">${esc(e.quote || e.value || "")}</span>
                  <span class="risk-meta" style="margin-top:4px"><span class="chip">${esc(e.kind)}</span>${e.period ? `<span class="chip">${esc(e.period)}</span>` : ""}</span>
                </span>
                <span class="ev-side"><span class="tiny muted">${esc((e.source || {}).form || (e.source || {}).title || "")}</span></span>
              </button>`
            )
            .join("")
        : `<div class="empty">No evidence matches.</div>`;
      $$("[data-ev]", list).forEach((b) => b.addEventListener("click", () => showEvidence(b.dataset.ev)));
    }
  }

  function renderResearchDetail() {
    const host = $("#research-detail");
    if (!host) return;
    if (state.researchMode === "ledger") {
      const byKind = {};
      state.evidence.forEach((e) => (byKind[e.kind] = (byKind[e.kind] || 0) + 1));
      const rows = Object.entries(byKind)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 12);
      const maxKind = rows.length ? Math.max(...rows.map(([, n]) => n)) : 1;
      host.innerHTML = `<div class="card-head"><h3>Ledger composition</h3><span class="sub">${state.evidence.length} items</span></div>
        <div class="card-body">
          <p class="small muted" style="margin-top:0;line-height:1.6">Each item records a fact, a quote or a derived metric with its locator. Click any row to inspect the source passage and the risks it supports.</p>
          ${rows
            .map(
              ([k, n]) => `<div style="display:flex;align-items:center;gap:10px;margin:9px 0">
                <span style="flex:0 0 110px;font-size:11px" class="muted">${esc(k)}</span>
                <span class="meter" style="flex:1"><i style="width:${(n / maxKind) * 100}%"></i></span>
                <span class="tiny mono" style="flex:0 0 30px;text-align:right">${n}</span>
              </div>`
            )
            .join("")}
          <div class="divider"></div>
          <div class="kicker" style="margin-bottom:6px">Traceability</div>
          <p class="tiny muted" style="line-height:1.6;margin:0">Evidence IDs are stable within a run. The memorandum cites them inline; the workbook lists every item with its URL and locator.</p>
        </div>`;
      return;
    }
    const src = state.sources.find((s) => s.id === state.selectedSource);
    if (!src) {
      host.innerHTML = `<div class="empty">Select a source to read it.</div>`;
      return;
    }
    host.innerHTML = `
      <div class="card-head">
        <h3>${esc(src.kind === "sec_filing" ? src.form || "Filing" : titleCase(src.kind))}</h3>
        <span class="sub">${esc(src.id)}</span>
      </div>
      <div class="card-body">
        <h3 style="font-family:var(--serif);font-size:20px;margin:0 0 6px;line-height:1.3">${esc(src.title)}</h3>
        <div class="tiny muted">${esc(src.publisher || "")}${src.published_at ? ` · ${esc(fmtDate(src.published_at))}` : ""}${src.accession ? ` · ${esc(src.accession)}` : ""}</div>
        ${src.url ? `<div style="margin-top:10px"><a class="small" href="${esc(src.url)}" target="_blank" rel="noopener">Open primary source ↗</a></div>` : ""}
        ${src.locator ? `<div class="tiny muted" style="margin-top:4px">Locator: ${esc(src.locator)}</div>` : ""}
        <div class="divider"></div>
        <div class="kicker" style="margin-bottom:8px">Extracted text</div>
        <div id="source-excerpt" class="small" style="white-space:pre-wrap;line-height:1.65;max-height:520px;overflow:auto;color:var(--ink-2)">Loading…</div>
      </div>`;
        api(`/api/deals/${state.deal.id}/sources/${src.id}?excerpt=6000`)
      .then((full) => {
        const el = $("#source-excerpt");
        if (!el) return;
        el.textContent = full.excerpt || "No extracted text stored for this source.";
      })
      .catch((e) => {
        const el = $("#source-excerpt");
        if (el) el.textContent = `Could not load source: ${e.message}`;
      });
  }

  // ------------------------------------------------------------------ risk
  function renderRisk() {
    const host = $("#view-risk");
    const risks = state.risks;
    if (!risks.length) {
      host.innerHTML = `<div class="card"><div class="empty">No risk matrix yet.<br><button class="btn btn-ghost btn-sm" style="margin-top:12px" data-run>Run due diligence</button></div></div>`;
      const b = $("[data-run]", host);
      if (b) b.addEventListener("click", triggerRun);
      return;
    }
    const counts = { critical: 0, high: 0, moderate: 0, low: 0 };
    risks.forEach((r) => counts[rating(r.score)]++);
    const cells = [];
    for (let s = 5; s >= 1; s--) {
      for (let l = 1; l <= 5; l++) {
        const cellRisks = risks.filter((r) => r.severity === s && r.likelihood === l);
        const score = s * l;
        const bg = score >= 16 ? "#f6c9c3" : score >= 12 ? "#f6ddb4" : score >= 6 ? "#e7ece7" : "#f1efe7";
        const sel = state.riskFilter && state.riskFilter.s === s && state.riskFilter.l === l ? "sel" : "";
        cells.push(`<div class="hcell ${sel}" data-s="${s}" data-l="${l}" style="background:${bg}" title="Severity ${s} × Likelihood ${l} = ${score}">
          ${cellRisks.map((r) => `<span class="rc ${r.score >= 16 ? "crit" : ""}" title="${esc(r.title)}">${esc(r.id.replace("R-", ""))}</span>`).join("")}
        </div>`);
      }
    }
    const shown = state.riskFilter ? risks.filter((r) => r.severity === state.riskFilter.s && r.likelihood === state.riskFilter.l) : risks;
    host.innerHTML = `
      <div class="grid" style="grid-template-columns:minmax(0,1.5fr) minmax(220px,.6fr)">
        <div class="card">
          <div class="card-head" style="padding:0 0 12px;border-bottom:0"><h3>Severity × likelihood</h3><span class="sub">${risks.length} risks · click a cell to filter</span></div>
          <div class="heat-wrap">
            <div class="heat">
              <div class="vaxis" style="grid-row:1;grid-column:1">S5</div><div class="vaxis" style="grid-row:2;grid-column:1">S4</div><div class="vaxis" style="grid-row:3;grid-column:1">S3</div><div class="vaxis" style="grid-row:4;grid-column:1">S2</div><div class="vaxis" style="grid-row:5;grid-column:1">S1</div>
              ${cells.join("")}
              <div class="haxis" style="grid-row:6;grid-column:2">L1</div><div class="haxis" style="grid-row:6;grid-column:3">L2</div><div class="haxis" style="grid-row:6;grid-column:4">L3</div><div class="haxis" style="grid-row:6;grid-column:5">L4</div><div class="haxis" style="grid-row:6;grid-column:6">L5</div>
              <div></div>
            </div>
            <div class="heat-legend">
              <div class="legend-item"><span class="sw" style="background:#f6c9c3"></span>Critical 16–25 · ${counts.critical}</div>
              <div class="legend-item"><span class="sw" style="background:#f6ddb4"></span>High 12–15 · ${counts.high}</div>
              <div class="legend-item"><span class="sw" style="background:#e7ece7"></span>Moderate 6–11 · ${counts.moderate}</div>
              <div class="legend-item"><span class="sw" style="background:#f1efe7"></span>Low 1–5 · ${counts.low}</div>
              <div class="divider" style="margin:6px 0"></div>
              <div class="tiny muted" style="line-height:1.5">Vertical: severity.<br>Horizontal: likelihood.<br>Score = S × L.</div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="kicker">Matrix summary</div>
          <div style="display:flex;align-items:baseline;gap:8px;margin:8px 0 2px"><div class="metric-value" style="font-size:34px">${risks.length}</div><span class="small muted">risks</span></div>
          <div class="small muted">Average score ${(risks.reduce((a, r) => a + r.score, 0) / risks.length).toFixed(1)} / 25</div>
          <div class="divider"></div>
          ${Object.entries(counts)
            .map(
              ([k, n]) => `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0"><span class="small"><span class="dot ${k === "critical" ? "bad" : k === "high" ? "warn" : "ok"}" style="display:inline-block;margin-right:6px"></span>${k[0].toUpperCase() + k.slice(1)}</span><b class="mono">${n}</b></div>`
            )
            .join("")}
          <div class="divider"></div>
          <p class="tiny muted" style="line-height:1.55;margin:0">Scores are an analyst aid, not a valuation adjustment. Edit severity or likelihood in a risk to re-score it.</p>
        </div>
      </div>

      <div class="section-title"><h2>Risk register</h2><span class="tiny muted">${state.riskFilter ? `Filtered to S${state.riskFilter.s} × L${state.riskFilter.l} · <button class="link" id="clear-filter">clear</button>` : "All categories"}</span></div>
      <div class="card flush" id="risk-list">
        ${shown.length ? shown.map(riskRow).join("") : `<div class="empty">No risks in this cell.</div>`}
      </div>`;
    $$(".hcell").forEach((c) =>
      c.addEventListener("click", () => {
        state.riskFilter = state.riskFilter && state.riskFilter.s == c.dataset.s && state.riskFilter.l == c.dataset.l ? null : { s: Number(c.dataset.s), l: Number(c.dataset.l) };
        renderRisk();
      })
    );
    const clear = $("#clear-filter");
    if (clear)
      clear.addEventListener("click", () => {
        state.riskFilter = null;
        renderRisk();
      });
    $$("[data-risk]", host).forEach((b) => b.addEventListener("click", () => showRisk(b.dataset.risk)));
  }

  function riskRow(r) {
    return `<button class="risk-row" data-risk="${esc(r.id)}">
      <div class="score-badge ${rating(r.score)}"><b>${r.score}</b><span>${rating(r.score)}</span></div>
      <div class="risk-main">
        <h4>${esc(r.id)} · ${esc(r.title)}</h4>
        <p>${esc(stripCites(r.description))}</p>
        <div class="risk-meta">
          <span class="cat">${esc(r.category)}</span>
          <span class="chip">S${r.severity} × L${r.likelihood}</span>
          <span class="chip">${(r.evidence_ids || []).length} evidence</span>
          ${r.status !== "open" ? `<span class="chip">${esc(titleCase(r.status))}</span>` : ""}
          ${r.exposure ? `<span class="chip">${esc(r.exposure)}</span>` : ""}
        </div>
      </div>
      <span class="tiny muted nowrap">View →</span>
    </button>`;
  }

  // ------------------------------------------------------------------ memo
  function renderMemo() {
    const host = $("#view-memo");
    const doc = state.doc;
    if (!doc) {
      host.innerHTML = `<div class="card"><div class="empty">No memorandum has been drafted yet.<br><button class="btn btn-ghost btn-sm" style="margin-top:12px" data-run>Run due diligence to generate one</button></div></div>`;
      const b = $("[data-run]", host);
      if (b) b.addEventListener("click", triggerRun);
      return;
    }
    const trust = doc.trust || {};
    const conflicts = (trust.numeric_conflicts || []).length;
    const broken = (trust.broken_citations || []).length;
    const unsupported = (trust.unsupported || []).length;
    const score = trust.score ?? 0;
    const gaugeColor = score >= 90 ? "#0f766e" : score >= 75 ? "#a95c09" : "#b42318";

    const evOrder = {};
    function evNumber(id) {
      if (!(id in evOrder)) evOrder[id] = Object.keys(evOrder).length + 1;
      return evOrder[id];
    }

    const body = (doc.sections || []).map((sec) => `<h2 id="sec-${esc(sec.key)}">${esc(sec.title)}</h2>${(sec.blocks || []).map((b) => renderBlock(b, evNumber)).join("")}`).join("");

    host.innerHTML = `
      <div class="memo-layout">
        <article class="paper">
          <div class="paper-kicker">Confidential information memorandum · draft</div>
          <h1>${esc(doc.company)}</h1>
          <div class="paper-sub">${esc(doc.subtitle || "")}</div>
          <div class="paper-meta">
            <span><b>Prepared for</b> ${esc(doc.client || "Client")}</span>
            <span><b>Prepared by</b> Dossier</span>
            <span><b>Date</b> ${esc(fmtDate(doc.date))}</span>
            <span><b>Status</b> ${esc(titleCase(doc.status))}</span>
            <span><b>Draft v${esc(doc.version)}</b></span>
          </div>
          ${body}
        </article>
        <aside class="memo-side">
          <div class="card">
            <div class="kicker">Verification</div>
            <div class="trust-gauge" style="margin-top:12px">
              <div class="gauge" style="background:conic-gradient(${gaugeColor} ${score * 3.6}deg,#e9e5da 0)">
                <span style="color:${gaugeColor}">${score}</span>
              </div>
              <div class="trust-copy"><b>${score >= 90 ? "Ready for review" : score >= 75 ? "Review recommended" : "Needs work"}</b><span>${esc(trust.citation_coverage ?? "—")}% of factual sentences carry a citation.</span></div>
            </div>
            <div class="divider"></div>
            <ul class="check-list">
              <li><span>Numeric conflicts</span><span class="v ${conflicts ? "bad" : "ok"}">${conflicts}</span></li>
              <li><span>Broken citations</span><span class="v ${broken ? "bad" : "ok"}">${broken}</span></li>
              <li><span>Unsupported paragraphs</span><span class="v ${unsupported > 2 ? "bad" : ""}">${unsupported}</span></li>
              <li><span>Numeric checks</span><span class="v">${trust.numeric_checks ?? "—"}</span></li>
              <li><span>Evidence ledger</span><span class="v">${trust.ledger_size ?? "—"}</span></li>
              <li><span>Words / pages</span><span class="v">${doc.stats?.words ?? "—"} / ~${doc.stats?.pages_estimate ?? "—"}</span></li>
            </ul>
            ${unsupported ? `<details style="margin-top:10px"><summary class="tiny muted" style="cursor:pointer">Review ${unsupported} unsupported paragraph${unsupported === 1 ? "" : "s"}</summary><div class="tiny muted" style="margin-top:6px;line-height:1.55">${(trust.unsupported || []).map((u) => `<div style="margin-bottom:6px">· ${esc(u.text)}</div>`).join("")}</div></details>` : ""}
          </div>
          <div class="card">
            <div class="kicker">Review</div>
            <p class="approve-note" style="margin-top:10px">${doc.status === "approved" ? "Approved. Further edits require a re-run or manual revision." : "Approve to lock the current draft and record the reviewer."}</p>
            <button class="btn btn-primary btn-block" id="approve-btn" ${doc.status === "approved" ? "disabled" : ""}>${doc.status === "approved" ? "Approved" : "Approve draft"}</button>
            <button class="btn btn-ghost btn-block" style="margin-top:7px" id="memo-export">Export memorandum</button>
            <div class="divider"></div>
            <div class="kicker" style="margin-bottom:6px">Evidence register</div>
            <p class="tiny muted" style="margin:0 0 8px;line-height:1.55">${Object.keys(evOrder).length} evidence items are cited in this draft. Click a superscript to inspect it.</p>
            <button class="btn btn-ghost btn-sm btn-block" id="open-ledger">Open full ledger</button>
          </div>
          <div class="card">
            <div class="kicker">Contents</div>
            <ol style="margin:8px 0 0;padding-left:18px;font-size:12px;line-height:1.9;color:var(--ink-2)">
              ${(doc.sections || []).map((s) => `<li><button class="toc-link" data-sec="${esc(s.key)}">${esc(s.title)}</button></li>`).join("")}
            </ol>
          </div>
        </aside>
      </div>`;

    $$(".cite", host).forEach((b) =>
      b.addEventListener("click", (e) => {
        e.preventDefault();
        showEvidence(b.dataset.ev);
      })
    );
    const ap = $("#approve-btn");
    if (ap) ap.addEventListener("click", approveDoc);
    const ex = $("#memo-export");
    if (ex) ex.addEventListener("click", () => ($("#export-menu").classList.contains("hidden") ? $("#export-menu").classList.remove("hidden") : null));
    const ol = $("#open-ledger");
    if (ol)
      ol.addEventListener("click", () => {
        state.researchMode = "ledger";
        go(state.deal.id, "research");
      });
    $$("[data-sec]", host).forEach((b) =>
      b.addEventListener("click", () => {
        const el = document.getElementById(`sec-${b.dataset.sec}`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      })
    );
  }

  function rich(text, evNumber) {
    let out = esc(text || "");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    out = out.replace(/\[\[(EV-\d+)\]\]/g, (_, id) => {
      const n = evNumber(id);
      const ev = state.evidence.find((e) => e.id === id);
      const tip = ev ? `${id} · ${ev.label}` : id;
      return `<button class="cite" data-ev="${esc(id)}" title="${esc(tip)}">${n}</button>`;
    });
    return out;
  }

  function renderBlock(b, evNumber) {
    const t = b.type;
    if (t === "p") return `<p>${rich(b.text, evNumber)}</p>`;
    if (t === "ul") {
      const title = b.title ? `<div class="table-title">${esc(b.title)}</div>` : "";
      return `${title}<ul>${(b.items || []).map((i) => `<li>${rich(i, evNumber)}</li>`).join("")}</ul>`;
    }
    if (t === "callout") return `<div class="callout ${b.tone === "warn" ? "warn" : ""}"><b>${esc(b.title)}</b><p>${rich(b.text, evNumber)}</p></div>`;
    if (t === "quote") return `<blockquote>“${esc(b.text || "")}”</blockquote>`;
    if (t === "table") {
      const cols = b.columns || [];
      return `<div class="table-title">${esc(b.title || "")}</div>
        <table><thead><tr>${cols.map((c, i) => `<th style="${i ? "text-align:right" : ""}">${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${(b.rows || []).map((r) => `<tr><td>${esc(r.label)}</td>${(r.values || []).map((v) => `<td style="text-align:right">${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>
        ${b.note ? `<div class="table-note">${esc(b.note)}</div>` : ""}`;
    }
    if (t === "risk") {
      return `<div class="riskblock ${b.score >= 16 ? "critical" : ""}">
        <div class="rb-top"><b>${esc(b.risk_id)} · ${esc(b.title)}</b><span class="rb-score">${esc(b.label)} · S${b.severity} × L${b.likelihood} = ${b.score}/25</span></div>
        <p>${rich(b.text, evNumber)}</p>
        ${b.mitigant ? `<div class="rb-mit">Mitigant: ${esc(b.mitigant)}</div>` : ""}
      </div>`;
    }
    if (t === "chart") return renderChart(b);
    return "";
  }

  function renderChart(b) {
    const series = b.series || [];
    if (!series.length) return "";
    const bar = series.find((s) => s.kind !== "line") || series[0];
    const line = series.find((s) => s.kind === "line");
    const labels = bar.labels || [];
    const w = 640, h = 210, padL = 52, padR = 44, padT = 14, padB = 30;
    const iw = w - padL - padR, ih = h - padT - padB;
    const vals = (bar.values || []).map((v) => Number(v) || 0);
    const max = Math.max(...vals, 1) * 1.15;
    const n = vals.length || 1;
    const slot = iw / n;
    const bars = vals
      .map((v, i) => {
        const bh = (v / max) * ih;
        const x = padL + i * slot + slot * 0.2;
        const y = padT + ih - bh;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(slot * 0.6).toFixed(1)}" height="${bh.toFixed(1)}" fill="#dcebe7" rx="2"/>
          <text x="${(x + slot * 0.3).toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle" font-size="9.5" fill="#3f4447" font-family="ui-monospace,monospace">${fmtMoney(v, 0)}</text>`;
      })
      .join("");
    const lvals = line ? (line.values || []).map((v) => Number(v) || 0) : [];
    const lmax = Math.max(...lvals, 0.05) * 1.4;
    const pts = lvals.map((v, i) => [padL + i * slot + slot / 2, padT + ih - (v / lmax) * ih * 0.75 - ih * 0.1]);
    const path = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const dots = pts
      .map((p, i) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.6" fill="#0f766e"/><text x="${p[0].toFixed(1)}" y="${(p[1] - 7).toFixed(1)}" text-anchor="middle" font-size="8.5" fill="#0f766e" font-family="ui-monospace,monospace">${fmtPct(lvals[i])}</text>`)
      .join("");
    const xlabels = labels
      .map((l, i) => `<text x="${(padL + i * slot + slot / 2).toFixed(1)}" y="${h - 8}" text-anchor="middle" font-size="9" fill="#767369">${esc(l)}</text>`)
      .join("");
    const grid = [0, 0.5, 1]
      .map((f) => {
        const y = padT + ih * f;
        return `<line x1="${padL}" x2="${w - padR}" y1="${y}" y2="${y}" stroke="#eeeae0" stroke-width="1"/>`;
      })
      .join("");
    return `<div class="chart-card">
      <div class="ct">${esc(b.title || "")}</div>
      <svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="${esc(b.title || "Financial chart")}">
        ${grid}${bars}${line ? `<path d="${path}" fill="none" stroke="#0f766e" stroke-width="1.8"/>${dots}` : ""}${xlabels}
      </svg>
      <div class="legend" style="margin-top:4px"><span><em style="background:#dcebe7"></em>Revenue</span>${line ? `<span><em style="background:#0f766e"></em>${esc(line.name)}</span>` : ""}</div>
    </div>`;
  }

  // --------------------------------------------------------------- drawer
  function openDrawer(kicker, html) {
    $("#drawer-kicker").textContent = kicker;
    $("#drawer-body").innerHTML = html;
    $("#drawer").classList.remove("hidden");
    $("#scrim").classList.remove("hidden");
    $$("[data-ev]", $("#drawer-body")).forEach((b) =>
      b.addEventListener("click", () => showEvidence(b.dataset.ev))
    );
  }
  function closeDrawer() {
    $("#drawer").classList.add("hidden");
    $("#scrim").classList.add("hidden");
  }

  async function showEvidence(id) {
    openDrawer(`Evidence · ${id}`, `<div class="skeleton" style="height:14px;width:60%;margin:10px 0"></div><div class="skeleton" style="height:70px"></div>`);
    try {
      const e = await api(`/api/deals/${state.deal.id}/evidence/${id}`);
      const src = e.source || {};
      const tags = (e.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("");
      const risks = (e.risks || []).map((r) => `<button class="chip" data-risk="${esc(r.id)}">${esc(r.id)} · ${esc(r.title)}</button>`).join("");
      openDrawer(
        `Evidence · ${e.id}`,
        `<h3>${esc(e.label)}</h3>
        <div class="tiny muted">${esc(e.kind)}${e.period ? ` · ${esc(e.period)}` : ""} · confidence ${(Number(e.confidence || 0) * 100).toFixed(0)}%</div>
        ${e.value ? `<div style="font-size:15px;margin:12px 0 0;font-weight:600">${esc(e.value)}${e.unit ? ` ${esc(e.unit)}` : ""}</div>` : ""}
        ${e.quote ? `<blockquote>${esc(e.quote)}</blockquote>` : ""}
        <dl class="dl">
          <dt>Source</dt><dd>${esc(src.title || e.locator || "—")}</dd>
          ${e.locator ? `<dt>Locator</dt><dd class="mono" style="font-size:11px">${esc(e.locator)}</dd>` : ""}
          ${src.publisher ? `<dt>Publisher</dt><dd>${esc(src.publisher)}</dd>` : ""}
          ${src.form ? `<dt>Filing</dt><dd>${esc(src.form)}</dd>` : ""}
          ${e.numeric != null ? `<dt>Numeric</dt><dd class="mono">${fmtNum(e.numeric)}</dd>` : ""}
        </dl>
        ${src.url ? `<a class="src-link" href="${esc(src.url)}" target="_blank" rel="noopener"><b>Open primary source ↗</b><span>${esc(src.title || src.url)}</span></a>` : ""}
        ${tags ? `<div class="drawer-section">Tags</div><div class="tag-row">${tags}</div>` : ""}
        ${risks ? `<div class="drawer-section">Used in risks</div><div class="tag-row">${risks}</div>` : ""}
        ${src.excerpt ? `<div class="drawer-section">Source excerpt</div><div class="small muted" style="white-space:pre-wrap;line-height:1.6;max-height:240px;overflow:auto">${esc(src.excerpt.slice(0, 2200))}</div>` : ""}`
      );
      $$("[data-risk]", $("#drawer-body")).forEach((b) =>
        b.addEventListener("click", () => showRisk(b.dataset.risk))
      );
    } catch (e) {
      openDrawer("Evidence", `<div class="empty">Could not load evidence: ${esc(e.message)}</div>`);
    }
  }

  function showRisk(id) {
    const r = state.risks.find((x) => x.id === id);
    if (!r) return;
    const evs = (r.evidence_ids || []).map((eid) => `<button class="chip ev" data-ev="${esc(eid)}">${esc(eid)}</button>`).join("");
    openDrawer(
      `Risk · ${r.id}`,
      `<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px"><span class="score-badge ${rating(r.score)}" style="width:42px;height:42px"><b>${r.score}</b><span>${rating(r.score)}</span></span><span class="cat">${esc(r.category)}</span><span class="chip">S${r.severity} × L${r.likelihood}</span></div>
      <h3>${esc(r.title)}</h3>
      <p class="small" style="line-height:1.65;color:var(--ink-2)">${esc(stripCites(r.description))}</p>
      ${r.exposure ? `<div class="drawer-section">Exposure</div><div class="small">${esc(r.exposure)}</div>` : ""}
      ${(r.mitigants || []).length ? `<div class="drawer-section">Mitigants</div><ul class="small" style="margin:0;padding-left:18px;line-height:1.6">${r.mitigants.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}
      ${(r.questions || []).length ? `<div class="drawer-section">Diligence questions</div><ul class="small" style="margin:0;padding-left:18px;line-height:1.6">${r.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}
      ${evs ? `<div class="drawer-section">Evidence</div><div class="tag-row">${evs}</div>` : ""}
      <div class="drawer-section">Status</div>
      <div class="tag-row">${["open", "mitigating", "accepted", "closed"].map((s) => `<button class="chip ${r.status === s ? "ev" : ""}" data-status="${s}">${esc(titleCase(s))}</button>`).join("")}</div>
      <div class="drawer-section">Re-score</div>
      <div style="display:flex;gap:8px;align-items:center">
        <label class="tiny muted">Severity <select class="input select" id="risk-sev" style="padding:4px 24px 4px 8px">${[1, 2, 3, 4, 5].map((n) => `<option ${n === r.severity ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <label class="tiny muted">Likelihood <select class="input select" id="risk-lik" style="padding:4px 24px 4px 8px">${[1, 2, 3, 4, 5].map((n) => `<option ${n === r.likelihood ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <button class="btn btn-ghost btn-sm" id="risk-save">Save</button>
      </div>`
    );
    $$("[data-ev]", $("#drawer-body")).forEach((b) => b.addEventListener("click", () => showEvidence(b.dataset.ev)));
    $$("[data-status]", $("#drawer-body")).forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/api/deals/${state.deal.id}/risks/${r.id}`, { method: "PATCH", body: { status: b.dataset.status, actor: "Reviewer" } });
          toast(`${r.id} marked ${b.dataset.status}.`, "ok");
          await loadDeal(state.deal.id, { quiet: true });
          renderView();
          showRisk(r.id);
        } catch (e) {
          toast(e.message, "err");
        }
      })
    );
    const save = $("#risk-save");
    if (save)
      save.addEventListener("click", async () => {
        try {
          await api(`/api/deals/${state.deal.id}/risks/${r.id}`, {
            method: "PATCH",
            body: { severity: Number($("#risk-sev").value), likelihood: Number($("#risk-lik").value), actor: "Reviewer" },
          });
          toast(`${r.id} re-scored.`, "ok");
          await loadDeal(state.deal.id, { quiet: true });
          renderView();
          showRisk(r.id);
        } catch (e) {
          toast(e.message, "err");
        }
      });
  }

  // --------------------------------------------------------------- actions
  async function triggerRun() {
    if (!state.deal) return;
    const btn = $("#run-btn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> Starting…`;
    try {
      const { run_id } = await api(`/api/deals/${state.deal.id}/run`, { method: "POST", body: {} });
      state.run = { id: run_id, status: "running", stage: "queued", progress: 0 };
      state.log = [];
      state.deal.running = true;
      updateHeader();
      renderRail();
      startStream(run_id);
      if (state.tab !== "overview") go(state.deal.id, "overview");
      else renderView();
      toast("Run started. The agents are working through the filings.", "ok");
    } catch (e) {
      toast(e.message, "err");
      updateHeader();
    }
  }

  async function approveDoc() {
    openModal(
      `<h2>Approve this draft?</h2>
       <p class="modal-sub">Approval locks the current version, records your name in the audit trail, and marks the deal approved. You can still re-run the pipeline later.</p>
       <div class="field"><label>Reviewer</label><input class="input" id="approve-actor" placeholder="Your name"></div>
       <div class="field"><label>Note (optional)</label><input class="input" id="approve-note" placeholder="e.g. Cleared for partner review"></div>
       <div class="modal-actions"><button class="btn btn-ghost" data-close>Cancel</button><button class="btn btn-primary" id="approve-confirm">Approve draft</button></div>`
    );
    $("#approve-confirm").addEventListener("click", async () => {
      const actor = $("#approve-actor").value.trim() || "Reviewer";
      const note = $("#approve-note").value.trim();
      try {
        await api(`/api/deals/${state.deal.id}/document/approve`, { method: "POST", body: { actor, note } });
        closeModal();
        toast("Draft approved and recorded in the review log.", "ok");
        await loadDeal(state.deal.id, { quiet: true });
        await refreshDeals();
        renderView();
        renderRail();
      } catch (e) {
        toast(e.message, "err");
      }
    });
  }

  function newDealModal(prefill = {}) {
    openModal(
      `<h2>New due diligence</h2>
       <p class="modal-sub">Enter a US-listed company. Dossier resolves it against SEC EDGAR and pulls its latest 10-K, three 10-Qs, proxy and recent 8-Ks.</p>
       <div class="field"><label>Company name, ticker or CIK</label><input class="input" id="nd-ident" placeholder="e.g. LOVE, Lovesac or 0001701758" value="${esc(prefill.identifier || "")}"></div>
       <div class="row2">
         <div class="field"><label>Deal name</label><input class="input" id="nd-name" placeholder="Optional"></div>
         <div class="field"><label>Client</label><input class="input" id="nd-client" placeholder="Optional"></div>
       </div>
       <div class="row2">
         <div class="field"><label>Transaction</label><select class="input" id="nd-type"><option value="sell_side">Sell-side</option><option value="buy_side">Buy-side</option><option value="capital_raise">Capital raise</option><option value="recapitalisation">Recapitalisation</option></select></div>
         <div class="field"><label>Sector</label><input class="input" id="nd-sector" placeholder="Optional"></div>
       </div>
       <div class="field"><label style="display:flex;align-items:center;gap:7px;text-transform:none;letter-spacing:0;font-size:12px;font-weight:500"><input type="checkbox" id="nd-start" checked> Start the pipeline immediately</label><div class="hint">Research and drafting are citation-gated. The configured model adds analyst-grade polish; the deterministic engine is the fallback.</div></div>
       <div class="modal-actions"><button class="btn btn-ghost" data-close>Cancel</button><button class="btn btn-primary" id="nd-create">Create deal</button></div>`
    );
    $("#nd-create").addEventListener("click", async () => {
      const identifier = $("#nd-ident").value.trim();
      if (!identifier) {
        $("#nd-ident").focus();
        return;
      }
      const body = {
        identifier,
        name: $("#nd-name").value.trim() || identifier,
        client: $("#nd-client").value.trim(),
        deal_type: $("#nd-type").value,
        sector: $("#nd-sector").value.trim(),
        start: $("#nd-start").checked,
      };
      const b = $("#nd-create");
      b.disabled = true;
      b.innerHTML = `<span class="spin"></span> Creating…`;
      try {
        const res = await api("/api/deals", { method: "POST", body });
        closeModal();
        await refreshDeals();
        toast(`Deal created — resolving ${identifier} in EDGAR.`, "ok");
        go(res.deal.id, "overview");
      } catch (e) {
        toast(e.message, "err");
        b.disabled = false;
        b.textContent = "Create deal";
      }
    });
  }

  // --------------------------------------------------------------- modal
  function openModal(html) {
    $("#modal").innerHTML = html;
    $("#modal-scrim").classList.remove("hidden");
    $$("[data-close]", $("#modal")).forEach((b) => b.addEventListener("click", closeModal));
    const first = $("#modal input, #modal select");
    if (first) setTimeout(() => first.focus(), 40);
  }
  function closeModal() {
    $("#modal-scrim").classList.add("hidden");
    $("#modal").innerHTML = "";
  }

  // --------------------------------------------------------------- chrome
  function bindChrome() {
    $("#new-deal-btn").addEventListener("click", () => newDealModal());
    $("#welcome-new").addEventListener("click", () => newDealModal());
    $("#welcome-demo").addEventListener("click", loadDemo);
    $("#demo-btn").addEventListener("click", loadDemo);
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#scrim").addEventListener("click", closeDrawer);
    $("#modal-scrim").addEventListener("click", (e) => {
      if (e.target.id === "modal-scrim") closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeDrawer();
        closeModal();
      }
    });
    $$(".tab").forEach((t) =>
      t.addEventListener("click", () => {
        if (!state.deal) return;
        closeDrawer();
        go(state.deal.id, t.dataset.tab);
      })
    );
    $("#run-btn").addEventListener("click", triggerRun);
    $("#export-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      if (!$("#export-btn").disabled) $("#export-menu").classList.toggle("hidden");
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".export-wrap")) $("#export-menu").classList.add("hidden");
    });
    $$("[data-export]").forEach((b) =>
      b.addEventListener("click", () => {
        if (!state.deal || !state.doc) return;
        $("#export-menu").classList.add("hidden");
        const fmt = b.dataset.export;
        toast(`Preparing ${fmt.toUpperCase()}…`);
        const url = `/api/deals/${state.deal.id}/export/${fmt}`;
        downloadExport(url, fmt);
      })
    );
    window.addEventListener("hashchange", route);
  }

  async function downloadExport(url, fmt) {
    try {
      const token = authToken();
      const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename="?([^";]+)"?/.exec(disposition);
      const name = match ? match[1] : `dossier-export.${fmt}`;
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 4000);
      toast(`${fmt.toUpperCase()} downloaded.`, "ok");
    } catch (e) {
      toast(`Export failed: ${e.message}`, "err");
    }
  }

  async function loadDemo() {
    const btns = [$("#demo-btn"), $("#welcome-demo")].filter(Boolean);
    btns.forEach((b) => (b.disabled = true));
    try {
      const res = await api("/api/demo", { method: "POST", body: { start: true } });
      await refreshDeals();
      toast("Demo deal ready — running the pipeline on Lovesac.", "ok");
      if (parseHash()?.dealId === res.deal_id) await route();
      else go(res.deal_id, "overview");
    } catch (e) {
      toast(`Could not load demo: ${e.message}`, "err");
    } finally {
      btns.forEach((b) => (b.disabled = false));
    }
  }

  boot();
})();

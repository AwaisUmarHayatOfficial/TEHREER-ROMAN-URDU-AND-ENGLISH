(() => {
  const $ = (s) => document.querySelector(s);
  const el = {
    formCard: $("#formCard"), form: $("#jobForm"), key: $("#apiKey"), url: $("#videoUrl"),
    remember: $("#rememberKey"), paste: $("#pasteBtn"), toggle: $("#toggleKey"), formErr: $("#formError"), start: $("#startBtn"),
    jobCard: $("#jobCard"), jobTitle: $("#jobTitle"), videoTitle: $("#videoTitle"),
    pct: $("#pctBig"), bar: $("#bar"), fill: $("#barFill"), stage: $("#stageText"), elapsed: $("#elapsed"),
    chunkWrap: $("#chunkWrap"), chunks: $("#chunks"), warn: $("#jobWarn"), err: $("#jobError"),
    cancel: $("#cancelBtn"), dlDocx: $("#dlDocx"), dlTxt: $("#dlTxt"), again: $("#newBtn"),
    logs: $("#logs"),
    chooser: $("#langChooser"), langPicked: $("#langPicked"), langChange: $("#langChange"), jobLang: $("#jobLang"), langHint: $("#langHint"),
  };

  const BASE_TITLE = document.title;
  const LS_KEY = "ru_gemini_key", LS_JOB = "ru_job_id";
  let jobId = null, nextLog = 0, timer = null, failStreak = 0;
  const LANG_LABEL = { english: "English", roman_urdu: "Roman Urdu" };
  let language = "";  // chosen notes language: "english" | "roman_urdu" (asked before every new session)

  // ---- language choice ------------------------------------------------
  function setLanguage(l) {
    language = LANG_LABEL[l] ? l : "";
    document.querySelectorAll(".lang-opt").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.lang === language)));
    el.chooser.hidden = !!language;
    el.form.hidden = !language;
    if (language) {
      el.langPicked.textContent = LANG_LABEL[language];
      showFormError("");
      (el.key.value ? el.url : el.key).focus();
    }
  }
  document.querySelectorAll(".lang-opt").forEach((b) => b.addEventListener("click", () => setLanguage(b.dataset.lang)));
  el.langChange.addEventListener("click", () => setLanguage(""));

  // ---- saved key ------------------------------------------------------
  try {
    const saved = localStorage.getItem(LS_KEY);
    if (saved) { el.key.value = saved; el.remember.checked = true; }
  } catch (_) {}

  el.toggle.addEventListener("click", () => {
    const show = el.key.type === "password";
    el.key.type = show ? "text" : "password";
    el.toggle.textContent = show ? "Hide" : "Show";
    el.toggle.setAttribute("aria-pressed", String(show));
  });

  function setStep(n) {
    [1, 2, 3].forEach((i) => {
      const e = document.getElementById("stp" + i);
      if (e) e.className = i < n ? "done" : i === n ? "on" : "";
    });
  }
  setStep(1);

  function showFormError(msg) { el.formErr.textContent = msg; el.formErr.hidden = !msg; }

  if (el.paste) el.paste.addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) { el.url.value = text; showFormError(""); }
    } catch (_) {
      showFormError("Your browser blocked clipboard access. Paste the link manually (Ctrl+V).");
    }
  });

  // ---- start ----------------------------------------------------------
  el.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError("");
    const key = el.key.value.trim(), url = el.url.value.trim();
    if (!language) return showFormError("Choose the notes language first: English or Roman Urdu.");
    if (!key) return showFormError("Enter your Gemini API key.");
    if (!url) return showFormError("Enter the YouTube video link.");

    el.start.disabled = true; el.start.textContent = "Checking key...";
    try {
      const r = await fetch("/api/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: key, url, language }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.status === 401) { location.href = "/login?next=/app"; return; }
      if (!r.ok) throw new Error(data.detail || "Something went wrong. Please try again.");

      try {
        if (el.remember.checked) localStorage.setItem(LS_KEY, key); else localStorage.removeItem(LS_KEY);
        localStorage.setItem(LS_JOB, data.id);
      } catch (_) {}
      beginJob(data.id);
    } catch (err) {
      showFormError(err.message);
    } finally {
      el.start.disabled = false; el.start.textContent = "Create notes";
    }
  });

  // ---- job view -------------------------------------------------------
  function beginJob(id) {
    jobId = id; nextLog = 0; failStreak = 0;
    setStep(2);
    el.logs.textContent = "";
    el.formCard.hidden = true; el.jobCard.hidden = false;
    el.err.hidden = true; el.warn.hidden = true;
    el.dlDocx.hidden = el.dlTxt.hidden = el.again.hidden = true;
    el.cancel.hidden = false; el.cancel.disabled = false;
    el.jobTitle.textContent = "Converting";
    el.videoTitle.textContent = "";
    if (el.jobLang) { el.jobLang.hidden = !language; el.jobLang.textContent = LANG_LABEL[language] || ""; }
    el.chunkWrap.hidden = true;
    render({ percent: 0, stage: "Waiting in the queue...", status: "queued", chunks: [], elapsed: 0 });
    poll();
  }

  async function poll() {
    clearTimeout(timer);
    if (!jobId) return;
    try {
      const r = await fetch(`/api/jobs/${jobId}?since=${nextLog}`, { cache: "no-store" });
      if (r.status === 401) { location.href = "/login?next=/app"; return; }
      if (r.status === 404) return expired();
      const d = await r.json();
      failStreak = 0;
      nextLog = d.next;
      appendLogs(d.logs);
      render(d);
      if (["done", "failed", "cancelled"].includes(d.status)) return finish(d);
    } catch (_) {
      if (++failStreak >= 3) el.stage.textContent = "Lost connection to the server, retrying...";
    }
    timer = setTimeout(poll, 1500);
  }

  function render(d) {
    if (d.language && el.jobLang && LANG_LABEL[d.language]) { el.jobLang.hidden = false; el.jobLang.textContent = LANG_LABEL[d.language]; }
    const p = Math.max(0, Math.min(100, d.percent || 0));
    el.pct.textContent = Math.floor(p) + "%";
    el.fill.style.width = p + "%";
    el.bar.setAttribute("aria-valuenow", String(Math.floor(p)));
    let stage = d.stage || "";
    if (d.status === "queued" && d.queue_position) stage = `In the queue (position ${d.queue_position}). The server is busy, please wait for your turn...`;
    el.stage.textContent = stage;
    el.elapsed.textContent = d.elapsed ? `· ${fmt(d.elapsed)}` : "";
    if (d.title) el.videoTitle.textContent = d.title;
    document.title = d.status === "done" ? "Ready - Notes tool" : `${Math.floor(p)}% - Notes tool`;

    if (d.chunks && d.chunks.length) {
      el.chunkWrap.hidden = false;
      if (el.chunks.childElementCount !== d.chunks.length) {
        el.chunks.textContent = "";
        d.chunks.forEach(() => { const i = document.createElement("i"); el.chunks.appendChild(i); });
      }
      d.chunks.forEach((s, i) => {
        const n = el.chunks.children[i];
        n.className = "c " + s;
        n.title = `Chunk ${i + 1}`;
      });
    }
  }

  function finish(d) {
    el.cancel.hidden = true; el.again.hidden = false;
    if (d.status === "done") {
      setStep(4);
      el.jobTitle.textContent = "Your notes are ready";
      if (d.files.docx) { el.dlDocx.href = d.files.docx; el.dlDocx.hidden = false; }
      if (d.files.txt) { el.dlTxt.href = d.files.txt; el.dlTxt.hidden = false; }
      if (d.warning) { el.warn.textContent = d.warning; el.warn.hidden = false; }
    } else if (d.status === "cancelled") {
      el.jobTitle.textContent = "Cancelled";
    } else {
      el.jobTitle.textContent = "Conversion failed";
      el.err.textContent = d.error || "The job failed. Check the logs.";
      el.err.hidden = false;
    }
  }

  function expired() {
    resetToForm();
    showFormError("Your previous job was not found (it expired or the server restarted). Please start again.");
  }

  function resetToForm() {
    clearTimeout(timer); jobId = null;
    try { localStorage.removeItem(LS_JOB); } catch (_) {}
    el.jobCard.hidden = true; el.formCard.hidden = false;
    setStep(1);
    document.title = BASE_TITLE;
  }

  el.cancel.addEventListener("click", async () => {
    el.cancel.disabled = true;
    try { await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } catch (_) {}
  });

  el.again.addEventListener("click", async () => {
    resetToForm();
    el.url.value = "";
  });

  // ---- logs -----------------------------------------------------------
  function appendLogs(lines) {
    if (!lines || !lines.length) return;
    const stick = el.logs.scrollTop + el.logs.clientHeight >= el.logs.scrollHeight - 24;
    const frag = document.createDocumentFragment();
    lines.forEach((ln) => {
      const s = document.createElement("span");
      if (/^\s*\[(✓|✔)\]/.test(ln)) s.className = "ok";
      else if (/^\s*\[X\]/.test(ln)) s.className = "err";
      else if (/^\s*\[!\]/.test(ln)) s.className = "wn";
      s.textContent = ln + "\n";
      frag.appendChild(s);
    });
    el.logs.appendChild(frag);
    if (stick) el.logs.scrollTop = el.logs.scrollHeight;
  }

  function fmt(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m ? `${m} min ${s} sec` : `${s} sec`;
  }

  // ---- server status (sidebar) ----------------------------------------
  async function loadStatus() {
    const box = document.querySelector("#serverStatus");
    if (!box) return;
    try {
      const d = await (await fetch("/api/health", { cache: "no-store" })).json();
      box.textContent = d.queued
        ? `${d.running} running, ${d.queued} waiting`
        : d.running
          ? `${d.running} of ${d.max_concurrent} slots in use`
          : "Idle - your job starts right away";
    } catch (_) {
      box.textContent = "Status unavailable";
    }
  }
  loadStatus();
  setInterval(loadStatus, 15000);

  // ---- resume after refresh -------------------------------------------
  (window.SITE_ME || Promise.resolve(null)).then((me) => {
    if (!me) return;  // site.js redirects signed-out visitors to the sign-in page
    const who = document.querySelector("#whoami");
    if (who) who.textContent = me.email;
    try {
      const saved = localStorage.getItem(LS_JOB);
      if (saved) beginJob(saved);
    } catch (_) {}
  });
})();

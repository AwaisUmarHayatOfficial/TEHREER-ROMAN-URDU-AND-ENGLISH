/* Sign in, register, forgot password (login.html, register.html, forgot-password.html) and the account page. */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var CFG = null;

  /* ------------------------------------------------------------ helpers */
  function safeNext() {
    var n = new URLSearchParams(location.search).get("next") || "/app";
    return /^\/(?!\/)[^\\\s]*$/.test(n) ? n : "/app";
  }
  function nextQuery() {
    var n = new URLSearchParams(location.search).get("next");
    return n ? "?next=" + encodeURIComponent(n) : "";
  }
  function api(method, url, body) {
    return fetch(url, {
      method: method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, data: j, retry: r.headers.get("Retry-After"), code: r.headers.get("X-Error-Code") };
      });
    });
  }
  function show(el, msg) { if (!el) return; el.textContent = msg || ""; el.hidden = !msg; }
  function detail(r, fallback) {
    var d = r.data && r.data.detail;
    return typeof d === "string" ? d : (Array.isArray(d) ? "Please check the highlighted fields and try again." : fallback);
  }
  function getConfig() {
    if (CFG) return Promise.resolve(CFG);
    return fetch("/api/auth/config").then(function (r) { return r.json(); })
      .catch(function () { return { google_client_id: "", password_min: 10 }; })
      .then(function (c) { CFG = c; return c; });
  }
  function countdown(state, sec) {
    clearInterval(state.timer);
    var btn = $("#resendBtn"), lbl = $("#resendTimer");
    if (!btn) return;
    btn.disabled = true;
    function tick() {
      if (sec <= 0) { clearInterval(state.timer); btn.disabled = false; lbl.textContent = ""; return; }
      lbl.textContent = "(" + sec + "s)"; sec--;
    }
    tick(); state.timer = setInterval(tick, 1000);
  }

  /* --------------------------------------------------------- passwords */
  function pwChecks(pw, min) {
    return { len: pw.length >= min, lower: /[a-z]/.test(pw), upper: /[A-Z]/.test(pw), digit: /\d/.test(pw), symbol: /[^A-Za-z0-9]/.test(pw) };
  }
  function pwProblem(pw, min, forbidden) {
    var c = pwChecks(pw, min);
    if (!c.len) return "Password must be at least " + min + " characters.";
    if (pw.length > 64) return "Password must be at most 64 characters.";
    if (!c.lower) return "Password needs at least one lowercase letter.";
    if (!c.upper) return "Password needs at least one uppercase letter.";
    if (!c.digit) return "Password needs at least one number.";
    if (!c.symbol) return "Password needs at least one symbol (for example ! @ # $ %).";
    var low = pw.toLowerCase();
    for (var i = 0; i < (forbidden || []).length; i++) {
      var f = (forbidden[i] || "").toLowerCase();
      if (f.length >= 4 && low.indexOf(f) > -1) return "Password must not contain your username or the name part of your e-mail.";
    }
    return "";
  }
  function wirePasswordUI() {
    document.querySelectorAll(".pw-toggle").forEach(function (b) {
      b.addEventListener("click", function () {
        var inp = document.getElementById(b.getAttribute("data-for"));
        var showing = inp.type === "password";
        inp.type = showing ? "text" : "password";
        b.textContent = showing ? "Hide" : "Show";
        b.setAttribute("aria-pressed", String(showing));
      });
    });
    var pw = $("#password"), rules = $("#pwRules"), bar = $("#pwBar");
    if (pw && rules) pw.addEventListener("input", function () {
      getConfig().then(function (cfg) {
        var c = pwChecks(pw.value, cfg.password_min || 10), ok = 0;
        rules.querySelectorAll("li").forEach(function (li) {
          var pass = !!c[li.getAttribute("data-rule")];
          li.classList.toggle("ok", pass);
          if (pass) ok++;
        });
        if (bar) { bar.style.width = (ok / 5 * 100) + "%"; bar.className = ok >= 5 ? "strong" : ok >= 3 ? "medium" : "weak"; }
      });
    });
  }

  /* ------------------------------------------------------------- Google */
  var GOOGLE_ERRORS = {
    google_denied: "Google sign-in was cancelled.",
    google_failed: "Google sign-in could not be completed. Please try again.",
    google_not_gmail: "Please use a Gmail account (name@gmail.com) to continue with Google.",
    google_blocked: "This account has been disabled. Please contact support.",
    google_unconfigured: "Google sign-in is not set up on this server yet. The site owner needs to add the Google credentials."
  };
  function initGoogleButton() {
    var btn = $("#googleBtn");
    if (!btn) return;
    var q = new URLSearchParams(location.search), n = q.get("next");
    if (n) btn.href = "/api/auth/google/start?next=" + encodeURIComponent(safeNext());
    var err = q.get("error");
    if (err && GOOGLE_ERRORS[err]) show($("#googleError"), GOOGLE_ERRORS[err]);
  }

  wirePasswordUI();
  initGoogleButton();
  var codeInput = $("#code");
  if (codeInput) codeInput.addEventListener("input", function () { this.value = this.value.replace(/\D/g, "").slice(0, 6); });

  /* ------------------------------------------------------------- login */
  if ($("#loginForm")) {
    var reg = $("#toRegister");
    if (reg) reg.href = "/register" + nextQuery();
    $("#loginForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var email = $("#email").value.trim(), pw = $("#password").value, err = $("#loginError"), hint = $("#verifyHint"), b = $("#loginBtn");
      show(err, ""); hint.hidden = true;
      if (!email || !pw) return show(err, "Enter your Gmail address and password.");
      b.disabled = true; b.textContent = "Signing in...";
      api("POST", "/api/auth/login", { email: email, password: pw }).then(function (r) {
        if (r.ok) { b.textContent = "Signed in"; location.replace(safeNext()); return; }
        b.disabled = false; b.textContent = "Sign in with Email";
        if (r.status === 403 && r.code === "email_not_verified") {
          $("#verifyLink").href = "/register?verify=" + encodeURIComponent(email) + (nextQuery() ? "&" + nextQuery().slice(1) : "");
          hint.hidden = false;
          return show(err, detail(r, ""));
        }
        show(err, detail(r, "Could not sign in. Please try again."));
      }).catch(function () { b.disabled = false; b.textContent = "Sign in with Email"; show(err, "Network problem. Please try again."); });
    });
  }

  /* ---------------------------------------------------------- register */
  if ($("#registerForm")) {
    var rstate = {}, remail = "";
    function toVerify(email, devMode, sec) {
      remail = email;
      $("#sentTo").textContent = email;
      $("#devHint").hidden = !devMode;
      $("#stepForm").hidden = true; $("#stepVerify").hidden = false;
      $("#code").value = ""; $("#code").focus();
      countdown(rstate, sec || 60);
    }
    var qv = new URLSearchParams(location.search).get("verify");
    if (qv) toVerify(qv, false, 0);

    $("#registerForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var err = $("#registerError"), b = $("#registerBtn");
      show(err, "");
      var d = {
        first_name: $("#first_name").value.trim(), last_name: $("#last_name").value.trim(), phone: $("#phone").value.trim(),
        email: $("#email").value.trim(), username: $("#username").value.trim().toLowerCase(), password: $("#password").value
      };
      var nameRe = /^[A-Za-z\u00C0-\u024F\u0600-\u06FF]+(?:[ '.\-][A-Za-z\u00C0-\u024F\u0600-\u06FF]+)*$/;
      if (!d.first_name || !nameRe.test(d.first_name)) return show(err, "Enter your first name (letters only).");
      if (!d.last_name || !nameRe.test(d.last_name)) return show(err, "Enter your last name (letters only).");
      if (!/^\+?\d{10,15}$/.test(d.phone.replace(/[\s\-().]/g, ""))) return show(err, "Enter a valid phone number with 10 to 15 digits.");
      if (!/^[^@\s]+@(gmail|googlemail)\.com$/i.test(d.email)) return show(err, "Please use your Gmail address (name@gmail.com).");
      if (!/^[a-z][a-z0-9_.]{2,29}$/.test(d.username)) return show(err, "Username must be 3 to 30 characters: start with a letter, then letters, numbers, dots or underscores.");
      var bad = pwProblem(d.password, (CFG && CFG.password_min) || 10, [d.username, d.email.split("@")[0].split("+")[0].replace(/\./g, "")]);
      if (bad) return show(err, bad);
      if (d.password !== $("#password2").value) return show(err, "The two passwords do not match.");
      if (!$("#terms").checked) return show(err, "Please accept the Terms of use and the Privacy policy.");
      b.disabled = true; b.textContent = "Creating account...";
      api("POST", "/api/auth/register", d).then(function (r) {
        b.disabled = false; b.textContent = "Create account";
        if (!r.ok) return show(err, detail(r, "Could not create the account. Please try again."));
        toVerify(r.data.email || d.email, r.data.dev_mode, r.data.resend_in);
      }).catch(function () { b.disabled = false; b.textContent = "Create account"; show(err, "Network problem. Please try again."); });
    });

    $("#verifyForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var err = $("#verifyError"), b = $("#verifyBtn"), code = $("#code").value.replace(/\D/g, "");
      show(err, "");
      if (code.length !== 6) return show(err, "Enter the 6-digit code from the e-mail.");
      b.disabled = true; b.textContent = "Checking...";
      api("POST", "/api/auth/verify-email", { email: remail, code: code }).then(function (r) {
        if (r.ok) { b.textContent = "Done"; location.replace(safeNext()); return; }
        b.disabled = false; b.textContent = "Verify and continue";
        show(err, detail(r, "That code did not work."));
      }).catch(function () { b.disabled = false; b.textContent = "Verify and continue"; show(err, "Network problem. Please try again."); });
    });
    $("#resendBtn").addEventListener("click", function () {
      var err = $("#verifyError"); show(err, "");
      api("POST", "/api/auth/resend-code", { email: remail }).then(function (r) {
        if (r.ok) countdown(rstate, r.data.resend_in || 60);
        else { if (r.status === 429 && r.retry && !isNaN(+r.retry)) countdown(rstate, +r.retry); show(err, detail(r, "Could not send the code.")); }
      });
    });
    $("#backBtn").addEventListener("click", function () { clearInterval(rstate.timer); $("#stepVerify").hidden = true; $("#stepForm").hidden = false; });
  }

  /* --------------------------------------------------- forgot password */
  if ($("#forgotForm")) {
    var fstate = {}, femail = "";
    function sendReset(isResend) {
      var err = isResend ? $("#resetError") : $("#forgotError"), b = $("#forgotBtn");
      show(err, "");
      if (!isResend) { b.disabled = true; b.textContent = "Sending..."; }
      return api("POST", "/api/auth/forgot-password", { email: femail }).then(function (r) {
        if (!isResend) { b.disabled = false; b.textContent = "Send me a code"; }
        if (!r.ok) {
          if (r.status === 429 && r.retry && !isNaN(+r.retry) && !$("#stepReset").hidden) countdown(fstate, +r.retry);
          return show(err, detail(r, "Could not send the code. Please try again."));
        }
        $("#sentTo").textContent = femail;
        $("#devHint").hidden = !r.data.dev_mode;
        $("#stepEmail").hidden = true; $("#stepReset").hidden = false;
        if (!isResend) { $("#code").value = ""; $("#code").focus(); }
        countdown(fstate, r.data.resend_in || 60);
      }).catch(function () { if (!isResend) { b.disabled = false; b.textContent = "Send me a code"; } show(err, "Network problem. Please try again."); });
    }
    $("#forgotForm").addEventListener("submit", function (e) {
      e.preventDefault();
      femail = $("#email").value.trim();
      if (!/^[^@\s]+@(gmail|googlemail)\.com$/i.test(femail)) return show($("#forgotError"), "Please use your Gmail address (name@gmail.com).");
      sendReset(false);
    });
    $("#resetForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var err = $("#resetError"), b = $("#resetBtn"), code = $("#code").value.replace(/\D/g, ""), pw = $("#password").value;
      show(err, "");
      if (code.length !== 6) return show(err, "Enter the 6-digit code from the e-mail.");
      var bad = pwProblem(pw, (CFG && CFG.password_min) || 10, [femail.split("@")[0].split("+")[0].replace(/\./g, "")]);
      if (bad) return show(err, bad);
      if (pw !== $("#password2").value) return show(err, "The two passwords do not match.");
      b.disabled = true; b.textContent = "Saving...";
      api("POST", "/api/auth/reset-password", { email: femail, code: code, password: pw }).then(function (r) {
        b.disabled = false; b.textContent = "Set new password";
        if (!r.ok) return show(err, detail(r, "Could not reset the password."));
        clearInterval(fstate.timer); $("#stepReset").hidden = true; $("#stepDone").hidden = false;
      }).catch(function () { b.disabled = false; b.textContent = "Set new password"; show(err, "Network problem. Please try again."); });
    });
    $("#resendBtn").addEventListener("click", function () { sendReset(true); });
    $("#backBtn").addEventListener("click", function () { clearInterval(fstate.timer); $("#stepReset").hidden = true; $("#stepEmail").hidden = false; });
  }

  /* ----------------------------------------------------------- account */
  if ($("#historyList")) {
    var refresh = null;

    function fmt(iso) {
      if (!iso) return "";
      try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; }
    }
    var LABEL = { done: "Done", failed: "Failed", cancelled: "Cancelled", running: "Running", queued: "Queued" };
    function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

    function renderHistory(data) {
      var box = $("#historyList");
      $("#historyNote").textContent = "Files are kept for " + data.retention_hours + " hours after a conversion finishes. History entries are kept for " + data.history_days + " days.";
      if (!data.jobs.length) {
        box.innerHTML = '<p class="muted">No conversions yet. <a href="/app">Convert your first video</a>.</p>';
        return false;
      }
      var active = false;
      box.innerHTML = data.jobs.map(function (j) {
        if (j.status === "queued" || j.status === "running") active = true;
        var title = esc(j.title || j.video_url), files = "";
        if (j.files.docx) files += '<a class="btn secondary sm" href="' + j.files.docx + '">Word</a>';
        if (j.files.txt) files += '<a class="btn ghost sm" href="' + j.files.txt + '">Text</a>';
        if (j.status === "done" && !j.files.docx && !j.files.txt) files += '<span class="fine">Files expired</span>';
        if (j.status === "queued" || j.status === "running") files += '<a class="btn ghost sm" href="/app">Open</a>';
        else files += '<button type="button" class="btn ghost sm danger" data-del="' + j.id + '">Delete</button>';
        var extra = j.status === "failed" && j.error ? '<p class="h-err">' + esc(j.error) + "</p>" : "";
        var pct = (j.status === "running" || j.status === "queued") ? " &middot; " + Math.floor(j.percent) + "%" : "";
        return '<article class="h-item"><div class="h-main"><a class="h-title" href="' + esc(j.video_url) + '" target="_blank" rel="noopener">' + title + "</a>" +
          '<div class="h-meta"><span class="status-badge s-' + j.status + '">' + (LABEL[j.status] || j.status) + '</span><span class="lang-badge">' + esc(j.language_label || "") + "</span> <span>" + fmt(j.created_at) + pct + "</span></div>" + extra + "</div>" +
          '<div class="h-actions">' + files + "</div></article>";
      }).join("");
      return active;
    }
    function loadHistory() {
      clearTimeout(refresh);
      api("GET", "/api/my/jobs").then(function (r) {
        if (!r.ok) return;
        if (renderHistory(r.data)) refresh = setTimeout(loadHistory, 5000);
      });
    }
    $("#historyList").addEventListener("click", function (e) {
      var b = e.target.closest("[data-del]");
      if (!b) return;
      if (!confirm("Delete this conversion and its files?")) return;
      api("DELETE", "/api/my/jobs/" + b.getAttribute("data-del")).then(loadHistory);
    });

    var hasPassword = false;
    function fillProfile(me) {
      $("#pfirst").value = me.first_name || "";
      $("#plast").value = me.last_name || "";
      $("#pphone").value = me.phone || "";
      $("#pemail").textContent = me.email;
      $("#puser").textContent = me.username || "-";
      $("#psince").textContent = me.created_at ? new Date(me.created_at).toLocaleDateString(undefined, { dateStyle: "long" }) : "";
      hasPassword = !!me.has_password;
      $("#pwForm").hidden = !hasPassword;
      $("#pwNone").hidden = hasPassword;
      $("#delPwWrap").hidden = !hasPassword;
    }
    (window.SITE_ME || Promise.resolve(null)).then(function (me) {
      if (!me) return;
      fillProfile(me);
      loadHistory();
    });

    $("#profileForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var msg = $("#pmsg"), err = $("#profileError");
      show(err, "");
      api("PATCH", "/api/me", { first_name: $("#pfirst").value, last_name: $("#plast").value, phone: $("#pphone").value }).then(function (r) {
        if (!r.ok) return show(err, detail(r, "Could not save."));
        msg.textContent = "Saved.";
        if (window.SITE_REFRESH_USER) window.SITE_REFRESH_USER(r.data.user);
        setTimeout(function () { msg.textContent = ""; }, 2500);
      });
    });

    $("#pwForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var err = $("#pwError"), msg = $("#pwMsg"), np = $("#newpw").value;
      show(err, "");
      var bad = pwProblem(np, (CFG && CFG.password_min) || 10, []);
      if (bad) return show(err, bad);
      api("POST", "/api/auth/change-password", { current_password: $("#curpw").value, new_password: np }).then(function (r) {
        if (!r.ok) return show(err, detail(r, "Could not change the password."));
        $("#curpw").value = ""; $("#newpw").value = "";
        msg.textContent = "Password changed. Other devices were signed out.";
        setTimeout(function () { msg.textContent = ""; }, 4000);
      });
    });

    $("#logoutBtn").addEventListener("click", function () { if (window.SITE_SIGN_OUT) window.SITE_SIGN_OUT(); });

    $("#delBtn").addEventListener("click", function () {
      var err = $("#delError"); show(err, "");
      api("DELETE", "/api/me", { confirm: $("#delConfirm").value.trim(), password: $("#delPw").value }).then(function (r) {
        if (!r.ok) return show(err, detail(r, "Could not delete the account."));
        try { localStorage.removeItem("ru_job_id"); localStorage.removeItem("ru_gemini_key"); } catch (e) {}
        location.replace("/");
      });
    });
  }
})();

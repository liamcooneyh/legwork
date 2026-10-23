// Legwork Fill: runs on Greenhouse, Lever and Ashby application pages in your own Chrome.
// If your dashboard has a prepared draft for this job it offers to fill the form; you review it
// and press Submit yourself. When the confirmation appears, the job is recorded as applied.
// It never presses Submit and never touches CAPTCHAs or security codes.
(() => {
  if (window.__autoApplyFill) return;
  window.__autoApplyFill = true;

  const send = (msg) => new Promise((resolve) => chrome.runtime.sendMessage(msg, (r) => resolve(r || { error: "no reply" })));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const SUCCESS = /thank you for applying|thanks for applying|application (has been |was )?(successfully )?(submitted|received)|(we.ve|we have)( successfully)? received your application|your application (has been|was) (received|submitted)/i;
  const STATES = { NY: "New York", MA: "Massachusetts", NJ: "New Jersey", CT: "Connecticut", CA: "California" };

  // ------------------------------------------------------------------ panel (isolated from the page's styles)
  let panel, body;
  function showPanel() {
    if (panel) return;
    const host = document.createElement("div");
    host.setAttribute("data-aa-skip", "1");
    host.style.cssText = "all:initial;position:fixed;left:16px;bottom:16px;z-index:2147483647";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>
      .card { all: initial; display: block; box-sizing: border-box; width: 340px; max-height: 60vh; overflow: auto; background: #23202a; color: #fff;
              font: 14px/1.45 -apple-system, "Segoe UI", sans-serif; padding: 12px 14px; border-radius: 14px; box-shadow: 0 8px 28px rgba(0,0,0,.35); }
      .top { display: flex; align-items: center; gap: 8px; } .title { font-weight: 700; flex: 1; }
      .dot { width: 9px; height: 9px; border-radius: 50%; background: #ff4f9d; }
      button { font: 600 13px/1 -apple-system, sans-serif; padding: 7px 11px; border-radius: 9px; border: 0; cursor: pointer; }
      .primary { background: #ff7ab6; color: #23202a; } .ghost { background: transparent; color: #cfc8da; border: 1px solid #4a4554; }
      .msg { margin-top: 6px; color: #e8e2ef; } .muted { color: #aaa4b4; font-size: 12.5px; }
      ul { margin: 6px 0 0; padding-left: 18px; } li { margin: 2px 0; }
      .good { color: #c0e68a; font-weight: 700; } .warn { color: #ffd873; }
      .row { display: flex; gap: 6px; margin-top: 10px; }
    </style><div class="card"><div class="top"><span class="dot"></span><span class="title">Legwork</span>
      <button class="ghost" id="hide">Hide</button></div><div id="body"></div></div>`;
    root.getElementById("hide").onclick = () => host.remove();
    body = root.getElementById("body");
    (document.body || document.documentElement).appendChild(host);
    panel = host;
  }
  function render(html) { showPanel(); body.innerHTML = html; return body; }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ------------------------------------------------------------------ filling helpers (real input events, so React forms notice)
  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  // Autocomplete boxes (Lever's location) only search on key presses.
  function typeValue(el, value) {
    setValue(el, value);
    const key = value.slice(-1) || " ";
    el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
  }
  const t0 = Date.now();
  const debug = (...a) => console.debug("[legwork]", ((Date.now() - t0) / 1000).toFixed(1) + "s", ...a);
  function blur(el) { el.dispatchEvent(new Event("blur", { bubbles: true })); el.blur && el.blur(); }
  function press(el) {
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    el.click();
  }
  const byKey = (key, opt) => document.querySelector(`[data-aa-key="${key}"]` + (opt != null ? `[data-aa-opt="${CSS.escape(opt)}"]` : ""));
  const visibleOptions = () => [...document.querySelectorAll("[role=option]")].filter((o) => o.offsetParent && o.innerText.trim());

  function bestPlace(value, options, loc) {
    const parts = value.split(",").map((p) => p.trim()).filter(Boolean);
    const city = norm(loc.city || parts[0] || "");
    const states = new Set([norm(loc.state || parts[1] || ""), norm(loc.state_name || STATES[(loc.state || "").toUpperCase()] || "")].filter(Boolean));
    const country = norm(loc.country || "");
    let best = null, bestScore = 0;
    for (const o of options) {
      const p = o.text.split(",").map(norm).filter(Boolean);
      if (!p.length) continue;
      let score = 0;
      if (city && (p[0] === city || p[0] === city + " city")) score += 3; else if (city && p[0].startsWith(city)) score += 1;
      if (p.slice(1).some((x) => states.has(x))) score += 1;
      if (country && p.slice(1).join(" ").includes(country)) score += 0.5;
      if (p.length >= 3) score += 0.5;
      if (score > bestScore) { best = o; bestScore = score; }
    }
    return bestScore >= 3 ? best : null;
  }
  function bestOption(value, options) {
    const v = norm(value);
    return options.find((o) => o.text === value) || options.find((o) => norm(o.text) === v) ||
      options.find((o) => norm(o.text).startsWith(v) || v.startsWith(norm(o.text))) || null;
  }
  const placeLike = (label) => /location|city|country|where|based|reside|live|address/i.test(label);

  async function fillCombobox(el, field, value, loc) {
    const queries = [value.slice(0, 60), value.split(",")[0].trim()];
    if (placeLike(field.label)) queries.push(loc.state_name || loc.state, loc.country);
    for (const q of [...new Set(queries.filter(Boolean))]) {
      press(el);
      setValue(el, "");
      typeValue(el, q);
      await sleep(1600);
      const options = visibleOptions().map((o) => ({ el: o, text: o.innerText.trim() }));
      if (!options.length) continue;
      const choice = (placeLike(field.label) && (options.find((o) => o.text === value) || bestPlace(value, options, loc) ||
                      options.find((o) => norm(o.text) === norm(q)))) || bestOption(value, options);
      if (choice) { press(choice.el); await sleep(500); blur(el); return; }
      if (!placeLike(field.label)) break;
    }
    document.activeElement && document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    throw new Error(`no option matched "${value}"`);
  }

  async function pickSuggestion(el, text, loc) {
    setValue(el, "");
    typeValue(el, text.split(",")[0].trim());
    await sleep(2000);
    const options = [...document.querySelectorAll("[role=option], .dropdown-location, .pac-item, [class*='suggestion' i] li")]
      .filter((o) => o.offsetParent && o.innerText.trim()).map((o) => ({ el: o, text: o.innerText.trim() }));
    if (!options.length) { setValue(el, text); return; }  // a plain text box after all
    const choice = bestPlace(text, options, loc) || bestOption(text, options) ||
      options.find((o) => norm(o.text).startsWith(norm(text.split(",")[0])));
    if (choice) press(choice.el); else setValue(el, text);
  }

  const ticks = (v) => { const t = norm(v); return !!t && !["no", "false", "0", "off", "unchecked", "not checked"].includes(t) && !/\b(not|don t|do not)\b/.test(t); };

  async function fillField(f, values, loc) {
    const el = byKey(f.key);
    if (!el) throw new Error("field disappeared");
    const v = values[0];
    if (["text", "email", "tel", "url", "number", "textarea", "date"].includes(f.type)) {
      if (f.type === "text" && /location|city/i.test(f.label)) await pickSuggestion(el, v, loc);
      else { setValue(el, f.max_length ? v.slice(0, f.max_length) : v); blur(el); }
    } else if (f.type === "select") {
      const opt = [...el.options].find((o) => o.text.trim() === v) || [...el.options].find((o) => norm(o.text) === norm(v));
      if (!opt) throw new Error(`no option "${v}"`);
      el.value = opt.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (f.widget === "buttons") {
      const b = byKey(f.key, v) || [...document.querySelectorAll(`button[data-aa-key="${f.key}"]`)].find((x) => norm(x.innerText) === norm(v));
      if (!b) throw new Error(`no button "${v}"`);
      if (b.getAttribute("aria-pressed") !== "true") press(b);
    } else if (f.type === "radio" || f.type === "checkbox_group") {
      for (const val of (f.type === "radio" ? [v] : values)) {
        const input = byKey(f.key, val) || [...document.querySelectorAll(`input[data-aa-key="${f.key}"]`)].find((x) => norm(x.getAttribute("data-aa-opt")) === norm(val));
        if (!input) throw new Error(`no option "${val}"`);
        if (!input.checked) input.click();
      }
    } else if (f.type === "checkbox") {
      if (ticks(v) && !el.checked) el.click();
    } else if (f.type === "combobox") {
      await fillCombobox(el, f, v, loc);
    }
  }

  async function attach(field, path, name) {
    const r = await send({ type: "file", path });
    if (r.error) throw new Error(r.error);
    const bytes = Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0));
    const file = new File([bytes], name, { type: r.type.split(";")[0] || "application/pdf" });
    const input = byKey(field.key);
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const fileRole = (f) => { const t = `${f.label} ${f.hint || ""}`.toLowerCase(); return /resume|cv\b|curriculum/.test(t) ? "resume" : /cover/.test(t) ? "cover_letter" : null; };
  const clearKeys = () => document.querySelectorAll("[data-aa-key]").forEach((e) => { e.removeAttribute("data-aa-key"); e.removeAttribute("data-aa-opt"); });

  // ------------------------------------------------------------------ the fill
  async function fill(draft) {
    debug("start fill");
    render(`<div class="msg">Filling in your answers… <span class="muted">(about a minute; keep this tab open)</span></div>`);
    await send({ type: "remember", jobId: draft.job.id, ats: draft.job.ats, formUrl: location.href.replace(/#.*$/, "") });
    watchForSubmit();
    const approved = {};
    for (const a of draft.answers) {
      if (["unanswered", "you_checked"].includes(a.source)) continue;
      const vals = (a.values && a.values.length ? a.values : (a.value ? [a.value] : [])).filter(Boolean);
      if (vals.length) approved[a.label] = vals;
    }
    const loc = draft.location || {};
    const problems = [];

    clearKeys();
    let fields = AA_EXTRACT(null);
    const files = fields.filter((f) => f.type === "file" && !/^(|autofill|upload file)$/i.test(f.label.trim()));
    let resumeField = files.find((f) => fileRole(f) === "resume") || (files.length === 1 ? files[0] : null);
    if (resumeField && draft.files.resume) {
      // Uploaders (Greenhouse's) finish loading a moment after the form appears; too early fails.
      await sleep(Math.max(0, 3000 - (Date.now() - t0)));
      const uploadError = () => /cannot read properties|upload (failed|error)|something went wrong/i.test(
        (resumeField && document.body.innerText.slice(0, 60000)) || "");
      for (let attempt = 0; attempt < 2; attempt++) {
        try { await attach(resumeField, draft.files.resume, draft.files.resume.split("/").pop()); }
        catch (e) { problems.push(`Resume: ${e.message}`); break; }
        await sleep(4000);  // let resume parsing finish before answering
        if (!uploadError()) break;
        debug("resume upload error; retrying");
        await sleep(2000);
        clearKeys();
        fields = AA_EXTRACT(null);
        resumeField = fields.find((f) => f.type === "file" && fileRole(f) === "resume") || resumeField;
      }
      if (uploadError()) problems.push("Resume: the site's uploader reported an error. Attach it by hand");
      clearKeys();
      fields = AA_EXTRACT(null);
    }
    const cover = fields.find((f) => f.type === "file" && fileRole(f) === "cover_letter");
    if (cover && draft.files.cover_letter) {
      try { await attach(cover, draft.files.cover_letter, draft.files.cover_letter.split("/").pop()); } catch (e) { problems.push(`Cover letter: ${e.message}`); }
    }

    let filled = 0;
    const unanswered = [];
    for (const f of fields) {
      if (f.type === "file") continue;
      const vals = approved[f.label];
      if (!vals) { if (f.required) unanswered.push(f.label); continue; }
      debug("fill", f.type, f.label.slice(0, 50));
      try { await fillField(f, vals, loc); filled++; }
      catch (e) { problems.push(`${f.label.slice(0, 70)}: ${e.message}`); }
      await sleep(350);  // one at a time: some forms save each answer as you go
    }
    await sleep(1200);
    // Read it back: anything required still empty is for you.
    const state = AA_STATE(fields.map((f) => f.key));
    const empty = fields.filter((f) => f.required && f.type !== "file" && state[f.key] === false).map((f) => f.label);
    const todo = [...new Set([...unanswered, ...empty])];
    await send({ type: "event", jobId: draft.job.id, event: "filled", detail: `${filled} filled, ${todo.length} for you` });
    render(`<div class="msg"><span class="good">Filled ${filled} answers.</span> Review everything, change anything you like,
        then press <b>Submit</b> yourself.</div>
      ${todo.length ? `<div class="msg warn">Still needs you:</div><ul>${todo.slice(0, 8).map((t) => `<li>${esc(t.slice(0, 90))}</li>`).join("")}</ul>` : ""}
      ${problems.length ? `<div class="msg warn">Couldn't fill:</div><ul>${problems.slice(0, 6).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      <div class="muted" style="margin-top:8px">If the site emails you a code, enter it here. When the confirmation appears, this job is marked applied.</div>`);
    watchForConfirmation(draft.job.id);
  }

  // Pressing Submit is noted, so a thank-you page that opens in a new tab, or on the company's own
  // site (where this extension doesn't run), still gets recorded: see background.js.
  let submitWatch = false;
  function watchForSubmit() {
    if (submitWatch) return;
    submitWatch = true;
    const note = () => send({ type: "submitting" });
    document.addEventListener("submit", note, true);
    document.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("button, input[type=submit], [role=button]");
      const text = b ? (b.innerText || b.value || b.getAttribute("aria-label") || "") : "";
      if (/submit|send application|^\s*apply\b/i.test(text)) note();
    }, true);
  }

  let watching = false;
  function watchForConfirmation(jobId) {
    if (watching) return;
    watching = true;
    const started = Date.now();
    const timer = setInterval(async () => {
      if (Date.now() - started > 3 * 3600 * 1000) { clearInterval(timer); return; }
      const text = (document.body && document.body.innerText || "").slice(0, 30000);
      if (SUCCESS.test(text) || /\/(confirmation|thanks)\b/.test(location.pathname)) {
        clearInterval(timer);
        const r = await send({ type: "event", jobId, event: "applied", detail: "You submitted it in Chrome; the confirmation appeared" });
        render(r.error ? `<div class="msg warn">Submitted, but couldn't tell the dashboard: ${esc(r.error)}. Mark it applied there.</div>`
                       : `<div class="msg good">Recorded as applied ✓</div><div class="muted">It's on your Applications page now.</div>`);
      }
    }, 2000);
  }

  async function waitForForm(ms) {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      if (document.querySelector("form input[type=file], form textarea, input[type=email], [data-field-path]")) return true;
      await sleep(500);
    }
    return false;
  }

  // ------------------------------------------------------------------ start
  async function start() {
    // After a submit the page may move to a confirmation URL: finish what this tab started.
    const pending = await send({ type: "pending" });
    if (pending && pending.jobId) { watchForConfirmation(pending.jobId); watchForSubmit(); }

    const auto = location.hash.includes("aa-fill");
    if (!(await waitForForm(auto ? 20000 : 8000))) return;
    debug("form found");
    const draft = await send({ type: "draft", url: location.href });
    if (draft.error) {
      if (auto) render(`<div class="msg warn">${esc(draft.error)}</div>`);
      return;
    }
    if (!draft.job) return;  // no prepared draft for this page
    const b = render(`<div class="msg"><b>${esc(draft.job.company)}</b>: ${esc(draft.job.title)}</div>
      <div class="muted">Your dashboard has a prepared draft for this application.</div>
      <div class="row"><button class="primary" id="fill">Fill this form</button></div>`);
    b.querySelector("#fill").onclick = () => fill(draft);
    if (auto) { history.replaceState(null, "", location.href.replace(/#aa-fill.*$/, "")); fill(draft); }
  }
  start();
})();

// Talks to your dashboard (with the password you saved in the extension's settings) on behalf
// of the application pages. Remembers which job each tab is filling, so the confirmation page
// that follows a submit can be recorded even after the page navigates.

async function settings() {
  const c = await chrome.storage.local.get(["url", "user", "password"]);
  return { url: (c.url || "").replace(/\/$/, ""), user: c.user || "admin", password: c.password || "" };
}

async function api(path, opts = {}) {
  const c = await settings();
  if (!c.url) throw new Error("Set your dashboard address in the extension's settings first");
  if (!c.password) throw new Error("Set your dashboard password in the extension's settings first");
  const headers = { ...(opts.headers || {}), Authorization: "Basic " + btoa(`${c.user}:${c.password}`) };
  const r = await fetch(c.url + path, { ...opts, headers });
  if (r.status === 401) throw new Error("The dashboard didn't accept the password in the extension's settings");
  if (!r.ok) throw new Error(`Dashboard error ${r.status}`);
  return r;
}

function toBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function handle(msg, sender) {
  const tabKey = sender && sender.tab ? `tab${sender.tab.id}` : null;
  switch (msg.type) {
    case "ping":
      await api("/api/draft?url=" + encodeURIComponent("https://example.com"));
      return { ok: true };
    case "draft": {
      const r = await api("/api/draft?url=" + encodeURIComponent(msg.url));
      const draft = await r.json();
      return draft;
    }
    case "file": {
      const r = await api(msg.path);
      return { b64: toBase64(await r.arrayBuffer()), type: r.headers.get("content-type") || "application/octet-stream" };
    }
    case "text": {
      const r = await api(msg.path);
      return { text: await r.text() };
    }
    case "remember":  // this tab is filling this job
      if (tabKey) await chrome.storage.session.set({ [tabKey]: { jobId: msg.jobId, ats: msg.ats, formUrl: msg.formUrl || "", at: Date.now() } });
      return { ok: true };
    case "submitting": {  // you pressed Submit in this tab
      if (!tabKey) return {};
      const got = (await chrome.storage.session.get(tabKey))[tabKey];
      if (got) await chrome.storage.session.set({ [tabKey]: { ...got, submittedAt: Date.now() } });
      return { ok: true };
    }
    case "pending": {
      if (!tabKey) return {};
      const got = await chrome.storage.session.get(tabKey);
      return got[tabKey] || {};
    }
    case "event": {
      await api(`/api/job/${encodeURIComponent(msg.jobId)}/helper`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: msg.event, detail: msg.detail || "", via: "extension" }),
      });
      if (msg.event === "applied" && tabKey) await chrome.storage.session.remove(tabKey);
      return { ok: true };
    }
  }
  return { error: "unknown request" };
}

// After you press Submit, the thank-you page can open in a new tab or on the company's own site,
// where the content script doesn't run. Either one, within a few minutes of Submit, counts as sent.
const AFTER_SUBMIT_MS = 5 * 60 * 1000;
const THANKS = /thank|confirm|success|submitted|received|applied|complete/i;

async function submittedIn(tabId) {
  const key = `tab${tabId}`;
  const p = (await chrome.storage.session.get(key))[key];
  return p && p.submittedAt && Date.now() - p.submittedAt < AFTER_SUBMIT_MS ? { key, p } : null;
}

async function recordFromNavigation(key, p, url, how) {
  await chrome.storage.session.remove(key);
  let where = url;
  try { where = new URL(url).host; } catch (e) {}
  const detail = THANKS.test(url) ? `You submitted it in Chrome; the thank-you page opened (${where})`
                                  : `You submitted it in Chrome; the page moved on to ${where} ${how}`;
  try {
    await api(`/api/job/${encodeURIComponent(p.jobId)}/helper`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: "applied", detail, via: "extension" }),
    });
  } catch (e) { console.warn("Auto-Apply: couldn't record the application", e); }
}

function sameForm(formUrl, url) {
  try { const a = new URL(formUrl), b = new URL(url); return a.origin === b.origin && a.pathname === b.pathname; }
  catch (e) { return false; }
}

chrome.webNavigation.onCreatedNavigationTarget.addListener(async (d) => {
  const hit = await submittedIn(d.sourceTabId);
  if (hit) await recordFromNavigation(hit.key, hit.p, d.url, "in a new tab");
});

chrome.webNavigation.onCommitted.addListener(async (d) => {
  if (d.frameId !== 0 || d.transitionType === "reload") return;
  const hit = await submittedIn(d.tabId);
  if (hit && !sameForm(hit.p.formUrl, d.url)) await recordFromNavigation(hit.key, hit.p, d.url, "after Submit");
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  handle(msg, sender).then(reply, (e) => reply({ error: String(e.message || e) }));
  return true;  // reply asynchronously
});

const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["url", "user", "password"]).then((c) => {
  $("url").value = c.url || "";
  $("user").value = c.user || "admin";
  $("password").value = c.password || "";
});
$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ url: $("url").value.trim().replace(/\/$/, ""), user: $("user").value.trim(), password: $("password").value });
  $("status").textContent = "Testing…";
  const r = await chrome.runtime.sendMessage({ type: "ping" });
  $("status").textContent = r && r.ok ? "Connected ✓" : `Not connected: ${(r && r.error) || "no reply"}`;
});

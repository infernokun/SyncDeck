// Ask Decky's frontend loader about SyncDeck and capture the console errors
// a plugin import produces. Those errors never reach the journal or the
// plugin log; this is the only way to see why a plugin is missing from the
// Decky list.
//
//   ssh -f -N -L 18080:127.0.0.1:8080 deck@<deck-ip>     # Steam's CEF debug port
//   node scripts/decky-console.mjs
//
// Needs Node >= 22 (built-in WebSocket). Steam must be in Gaming Mode.
const base = "http://127.0.0.1:18080";
const targets = await (await fetch(base + "/json")).json();
const shared = targets.find((t) => t.title === "SharedJSContext");
if (!shared) { console.log("no SharedJSContext; targets:", targets.map((t) => t.title)); process.exit(1); }
const ws = new WebSocket(shared.webSocketDebuggerUrl.replace("127.0.0.1:8080", "127.0.0.1:18080"));
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let id = 0; const pending = new Map(); const logs = [];
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  else if (msg.method === "Runtime.consoleAPICalled") logs.push(`[console.${msg.params.type}] ` + msg.params.args.map((a) => a.value ?? a.description ?? JSON.stringify(a)).join(" "));
  else if (msg.method === "Runtime.exceptionThrown") logs.push("[exception] " + (msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text));
  else if (msg.method === "Log.entryAdded") logs.push(`[log.${msg.params.entry.level}] ${msg.params.entry.text}`);
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expression) => {
  const res = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, timeout: 15000 });
  if (res.result?.exceptionDetails) return "EXCEPTION: " + (res.result.exceptionDetails.exception?.description ?? res.result.exceptionDetails.text);
  return res.result?.result?.value ?? res.result?.result?.description ?? JSON.stringify(res.result);
};
await send("Runtime.enable"); await send("Log.enable");

console.log("=== loader state ===");
console.log(await evaluate(`JSON.stringify({
  hasLoader: !!window.DeckyPluginLoader,
  loaderKeys: Object.keys(window.DeckyPluginLoader || {}).slice(0, 40),
  plugins: (window.DeckyPluginLoader?.plugins || []).map(p => p.name),
})`));

console.log("=== importPlugin('SyncDeck') ===");
console.log(await evaluate(`(async () => {
  try {
    const r = await window.DeckyPluginLoader.importPlugin("SyncDeck", "0.1.0");
    return "import resolved: " + JSON.stringify(r) + " | plugins now: " + window.DeckyPluginLoader.plugins.map(p => p.name).join(", ");
  } catch (e) { return "import threw: " + (e && (e.stack || e.message || String(e))); }
})()`));

await new Promise((r) => setTimeout(r, 1500));
console.log("=== console/log events during import ===");
console.log(logs.filter((l) => /syncdeck|error|exception|fail/i.test(l)).join("\n") || "(none matching)");
console.log("=== all events (last 25) ===");
console.log(logs.slice(-25).join("\n"));
ws.close();

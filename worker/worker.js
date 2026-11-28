// IG Viral Radar — Cloudflare Worker
// Receives Telegram messages instantly (webhook) and starts the GitHub workflow.
// Also runs on a timer (Cron Trigger) so scans happen on time.
//
// Settings (Worker → Settings → Variables and Secrets):
//   TG_TOKEN   (secret)  Telegram bot token from BotFather
//   GH_TOKEN   (secret)  GitHub fine-grained token with "Actions: Read and write" on the repo
//   CHAT_ID    (text)    your Telegram chat id
//   REPO       (text)    owner/repo, e.g. mostlyhumour123-hue/ig-viral-radar
//   MH_SYNC    (KV)      optional: MH Studio's sync KV namespace, enables /cmd for MH Studio
//
// Open https://<your-worker>.workers.dev/setup once to connect the bot to this Worker.

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

async function startWorkflow(env, commands, force) {
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/scan.yml/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "ig-viral-radar-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main", inputs: { commands, force } }),
  });
  return r.status; // 204 = started
}

const SLOW = ["scan", "add", "remove"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const secret = (await sha256hex(env.TG_TOKEN || "")).slice(0, 32);

    if (url.pathname === "/setup") {
      const hook = `${url.origin}/telegram`;
      const r = await tg(env, "setWebhook", { url: hook, secret_token: secret, allowed_updates: ["message"],
                                             drop_pending_updates: false });
      const gh = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/scan.yml`, {
        headers: { "Authorization": `Bearer ${env.GH_TOKEN}`, "User-Agent": "ig-viral-radar-worker",
                   "Accept": "application/vnd.github+json" } });
      return new Response(JSON.stringify({ telegram: await r.json(), github_access: gh.status === 200 ? "ok" : gh.status },
                                         null, 2), { headers: { "Content-Type": "application/json" } });
    }

    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return new Response("forbidden", { status: 403 });
      const update = await request.json();
      const msg = update.message;
      if (!msg || String(msg.chat?.id) !== String(env.CHAT_ID) || !msg.text) return new Response("ok");
      const cmd = msg.text.trim().split(/\s+/)[0].replace(/^\//, "").split("@")[0].toLowerCase();
      const status = await startWorkflow(env, JSON.stringify([{ chat: { id: msg.chat.id }, text: msg.text }]), false);
      if (status !== 204) {
        await tg(env, "sendMessage", { chat_id: msg.chat.id, text: `⚠️ Couldn't start the scanner (GitHub said ${status}).` });
      } else if (SLOW.includes(cmd)) {
        await tg(env, "sendMessage", { chat_id: msg.chat.id,
          text: cmd === "scan" ? "🔎 Scan started — results in about 2 minutes."
                               : "⏳ Got it — updating the list and scanning (about 2 minutes)." });
      } else {
        await tg(env, "sendChatAction", { chat_id: msg.chat.id, action: "typing" });
      }
      return new Response("ok");
    }

    // MH Studio (Finder → Instagram radar → Accounts): add/remove/assign accounts. Allowed only with the id of an
    // existing MH Studio cloud-settings item (= someone who knows your MH Studio cloud password).
    if (url.pathname === "/cmd") {
      const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
                     "Access-Control-Allow-Headers": "Content-Type, X-Sync-Id" };
      const J = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { ...cors, "Content-Type": "application/json" } });
      if (request.method === "OPTIONS") return new Response(null, { headers: cors });
      if (request.method !== "POST") return J({ ok: false, error: "POST only" }, 405);
      const id = request.headers.get("X-Sync-Id") || "";
      if (!env.MH_SYNC || !/^[a-f0-9]{64}$/.test(id) || !(await env.MH_SYNC.get(id + ":t"))) return J({ ok: false, error: "locked" }, 403);
      const body = await request.json().catch(() => ({}));
      const lines = (Array.isArray(body.commands) ? body.commands : [body.text]).map(x => String(x || "").trim()).filter(Boolean).slice(0, 30);
      if (!lines.length || lines.some(t => t.length > 300 || !/^\/(add|remove|assign|unassign|list|scan)(\s|$)/i.test(t)))
        return J({ ok: false, error: "only /add /remove /assign /unassign /list /scan" }, 400);
      const status = await startWorkflow(env, JSON.stringify(lines.map(text => ({ chat: { id: env.CHAT_ID }, text }))), false);
      return J({ ok: status === 204, status });
    }

    return new Response("IG Viral Radar worker is running.");
  },

  // Cron Trigger: keep scans on schedule (the workflow decides if a scan is due).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(startWorkflow(env, "", false));
  },
};

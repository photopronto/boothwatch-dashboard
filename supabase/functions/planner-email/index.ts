// Planner: turn an email into tasks using Claude.
// Two ways in:
//  1. From the app ("From email" button): signed-in request with the pasted email -> returns tasks to review.
//  2. Forwarded email: an inbound email service posts to ?token=<your secret token> -> tasks are added straight away
//     and a phone notification says what was added. Accepts Resend, Postmark, Mailgun, CloudMailin or plain {from,subject,text}.
import webpush from "npm:web-push@3.6.7";
import { createClient } from "npm:@supabase/supabase-js@2";

const SITE = "https://dashboard.photopronto.co.uk/planner/";
const MODEL = Deno.env.get("PLANNER_MODEL") || "claude-haiku-5-5";
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), { status, headers: { ...CORS, "Content-Type": "application/json" } });

function londonToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
const stripHtml = (h: string) => h.replace(/<style[\s\S]*?<\/style>/gi, "").replace(/<br\s*\/?>/gi, "\n").replace(/<\/p>/gi, "\n").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n");

async function readInbound(req: Request): Promise<string> {
  const ct = req.headers.get("content-type") || "";
  let b: Record<string, any> = {};
  if (ct.includes("application/json")) b = await req.json();
  else { const fd = await req.formData(); fd.forEach((v, k) => { if (typeof v === "string") b[k] = v; }); }
  // Resend only sends a notice; fetch the email itself with the Resend API key
  if (b.type === "email.received" && b.data?.email_id) {
    const rk = Deno.env.get("RESEND_API_KEY");
    if (!rk) throw new Error("RESEND_API_KEY is not set");
    const r = await fetch(`https://api.resend.com/emails/receiving/${b.data.email_id}`, { headers: { Authorization: `Bearer ${rk}` } });
    if (!r.ok) throw new Error(`Resend ${r.status}: ${(await r.text()).slice(0, 200)}`);
    const e = await r.json();
    b = { from: e.from || b.data.from, subject: e.subject || b.data.subject, text: e.text || "", html: e.html || "" };
  }
  const h = b.headers || {};
  const from = b.From || b.from || b.sender || h.from || h.From || b.envelope?.from || "";
  const subject = b.Subject || b.subject || h.subject || h.Subject || "";
  let text = b.TextBody || b["body-plain"] || b.plain || b.text || b["stripped-text"] || "";
  if (!text) text = stripHtml(b.HtmlBody || b["body-html"] || b.html || "");
  return `From: ${typeof from === "string" ? from : JSON.stringify(from)}\nSubject: ${subject}\n\n${text}`;
}

async function askClaude(email: string, clients: Record<string, string>, projects: Record<string, string>) {
  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) throw new Error("No Claude API key set yet");
  const today = londonToday();
  const weekday = new Date(today + "T12:00:00Z").toLocaleDateString("en-GB", { weekday: "long" });
  const prompt = `You turn an email into to-do tasks for Matthew, a UK freelancer. Today is ${weekday} ${today}.
His areas:
- "freelance": design and photography freelance work for clients
- "pronto": Photo Pronto, his photobooth business (permanent installs, hello@photopronto.co.uk)
- "home": personal and household
Known clients (id = name): ${Object.entries(clients).map(([k, v]) => `${k} = ${v}`).join("; ") || "none"}
Active projects (id = title): ${Object.entries(projects).map(([k, v]) => `${k} = ${v}`).join("; ") || "none"}

The email may have been forwarded by Matthew; the real sender is usually inside the forwarded text.
List only the concrete actions Matthew needs to take. Reply with JSON only, exactly this shape:
{"tasks":[{"title":"short imperative task","area":"freelance|pronto|home","client":"client id or empty string","projectId":"project id or empty string","estimate":minutes (15,30,60,120,240 or 420),"deadline":"YYYY-MM-DD or empty string","priority":"high|normal|low","notes":"one short line: who it's from and the key detail"}]}
Resolve relative dates ("Friday", "end of month") against today. If there is nothing to do, reply {"tasks":[]}.

EMAIL:
"""
${email.slice(0, 15000)}
"""`;
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({ model: MODEL, max_tokens: 2000, messages: [{ role: "user", content: prompt }] }),
  });
  if (!r.ok) throw new Error(`Claude API ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const out = await r.json();
  const text = (out.content || []).map((c: any) => c.text || "").join("");
  const m = text.match(/\{[\s\S]*\}/);
  const parsed = JSON.parse(m ? m[0] : text);
  const areas = ["freelance", "pronto", "home"];
  return ((parsed.tasks || []) as any[]).filter((t) => t && t.title).map((t) => ({
    title: String(t.title).slice(0, 200),
    area: areas.includes(t.area) ? t.area : "home",
    client: clients[t.client] ? t.client : "",
    projectId: projects[t.projectId] ? t.projectId : "",
    estimate: Number(t.estimate) || 30,
    deadline: /^\d{4}-\d{2}-\d{2}$/.test(t.deadline || "") ? t.deadline : "",
    priority: ["high", "normal", "low"].includes(t.priority) ? t.priority : "normal",
    notes: String(t.notes || "").slice(0, 300),
  }));
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const token = new URL(req.url).searchParams.get("token");

  let owner: string | null = null, email = "", fromApp = false;
  try {
    if (token) {
      const { data } = await admin.from("planner_inbox").select("owner_id").eq("token", token).maybeSingle();
      if (!data) return json({ error: "Unknown address" }, 404);
      owner = data.owner_id;
      email = await readInbound(req);
    } else {
      const jwt = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
      const { data } = await admin.auth.getUser(jwt);
      if (!data?.user) return json({ error: "Please sign in" }, 401);
      owner = data.user.id; fromApp = true;
      const b = await req.json();
      email = String(b.text || "");
    }
    if (!email.trim()) return json({ tasks: [] });

    const { data: docs } = await admin.from("planner_docs").select("col, id, data").eq("owner_id", owner).in("col", ["clients", "projects"]);
    const clients = Object.fromEntries((docs || []).filter((d) => d.col === "clients").map((d) => [d.id, d.data?.name || d.id]));
    const projects = Object.fromEntries((docs || []).filter((d) => d.col === "projects" && d.data?.status !== "done").map((d) => [d.id, d.data?.title || d.id]));
    const tasks = await askClaude(email, clients, projects);
    if (fromApp) return json({ tasks });

    // Forwarded email: add the tasks now (the app schedules them next time it opens)
    const now = new Date().toISOString();
    const rows = tasks.map((t, i) => ({
      owner_id: owner, col: "tasks", id: Date.now().toString(36) + i + Math.random().toString(36).slice(2, 6),
      data: { ...t, status: "todo", bumps: 0, scheduled: null, pinned: false, created: now, fromEmail: true, order: 0, notBefore: "", recur: "", listId: "" },
    }));
    if (rows.length) await admin.from("planner_docs").insert(rows);

    const { data: subs } = await admin.from("planner_push_subs").select("id, endpoint, p256dh, auth").eq("owner_id", owner);
    if (subs?.length) {
      webpush.setVapidDetails(SITE, Deno.env.get("VAPID_PUBLIC_KEY")!, Deno.env.get("VAPID_PRIVATE_KEY")!);
      const payload = JSON.stringify({
        title: rows.length ? `Added ${rows.length} task${rows.length > 1 ? "s" : ""} from email` : "Email read: nothing to do",
        body: tasks.map((t) => t.title).join(" · ").slice(0, 180) || "No actions found in that email.",
        tag: "planner-email-" + Date.now(), url: SITE,
      });
      await Promise.all(subs.map((s) => webpush.sendNotification({ endpoint: s.endpoint, keys: { p256dh: s.p256dh, auth: s.auth } }, payload, { TTL: 3600 }).catch(() => {})));
    }
    return json({ added: rows.length });
  } catch (e) {
    console.error(e);
    return json({ error: (e as Error).message || "Something went wrong" }, 500);
  }
});

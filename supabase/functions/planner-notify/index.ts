// Planner reminders. Runs every 15 minutes (cron job "planner_reminders").
// Sends the morning plan and the evening check-in to phones that turned on planner notifications.
import webpush from "npm:web-push@3.6.7";
import { createClient } from "npm:@supabase/supabase-js@2";

const SITE = "https://dashboard.photopronto.co.uk/planner/";

function londonNow() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false, weekday: "short",
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  const dows: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  return { date: `${parts.year}-${parts.month}-${parts.day}`, hour: Number(parts.hour) % 24, dow: dows[parts.weekday] };
}
const fmtMins = (m: number) => { m = Math.round(m || 0); if (m < 60) return m + "m"; const h = Math.floor(m / 60), r = m % 60; return r ? `${h}h ${r}m` : `${h}h`; };

Deno.serve(async (req) => {
  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info", "Access-Control-Allow-Methods": "POST, OPTIONS" };
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const key = req.headers.get("x-cron-key") || "";
  const { data: ok } = key ? await admin.rpc("planner_cron_ok", { k: key }) : { data: false };
  // Without the job's key, only a signed-in "send a test" to yourself is allowed
  let onlyOwner: string | null = null;
  if (!ok) {
    const jwt = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    const { data } = jwt ? await admin.auth.getUser(jwt) : { data: null };
    if (!data?.user || !new URL(req.url).searchParams.get("test")) return new Response("Not allowed", { status: 401, headers: CORS });
    onlyOwner = data.user.id;
  }

  webpush.setVapidDetails(SITE, Deno.env.get("VAPID_PUBLIC_KEY")!, Deno.env.get("VAPID_PRIVATE_KEY")!);
  const now = londonNow();
  const force = new URL(req.url).searchParams.get("test"); // "morning" | "checkin" to send now (testing)

  const { data: subs } = await admin.from("planner_push_subs").select("id, owner_id, endpoint, p256dh, auth");
  const owners = [...new Set((subs || []).map((s) => s.owner_id))].filter((o) => !onlyOwner || o === onlyOwner);
  const results: unknown[] = [];

  for (const owner of owners) {
    const { data: docs } = await admin.from("planner_docs").select("col, id, data").eq("owner_id", owner).in("col", ["config", "tasks", "worklog", "clients"]);
    const settings = (docs || []).find((d) => d.col === "config" && d.id === "settings")?.data || {};
    const tasks = (docs || []).filter((d) => d.col === "tasks").map((d) => ({ id: d.id, ...d.data }));
    const worklog = new Set((docs || []).filter((d) => d.col === "worklog").map((d) => d.id));
    const clients = Object.fromEntries((docs || []).filter((d) => d.col === "clients").map((d) => [d.id, d.data?.name || d.id]));
    const checkinHour = Number(settings.checkinHour ?? 17);
    const morningHour = Number(settings.morningHour ?? 8);
    const open = tasks.filter((t) => t.status !== "done");

    const { data: logRows } = await admin.from("planner_notify_log").select("kind").eq("owner_id", owner).eq("day", now.date);
    const sentToday = new Set((logRows || []).map((r) => r.kind));
    const messages: { kind: string; title: string; body: string; hash: string }[] = [];

    // Morning plan
    if (force === "morning" || (morningHour > 0 && now.hour >= morningHour && now.hour < 12 && !sentToday.has("morning"))) {
      const todays = open.filter((t) => t.scheduled === now.date);
      const waiting = open.filter((t) => t.scheduled && t.scheduled < now.date).length;
      const fresh = open.filter((t) => !t.scheduled && t.fromEmail).length;
      if (todays.length || waiting || fresh || force) {
        const mins = todays.reduce((s, t) => s + (Number(t.estimate) || 30), 0);
        const first = todays.slice(0, 3).map((t) => t.title).join(", ");
        const bits = [todays.length ? `${todays.length} task${todays.length > 1 ? "s" : ""} · ${fmtMins(mins)} planned${first ? ". " + first : ""}` : "Nothing planned today"];
        if (waiting) bits.push(`${waiting} still to check in from before`);
        if (fresh) bits.push(`${fresh} new from email`);
        messages.push({ kind: "morning", title: "Today's plan", body: bits.join(" · "), hash: "" });
      }
    }

    // Evening check-in
    if (force === "checkin" || (now.hour >= checkinHour && !sentToday.has("checkin"))) {
      const due = open.filter((t) => t.scheduled && t.scheduled <= now.date).length;
      const slots = (settings.days?.[String(now.dow)] || []) as { client?: string }[];
      const days = [...new Set(slots.filter((s) => s.client).map((s) => s.client as string))].filter((c) => !worklog.has(`${now.date}_${c}`));
      if (due || days.length || force) {
        const bits = [];
        if (due) bits.push(`${due} task${due > 1 ? "s" : ""} to confirm`);
        if (days.length) bits.push(`log your ${days.map((c) => clients[c] || c).join(" & ")} day`);
        messages.push({ kind: "checkin", title: "Check-in time", body: (bits.join(" · ") || "Nothing to confirm") + ". Done, or move it?", hash: "#checkin" });
      }
    }

    for (const m of messages) {
      const payload = JSON.stringify({ title: m.title, body: m.body, tag: "planner-" + m.kind, url: SITE + m.hash });
      const gone: number[] = [];
      let sent = 0;
      await Promise.all((subs || []).filter((s) => s.owner_id === owner).map(async (s) => {
        try { await webpush.sendNotification({ endpoint: s.endpoint, keys: { p256dh: s.p256dh, auth: s.auth } }, payload, { TTL: 3600, urgency: "normal" }); sent++; }
        catch (e) { if (e && ((e as any).statusCode === 404 || (e as any).statusCode === 410)) gone.push(s.id); }
      }));
      if (gone.length) await admin.from("planner_push_subs").delete().in("id", gone);
      if (!force) await admin.from("planner_notify_log").upsert({ owner_id: owner, kind: m.kind, day: now.date });
      results.push({ kind: m.kind, sent });
    }
  }
  return new Response(JSON.stringify({ now, results }), { headers: { ...CORS, "Content-Type": "application/json" } });
});

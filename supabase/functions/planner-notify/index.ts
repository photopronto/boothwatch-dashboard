// Planner reminders. Runs every 15 minutes (cron job "planner_reminders").
// - Each morning: carries over tasks you didn't tick, adds month-end invoice and overdue-chase tasks,
//   and sends "Today's plan" (with anything due today/tomorrow).
// - Evening check-in, Sunday "plan next week", and "Remind me at…" reminders on individual tasks.
import webpush from "npm:web-push@3.6.7";
import { createClient } from "npm:@supabase/supabase-js@2";

const SITE = "https://dashboard.photopronto.co.uk/planner/";

function londonNow() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, weekday: "short",
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  const dows: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  const hour = Number(parts.hour) % 24;
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  return { date, hour, dow: dows[parts.weekday], stamp: `${date}T${String(hour).padStart(2, "0")}:${parts.minute}` };
}
const fmtMins = (m: number) => { m = Math.round(m || 0); if (m < 60) return m + "m"; const h = Math.floor(m / 60), r = m % 60; return r ? `${h}h ${r}m` : `${h}h`; };
const addDays = (date: string, n: number) => { const d = new Date(date + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const dowOf = (date: string) => new Date(date + "T12:00:00Z").getUTCDay();
const monday = (date: string) => addDays(date, -((dowOf(date) + 6) % 7));
const money = (n: number) => "£" + (Number(n) || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const short = (s: string, n = 40) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

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
  const force = new URL(req.url).searchParams.get("test"); // "morning" | "checkin" | "week" to send now (testing)

  const { data: subs } = await admin.from("planner_push_subs").select("id, owner_id, endpoint, p256dh, auth");
  const { data: ownerRows } = await admin.from("planner_docs").select("owner_id").eq("col", "config").eq("id", "settings");
  const owners = [...new Set([...(ownerRows || []).map((r) => r.owner_id), ...(subs || []).map((s) => s.owner_id)])].filter((o) => !onlyOwner || o === onlyOwner);
  const results: unknown[] = [];

  for (const owner of owners) {
    const { data: docs } = await admin.from("planner_docs").select("col, id, data").eq("owner_id", owner).in("col", ["config", "tasks", "worklog", "clients", "weeks", "invoices"]);
    const settings = (docs || []).find((d) => d.col === "config" && d.id === "settings")?.data || {};
    let tasks = (docs || []).filter((d) => d.col === "tasks").map((d) => ({ id: d.id, ...d.data }));
    const worklog = Object.fromEntries((docs || []).filter((d) => d.col === "worklog").map((d) => [d.id, d.data]));
    const clientDocs = Object.fromEntries((docs || []).filter((d) => d.col === "clients").map((d) => [d.id, d.data]));
    const clients = Object.fromEntries(Object.entries(clientDocs).map(([k, v]: [string, any]) => [k, v?.name || k]));
    const weeks = Object.fromEntries((docs || []).filter((d) => d.col === "weeks").map((d) => [d.id, d.data]));
    const invoices = (docs || []).filter((d) => d.col === "invoices").map((d) => ({ id: d.id, ...d.data }));
    const dayType = (date: string) => weeks[monday(date)]?.days?.[String(dowOf(date))] || settings.usual?.[String(dowOf(date))] || "off";
    const weekPlanHour = Number(settings.weekPlanHour ?? 18);
    const checkinHour = Number(settings.checkinHour ?? 17);
    const morningHour = Number(settings.morningHour ?? 8);
    const al = { morning: true, checkin: true, weekplan: true, weekPlanDay: 0, deadlines: "both", reminders: true, carryOver: true, invoiceTasks: true, chase: true, chaseAfterDays: 0, ...(settings.alerts || {}) };
    const patch = (id: string, p: Record<string, unknown>) => admin.rpc("planner_patch_doc", { p_owner: owner, p_col: "tasks", p_id: id, p_patch: p });

    const { data: logRows } = await admin.from("planner_notify_log").select("kind").eq("owner_id", owner).eq("day", now.date);
    const sentToday = new Set((logRows || []).map((r) => r.kind));
    const messages: { kind: string; title: string; body: string; hash: string; tag?: string; log?: boolean }[] = [];
    const created: string[] = [];
    let carried: string[] = [];

    // ---- Daily housekeeping (once a day, from 6am)
    if (!force && now.hour >= 6 && !sentToday.has("daily")) {
      // 1. Anything not ticked before today moves to the next suitable slot (the app re-slots it)
      const stale = al.carryOver === false ? [] : tasks.filter((t) => t.status !== "done" && t.scheduled && t.scheduled < now.date);
      for (const t of stale) {
        await patch(t.id, { scheduled: null, pinned: false, bumps: (Number(t.bumps) || 0) + 1, carried: now.date });
        t.scheduled = null;
      }
      carried = stale.map((t) => t.title);

      const newTask = async (id: string, data: Record<string, unknown>) => {
        if (tasks.some((t) => t.id === id)) return;
        const row = { status: "todo", bumps: 0, scheduled: null, pinned: false, created: new Date().toISOString(), area: "freelance", client: "", size: "quick", estimate: 15, priority: "high", notBefore: "", recur: "", listId: "", projectId: "", order: 0, ...data };
        const { error } = await admin.from("planner_docs").insert({ owner_id: owner, col: "tasks", id, data: row });
        if (!error) { tasks.push({ id, ...row }); created.push(String(data.title)); }
      };

      // 2. Last working day of the month: invoice each workplace you worked for
      const ym = now.date.slice(0, 7);
      const [yy, mm] = ym.split("-").map(Number);
      let lastWork = new Date(Date.UTC(yy, mm, 0)).toISOString().slice(0, 10); // last day of this month
      while (dowOf(lastWork) === 0 || dowOf(lastWork) === 6) lastWork = addDays(lastWork, -1);
      if (al.invoiceTasks !== false && now.date === lastWork) {
        for (const [cid, c] of Object.entries(clientDocs) as [string, any][]) {
          if (!c?.workplace) continue;
          let days = 0;
          for (let d = ym + "-01"; d.slice(0, 7) === ym; d = addDays(d, 1)) if (dayType(d) === cid && worklog[`${d}_${cid}`]?.units !== 0) days++;
          if (!days || invoices.some((i) => i.clientId === cid && i.month === ym)) continue;
          const monthName = new Date(ym + "-15T12:00:00Z").toLocaleDateString("en-GB", { month: "long" });
          await newTask(`inv-${cid}-${ym}`, { title: `Send ${c.name} invoice for ${monthName}`, deadline: addDays(now.date, 1), notes: `About ${days} day${days > 1 ? "s" : ""} worked. Tap "Create invoice" to fill it in.`, action: { type: "invoice", client: cid, month: ym } });
        }
      }

      // 3. Overdue invoices: add a chase task (once per invoice)
      for (const inv of al.chase === false ? [] : invoices) {
        if (inv.status !== "sent" || !inv.due || addDays(inv.due, Number(al.chaseAfterDays) || 0) >= now.date) continue;
        const total = (inv.lines || []).reduce((s: number, l: any) => s + (Number(l.qty) || 0) * (Number(l.rate) || 0), 0);
        await newTask(`chase-${inv.id}`, { title: `Chase ${inv.client?.name || clients[inv.clientId] || "client"} for ${inv.number} (${money(total)})`, notes: `Was due ${inv.due}.`, action: { type: "chase", invoice: inv.id } });
      }
      await admin.from("planner_notify_log").upsert({ owner_id: owner, kind: "daily", day: now.date });
    }

    const open = tasks.filter((t) => t.status !== "done");
    const effDeadline = (t: any) => t.deadline || "";

    // ---- Morning plan
    if (force === "morning" || (!force && al.morning !== false && morningHour > 0 && now.hour >= morningHour && now.hour < 12 && !sentToday.has("morning"))) {
      const todays = open.filter((t) => t.scheduled === now.date);
      const dueToday = al.deadlines === "off" ? [] : open.filter((t) => effDeadline(t) === now.date);
      const dueTomorrow = al.deadlines === "both" ? open.filter((t) => effDeadline(t) === addDays(now.date, 1)) : [];
      const fresh = open.filter((t) => !t.scheduled && t.fromEmail && !t.carried).length;
      const mins = todays.reduce((s, t) => s + (Number(t.estimate) || 30), 0);
      const bits = [todays.length ? `${todays.length} task${todays.length > 1 ? "s" : ""} · ${fmtMins(mins)}` : "Nothing planned yet"];
      if (dueToday.length) bits.push(`Due today: ${dueToday.map((t) => short(t.title)).join(", ")}`);
      if (dueTomorrow.length) bits.push(`Due tomorrow: ${dueTomorrow.map((t) => short(t.title)).join(", ")}`);
      if (carried.length) bits.push(`Not ticked yesterday, carried over: ${carried.map((t) => short(t, 30)).join(", ")}`);
      if (created.length) bits.push(`New: ${created.join(", ")}`);
      if (fresh) bits.push(`${fresh} new from email`);
      if (todays.length || dueToday.length || dueTomorrow.length || carried.length || created.length || fresh || force === "morning") {
        messages.push({ kind: "morning", title: "Today's plan", body: bits.join(" · "), hash: "", log: true });
      }
    }

    // ---- Evening check-in
    if (force === "checkin" || (!force && al.checkin !== false && now.hour >= checkinHour && !sentToday.has("checkin"))) {
      const due = open.filter((t) => t.scheduled && t.scheduled <= now.date).length;
      const t = dayType(now.date);
      const days = (t !== "free" && t !== "off" && clients[t] && !worklog[`${now.date}_${t}`]) ? [t] : [];
      if (due || days.length || force === "checkin") {
        const bits = [];
        if (due) bits.push(`${due} task${due > 1 ? "s" : ""} to tick off`);
        if (days.length) bits.push(`log your ${days.map((c) => clients[c] || c).join(" & ")} day`);
        messages.push({ kind: "checkin", title: "Check-in time", body: (bits.join(" · ") || "Nothing to confirm") + ". Anything not ticked moves to your next slot tomorrow.", hash: "#checkin", log: true });
      }
    }

    // ---- Sunday evening: where are you working next week?
    const nextMon = addDays(monday(now.date), 7);
    if (force === "week" || (!force && al.weekplan !== false && now.dow === Number(al.weekPlanDay ?? 0) && weekPlanHour > 0 && now.hour >= weekPlanHour && !sentToday.has("weekplan") && !weeks[nextMon])) {
      messages.push({ kind: "weekplan", title: "Plan next week", body: "Where are you working next week? Tap to set your days.", hash: "#plan-week", log: true });
    }

    // ---- "Remind me at…" on individual tasks
    if (!force && al.reminders !== false) {
      for (const t of open) {
        if (!t.remindAt || t.remindSent || String(t.remindAt) > now.stamp) continue;
        messages.push({ kind: "remind", title: "Reminder", body: t.title + (t.deadline ? ` (due ${t.deadline})` : ""), hash: "", tag: "planner-remind-" + t.id });
        await patch(t.id, { remindSent: true });
      }
    }

    const mySubs = (subs || []).filter((s) => s.owner_id === owner);
    for (const m of messages) {
      const payload = JSON.stringify({ title: m.title, body: m.body, tag: m.tag || "planner-" + m.kind, url: SITE + m.hash });
      const gone: number[] = [];
      let sent = 0;
      await Promise.all(mySubs.map(async (s) => {
        try { await webpush.sendNotification({ endpoint: s.endpoint, keys: { p256dh: s.p256dh, auth: s.auth } }, payload, { TTL: 3600, urgency: "normal" }); sent++; }
        catch (e) { if (e && ((e as any).statusCode === 404 || (e as any).statusCode === 410)) gone.push(s.id); }
      }));
      if (gone.length) await admin.from("planner_push_subs").delete().in("id", gone);
      if (!force && m.log) await admin.from("planner_notify_log").upsert({ owner_id: owner, kind: m.kind, day: now.date });
      results.push({ kind: m.kind, sent });
    }
    if (carried.length || created.length) results.push({ carried: carried.length, created });
  }
  return new Response(JSON.stringify({ now, results }), { headers: { ...CORS, "Content-Type": "application/json" } });
});

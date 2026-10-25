// Giant strip kiosk: booth sessions in the cloud.
//
//   Booth agent  (header x-agent-token)
//     {action:"begin",  session_key, taken_at, event_name, info, files:[{name, kind:"print"|"photo"}]}
//         -> {skip:true} if this booth isn't sending to the kiosk, {done:true} if already uploaded,
//            otherwise {uploads:[{name, url}]}  (PUT each file to its url)
//     {action:"finish", session_key}  -> {ok:true} once every file is there
//
//   Kiosk  (header x-kiosk-token)
//     {action:"list", after}  -> sessions that became ready after `after` (oldest first, max 100),
//                                each with short-lived download links, and `pending`: how many
//                                sessions booths started uploading in the last 5 minutes
//     {action:"get", ids:[...]} -> those sessions again (fresh links), to repair missing files
//     {action:"heartbeat", programs, status, version, days:[{day, sold, printed}], price, events}
//         -> shows the kiosk on the BoothWatch dashboard (alerts work as for a booth)
//
//   Daily cleanup (header x-cleanup-key, from a scheduled database job)
//     {action:"cleanup"}  -> removes sessions older than app_settings.kiosk_retention_days

import { createClient } from "npm:@supabase/supabase-js@2";

const BUCKET = "kiosk-sessions";
const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_NAME = /^[A-Za-z0-9_.-]{1,80}$/;

function folderFor(boothId: string, key: string) {
  return `${boothId}/${key.replace(/[^A-Za-z0-9_\-/]/g, "_").replace(/\/+/g, "/").slice(0, 120)}`;
}

async function retentionDays(): Promise<number> {
  const { data } = await db.from("app_settings").select("value").eq("key", "kiosk_retention_days").maybeSingle();
  const n = Number(data?.value);
  return Number.isFinite(n) && n >= 1 ? Math.min(n, 365) : 30;
}

async function agentBooth(req: Request) {
  const t = req.headers.get("x-agent-token") ?? "";
  if (!UUID.test(t)) return null;
  const { data } = await db.from("booths").select("id, name, config").eq("agent_token", t).maybeSingle();
  return data;
}

async function kiosk(req: Request) {
  const t = req.headers.get("x-kiosk-token") ?? "";
  if (!UUID.test(t)) return null;
  const { data } = await db.from("kiosks").select("id, name").eq("token", t).maybeSingle();
  return data;
}

async function begin(booth: any, body: any) {
  if (booth.config?.kiosk_upload !== true) return json({ skip: true });
  const key = String(body.session_key ?? "");
  const taken = new Date(body.taken_at);
  const files = Array.isArray(body.files) ? body.files : [];
  if (!key || key.length > 120 || isNaN(taken.getTime())) return json({ error: "bad session" }, 400);
  if (!files.length || files.length > 12) return json({ error: "bad files" }, 400);
  for (const f of files) {
    if (!SAFE_NAME.test(String(f?.name)) || !["print", "photo"].includes(f?.kind)) {
      return json({ error: "bad file name" }, 400);
    }
  }
  const days = await retentionDays();
  if (taken.getTime() < Date.now() - days * 86400000) return json({ skip: true, reason: "too old" });

  const { data: existing } = await db.from("kiosk_sessions").select("id, status")
    .eq("booth_id", booth.id).eq("session_key", key).maybeSingle();
  if (existing?.status === "ready") return json({ done: true });

  const folder = folderFor(booth.id, key);
  const row = {
    booth_id: booth.id, session_key: key, taken_at: taken.toISOString(),
    event_name: body.event_name ? String(body.event_name).slice(0, 200) : null,
    folder,
    print_file: files.find((f: any) => f.kind === "print")?.name ?? null,
    photo_files: files.filter((f: any) => f.kind === "photo").map((f: any) => f.name),
    info: typeof body.info === "object" && body.info ? body.info : {},
    status: "uploading",
  };
  const { error } = await db.from("kiosk_sessions").upsert(row, { onConflict: "booth_id,session_key" });
  if (error) return json({ error: error.message }, 500);

  const uploads = [];
  for (const f of files) {
    const { data, error: ue } = await db.storage.from(BUCKET).createSignedUploadUrl(`${folder}/${f.name}`, { upsert: true });
    if (ue) return json({ error: ue.message }, 500);
    uploads.push({ name: f.name, url: data.signedUrl });
  }
  return json({ uploads });
}

async function finish(booth: any, body: any) {
  const key = String(body.session_key ?? "");
  const { data: s } = await db.from("kiosk_sessions").select("*")
    .eq("booth_id", booth.id).eq("session_key", key).maybeSingle();
  if (!s) return json({ error: "unknown session" }, 404);
  const { data: objs, error } = await db.storage.from(BUCKET).list(s.folder, { limit: 100 });
  if (error) return json({ error: error.message }, 500);
  const have = new Set((objs ?? []).map((o: any) => o.name));
  const want = [s.print_file, ...s.photo_files].filter(Boolean);
  const missing = want.filter((n: string) => !have.has(n));
  if (missing.length) return json({ error: "missing files", missing }, 409);
  await db.from("kiosk_sessions").update({ status: "ready", ready_at: new Date().toISOString() }).eq("id", s.id);
  return json({ ok: true });
}

const SESSION_COLS = "id, session_key, taken_at, event_name, folder, print_file, photo_files, info, ready_at, booth_id, booths(name)";

async function withLinks(rows: any[]) {
  const paths = (rows ?? []).flatMap((r: any) =>
    [r.print_file, ...r.photo_files].filter(Boolean).map((n: string) => `${r.folder}/${n}`));
  const links: Record<string, string> = {};
  for (let i = 0; i < paths.length; i += 300) {
    const { data, error: se } = await db.storage.from(BUCKET).createSignedUrls(paths.slice(i, i + 300), 3600);
    if (se) throw new Error(se.message);
    for (const d of data ?? []) if (d.path && d.signedUrl) links[d.path] = d.signedUrl;
  }
  return (rows ?? []).map((r: any) => ({
    id: r.id, session_key: r.session_key, taken_at: r.taken_at, ready_at: r.ready_at,
    booth_id: r.booth_id, booth: r.booths?.name ?? "", event_name: r.event_name, info: r.info,
    print: r.print_file ? { name: r.print_file, url: links[`${r.folder}/${r.print_file}`] } : null,
    photos: r.photo_files.map((n: string) => ({ name: n, url: links[`${r.folder}/${n}`] })),
  }));
}

async function list(k: any, body: any) {
  await db.from("kiosks").update({ last_seen_at: new Date().toISOString() }).eq("id", k.id);
  const after = body.after ? new Date(body.after) : new Date(0);
  if (isNaN(after.getTime())) return json({ error: "bad after" }, 400);
  const days = await retentionDays();
  const { data: rows, error } = await db.from("kiosk_sessions").select(SESSION_COLS)
    .eq("status", "ready").gt("ready_at", after.toISOString())
    .gt("taken_at", new Date(Date.now() - days * 86400000).toISOString())
    .order("ready_at", { ascending: true }).limit(100);
  if (error) return json({ error: error.message }, 500);
  const sessions = await withLinks(rows ?? []);
  // sessions a booth is part-way through uploading (a guest may be on their way to the kiosk with one)
  const { count: pending } = await db.from("kiosk_sessions").select("id", { count: "exact", head: true })
    .eq("status", "uploading").gt("created_at", new Date(Date.now() - 5 * 60000).toISOString());
  return json({ sessions, retention_days: days, more: (rows ?? []).length === 100, pending: pending ?? 0 });
}

// Sessions by id, e.g. to download again files that went missing on the kiosk.
async function get(_k: any, body: any) {
  const ids = (Array.isArray(body.ids) ? body.ids : []).filter((x: any) => UUID.test(String(x))).slice(0, 50);
  if (!ids.length) return json({ sessions: [] });
  const { data: rows, error } = await db.from("kiosk_sessions").select(SESSION_COLS).eq("status", "ready").in("id", ids);
  if (error) return json({ error: error.message }, 500);
  return json({ sessions: await withLinks(rows ?? []), retention_days: await retentionDays() });
}

// The kiosk checking in, so it shows on the dashboard like a booth.
async function heartbeat(k: any, body: any) {
  const arr = (x: any, n: number) => (Array.isArray(x) ? x.slice(0, n) : []);
  const price = Number(body.price);
  const { data, error } = await db.rpc("kiosk_heartbeat", {
    p_kiosk: k.id,
    p_programs: arr(body.programs, 20),
    p_status: body.status && typeof body.status === "object" ? body.status : {},
    p_version: body.version ? String(body.version).slice(0, 40) : null,
    p_days: arr(body.days, 3),
    p_price: Number.isFinite(price) && price >= 0 && price < 1000 ? price : null,
    p_events: arr(body.events, 300),
  });
  if (error) return json({ error: error.message }, 500);
  return json(data ?? { ok: true });
}

async function cleanup() {
  const days = await retentionDays();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString();
  const stale = new Date(Date.now() - 2 * 86400000).toISOString();
  let removed = 0;
  for (let round = 0; round < 50; round++) {
    const { data: old, error } = await db.from("kiosk_sessions")
      .select("id, folder, print_file, photo_files")
      .or(`taken_at.lt.${cutoff},and(status.eq.uploading,created_at.lt.${stale})`).limit(100);
    if (error) return json({ error: error.message, removed }, 500);
    if (!old?.length) break;
    const paths = old.flatMap((s: any) => [s.print_file, ...s.photo_files].filter(Boolean).map((n: string) => `${s.folder}/${n}`));
    if (paths.length) {
      const { error: se } = await db.storage.from(BUCKET).remove(paths);
      if (se) return json({ error: se.message, removed }, 500);
    }
    await db.from("kiosk_sessions").delete().in("id", old.map((s: any) => s.id));
    removed += old.length;
  }
  return json({ ok: true, removed, retention_days: days });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  let body: any = {};
  try { body = await req.json(); } catch { return json({ error: "bad json" }, 400); }
  const action = body?.action;
  try {
    if (action === "begin" || action === "finish") {
      const booth = await agentBooth(req);
      if (!booth) return json({ error: "not allowed" }, 401);
      return action === "begin" ? await begin(booth, body) : await finish(booth, body);
    }
    if (action === "list" || action === "get" || action === "heartbeat") {
      const k = await kiosk(req);
      if (!k) return json({ error: "not allowed" }, 401);
      if (action === "heartbeat") return await heartbeat(k, body);
      return action === "list" ? await list(k, body) : await get(k, body);
    }
    if (action === "cleanup") {
      const { data: ok } = await db.rpc("kiosk_cleanup_key_ok", { p_key: req.headers.get("x-cleanup-key") ?? "" });
      if (!ok) return json({ error: "not allowed" }, 401);
      return await cleanup();
    }
    return json({ error: "unknown action" }, 400);
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

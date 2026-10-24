// Giant strip kiosk: a zip of booth prints to print at home and test the strip matching.
// For each booth sending sessions to the kiosk: about 10 prints, mostly pairs of sessions taken
// minutes apart (the same people, so very similar strips) plus some spread over the month.
// Prints that are a single strip are put twice on a 4x6, like the booth's double strips.
// Called with the private cleanup key (from the database), answers with a 7-day download link.
//   POST {per_booth?: 10}   header x-cleanup-key

import { createClient } from "npm:@supabase/supabase-js@2";
import { zipSync } from "npm:fflate@0.8.2";
import { Image } from "https://deno.land/x/imagescript@1.3.0/mod.ts";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } });
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function jpegSize(b: Uint8Array): [number, number] | null {
  let i = 2;
  while (i < b.length - 9) {
    if (b[i] !== 0xff) { i++; continue; }
    const m = b[i + 1];
    if (m >= 0xc0 && m <= 0xc3) return [(b[i + 7] << 8) | b[i + 8], (b[i + 5] << 8) | b[i + 6]];   // w, h
    i += 2 + ((b[i + 2] << 8) | b[i + 3]);
  }
  return null;
}

const tidy = (s: string) => s.replace(/[^A-Za-z0-9]+/g, " ").trim().replace(/ /g, "-");
const london = (iso: string) => new Date(iso).toLocaleString("en-GB", { timeZone: "Europe/London",
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

function pick(sessions: any[], n: number) {
  const s = [...sessions].sort((a, b) => a.taken_at.localeCompare(b.taken_at));
  // groups of sessions taken within 10 minutes of each other
  const groups: any[][] = [];
  for (const x of s) {
    const g = groups[groups.length - 1];
    if (g && new Date(x.taken_at).getTime() - new Date(g[g.length - 1].taken_at).getTime() < 10 * 60000) g.push(x);
    else groups.push([x]);
  }
  const chosen: any[] = [];
  const why = new Map<string, string>();
  const close = groups.filter((g) => g.length >= 2).sort((a, b) => b.length - a.length);
  let gi = 0;
  for (const g of close) {
    if (chosen.length + 2 > Math.ceil(n * 0.6)) break;
    gi++;
    for (const x of g.slice(0, 2)) { chosen.push(x); why.set(x.id, `similar pair ${String.fromCharCode(64 + gi)}`); }
  }
  const rest = s.filter((x) => !why.has(x.id));
  const need = n - chosen.length;
  for (let k = 0; k < need && rest.length; k++) {
    const x = rest[Math.min(rest.length - 1, Math.floor((k + 0.5) * rest.length / need))];
    if (!why.has(x.id)) { chosen.push(x); why.set(x.id, "spread over the month"); }
  }
  return chosen.sort((a, b) => a.taken_at.localeCompare(b.taken_at)).map((x) => ({ ...x, why: why.get(x.id) }));
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const { data: ok } = await db.rpc("kiosk_cleanup_key_ok", { p_key: req.headers.get("x-cleanup-key") ?? "" });
  if (!ok) return json({ error: "not allowed" }, 401);
  let body: any = {};
  try { body = await req.json(); } catch { /* defaults */ }
  const perBooth = Math.max(1, Math.min(30, Number(body.per_booth) || 10));

  const { data: rows, error } = await db.from("kiosk_sessions")
    .select("id, session_key, taken_at, folder, print_file, booths(name)")
    .eq("status", "ready").not("print_file", "is", null).order("taken_at");
  if (error) return json({ error: error.message }, 500);
  const byBooth = new Map<string, any[]>();
  for (const r of rows ?? []) {
    const b = (r as any).booths?.name ?? "Booth";
    if (!byBooth.has(b)) byBooth.set(b, []);
    byBooth.get(b)!.push(r);
  }

  const files: Record<string, Uint8Array> = {};
  const key: string[] = ["Giant strip kiosk - test prints", "",
    "Print each file on 4x6 (10x15cm) photo paper at 100% / actual size, cut the strips apart",
    "and feed ONE strip into the kiosk. It should be recognised as the session below.",
    "\"Similar pair\" = two sessions taken minutes apart, usually the same people - the hardest test.", "",
    "File | Booth | Taken | Session | Why picked", ""];
  const made: Record<string, number> = {};
  for (const [booth, sessions] of [...byBooth.entries()].sort()) {
    const chosen = pick(sessions, perBooth);
    made[booth] = 0;
    for (const [i, s] of chosen.entries()) {
      const { data: blob, error: de } = await db.storage.from("kiosk-sessions").download(`${s.folder}/${s.print_file}`);
      if (de || !blob) continue;
      let bytes = new Uint8Array(await blob.arrayBuffer());
      const size = jpegSize(bytes);
      if (size && size[1] >= size[0] * 2.2) {
        // a single strip: two copies side by side on a 4x6, like the booth prints
        const strip = await Image.decode(bytes);
        const sheet = new Image(strip.width * 2, strip.height).fill(0xffffffff);
        sheet.composite(strip, 0, 0);
        sheet.composite(strip, strip.width, 0);
        bytes = await sheet.encodeJPEG(92);
      }
      const name = `${tidy(booth)}/${String(i + 1).padStart(2, "0")}_${tidy(booth)}_${london(s.taken_at).replace(/[/:, ]+/g, "-")}.jpg`;
      files[name] = bytes;
      key.push(`${name} | ${booth} | ${london(s.taken_at)} | ${s.session_key} | ${s.why}`);
      made[booth]++;
    }
  }
  files["Answer key.txt"] = new TextEncoder().encode(key.join("\r\n") + "\r\n");
  const zip = zipSync(files, { level: 0 });
  const path = `test-prints-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.zip`;
  const { error: ue } = await db.storage.from("kiosk-test-packs").upload(path, zip, { contentType: "application/zip", upsert: true });
  if (ue) return json({ error: ue.message }, 500);
  const { data: link, error: le } = await db.storage.from("kiosk-test-packs").createSignedUrl(path, 7 * 86400,
    { download: "Giant strip test prints.zip" });
  if (le) return json({ error: le.message }, 500);
  return json({ ok: true, url: link.signedUrl, size_mb: Math.round(zip.length / 1e5) / 10, made });
});

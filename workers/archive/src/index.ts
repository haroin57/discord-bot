/** Private, prefix-scoped streaming archive access. No bucket listing/deletion. */

class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

function json(data: object, status = 200): Response {
  return Response.json(data, { status, headers: { "Cache-Control": "no-store" } });
}

async function authorize(request: Request, token: string | undefined): Promise<void> {
  if (!token || token.length < 32) throw new HttpError(503, "archive auth unavailable");
  const supplied = request.headers.get("Authorization") || "";
  const encoder = new TextEncoder();
  const [actual, expected] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(supplied)),
    crypto.subtle.digest("SHA-256", encoder.encode(`Bearer ${token}`)),
  ]);
  if (!crypto.subtle.timingSafeEqual(actual, expected)) throw new HttpError(401, "unauthorized");
}

function objectKey(url: URL, prefix: string): string {
  const key = url.searchParams.get("key") || "";
  const invalid = (value: string) => value.split("/").some(p => !p || p === "." || p === "..");
  if (!prefix || invalid(prefix)) throw new HttpError(503, "archive prefix unavailable");
  if (
    !key.startsWith(`${prefix}/`) || invalid(key) || /[\\\x00-\x1f\x7f]/.test(key)
    || new TextEncoder().encode(key).byteLength > 1024
  ) throw new HttpError(400, "invalid archive key");
  return key;
}

async function upload(request: Request, env: Env, key: string): Promise<Response> {
  const rawSize = request.headers.get("Content-Length") || "";
  const size = Number(rawSize);
  const maximum = Number(env.MAX_OBJECT_BYTES);
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 67108864) {
    throw new HttpError(503, "invalid size configuration");
  }
  if (!/^\d+$/.test(rawSize) || !Number.isSafeInteger(size)) throw new HttpError(411, "length required");
  if (size > maximum) throw new HttpError(413, "object too large");
  const sha256 = request.headers.get("X-Archive-SHA256") || "";
  const revision = request.headers.get("X-Archive-Revision") || "";
  if (!/^[a-f0-9]{64}$/.test(sha256) || !/^\d{19}-[a-f0-9]{32}$/.test(revision)) {
    throw new HttpError(400, "invalid checksum or revision");
  }
  const previous = await env.ARCHIVE.head(key);
  const priorRevision = previous?.customMetadata?.revision;
  if (priorRevision && priorRevision >= revision) {
    if (priorRevision === revision && (previous?.customMetadata?.sha256 !== sha256 || previous.size !== size)) {
      throw new HttpError(409, "revision conflict");
    }
    // A timed-out attempt may already have committed, or a newer snapshot won.
    // Drain no body here: the client retains its own durable copy until this ACK.
    return json({ ok: true, sha256, size, superseded: priorRevision > revision });
  }
  const contentType = request.headers.get("Content-Type") || "application/octet-stream";
  const object = await env.ARCHIVE.put(key, request.body, {
    sha256,
    onlyIf: previous ? { etagMatches: previous.etag } : { etagDoesNotMatch: "*" },
    httpMetadata: { contentType },
    customMetadata: { sha256, revision },
  });
  if (!object) throw new HttpError(409, "concurrent write; retry from durable spool");
  return json({ ok: true, sha256, size: object.size, superseded: false });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      await authorize(request, env.ARCHIVE_AUTH_TOKEN);
      const url = new URL(request.url);
      if (url.pathname === "/healthz" && request.method === "GET") return json({ ok: true, version: 1 });
      if (url.pathname !== "/v1/objects") throw new HttpError(404, "not found");
      const key = objectKey(url, env.ARCHIVE_PREFIX);
      if (request.method === "PUT") return await upload(request, env, key);
      if (request.method !== "GET" && request.method !== "HEAD") throw new HttpError(405, "method not allowed");
      if (request.method === "HEAD") {
        const object = await env.ARCHIVE.head(key);
        if (!object) throw new HttpError(404, "object not found");
        return new Response(null, { headers: { "Cache-Control": "no-store", "Content-Length": String(object.size), "ETag": object.httpEtag } });
      }
      const object = await env.ARCHIVE.get(key);
      if (!object) throw new HttpError(404, "object not found");
      const headers = new Headers({
        "Cache-Control": "private, no-store",
        "Content-Disposition": "attachment",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": String(object.size),
        "ETag": object.httpEtag,
      });
      headers.set("Content-Type", object.httpMetadata?.contentType || "application/octet-stream");
      const checksum = object.customMetadata?.sha256;
      if (checksum) headers.set("X-Archive-SHA256", checksum);
      return new Response(object.body, { headers });
    } catch (error) {
      if (error instanceof HttpError) return json({ error: error.message }, error.status);
      // Do not log payloads, tokens, raw provider errors, or private object names.
      console.error(JSON.stringify({ event: "archive_storage_failure", requestId: crypto.randomUUID() }));
      return json({ error: "archive storage unavailable" }, 503);
    }
  },
} satisfies ExportedHandler<Env>;

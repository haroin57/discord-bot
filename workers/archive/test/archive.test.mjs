import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { after, before, test } from "node:test";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const token = "test-archive-token-".repeat(3);
const prefix = "discord-bot/v1";
const digest = body => createHash("sha256").update(body).digest("hex");
const revision = n => `${1900000000000000000n + BigInt(n)}-${"a".repeat(32)}`;
let worker;
let bucket;
before(async () => {
  worker = new Miniflare(convertV4MiniflareOptions({ workers: [{
    name: "archive",
    modules: true, scriptPath: "dist/index.js", compatibilityDate: "2026-09-09",
    compatibilityFlags: ["nodejs_compat"], r2Buckets: ["ARCHIVE"],
    bindings: { ARCHIVE_AUTH_TOKEN: token, ARCHIVE_PREFIX: prefix, MAX_OBJECT_BYTES: "1048576" },
  }] }));
  bucket = await worker.getR2Bucket("ARCHIVE");
});
after(async () => { await worker?.dispose(); });

async function request(key, { method = "GET", body, headers = {} } = {}) {
  return worker.dispatchFetch(`https://archive.test/v1/objects?${new URLSearchParams({ key })}`, {
    method, body,
    headers: { Authorization: `Bearer ${token}`, ...headers },
  });
}
function upload(key, body, rev, extra = {}) {
  return request(key, { method: "PUT", body, headers: {
    "Content-Type": "application/octet-stream", "Content-Length": String(Buffer.byteLength(body)),
    "X-Archive-SHA256": digest(body), "X-Archive-Revision": revision(rev), ...extra,
  } });
}

test("auth protects reads, writes, health, and bucket existence", async () => {
  for (const method of ["GET", "HEAD", "PUT", "DELETE"]) {
    const response = await request(`${prefix}/private`, { method, headers: { Authorization: "Bearer wrong" } });
    assert.equal(response.status, 401);
  }
  assert.equal((await worker.dispatchFetch("https://archive.test/healthz")).status, 401);
});

test("prefix escape, traversal and listing are rejected", async () => {
  for (const key of ["other/v1/file", `${prefix}evil/file`, `${prefix}/../secret`, `${prefix}//secret`, `${prefix}/./x`, `${prefix}/bad\u0000name`]) {
    assert.equal((await request(key)).status, 400);
  }
  assert.equal((await request(`${prefix}/a`, { method: "DELETE" })).status, 405);
  assert.equal((await request(`${prefix}/missing`)).status, 404);
});

test("upload streams to R2 and authenticated download preserves bytes/checksum", async () => {
  const body = Buffer.alloc(1024 * 1024, 43);
  const key = `${prefix}/history/123/日本語.jsonl`;
  const response = await upload(key, body, 1);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, sha256: digest(body), size: body.length, superseded: false });
  const stored = await bucket.get(key);
  assert.equal(stored.customMetadata.sha256, digest(body));
  const downloaded = await request(key);
  assert.equal(downloaded.headers.get("X-Archive-SHA256"), digest(body));
  assert.equal(downloaded.headers.get("Cache-Control"), "private, no-store");
  assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), body);
  const head = await request(key, { method: "HEAD" });
  assert.equal(head.headers.get("Content-Length"), String(body.length));
  assert.equal((await head.arrayBuffer()).byteLength, 0);
});

test("a lost acknowledgement can be retried without overwriting later revisions", async () => {
  const key = `${prefix}/state/latest/file.json`;
  assert.equal((await upload(key, "old", 1)).status, 200);
  assert.equal((await upload(key, "old", 1)).status, 200);
  assert.equal((await upload(key, "different", 1)).status, 409);
  assert.equal((await upload(key, "new", 3)).status, 200);
  const late = await upload(key, "middle", 2);
  assert.equal((await late.json()).superseded, true);
  assert.equal(await (await request(key)).text(), "new");
});

test("checksum failures cannot commit corrupt payloads", async () => {
  const key = `${prefix}/corruption`;
  const result = await upload(key, "body", 1, { "X-Archive-SHA256": "0".repeat(64) });
  assert.equal(result.status, 503);
  assert.equal(await bucket.head(key), null);
});

test("request size is checked before R2 writes", async () => {
  const response = await upload(`${prefix}/large`, Buffer.alloc(1048577), 1);
  assert.equal(response.status, 413);
  assert.equal(await bucket.head(`${prefix}/large`), null);
});

test("legacy Wrangler objects remain readable", async () => {
  const key = `${prefix}/legacy`;
  await bucket.put(key, "legacy");
  const response = await request(key);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "legacy");
});

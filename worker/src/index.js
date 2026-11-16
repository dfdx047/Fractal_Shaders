// Fractal shader cache uploads (no account needed in the app).
//
//   POST /v1/upload?titleId=<16 hex>&kind=shaders|vkpipeline&installId=<random id of the installation>
//        body: the cache file (Content-Length required)  -> {"asset": "<name>"}
//        The file is checked (Cemu cache header of that game) and passed through, unchanged, as an asset of the
//        repository's "submissions" prerelease. Nothing is decoded here: the free plan allows 10 ms of CPU.
//   POST /v1/submit   {"titleId","gameName","appVersion","device","gpu","driver","installId",
//                      "shaders","pipelines","assets":{"shaders":name,"vkpipeline":name}}
//        -> {"pullRequest": "https://github.com/..."}  a pull request adding submissions/<title id>/<id>-<time>/meta.json
//
// The repository's "Validate submission" workflow downloads the assets and checks every entry; merging the pull
// request adds them to the "caches" release.

const MAX_BYTES = 95 * 1024 * 1024;
const MAGIC_V2 = 0x8371b695;
const MAGIC_V3 = 0x8371b696;
const UPLOADS_PER_HOUR = 20;
const SUBMITS_PER_HOUR = 6;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "POST" && url.pathname === "/v1/upload") return await upload(request, env, url);
      if (request.method === "POST" && url.pathname === "/v1/submit") return await submit(request, env);
      if (request.method === "GET" && url.pathname === "/")
        return new Response(`Fractal shader uploads for https://github.com/${env.REPO}\n`);
      return reply(404, { error: "not found" });
    } catch (e) {
      return reply(e.status || 500, { error: e.message || String(e) });
    }
  },
};

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function reply(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

// Cemu FileCache "extra version" of a title's caches (LatteShaderCache.cpp / VulkanPipelineStableCache.cpp)
function expectedExtraVersion(titleId, kind) {
  const hi = BigInt.asUintN(32, titleId >> 32n);
  const lo = BigInt.asUintN(32, titleId);
  const base = BigInt.asUintN(32, hi + lo * 3n + 1n);
  return Number(kind === "shaders" ? BigInt.asUintN(32, base + 0xe97af1adn) : base);
}

function parseTitleId(text) {
  if (!/^00050000[0-9a-f]{8}$/.test(text || "")) throw new HttpError(400, "titleId must be a base game id (00050000xxxxxxxx)");
  return BigInt("0x" + text);
}

function parseKind(text) {
  if (text !== "shaders" && text !== "vkpipeline") throw new HttpError(400, "kind must be shaders or vkpipeline");
  return text;
}

async function rateLimit(env, request, what, limit) {
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const hour = Math.floor(Date.now() / 3600000);
  const key = `rate:${what}:${ip}:${hour}`;
  const count = parseInt((await env.KV.get(key)) || "0", 10);
  if (count >= limit) throw new HttpError(429, "too many uploads, try again later");
  await env.KV.put(key, String(count + 1), { expirationTtl: 3700 });
}

async function github(env, method, path, body) {
  const response = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "fractal-shaders-worker",
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
    },
    body: body === undefined ? undefined : typeof body === "string" || body instanceof ReadableStream ? body : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new HttpError(502, `GitHub ${method} ${path}: ${response.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : {};
}

function anonId(installId) {
  return crypto.subtle
    .digest("SHA-256", new TextEncoder().encode(`fractal:${clean(installId, 64)}`))
    .then((d) => [...new Uint8Array(d)].slice(0, 5).map((x) => x.toString(16).padStart(2, "0")).join(""));
}

function stampNow() {
  return new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
}

async function submissionsRelease(env) {
  const cached = await env.KV.get("release:submissions");
  if (cached) return cached;
  let id;
  try {
    id = (await github(env, "GET", `/repos/${env.REPO}/releases/tags/submissions`)).id;
  } catch (e) {
    id = (
      await github(env, "POST", `/repos/${env.REPO}/releases`, {
        tag_name: "submissions",
        target_commitish: "main",
        name: "Pending submissions",
        body: "Shader caches sent from the Fractal app, waiting for review. Merged ones are moved to the caches release.",
        prerelease: true,
      })
    ).id;
  }
  await env.KV.put("release:submissions", String(id), { expirationTtl: 86400 });
  return String(id);
}

// Passes the bytes through unchanged; fails the stream when the first 8 bytes aren't the cache header expected.
function headerCheck(titleId, kind, length) {
  let seen = 0;
  let head = new Uint8Array(0);
  return new TransformStream({
    transform(chunk, controller) {
      if (head.length < 8) {
        const merged = new Uint8Array(head.length + chunk.length);
        merged.set(head);
        merged.set(chunk, head.length);
        head = merged.slice(0, 8);
        if (head.length >= 8) {
          const view = new DataView(head.buffer);
          const magic = view.getUint32(0, true);
          if (magic !== MAGIC_V3 && magic !== MAGIC_V2) throw new HttpError(400, "not a Cemu cache file");
          if (view.getUint32(4, true) !== expectedExtraVersion(titleId, kind))
            throw new HttpError(400, `not a ${kind} cache of this game`);
        }
      }
      seen += chunk.length;
      if (seen > length) throw new HttpError(400, "body longer than Content-Length");
      controller.enqueue(chunk);
    },
  });
}

async function upload(request, env, url) {
  const titleIdText = url.searchParams.get("titleId");
  const titleId = parseTitleId(titleIdText);
  const kind = parseKind(url.searchParams.get("kind"));
  const length = parseInt(request.headers.get("content-length") || "0", 10);
  if (!(length >= 128)) throw new HttpError(411, "Content-Length required");
  if (length > MAX_BYTES) throw new HttpError(413, "file over 95 MB");
  await rateLimit(env, request, "upload", UPLOADS_PER_HOUR);

  const release = await submissionsRelease(env);
  const random = [...crypto.getRandomValues(new Uint8Array(3))].map((x) => x.toString(16).padStart(2, "0")).join("");
  const name = `${titleIdText}_${kind}_p${await anonId(url.searchParams.get("installId"))}-${stampNow()}-${random}.bin`;
  const { readable, writable } = new FixedLengthStream(length);
  let streamError = null;
  const pipe = request.body
    .pipeThrough(headerCheck(titleId, kind, length))
    .pipeTo(writable)
    .catch((e) => {
      streamError = e;
    });
  let asset;
  try {
    const response = await fetch(
      `https://uploads.github.com/repos/${env.REPO}/releases/${release}/assets?name=${encodeURIComponent(name)}`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GITHUB_TOKEN}`,
          accept: "application/vnd.github+json",
          "x-github-api-version": "2022-11-28",
          "user-agent": "fractal-shaders-worker",
          "content-type": "application/octet-stream",
        },
        body: readable,
      },
    );
    const text = await response.text();
    if (!response.ok) throw new HttpError(502, `GitHub upload: ${response.status} ${text.slice(0, 200)}`);
    asset = JSON.parse(text);
  } catch (e) {
    await pipe;
    throw streamError || e;
  }
  await pipe;
  if (streamError) throw streamError;
  await env.KV.put(`asset:${name}`, JSON.stringify({ titleId: titleIdText, kind, size: length, id: asset.id }), {
    expirationTtl: 7200,
  });
  return reply(200, { asset: name });
}

function clean(text, max = 80) {
  return String(text ?? "").replace(/[^\p{L}\p{N} .,:;!?'()&+\-_/®™]/gu, "").trim().slice(0, max);
}

async function submit(request, env) {
  const body = await request.json().catch(() => {
    throw new HttpError(400, "JSON body required");
  });
  const titleIdText = body.titleId;
  parseTitleId(titleIdText);
  await rateLimit(env, request, "submit", SUBMITS_PER_HOUR);

  const assets = {};
  for (const kind of ["shaders", "vkpipeline"]) {
    const name = body.assets?.[kind];
    if (!name) continue;
    if (typeof name !== "string" || !/^[0-9a-f]{16}_(shaders|vkpipeline)_[A-Za-z0-9-]{1,40}\.bin$/.test(name))
      throw new HttpError(400, "bad asset name");
    const known = JSON.parse((await env.KV.get(`asset:${name}`)) || "null");
    if (!known || known.titleId !== titleIdText || known.kind !== kind) throw new HttpError(400, `unknown ${kind} upload`);
    assets[kind] = name;
  }
  if (Object.keys(assets).length === 0) throw new HttpError(400, "nothing to submit");

  const anon = await anonId(body.installId);
  const stamp = stampNow();
  const folder = `submissions/${titleIdText}/p${anon}-${stamp}`;
  const gameName = clean(body.gameName);
  const meta = {
    titleId: titleIdText,
    gameName,
    appVersion: clean(body.appVersion, 32),
    device: clean(body.device),
    gpu: clean(body.gpu),
    driver: clean(body.driver),
    shaders: Number.isFinite(body.shaders) ? body.shaders : undefined,
    pipelines: Number.isFinite(body.pipelines) ? body.pipelines : undefined,
    assets,
  };

  const repo = env.REPO;
  const main = await github(env, "GET", `/repos/${repo}/git/ref/heads/main`);
  const baseCommit = await github(env, "GET", `/repos/${repo}/git/commits/${main.object.sha}`);
  const tree = await github(env, "POST", `/repos/${repo}/git/trees`, {
    base_tree: baseCommit.tree.sha,
    tree: [{ path: `${folder}/meta.json`, mode: "100644", type: "blob", content: JSON.stringify(meta, null, 2) + "\n" }],
  });
  const title = `Shaders: ${gameName || titleIdText}`;
  const commit = await github(env, "POST", `/repos/${repo}/git/commits`, {
    message: title,
    tree: tree.sha,
    parents: [main.object.sha],
  });
  const branch = `submission/${titleIdText}-p${anon}-${stamp}`;
  await github(env, "POST", `/repos/${repo}/git/refs`, { ref: `refs/heads/${branch}`, sha: commit.sha });
  const pr = await github(env, "POST", `/repos/${repo}/pulls`, {
    title,
    head: branch,
    base: "main",
    body:
      `Sent from Fractal ${meta.appVersion || "?"} on ${meta.device || "?"} (${meta.gpu || "?"}).\n\n` +
      `Shaders: ${meta.shaders ?? "?"}, pipelines: ${meta.pipelines ?? "?"}.\n\n` +
      `Files (assets of the submissions release): ${Object.values(assets).join(", ")}`,
  });
  for (const name of Object.values(assets)) await env.KV.delete(`asset:${name}`);
  return reply(200, { pullRequest: pr.html_url });
}

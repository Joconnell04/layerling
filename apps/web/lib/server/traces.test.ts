// @vitest-environment node
// Trace API routes against the fixture source: auth, path checks, redaction.
import { GET as jobsRoute } from "@/app/api/admin/traces/route";
import { GET as jobRoute } from "@/app/api/admin/traces/[id]/route";
import { GET as rawRoute } from "@/app/api/admin/traces/[id]/raw/route";
import { GET as bodyRoute } from "@/app/api/admin/traces/[id]/body/route";
import { GET as fileRoute } from "@/app/api/admin/traces/[id]/file/route";
import { GET as callsRoute } from "@/app/api/admin/traces/calls/route";
import { SECRET_PATTERN } from "../traces/redact";
import type { CallsResponse, JobTrace } from "../traces/types";
import { ACCESS_COOKIE, signAccess } from "./accessCookie";

const SECRET = "test-cookie-secret-0123456789abcdef";
const BRONCO = "1785a480-84b8-41f9-8f97-1005b6d7d0a6";
const params = (id: string) => ({ params: Promise.resolve({ id }) });
const req = (path: string, cookie?: string) =>
  new Request(`https://p2p.example${path}`, { headers: cookie ? { cookie: `${ACCESS_COOKIE}=${cookie}` } : {} });

type Handler = (r: Request, p?: { params: Promise<{ id: string }> }) => Promise<Response>;
const ROUTES: [string, Handler, boolean][] = [
  ["/api/admin/traces", jobsRoute as Handler, false],
  ["/api/admin/traces/calls", callsRoute as Handler, false],
  [`/api/admin/traces/${BRONCO}`, jobRoute as Handler, true],
  [`/api/admin/traces/${BRONCO}/raw?stage=s05_mesh&attempt=1`, rawRoute as Handler, true],
  [`/api/admin/traces/${BRONCO}/body?path=s05_mesh/a1/trace/01-meshy.json`, bodyRoute as Handler, true],
  [`/api/admin/traces/${BRONCO}/file?bucket=inputs&path=input/photo_1.jpg`, fileRoute as Handler, true],
];

let cookie: string;
beforeEach(async () => {
  vi.stubEnv("NEXT_PUBLIC_DATA", "");
  vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "");
  vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "");
  vi.stubEnv("ACCESS_COOKIE_SECRET", SECRET);
  cookie = await signAccess(SECRET);
});
afterEach(() => vi.unstubAllEnvs());

describe("trace API", () => {
  it("needs the access cookie outside mock mode; with it every route answers", async () => {
    for (const [path, h, withId] of ROUTES) {
      const p = withId ? params(BRONCO) : undefined;
      expect((await h(req(path), p)).status, path).toBe(401);
      expect((await h(req(path, `${cookie}x`), p)).status, path).toBe(401);
      const ok = await h(req(path, cookie), p);
      expect(ok.status, path).toBe(200);
      if (ok.headers.get("content-type")?.includes("json")) expect(ok.headers.get("cache-control")).toBe("no-store");
    }
  });

  it("mock mode needs no cookie", async () => {
    vi.stubEnv("NEXT_PUBLIC_DATA", "mock");
    expect((await jobsRoute(req("/api/admin/traces"))).status).toBe(200);
  });

  it("rejects bad ids, stages and paths", async () => {
    const c = cookie;
    expect((await jobRoute(req("/api/admin/traces/nope", c), params("nope"))).status).toBe(400);
    expect((await jobRoute(req("/api/admin/traces/x", c), params("00000000-0000-0000-0000-000000000000"))).status).toBe(404);
    expect((await rawRoute(req(`/api/admin/traces/${BRONCO}/raw?stage=../x&attempt=1`, c), params(BRONCO))).status).toBe(400);
    expect((await rawRoute(req(`/api/admin/traces/${BRONCO}/raw?stage=s05_mesh&attempt=0`, c), params(BRONCO))).status).toBe(400);
    for (const bad of ["../job.json", "s05_mesh/a1/trace/../../job.json", "/etc/passwd", "s05_mesh/a1/mesh.json", "s05_mesh/a1/trace/x.txt", ""]) {
      const r = await bodyRoute(req(`/api/admin/traces/${BRONCO}/body?path=${encodeURIComponent(bad)}`, c), params(BRONCO));
      expect(r.status, bad).toBe(400);
    }
    expect((await bodyRoute(req(`/api/admin/traces/${BRONCO}/body?path=s05_mesh/a9/trace/01-meshy.json`, c), params(BRONCO))).status).toBe(404);
    const other = "819cc491-c341-470a-bb97-63a194fa8dca";
    for (const q of [
      "bucket=inputs&path=../input/photo_1.jpg",
      "bucket=inputs&path=/input/photo_1.jpg",
      "bucket=orders&path=input/photo_1.jpg",
      "bucket=artifacts&path=",
      `path=${encodeURIComponent(`storage:artifacts/${other}/s03_concept/a1/concept_r1c1.png`)}`,
    ]) {
      const r = await fileRoute(req(`/api/admin/traces/${BRONCO}/file?${q}`, c), params(BRONCO));
      expect(r.status, q).toBe(400);
    }
    expect((await callsRoute(req("/api/admin/traces/calls?days=-3", c))).status).toBe(400);
    expect((await callsRoute(req("/api/admin/traces/calls?vendor=openai", c))).status).toBe(400);
  });

  it("serves fixture files: images, JSON outputs and storage: refs", async () => {
    const img = await fileRoute(req(`/api/admin/traces/${BRONCO}/file?bucket=artifacts&path=s03_concept/a1/concept_r1c1.png`, cookie), params(BRONCO));
    expect(img.status).toBe(200);
    expect(img.headers.get("content-type")).toBe("image/jpeg");
    const ref = encodeURIComponent(`storage:inputs/${BRONCO}/input/photo_1.jpg`);
    expect((await fileRoute(req(`/api/admin/traces/${BRONCO}/file?path=${ref}`, cookie), params(BRONCO))).status).toBe(200);
    const doc = await fileRoute(req(`/api/admin/traces/${BRONCO}/file?bucket=artifacts&path=s01_intake/a1/intake.json`, cookie), params(BRONCO));
    expect(doc.headers.get("content-type")).toBe("application/json");
    const glb = await fileRoute(req(`/api/admin/traces/${BRONCO}/file?bucket=artifacts&path=s05_mesh/a1/mesh_m1.glb`, cookie), params(BRONCO));
    expect(glb.status).toBe(404);
  });

  it("returns the Bronco trace and calls with no secret shape anywhere", async () => {
    const texts: string[] = [];
    const job = await jobRoute(req(`/api/admin/traces/${BRONCO}`, cookie), params(BRONCO));
    const t = (await job.clone().json()) as JobTrace;
    texts.push(await job.text());
    for (const a of t.attempts) {
      const raw = await rawRoute(req(`/api/admin/traces/${BRONCO}/raw?stage=${a.stage}&attempt=${a.attempt}`, cookie), params(BRONCO));
      expect(raw.status).toBe(200);
      texts.push(await raw.text());
      for (const c of a.calls.filter((c) => c.path)) {
        const b = await bodyRoute(req(`/api/admin/traces/${BRONCO}/body?path=${encodeURIComponent(c.path!)}`, cookie), params(BRONCO));
        expect(b.status).toBe(200);
        texts.push(await b.text());
      }
    }
    const calls = await callsRoute(req("/api/admin/traces/calls", cookie));
    texts.push(await calls.clone().text());
    texts.push(await (await jobsRoute(req("/api/admin/traces", cookie))).text());
    expect(texts.length).toBeGreaterThan(10);
    for (const s of texts) expect(s).not.toMatch(SECRET_PATTERN);
    const body = (await calls.json()) as CallsResponse;
    expect(body.calls).toHaveLength(12);
    expect(body.summary.credits).toBe(132);
    const claude = (await (await callsRoute(req("/api/admin/traces/calls?vendor=claude&q=intake", cookie))).json()) as CallsResponse;
    expect(claude.calls.map((c) => c.model)).toEqual(["claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"]);
    expect(claude.summary.calls).toBe(12); // the summary covers the day range only
  });
});

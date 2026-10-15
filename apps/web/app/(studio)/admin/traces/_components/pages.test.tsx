// The three trace pages rendered with fixture data. fetch goes to the real route handlers
// (mock mode, fixture source), so the pages see exactly what the API returns.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { GET as jobsRoute } from "@/app/api/admin/traces/route";
import { GET as jobRoute } from "@/app/api/admin/traces/[id]/route";
import { GET as rawRoute } from "@/app/api/admin/traces/[id]/raw/route";
import { GET as bodyRoute } from "@/app/api/admin/traces/[id]/body/route";
import { GET as callsRoute } from "@/app/api/admin/traces/calls/route";
import { TraceCalls } from "./TraceCalls";
import { TraceJob } from "./TraceJob";
import { TraceJobs } from "./TraceJobs";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  usePathname: () => "/admin/traces",
  useParams: () => ({}),
}));

const BRONCO = "1785a480-84b8-41f9-8f97-1005b6d7d0a6";
const FRENCHIE = "819cc491-c341-470a-bb97-63a194fa8dca";
type Handler = (r: Request, p?: { params: Promise<{ id: string }> }) => Promise<Response>;

const api = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
  const url = new URL(String(input), "https://p2p.example");
  const req = new Request(url);
  const m = /^\/api\/admin\/traces\/([0-9a-f-]{36})(\/raw|\/body)?$/.exec(url.pathname);
  if (url.pathname === "/api/admin/traces") return (jobsRoute as Handler)(req);
  if (url.pathname === "/api/admin/traces/calls") return (callsRoute as Handler)(req);
  if (m) {
    const h = (m[2] === "/raw" ? rawRoute : m[2] === "/body" ? bodyRoute : jobRoute) as Handler;
    return h(req, { params: Promise.resolve({ id: m[1] }) });
  }
  return new Response("not found", { status: 404 });
}) as unknown as typeof fetch;

beforeEach(() => vi.stubEnv("NEXT_PUBLIC_DATA", "mock"));
afterEach(() => vi.unstubAllEnvs());

describe("trace pages", () => {
  it("the jobs list shows both jobs with their numbers and filters them", async () => {
    render(<TraceJobs fetchImpl={api} />);
    const bronco = await screen.findByTestId(`trace-job-${BRONCO}`);
    const frenchie = screen.getByTestId(`trace-job-${FRENCHIE}`);
    expect(bronco).toHaveAttribute("href", `/admin/traces/${BRONCO}`);
    expect(within(bronco).getByText("Bronco")).toBeInTheDocument();
    expect(within(bronco).getByText("nameplate for my beagle Bronco")).toBeInTheDocument();
    expect(within(bronco).getByText("7m 59s")).toBeInTheDocument();
    expect(within(frenchie).getByText("keychain of my frenchie")).toBeInTheDocument();
    expect(within(frenchie).getByText("Keychain")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search jobs"), { target: { value: "beagle" } });
    expect(screen.queryByTestId(`trace-job-${FRENCHIE}`)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search jobs"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("radio", { name: "Failed" }));
    expect(screen.getByText("No jobs")).toBeInTheDocument();
  });

  it("the Bronco trace shows stage cards, a Meshy prompt, concept thumbnails and slicer numbers", async () => {
    render(<TraceJob jobId={BRONCO} fetchImpl={api} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Bronco" })).toBeInTheDocument();
    for (const name of ["Request", "Intake", "Concept", "3D model", "Geometry"]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("heading", { level: 2, name: "Baseline" })).toHaveLength(2);
    expect(screen.getByText("Stale")).toBeInTheDocument();
    expect(screen.getByText(/Skipped: Color spec, Multi-view, Palette, Paint, Write/)).toBeInTheDocument();
    expect(within(screen.getByTestId("trace-totals")).getByText("66")).toBeInTheDocument();

    for (const alt of ["r1c1", "r1c2", "approved"]) {
      const img = screen.getByAltText(alt);
      expect(img.getAttribute("src")).toMatch(new RegExp(`^/api/admin/traces/${BRONCO}/file\\?bucket=artifacts&path=s03_concept`));
    }
    const chips = screen.getByTestId("slicer-chips");
    for (const t of ["Gate pass", "9h 21m", "127.2 g", "Purge 98.7 g", "3 slots", "0 errors"]) {
      expect(within(chips).getByText(t)).toBeInTheDocument();
    }

    const concept = screen.getAllByTestId("trace-call").find((b) => b.textContent?.includes("concept r1c1"))!;
    fireEvent.click(concept);
    const detail = await screen.findByTestId("trace-call-detail");
    await waitFor(() => expect(within(detail).getByText(/A figure that will sit on a flat name plate/)).toBeInTheDocument());
    expect(within(detail).getByText("remove_background")).toBeInTheDocument();
    expect(within(detail).getAllByAltText("photo 1")).toHaveLength(1);
    expect(within(detail).getByAltText("r1c1")).toBeInTheDocument();
  });

  it("the calls page shows the spend summary and one row per call", async () => {
    render(<TraceCalls fetchImpl={api} />);
    const rows = await screen.findAllByTestId("call-row");
    expect(rows).toHaveLength(12);
    expect(within(screen.getByTestId("stat-Spent $")).getByText("2.64")).toBeInTheDocument();
    expect(within(screen.getByTestId("stat-Credits")).getByText("132")).toBeInTheDocument();
    expect(within(screen.getByTestId("stat-Calls")).getByText("12")).toBeInTheDocument();
    expect(within(screen.getByTestId("by-model")).getByText("multi-image-to-3d")).toBeInTheDocument();
    expect(rows[0]).toHaveAttribute("href", expect.stringMatching(new RegExp(`^/admin/traces/${FRENCHIE}#s07_baseline-a1$`)));
    fireEvent.click(screen.getByRole("radio", { name: "Claude" }));
    expect(screen.getAllByTestId("call-row")).toHaveLength(2);
  });
});

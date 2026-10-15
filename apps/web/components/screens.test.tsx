import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockClient, renderWith, seededJob } from "@/test/utils";
import { costView } from "@/lib/derive";
import { CostMeter } from "./CostMeter";
import { Filaments } from "./Filaments";
import { JobPage } from "./JobPage";
import { JobsList } from "./JobsList";
import { NewJobForm } from "./NewJobForm";
import { ReviewQueue } from "./ReviewQueue";
import { WorkerStatus } from "./WorkerStatus";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  usePathname: () => "/",
  useParams: () => ({}),
}));
// The viewer stub reports what the real one would find in the preview GLB (per-face colors or not).
const viewer = vi.hoisted(() => ({ colors: null as boolean | null }));
vi.mock("./Viewer", async () => {
  const { useEffect } = await import("react");
  return {
    Viewer: (p: { mode: string; filamentPath: string | null; onInfo?: (i: unknown) => void }) => {
      useEffect(() => {
        if (viewer.colors != null && p.mode === "filament") p.onInfo?.({ url: "x", mode: p.mode, hasColors: viewer.colors, tris: 1 });
      }, [p]);
      return <div data-testid="viewer" data-mode={p.mode} data-filament={p.filamentPath ?? ""} />;
    },
  };
});
// No WebGL in jsdom: the page's own proof renders come back empty (tiles say No render).
vi.mock("./job/glbViews", () => ({ renderGlbViews: async () => null }));
afterEach(() => {
  viewer.colors = null;
});

describe("JobsList", () => {
  it("lists seeded jobs with status, stage and cost", async () => {
    renderWith(mockClient(), <JobsList />);
    const list = await screen.findByRole("list", { name: "Jobs" });
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(10));
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(10);
    expect(within(rows[0]).getByText("Your turn")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Review")).toBeInTheDocument();
    expect(screen.getByText("PEPPER")).toBeInTheDocument();
    expect(screen.getByText("BISCUIT")).toBeInTheDocument();
    expect(screen.getByText("Nameplate")).toBeInTheDocument();
    expect(screen.getByText("Three chickens on a fence").closest("a")).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/jobs\//),
    );
  });

  it("shows the worker state and no keycaps", async () => {
    renderWith(mockClient(), <JobsList />);
    await screen.findByRole("list", { name: "Jobs" });
    expect(await screen.findByTestId("worker-status")).toHaveAttribute("data-state", "online");
    expect(document.querySelector("kbd")).toBeNull();
  });
});

describe("JobPage", () => {
  it("renders the review gate and cost for the seeded review job, without a stage list", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Miso/);
    renderWith(client, <JobPage jobId={job.id} />);
    // "Miso the tabby" -> the page talks about Miso.
    expect(await screen.findByRole("heading", { name: "Miso", level: 1 })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-stage]")).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText(/Confidence/, { selector: "p" })).toHaveTextContent("58%");
    expect(screen.getByRole("button", { name: "Cost" })).toHaveAttribute("data-level", "ok");
    expect(screen.getByTestId("model-stage")).toBeInTheDocument();
    expect(document.querySelector("kbd")).toBeNull();
  });

  it("shows the result for a done thin slice job", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    renderWith(client, <JobPage jobId={job.id} />);
    await screen.findByRole("heading", { name: "Biscuit", level: 1 });
    expect(screen.getByTestId("name-text")).toHaveTextContent("BISCUIT");
    expect(screen.getByTestId("job-meta")).toHaveTextContent("Keychain · 45 mm · Flat charm · keyring loop");
    expect(screen.getByRole("heading", { name: "Ready to print" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Download 3MF" })).toHaveAttribute("href", "/mock/sample.3mf");
    expect(screen.getByText("1h 37m")).toBeInTheDocument();
    expect(screen.getByText("Pass")).toBeInTheDocument();
    const filaments = await screen.findByRole("list", { name: "Filaments" });
    await waitFor(() => expect(within(filaments).getByText("Brown")).toBeInTheDocument());
    expect(within(filaments).getByText("9.4 g")).toBeInTheDocument();
    expect(within(filaments).getAllByRole("listitem")).toHaveLength(4);
    // A flat keychain: width, face-on height and relief thickness from the s06 bbox, as one line.
    await waitFor(() => expect(screen.getByTestId("stage-size")).toHaveTextContent("W 21.4 · H 45 · T 8 mm"));
    // Proof first: the concept next to the piece from every side. The 3D view is one click away
    // and opens on what prints (the preview in filament colors); the strip holds images only.
    expect(screen.getByTestId("proof-gallery")).toHaveAttribute("data-kind", "paint");
    await userEvent.setup().click(screen.getByRole("radio", { name: "3D" }));
    expect(screen.getByTestId("stage-view")).toHaveAttribute("data-view", "print");
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-filament", expect.stringMatching(/preview_flat\.glb$/));
    const tabs = screen.getAllByRole("tab").map((t) => t.getAttribute("aria-label"));
    expect(tabs).toEqual(["Sheet", "Concept", "Photo 1", "Photo 2"]);
    expect(screen.getByRole("radiogroup", { name: "View" })).toBeInTheDocument();
    // Printability counts, not just "warning".
    const warnings = await screen.findByTestId("warnings");
    await waitFor(() => expect(within(warnings).getByText("Warnings (3)")).toBeInTheDocument());
    expect(within(warnings).getByText("Printability warning: 164,801 degenerate faces")).toBeInTheDocument();
    expect(within(warnings).queryByText("Printability: warning")).toBeNull();
    // The palette's spool suggestion names the catalog color and applies in one click.
    const spool = await screen.findByTestId("spool-suggestion");
    await waitFor(() => expect(spool).toHaveTextContent("Add Gray PLA"));
    expect(within(spool).getByRole("button", { name: "Apply gray PLA" })).toBeEnabled();
    // No drawing-sheet decoration or jargon.
    expect(document.body.textContent).not.toMatch(/dE00|triangles|As printed|Proof \d/i);
    expect(document.querySelector("kbd")).toBeNull();
  });

  it("shows an image from the strip and returns to 3D", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    renderWith(client, <JobPage jobId={job.id} />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("radio", { name: "3D" }));
    await screen.findByTestId("stage-view");
    await u.click(screen.getByRole("tab", { name: "Photo 1" }));
    expect(screen.getByRole("tab", { name: "Photo 1" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("stage-view")).toBeNull();
    await u.click(screen.getByRole("radio", { name: "Print" }));
    expect(screen.getByTestId("stage-view")).toHaveAttribute("data-view", "print");
  });

  it("switches between the print and Meshy's model with the header toggle", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    renderWith(client, <JobPage jobId={job.id} />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("radio", { name: "3D" }));
    await screen.findByRole("radio", { name: "Print" });
    await u.click(screen.getByRole("radio", { name: "Model" }));
    expect(screen.getByTestId("stage-view")).toHaveAttribute("data-view", "model");
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-mode", "texture");
    await u.click(screen.getByRole("radio", { name: "Print" }));
    expect(screen.getByTestId("stage-view")).toHaveAttribute("data-view", "print");
  });

  it("opens the filament view on a painted preview and falls back to Texture only on an unpainted one", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    viewer.colors = true;
    const u = userEvent.setup();
    const { unmount } = renderWith(client, <JobPage jobId={job.id} />);
    await u.click(await screen.findByRole("radio", { name: "3D" }));
    await waitFor(() => expect(screen.getByTestId("viewer")).toHaveAttribute("data-mode", "filament"));
    expect(screen.getByRole("radiogroup", { name: "View" })).toBeInTheDocument();
    unmount();

    viewer.colors = false;
    renderWith(client, <JobPage jobId={job.id} />);
    await u.click(await screen.findByRole("radio", { name: "3D" }));
    expect(await screen.findByTestId("texture-only")).toHaveTextContent("Texture only");
    expect(screen.queryByRole("radiogroup", { name: "View" })).toBeNull();
    // The render sheet still shows what prints.
    await waitFor(() => expect(screen.getByRole("tab", { name: "Sheet" })).toHaveAttribute("aria-selected", "true"));
  });

  it("says Needs review with the reason when the result did not pass the slicer gate", async () => {
    const client = mockClient();
    const seeded = await seededJob(client, /Biscuit/);
    const job = client.store.jobs.get(seeded.id)!;
    client.store.jobs.set(job.id, { ...job, status: "review", review_reason: "Slicer: floating region at z 3.2 mm", result: { ...job.result!, slice_ok: false } });
    renderWith(client, <JobPage jobId={job.id} />);
    expect(await screen.findByRole("heading", { name: "Needs review" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ready to print" })).toBeNull();
    expect(screen.getByTestId("result-reason")).toHaveTextContent("Slicer: floating region at z 3.2 mm");
    expect(screen.getByText("Fail")).toBeInTheDocument();
  });

  it("shows the pause reason for a job stopped by the credit cap", async () => {
    const client = mockClient();
    const job = await seededJob(client, /chickens/);
    renderWith(client, <JobPage jobId={job.id} />);
    expect((await screen.findAllByText(/Credit cap 400/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Cost" })).toHaveAttribute("data-level", "over");

    // Resume with a higher credit cap: a contract `resume` message, and the mock worker carries on.
    const u = userEvent.setup();
    const credits = screen.getByLabelText("Cap credits");
    await u.clear(credits);
    await u.type(credits, "800");
    await u.click(screen.getByRole("button", { name: "Resume" }));
    const sent = client.store.messagesFor(job.id).filter((m) => m.kind === "resume");
    expect(sent.map((m) => m.payload)).toEqual([{ cap_usd: null, cap_credits: 800, note: null }]);
    await waitFor(() => expect(client.store.jobs.get(job.id)!.status).toBe("waiting_user"));
    expect(client.store.jobs.get(job.id)!.budget_credits).toBe(800);
    client.dispose();
  });

  it("updates live as the simulated worker progresses", async () => {
    const client = mockClient({ seed: false });
    const job = await client.createJob({
      prompt: "A bust of me",
      productType: "figurine",
      targetSizeMm: 100,
      profile: "thin_slice",
      photos: [new File([new Uint8Array([1])], "me.jpg", { type: "image/jpeg" })],
    });
    renderWith(client, <JobPage jobId={job.id} />);
    expect(await screen.findByRole("button", { name: /Send answers/ })).toBeInTheDocument();
    client.dispose();
  });
});

describe("Nameplate job", () => {
  it("shows the raised name and waits on the 3D model", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Nameplate for Pepper/);
    renderWith(client, <JobPage jobId={job.id} />);
    expect(await screen.findByTestId("name-text")).toHaveTextContent("PEPPER");
    expect(screen.getByRole("heading", { name: "Does this look like Pepper?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Yes, keep going" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recolor/ })).toBeInTheDocument();
    // The model waiting on you, from every side, before the 3D view.
    expect(screen.getByTestId("proof-gallery")).toHaveAttribute("data-kind", "model");
    await userEvent.setup().click(screen.getByRole("radio", { name: "3D" }));
    expect(screen.getByTestId("stage-view")).toHaveAttribute("data-view", "model");
    // No geometry yet: the 120 mm target is the plate width, labelled as a target.
    expect(screen.getByTestId("stage-size")).toHaveTextContent("120 mm plate target");
    // No preview GLB yet, so there is no filament view to toggle to.
    expect(screen.queryByTestId("texture-only")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "View" })).toBeNull();
  });
});

describe("Concept pick", () => {
  it("shows the photo next to both candidates in the wide band", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Ornament/);
    renderWith(client, <JobPage jobId={job.id} />);
    const band = await screen.findByTestId("concept-band");
    expect(within(band).getAllByRole("radio")).toHaveLength(2);
    // Skeletons hold the space until each image loads.
    expect(within(band).getAllByTestId("image-skeleton").length).toBeGreaterThanOrEqual(3);
    // The photo's URL resolves asynchronously.
    expect(await within(band).findByRole("img", { name: "Original photo" })).toBeInTheDocument();
    expect(band.className).toContain("minmax(420px,1fr)");
    expect(screen.queryByTestId("model-stage")).toBeNull();
  });
});

describe("CostMeter", () => {
  it("levels cost", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Ornament/);
    const cost = costView({ ...job, spent_usd: 8.2, spent_credits: 100 });
    renderWith(client, <CostMeter cost={cost} spend={[]} />);
    const meter = screen.getAllByRole("button", { name: "Cost" }).at(-1)!;
    expect(meter).toHaveAttribute("data-level", "warn");
    expect(meter).toHaveTextContent("$8.20 of $10");
  });
});

describe("Filaments", () => {
  it("shows LAB computed from hex and adds a spool", async () => {
    const client = mockClient();
    renderWith(client, <Filaments />);
    const black = await screen.findByTestId("filament-generic-pla-black");
    expect(within(black).getByTestId("lab")).toHaveTextContent("7.3 0.0 0.0");
    const red = screen.getByTestId("filament-bambu-pla-basic-red");
    expect(within(red).getByText("#F72323")).toBeInTheDocument();

    const u = userEvent.setup();
    await u.click(screen.getByRole("button", { name: /Add spool/ }));
    const form = screen.getByRole("form", { name: "Add spool" });
    await u.type(within(form).getByLabelText("Brand"), "Bambu Lab");
    await u.type(within(form).getByLabelText("Line"), "PLA Matte");
    await u.type(within(form).getByLabelText("Name"), "Ash Gray");
    await u.clear(within(form).getByLabelText("Hex"));
    await u.type(within(form).getByLabelText("Hex"), "#9B9EA0");
    expect(within(form).getByText("bambu-pla-matte-ash-gray")).toBeInTheDocument();
    await u.click(within(form).getByRole("button", { name: /Save/ }));
    const row = await screen.findByTestId("filament-bambu-pla-matte-ash-gray");
    expect(within(row).getByTestId("lab")).toHaveTextContent("64.9 -0.7 -1.4");
    expect(client.store.filaments.get("bambu-pla-matte-ash-gray")?.lab_source).toBe("nominal");
  });

  it("changes the AMS slot and uploads a swatch photo", async () => {
    const client = mockClient();
    renderWith(client, <Filaments />);
    const blue = await screen.findByTestId("filament-generic-petg-blue");
    await userEvent.setup().selectOptions(within(blue).getByLabelText("AMS slot Blue"), "5");
    await waitFor(() => expect(client.store.filaments.get("generic-petg-blue")?.ams_slot).toBe(5));

    const input = screen.getByTestId("swatch-input-generic-petg-blue");
    fireEvent.change(input, { target: { files: [new File([new Uint8Array([1])], "s.jpg", { type: "image/jpeg" })] } });
    await waitFor(() =>
      expect(client.store.filaments.get("generic-petg-blue")?.swatch_path).toMatch(/generic-petg-blue\.jpg$/),
    );
  });
});

describe("NewJobForm", () => {
  it("creates a job with photos, product, size and pipeline", async () => {
    const client = mockClient({ seed: false });
    const onCreated = vi.fn();
    renderWith(client, <NewJobForm onCreated={onCreated} />);
    const u = userEvent.setup();
    const start = screen.getByRole("button", { name: /Start job/ });
    expect(start).toBeDisabled();

    fireEvent.change(screen.getByTestId("photo-input"), {
      target: { files: [new File([new Uint8Array([1])], "dog.png", { type: "image/png" })] },
    });
    await u.type(screen.getByLabelText("Request"), "Keychain of me and my dog");
    await u.click(screen.getByRole("radio", { name: "Keychain" }));
    expect(screen.getByLabelText("Size mm")).toHaveValue("52");
    await u.type(screen.getByLabelText("Name text"), "Biscuit");
    await u.click(screen.getByRole("radio", { name: "Full" }));
    await u.click(start);

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const job = client.store.jobs.get(onCreated.mock.calls[0][0])!;
    expect(job).toMatchObject({
      prompt: "Keychain of me and my dog",
      product_type: "keychain",
      target_size_mm: 52,
      profile: "full",
      addons: [{ kind: "keyring_loop" }, { kind: "nameplate", text: { text: "Biscuit" } }],
    });
    expect(job.photos).toEqual([{ path: "input/photo_1.png" }]);
    // The mock worker may already have picked the job up on a busy machine.
    expect(["queued", "running"]).toContain(job.status);
    client.dispose();
  });
});

describe("ReviewQueue and WorkerStatus", () => {
  it("lists low-confidence jobs with their request and the Paint panel", async () => {
    renderWith(mockClient(), <ReviewQueue />);
    expect((await screen.findAllByText("Left eye region under 2 mm; merged in simulation")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    const panel = await screen.findByTestId("paint-panel");
    expect(within(panel).getByTestId("paint-iterations")).toHaveTextContent("Iteration 6 of 8");
    expect(within(panel).getByTestId("paint-confidence")).toHaveTextContent("Confidence 58%");
    expect(within(panel).getAllByTestId(/^checkpoint-/)).toHaveLength(6);
    await waitFor(() => expect(within(panel).getAllByRole("img", { name: "Print sim" })).toHaveLength(6));
    await waitFor(() => expect(within(panel).getByTestId("check-face_parts")).toHaveAttribute("data-passed", "false"));
    expect(within(panel).getByTestId("check-spec_coverage")).toHaveTextContent("Pass");
    expect(within(panel).getByTestId("check-creases")).toHaveTextContent("n/a");
    expect(within(panel).getByTestId("paint-checklist-count")).toHaveTextContent("5 of 7 pass");
  });

  it("shows the worker heartbeat state", async () => {
    renderWith(mockClient(), <WorkerStatus />);
    const el = await screen.findByTestId("worker-status");
    expect(el).toHaveAttribute("data-state", "online");
    expect(within(el).getByText("test")).toBeInTheDocument();
  });
});

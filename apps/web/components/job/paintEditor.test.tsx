// The paint editor on the job page against the mock worker and the mock paint fixture
// (public/mock/paint, made by the real pipeline). The three.js scene is replaced by a stand-in
// whose pick() returns the face the test aims at, so Brush, Fill, Pick, Undo and Send run
// exactly as in the browser up to the GPU.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MessageRow } from "@/lib/db.types";
import { decodeFaces } from "@/lib/paintEdit";
import { validatePayload } from "@/lib/validate";
import { mockClient, renderWith, seededJob, userMessages } from "@/test/utils";
import { JobPage } from "../JobPage";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/",
  useParams: () => ({}),
}));
vi.mock("../Viewer", () => ({ Viewer: () => <div data-testid="viewer" /> }));

const scene = vi.hoisted(() => ({ face: null as number | null, colors: null as Uint8Array | null, painted: [] as number[], meshes: 0 }));
vi.mock("./paintScene", () => ({
  linearRgb: (hex: string) => [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)],
  PaintScene: class {
    controls = { enabled: true };
    setMesh() {
      scene.meshes++;
    }
    setFaceColors(slots: Uint8Array) {
      scene.colors = slots;
    }
    paintFaces(faces: ArrayLike<number>) {
      scene.painted.push(...Array.from(faces));
    }
    pick() {
      return scene.face;
    }
    viewDir(): [number, number, number] {
      return [0, 1, 0];
    }
    pxPerMm() {
      return 10;
    }
    dispose() {}
  },
}));

const PUBLIC = join(__dirname, "..", "..", "public");

/** Artifact URLs of the mock client: public/ files and data: URLs. */
async function fakeFetch(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  if (url.startsWith("data:")) {
    const [, meta, body] = /^data:([^,]*),(.*)$/s.exec(url)!;
    return new Response(meta.endsWith(";base64") ? Buffer.from(body, "base64") : decodeURIComponent(body));
  }
  try {
    return new Response(readFileSync(join(PUBLIC, url.split("?")[0])));
  } catch {
    return new Response("missing", { status: 404 });
  }
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  Object.assign(scene, { face: null, colors: null, painted: [], meshes: 0 });
});
afterEach(() => vi.unstubAllGlobals());

// Faces of the fixture (region map s09_paint/a1): 1284 in s1.body (brown), 900 in the collar
// (red), 131 in s1.white_1.
const BODY = 1284;
const COLLAR = 900;
const WHITE = 131;

async function openOlive() {
  const client = mockClient();
  const job = await seededJob(client, /Olive/);
  renderWith(client, <JobPage jobId={job.id} />);
  const u = userEvent.setup();
  await u.click(await screen.findByRole("radio", { name: "Edit" }));
  const slots = await screen.findByTestId("paint-slots");
  await waitFor(() => expect(scene.colors).not.toBeNull());
  return { client, job, u, slots };
}

const edits = (client: ReturnType<typeof mockClient>, jobId: string) =>
  userMessages(client, jobId).filter((m) => m.kind === "paint_edit") as MessageRow<"paint_edit">[];

async function clickFace(face: number) {
  scene.face = face;
  const canvas = screen.getByTestId("paint-canvas");
  fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
  fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
}

describe("Paint editor", () => {
  it("loads the shipped paint: every face in its filament, the slots in palette order", async () => {
    const { client, slots } = await openOlive();
    expect(within(slots).getAllByRole("radio").map((r) => r.getAttribute("aria-label"))).toEqual([
      "Slot 1 Brown",
      "Slot 2 White",
      "Slot 3 Black",
      "Slot 4 Red",
    ]);
    expect(scene.meshes).toBe(1);
    expect(scene.colors).toHaveLength(3128);
    expect(scene.colors![BODY]).toBe(0); // brown, slot 1
    expect(scene.colors![COLLAR]).toBe(3); // red, slot 4
    expect(screen.getByTestId("paint-edits")).toHaveTextContent("No edits");
    expect(screen.getByTestId("paint-send")).toBeDisabled();
    client.dispose();
  });

  it("fills a region, brushes faces, picks a color and undoes", async () => {
    const { client, u } = await openOlive();
    // Fill: the collar takes the active slot (Black).
    await u.click(screen.getByRole("radio", { name: "Slot 3 Black" }));
    await u.click(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Fill" }));
    await act(() => clickFace(COLLAR));
    expect(screen.getByTestId("paint-edits")).toHaveTextContent("1 edit · 384 faces");
    expect(scene.colors![COLLAR]).toBe(2);
    // Pick: the white patch's filament becomes the active slot.
    await u.click(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Pick" }));
    await act(() => clickFace(WHITE));
    expect(screen.getByRole("radio", { name: "Slot 2 White" })).toHaveAttribute("aria-checked", "true");
    // Pick is an eyedropper: the tool goes back to Fill.
    expect(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Fill" })).toHaveAttribute("aria-checked", "true");
    // Brush: a dab on the body paints the faces within 2 mm, facing the camera, white.
    await u.click(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Brush" }));
    scene.face = BODY;
    const canvas = screen.getByTestId("paint-canvas");
    fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    expect(scene.painted.length).toBeGreaterThan(0);
    fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    await waitFor(() => expect(screen.getByTestId("paint-edits")).toHaveTextContent(/^2 edits/));
    expect(scene.colors![BODY]).toBe(1);
    // Red is unused now: its slot leaves the list, as the worker drops it.
    expect(screen.queryByRole("radio", { name: /Red/ })).toBeNull();
    // Undo the stroke, then the fill.
    await u.click(screen.getByRole("button", { name: "Undo" }));
    expect(scene.colors![BODY]).toBe(0);
    await u.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByTestId("paint-edits")).toHaveTextContent("No edits");
    expect(screen.getByRole("radio", { name: "Slot 4 Red" })).toBeInTheDocument();
    client.dispose();
  });

  it("sends paint_edit on the current paint, shows the worker's phase, then the new paint and the preview", async () => {
    const { client, job, u } = await openOlive();
    // A catalog color from the picker, filled into the collar.
    await u.click(within(screen.getByTestId("paint-editor")).getByRole("button", { name: "Add color" }));
    await u.click(screen.getByTestId("pick-bambu-pla-basic-orange"));
    expect(screen.getByRole("radio", { name: "Slot 5 Orange" })).toHaveAttribute("aria-checked", "true");
    await u.click(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Fill" }));
    await act(() => clickFace(COLLAR));
    await u.click(within(screen.getByRole("radiogroup", { name: "Tool" })).getByRole("radio", { name: "Brush" }));
    await u.click(screen.getByRole("radio", { name: "Slot 2 White" }));
    scene.face = BODY;
    const canvas = screen.getByTestId("paint-canvas");
    fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    fireEvent.pointerUp(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    await waitFor(() => expect(screen.getByTestId("paint-edits")).toHaveTextContent(/^2 edits/));
    await u.click(screen.getByTestId("paint-send"));

    const [edit] = edits(client, job.id);
    expect(validatePayload("paint_edit", edit.payload).ok).toBe(true);
    expect(edit.payload).toMatchObject({ base_attempt: "s09_paint/a1", source: "any" });
    const [fill, brush] = edit.payload.ops!;
    expect(fill).toEqual({ op: "fill_region", region_ids: [3], filament_id: "bambu-pla-basic-orange" });
    expect(brush.op).toBe("brush_faces");
    expect(brush.filament_id).toBe("generic-pla-white");
    expect(decodeFaces(brush.faces!)).toContain(BODY);

    // The worker's phase shows, tools lock, the result column waits.
    expect(await screen.findByTestId("paint-status")).toHaveTextContent(/Queued|Painting|Slicing|Gate check/);
    expect(screen.getByTestId("paint-send")).toBeDisabled();
    expect(await screen.findByTestId("palette-pending")).toHaveTextContent("Updating paint");
    // Done: the editor reloads the new paint (s09_paint/a2), the preview shows before and after.
    await waitFor(() => expect(screen.getByTestId("paint-edits")).toHaveTextContent("No edits"), { timeout: 8000 });
    await waitFor(() => expect(screen.getByRole("radio", { name: "Slot 4 Orange" })).toBeInTheDocument());
    const preview = await screen.findByTestId("paint-preview");
    expect(within(preview).getByText("s09_paint/a2")).toBeInTheDocument();
    await waitFor(() => expect(within(preview).getByTestId("preview-time")).toHaveTextContent("Was 1h 53m"));
    expect(within(preview).getByTestId("preview-time")).toHaveTextContent("2h 05m");
    expect(within(preview).getByTestId("preview-grams")).toHaveTextContent("25.9 g");
    expect(within(preview).getByTestId("preview-purge")).toHaveTextContent("−0.2 g");
    await waitFor(() => expect(within(preview).getAllByRole("img")).toHaveLength(8));
    // s10 gates the same paint and posts the new result.
    await waitFor(() => expect(client.store.jobs.get(job.id)!.result!.threemf).toBe("s10_write/a2/final.3mf"));
    await waitFor(() => expect(screen.queryByTestId("palette-pending")).toBeNull());
    client.dispose();
  });

  it("offers Map paint on a job with no paint map", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    renderWith(client, <JobPage jobId={job.id} />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("radio", { name: "Edit" }));
    await u.click(await screen.findByRole("button", { name: "Map paint" }));
    const [edit] = edits(client, job.id);
    expect(edit.payload).toEqual({ ops: [], source: "owned" });
    await waitFor(() => expect(screen.queryByTestId("paint-empty")).toBeNull(), { timeout: 8000 });
    client.dispose();
  });
});

it("reads the fixture's face ranges the way the worker wrote them", () => {
  const preview = JSON.parse(readFileSync(join(PUBLIC, "mock/paint/edit/paint_preview.json"), "utf8"));
  expect(preview.base_attempt).toBe("s09_paint/a1");
  expect(preview.after.slicer).toBe("p2p_slicer");
  expect(preview.proof_images).toHaveLength(16);
});

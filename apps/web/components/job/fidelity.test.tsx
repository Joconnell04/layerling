// The studio's fidelity screens in mock mode: the form choice and asks on the new-job form, the
// form question at intake, the product concept with the figure and the check, and the proof
// galleries at the mesh gate and on the result.

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockClient, renderWith, seededJob, userMessages } from "@/test/utils";
import { JobPage } from "../JobPage";
import { NewJobForm } from "../NewJobForm";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/",
  useParams: () => ({}),
}));
vi.mock("../Viewer", () => ({ Viewer: () => <div data-testid="viewer" /> }));
// The page renders a GLB from the eight views with WebGL; jsdom has none, so stand in data URLs.
const glb = vi.hoisted(() => ({ urls: [] as string[] }));
vi.mock("./glbViews", () => ({
  renderGlbViews: async (url: string) => {
    glb.urls.push(url);
    const views = ["front", "front_3q", "left_3q", "left", "right", "back", "back_3q", "top"];
    return Object.fromEntries(views.map((v) => [v, `data:image/png;base64,${v}`]));
  },
}));

const photo = () => new File([new Uint8Array([1])], "dog.png", { type: "image/png" });

describe("New job form", () => {
  it("offers the form for keychains and magnets, follows the request's words and shows the asks", async () => {
    const client = mockClient({ seed: false });
    const onCreated = vi.fn();
    renderWith(client, <NewJobForm onCreated={onCreated} />);
    const u = userEvent.setup();
    expect(screen.queryByRole("radiogroup", { name: "Form" })).toBeNull(); // figurine: always 3D

    fireEvent.change(screen.getByTestId("photo-input"), { target: { files: [photo()] } });
    await u.type(screen.getByLabelText("Request"), "3d keychain of my frenchie wearing a sombrero");
    await u.click(screen.getByRole("radio", { name: "Keychain" }));
    const form = screen.getByRole("radiogroup", { name: "Form" });
    expect(within(form).getAllByRole("radio").map((r) => r.textContent)).toEqual(["Ask", "Flat charm", "3D figure"]);
    // "3d" in the request picks 3D figure until someone picks.
    expect(within(form).getByRole("radio", { name: "3D figure" })).toHaveAttribute("aria-checked", "true");
    const asks = screen.getByRole("list", { name: "Asks" });
    expect(asks).toHaveTextContent("Outfit");
    expect(asks).toHaveTextContent("wearing a sombrero");

    await u.click(screen.getByRole("button", { name: /Start job/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(client.store.jobs.get(onCreated.mock.calls[0][0])!.form).toBe("figure");
    client.dispose();
  });

  it("leaves the form to intake on Ask, and magnets say Flat relief", async () => {
    const client = mockClient({ seed: false });
    const onCreated = vi.fn();
    renderWith(client, <NewJobForm onCreated={onCreated} />);
    const u = userEvent.setup();
    fireEvent.change(screen.getByTestId("photo-input"), { target: { files: [photo()] } });
    await u.type(screen.getByLabelText("Request"), "Magnet of my parrot");
    await u.click(screen.getByRole("radio", { name: "Magnet" }));
    const form = screen.getByRole("radiogroup", { name: "Form" });
    expect(within(form).getByRole("radio", { name: "Ask" })).toHaveAttribute("aria-checked", "true");
    expect(within(form).getByRole("radio", { name: "Flat relief" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Asks" })).toBeNull();
    await u.click(screen.getByRole("button", { name: /Start job/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(client.store.jobs.get(onCreated.mock.calls[0][0])!.form).toBeNull();
    client.dispose();
  });
});

describe("Intake", () => {
  it("asks Flat charm or 3D figure like any other question and keeps the answer", async () => {
    const client = mockClient();
    const job = await seededJob(client, /cat Pip/);
    renderWith(client, <JobPage jobId={job.id} />);
    const q = await screen.findByRole("group", { name: /Flat charm or 3D figure\?/ });
    const pose = screen.getByRole("group", { name: /Pose\?/ });
    // Same markup as the other questions: one radio per option.
    expect(within(q).getAllByRole("radio").map((r) => r.textContent)).toEqual(["3D figure", "Flat charm"]);
    expect(within(q).getByRole("radio", { name: "Flat charm" }).className).toBe(within(pose).getByRole("radio", { name: "Sitting" }).className);

    const u = userEvent.setup();
    await u.click(within(q).getByRole("radio", { name: "Flat charm" }));
    await u.click(within(pose).getByRole("radio", { name: "Sitting" }));
    await u.click(within(screen.getByRole("group", { name: /Keyring loop\?/ })).getByRole("radio", { name: "Top" }));
    await u.click(screen.getByRole("button", { name: "Send answers" }));
    const sent = userMessages(client, job.id).find((m) => m.kind === "answers")!;
    expect(sent.payload).toMatchObject({ answers: expect.arrayContaining([{ question_id: "form", option_ids: ["flat"], text: null }]) });
    await waitFor(() => expect(client.store.jobs.get(job.id)!.form).toBe("flat"), { timeout: 5_000 });
    expect(await screen.findByTestId("job-meta")).toHaveTextContent("Flat charm");
    client.dispose();
  });
});

describe("Concept pick", () => {
  it("shows the product large, the figure alone small and the check in a few words", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Juniper/);
    renderWith(client, <JobPage jobId={job.id} />);
    const band = await screen.findByTestId("concept-band");
    // What the customer gets: the flat charm with its loop.
    expect(await within(band).findByRole("img", { name: "Concept r1c1" })).toHaveAttribute("src", "/mock/charm-1.svg");
    expect(within(band).getByTestId("figure-r1c1")).toBeInTheDocument();
    expect(await within(band).findByRole("img", { name: "Figure r1c1" })).toHaveAttribute("src", "/mock/figure-1.svg");
    const checksA = within(band).getAllByRole("list", { name: "Checks" })[0];
    const chip = (li: HTMLElement) => li.querySelector("[tabindex]")!.textContent;
    expect(within(checksA).getAllByRole("listitem").map(chip)).toEqual([
      "Charm checked",
      "Loop checked",
      "Sitting checked",
      "Markings checked",
      "No extra base",
    ]);
    // B's check failed on the markings: listed first, in the bad tone.
    const checksB = within(band).getAllByRole("list", { name: "Checks" })[1];
    const off = within(checksB).getAllByRole("listitem")[0];
    expect(chip(off)).toBe("Markings off");
    expect(off).toHaveAttribute("data-passed", "false");
    expect(within(off).getByRole("tooltip")).toHaveTextContent("Chest patch smaller than in the photos");
    expect(screen.getByTestId("job-meta")).toHaveTextContent("Keychain · 45 mm · Flat charm");
  });
});

describe("Proof galleries", () => {
  it("shows the 3D keychain waiting at the mesh gate from all eight views next to the approved concept", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Mochi/);
    renderWith(client, <JobPage jobId={job.id} />);
    const gallery = await screen.findByTestId("proof-gallery");
    expect(gallery).toHaveAttribute("data-kind", "model");
    expect(gallery).toHaveAttribute("data-source", "glb");
    expect(await within(gallery).findByRole("img", { name: "Approved concept" })).toHaveAttribute("src", "/mock/kc3d-1.svg");
    await waitFor(() => expect(within(gallery).getByTestId("proof-count")).toHaveTextContent("8 of 8 views"));
    for (const label of ["Front", "Front 3/4", "Left 3/4", "Left", "Right", "Back", "Back 3/4", "Top"]) {
      expect(within(gallery).getByRole("img", { name: label })).toBeInTheDocument();
    }
    // Rendered from the model waiting for approval: the 3D keychain GLB.
    expect(glb.urls).toContain("/mock/kc3d.glb");
    expect(screen.getByRole("heading", { name: "Does this look like Mochi?" })).toBeInTheDocument();
  });

  it("shows the shipped proof set on the result: the simulated print first, then the paint and the model", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Olive/);
    renderWith(client, <JobPage jobId={job.id} />);
    const gallery = await screen.findByTestId("proof-gallery");
    expect(gallery).toHaveAttribute("data-kind", "print");
    expect(gallery).toHaveAttribute("data-source", "proof");
    const kinds = within(gallery).getByRole("radiogroup", { name: "Proof" });
    expect(within(kinds).getAllByRole("radio").map((r) => r.textContent)).toEqual(["Print", "Paint", "Model"]);
    expect(await within(gallery).findByRole("img", { name: "Back 3/4" })).toHaveAttribute("src", "/mock/proof/kc3d/print_back_3q.jpg");
    await userEvent.setup().click(within(kinds).getByRole("radio", { name: "Model" }));
    expect(await within(gallery).findByRole("img", { name: "Top" })).toHaveAttribute("src", "/mock/proof/kc3d/model_top.jpg");
    expect(screen.getByRole("heading", { name: "Ready to print" })).toBeInTheDocument();
    expect(screen.getByTestId("job-meta")).toHaveTextContent("Keychain · 45 mm · 3D figure");
  });

  it("renders a thin slice flat charm's preview from every view when no proof set shipped", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Biscuit/);
    renderWith(client, <JobPage jobId={job.id} />);
    const gallery = await screen.findByTestId("proof-gallery");
    expect(gallery).toHaveAttribute("data-kind", "paint");
    expect(gallery).toHaveAttribute("data-source", "glb");
    await waitFor(() => expect(within(gallery).getByTestId("proof-count")).toHaveTextContent("8 of 8 views"));
    expect(glb.urls).toContain("/mock/preview-flat.glb");
    expect(await within(gallery).findByRole("img", { name: "Approved concept" })).toHaveAttribute("src", "/mock/charm-2.svg");
  });
});

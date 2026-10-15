import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MessageRow } from "@/lib/db.types";
import { validatePayload } from "@/lib/validate";
import { mockClient, renderWith, seededJob, userMessages } from "@/test/utils";
import { JobPage } from "../JobPage";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/",
  useParams: () => ({}),
}));
vi.mock("../Viewer", () => ({ Viewer: () => <div data-testid="viewer" /> }));

async function openBiscuit() {
  const client = mockClient();
  const job = await seededJob(client, /Biscuit/);
  renderWith(client, <JobPage jobId={job.id} />);
  const list = await screen.findByRole("list", { name: "Filaments" });
  await waitFor(() => expect(within(list).getByText("Brown")).toBeInTheDocument());
  return { client, job, list, u: userEvent.setup() };
}

const edits = (client: ReturnType<typeof mockClient>, jobId: string) =>
  userMessages(client, jobId).filter((m) => m.kind === "palette_edit") as MessageRow<"palette_edit">[];

describe("Filament picker", () => {
  it("lists owned spools first, then catalog colors by line, with a Not owned tag and search", async () => {
    const { u } = await openBiscuit();
    await u.click(screen.getByRole("button", { name: "Change slot 1" }));
    const picker = screen.getByTestId("filament-picker");
    const groups = within(picker).getAllByRole("group").map((g) => g.getAttribute("aria-label"));
    expect(groups).toEqual(["Owned", "Bambu PETG HF", "Bambu PLA Basic", "Bambu PLA Matte"]);
    const owned = within(within(picker).getByRole("group", { name: "Owned" })).getAllByRole("option");
    expect(owned.map((o) => o.getAttribute("data-testid"))).toEqual([
      "pick-generic-pla-black",
      "pick-generic-pla-brown",
      "pick-bambu-pla-basic-red",
      "pick-generic-pla-white",
      "pick-generic-petg-blue",
    ]);
    expect(within(owned[0]).queryByText("Not owned")).toBeNull();
    expect(within(screen.getByTestId("pick-bambu-pla-basic-orange")).getByText("Not owned")).toBeInTheDocument();
    // The current color is marked.
    expect(screen.getByTestId("pick-generic-pla-brown")).toHaveAttribute("aria-selected", "true");

    await u.type(within(picker).getByLabelText("Search colors"), "charcoal");
    expect(within(picker).getAllByRole("option").map((o) => o.getAttribute("data-testid"))).toEqual(["pick-bambu-pla-matte-charcoal"]);
    await u.clear(within(picker).getByLabelText("Search colors"));
    await u.type(within(picker).getByLabelText("Search colors"), "zzz");
    expect(within(picker).getByText("No match")).toBeInTheDocument();
  });

  it("swaps a color and applies a recolor with the filament ids in slot order", async () => {
    const { client, job, list, u } = await openBiscuit();
    await u.click(screen.getByRole("button", { name: "Change slot 1" }));
    await u.click(screen.getByTestId("pick-bambu-pla-basic-orange"));
    expect(within(list).getByText("Orange")).toBeInTheDocument();
    expect(within(within(list).getByTestId("filament-row-1")).getByText("Not owned")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Apply" }));

    const [edit] = edits(client, job.id);
    expect(edit.payload).toEqual({
      slots: ["bambu-pla-basic-orange", "generic-pla-white", "generic-pla-black", "bambu-pla-basic-red"],
      mode: "recolor",
      source: "any",
    });
    expect(validatePayload("palette_edit", edit.payload).ok).toBe(true);
    // Pending until the worker posts a new result, then the rows follow the new result.
    expect(await screen.findByTestId("palette-pending")).toHaveTextContent("Updating colors");
    await waitFor(() => expect(screen.queryByTestId("palette-pending")).toBeNull());
    expect(client.store.jobs.get(job.id)!.result!.filaments![0].filament_id).toBe("bambu-pla-basic-orange");
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    client.dispose();
  });

  it("adds and removes colors and applies a refit from owned spools", async () => {
    const { client, job, list, u } = await openBiscuit();
    await u.click(screen.getByRole("button", { name: "Remove slot 4" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    await u.click(screen.getByRole("button", { name: "Add color" }));
    await u.click(screen.getByTestId("pick-generic-petg-blue"));
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(within(list).getByTestId("filament-row-4")).getByText("New")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Apply" }));
    const [edit] = edits(client, job.id);
    expect(edit.payload).toEqual({
      slots: ["generic-pla-brown", "generic-pla-white", "generic-pla-black", "generic-petg-blue"],
      mode: "refit",
      source: "owned",
    });
    client.dispose();
  });

  it("resets the draft", async () => {
    const { client, list, u } = await openBiscuit();
    await u.click(screen.getByRole("button", { name: "Remove slot 2" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    await u.click(screen.getByRole("button", { name: "Reset" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    client.dispose();
  });

  it("applies a spool suggestion in one click", async () => {
    const { client, job, u } = await openBiscuit();
    const spool = await screen.findByTestId("spool-suggestion");
    await u.click(within(spool).getByRole("button", { name: "Apply gray PLA" }));
    const [edit] = edits(client, job.id);
    // 4 of 5 AMS slots used: the gray is added and the model colors are refit.
    expect(edit.payload).toEqual({
      slots: ["generic-pla-brown", "generic-pla-white", "generic-pla-black", "bambu-pla-basic-red", "bambu-pla-basic-gray"],
      mode: "refit",
      source: "any",
    });
    expect(await screen.findByTestId("palette-pending")).toBeInTheDocument();
    client.dispose();
  });
});

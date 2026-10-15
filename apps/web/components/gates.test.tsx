import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { deriveGates } from "@/lib/derive";
import type { MessageRow } from "@/lib/db.types";
import { validatePayload } from "@/lib/validate";
import { mockClient, renderWith, seededJob, userMessages } from "@/test/utils";
import { ConceptDecision, MeshDecision } from "./job/DecisionRail";
import { ConceptBand } from "./job/ConceptBand";
import { QuestionGate } from "./QuestionGate";
import { ReviewPanel } from "./ReviewPanel";
import type { Gates } from "@/lib/derive";
import type { JobRow } from "@/lib/db.types";

vi.mock("./Viewer", () => ({ Viewer: () => <div data-testid="viewer" /> }));

describe("QuestionGate", () => {
  it("sends contract answers once every question has a choice", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Desk figure/);
    const gates = deriveGates(await client.listMessages(job.id));
    renderWith(client, <QuestionGate jobId={job.id} gate={gates.questions!} />);

    const send = screen.getByRole("button", { name: /Send answers/ });
    expect(send).toBeDisabled();
    // Subjects are people here, not pets.
    expect(screen.getByText("Me")).toBeInTheDocument();

    const u = userEvent.setup();
    await u.click(screen.getByRole("radio", { name: "Side by side" }));
    await u.click(screen.getByRole("radio", { name: "Chibi" }));
    await u.click(screen.getByRole("checkbox", { name: "Name on base" }));
    await u.click(screen.getByRole("checkbox", { name: "Accessory" }));
    await u.type(screen.getByLabelText("Extras? other"), "Bike helmets");
    expect(send).toBeEnabled();
    await u.click(send);

    const [msg] = userMessages(client, job.id) as MessageRow<"answers">[];
    expect(msg.kind).toBe("answers");
    expect(msg.reply_to).toBe(gates.questions!.message.id);
    expect(msg.stage).toBe("s01_intake");
    expect(msg.payload.answers).toEqual([
      { question_id: "pose", option_ids: ["side"], text: null },
      { question_id: "style", option_ids: ["chibi"], text: null },
      { question_id: "extras", option_ids: ["name", "accessory"], text: "Bike helmets" },
    ]);
    expect(validatePayload("answers", msg.payload).ok).toBe(true);
  });
});

/** Concept band candidates and the rail's concept decision share the selected base. */
function ConceptHarness({ job, gates }: { job: JobRow; gates: Gates }) {
  const [base, setBase] = useState<string | null>(null);
  return (
    <>
      <ConceptBand job={job} gates={gates} base={base} onBase={setBase} />
      <ConceptDecision job={job} gate={gates.concepts!} name="Pepper" base={base} showPhoto={false} />
    </>
  );
}

describe("Concept decision", () => {
  it("approves the selected candidate with concept_pick approve=true", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Ornament/);
    const gates = deriveGates(await client.listMessages(job.id));
    renderWith(client, <ConceptHarness job={job} gates={gates} />);

    expect(screen.getByRole("heading", { name: "Which one looks like Pepper?" })).toBeInTheDocument();
    const u = userEvent.setup();
    await u.click(screen.getByRole("radio", { name: "Use r1c2 as base" }));
    await u.click(screen.getByRole("button", { name: "Go with B" }));
    const [pick] = userMessages(client, job.id).filter((m) => m.kind === "concept_pick") as MessageRow<"concept_pick">[];
    expect(pick.payload).toEqual({ candidate_id: "r1c2", approve: true });
    expect(pick.reply_to).toBe(gates.concepts!.latest.id);
  });

  it("tweaks from the chosen base: pick approve=false, then tweak", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Ornament/);
    const gates = deriveGates(await client.listMessages(job.id));
    renderWith(client, <ConceptHarness job={job} gates={gates} />);

    const u = userEvent.setup();
    await u.click(screen.getByRole("radio", { name: "Use r1c2 as base" }));
    expect(screen.getByRole("radio", { name: "Use r1c2 as base" })).toHaveAttribute("aria-checked", "true");
    await u.type(screen.getByLabelText("Tweak"), "Add a red scarf{Enter}");

    const msgs = userMessages(client, job.id).filter((m) => m.kind !== "answers");
    expect(msgs.map((m) => m.kind)).toEqual(["concept_pick", "tweak"]);
    expect(msgs[0].payload).toEqual({ candidate_id: "r1c2", approve: false });
    expect(msgs[1].payload).toEqual({ text: "Add a red scarf", candidate_id: "r1c2" });
    expect(screen.getByText("5 rounds left")).toBeInTheDocument();
  });

  it("shows the tweak thread for a seeded tweak loop", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Miso/);
    const gates = deriveGates(await client.listMessages(job.id));
    renderWith(client, <ConceptDecision job={job} gate={gates.concepts!} name={null} base={null} />);
    expect(screen.getByText("Bigger ears, rounder cushion")).toBeInTheDocument();
    expect(screen.getByText("rerender concept")).toBeInTheDocument();
  });
});

describe("Mesh decision", () => {
  it("sends retexture with the note, and approve as 'Yes, keep going'", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Nameplate for Pepper/);
    const gate = deriveGates(await client.listMessages(job.id)).mesh!;
    renderWith(client, <MeshDecision job={job} gate={gate} name="Pepper" />);
    expect(screen.getByRole("heading", { name: "Does this look like Pepper?" })).toBeInTheDocument();

    const u = userEvent.setup();
    await u.type(screen.getByLabelText("Change something"), "Warmer fur");
    await u.click(screen.getByRole("button", { name: "Recolor · 10 cr" }));
    await waitFor(() => expect(userMessages(client, job.id).at(-1)?.kind).toBe("mesh_decision"));
    const last = userMessages(client, job.id).at(-1) as MessageRow<"mesh_decision">;
    expect(last.payload).toEqual({ action: "retexture", text: "Warmer fur" });
    expect(last.reply_to).toBe(gate.latest.id);
    expect(screen.getByRole("button", { name: "Redo · 35 cr" })).toBeInTheDocument();
    client.dispose();
  });
});

describe("ReviewPanel", () => {
  it("sends review_decision rerun_paint with notes", async () => {
    const client = mockClient();
    const job = await seededJob(client, /Miso/);
    const gates = deriveGates(await client.listMessages(job.id));
    renderWith(
      client,
      <ReviewPanel jobId={job.id} request={gates.review!.request} decision={gates.review!.decision} />,
    );
    expect(screen.getByText(/Confidence 58%/)).toBeInTheDocument();
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("Notes"), "Grow left eye");
    await u.click(screen.getByRole("button", { name: "Rerun paint" }));
    const last = userMessages(client, job.id).at(-1) as MessageRow<"review_decision">;
    expect(last.payload).toEqual({ action: "rerun_paint", notes: "Grow left eye" });
    expect(last.stage).toBe("s09_paint");
  });
});

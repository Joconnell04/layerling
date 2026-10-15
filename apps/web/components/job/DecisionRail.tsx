"use client";

// The job page's right column: the one decision waiting on you, in plain words
// ("Does this look like Biscuit?"), with pill buttons, then spend against the caps. Once a result
// exists the column is the ResultRail (colors, picker, download).

import { useEffect, useRef, useState } from "react";
import { STAGE_BY_ID } from "@/lib/contracts";
import type { AnyMessage, FilamentRow, JobRow, SpendRow } from "@/lib/db.types";
import type { CostView, Gates } from "@/lib/derive";
import type { SpoolSuggestion } from "@/lib/spool";
import { usd } from "@/lib/format";
import { useData } from "@/lib/hooks";
import { ArtifactImage } from "../ArtifactImage";
import { CostMeter } from "../CostMeter";
import { QuestionGate } from "../QuestionGate";
import { ReviewPanel } from "../ReviewPanel";
import { Button, ErrorLine, Tag } from "../ui";
import { ResultRail } from "./ResultRail";

export { resultVerdict } from "./ResultRail";

const H2 = "font-display text-[30px] font-bold leading-[1.05] tracking-[-0.01em]";
const PRIMARY =
  "h-[54px] w-full rounded-full bg-accent text-[17px] font-semibold text-accent-fg hover:brightness-110 disabled:opacity-50";
const TEXTAREA =
  "w-full resize-none rounded-[10px] border border-line-strong bg-panel px-3.5 py-3 text-[15px] placeholder:text-faint focus:border-ink focus:outline-none";

export function DecisionRail({
  job,
  gates,
  name,
  filaments,
  cost,
  spend,
  base,
  suggestions = [],
  messages = [],
  paintBusy = false,
}: {
  job: JobRow;
  gates: Gates;
  name: string | null;
  filaments: FilamentRow[];
  cost: CostView;
  spend: SpendRow[];
  base: string | null;
  /** Spool suggestions from the palette stage. */
  suggestions?: SpoolSuggestion[];
  /** The job's messages (a palette edit waits for the next result). */
  messages?: AnyMessage[];
  /** A paint edit is on its way to a new result. */
  paintBusy?: boolean;
}) {
  const result = job.result ?? gates.result?.payload ?? null;
  const meter = <CostMeter cost={cost} spend={spend} />;
  const reviewing = gates.active === "review_request" && !!gates.review;
  if (result && !reviewing && !["mesh_review", "concepts", "questions"].includes(gates.active ?? "")) {
    return (
      <ResultRail
        job={job}
        result={result}
        name={name}
        filaments={filaments}
        messages={messages}
        suggestions={suggestions}
        cost={meter}
        paintBusy={paintBusy}
      />
    );
  }
  let body: React.ReactNode;
  if (gates.active === "review_request" && gates.review) {
    body = (
      <>
        <h2 className={H2}>Review</h2>
        <ReviewPanel jobId={job.id} request={gates.review.request} decision={gates.review.decision} bare />
      </>
    );
  } else if (gates.active === "mesh_review" && gates.mesh) {
    body = <MeshDecision job={job} gate={gates.mesh} name={name} />;
  } else if (gates.active === "concepts" && gates.concepts) {
    body = <ConceptDecision job={job} gate={gates.concepts} name={name} base={base} showPhoto={false} />;
  } else if (gates.active === "questions" && gates.questions) {
    body = (
      <>
        <h2 className={H2}>A few questions</h2>
        <QuestionGate jobId={job.id} gate={gates.questions} bare />
      </>
    );
  } else if (gates.concepts?.pending) {
    body = <ConceptDecision job={job} gate={gates.concepts} name={name} base={base} showPhoto={false} />;
  } else {
    body = <StatusBlock job={job} />;
  }

  return (
    <div className="flex h-full flex-col gap-[18px]" data-testid="decision-rail">
      {body}
      <div className="mt-auto border-t border-line pt-4">{meter}</div>
    </div>
  );
}

function FirstPhoto({ job }: { job: JobRow }) {
  const p = job.photos[0];
  if (!p) return null;
  return (
    <ArtifactImage jobId={job.id} path={p.path} bucket="inputs" alt="Original photo" className="h-[190px] w-full rounded-lg" />
  );
}

// ------------------------------------------------------------------ 3D model

export function MeshDecision({ job, gate, name }: { job: JobRow; gate: NonNullable<Gates["mesh"]>; name: string | null }) {
  const client = useData();
  const review = gate.latest.payload;
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const actions = review.actions ?? ["approve", "retexture", "regenerate"];

  const decide = async (action: "approve" | "retexture" | "regenerate") => {
    if (!client || busy) return;
    setBusy(action);
    setError(null);
    try {
      await client.sendMessage({
        jobId: job.id,
        kind: "mesh_decision",
        payload: { action, text: note.trim() || null },
        replyTo: gate.latest.id,
        stage: "s05_mesh",
      });
      setNote("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <h2 className={H2}>{name ? `Does this look like ${name}?` : "Does this look right?"}</h2>
      <FirstPhoto job={job} />
      {actions.includes("approve") && (
        <button type="button" className={PRIMARY} disabled={!!busy} onClick={() => void decide("approve")}>
          Yes, keep going
        </button>
      )}
      <div className="grid grid-cols-2 gap-2.5">
        {actions.includes("retexture") && (
          <Button className="h-[46px] text-[15px] font-normal" disabled={!!busy} onClick={() => void decide("retexture")}>
            Recolor · {review.retexture_credits ?? 10} cr
          </Button>
        )}
        {actions.includes("regenerate") && (
          <Button className="h-[46px] text-[15px] font-normal" disabled={!!busy} onClick={() => void decide("regenerate")}>
            Redo{review.regenerate_credits != null ? ` · ${review.regenerate_credits} cr` : ""}
          </Button>
        )}
      </div>
      <label htmlFor="mesh-note" className="text-sm font-semibold">
        Change something
      </label>
      <textarea
        id="mesh-note"
        rows={2}
        placeholder="Make the ears longer"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        className={TEXTAREA}
      />
      <ErrorLine>{error}</ErrorLine>
    </>
  );
}

// ------------------------------------------------------------------ concepts

export function ConceptDecision({
  job,
  gate,
  name,
  base,
  showPhoto = true,
}: {
  job: JobRow;
  gate: NonNullable<Gates["concepts"]>;
  name: string | null;
  base: string | null;
  /** The concept band already shows the photo next to the candidates. */
  showPhoto?: boolean;
}) {
  const client = useData();
  const round = gate.latest.payload;
  const selected = base && round.candidates.some((c) => c.id === base) ? base : (round.candidates[0]?.id ?? "");
  const letter = String.fromCharCode(65 + Math.max(0, round.candidates.findIndex((c) => c.id === selected)));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canTweak = gate.open && round.rounds_left > 0;

  const run = async (fn: () => Promise<unknown>) => {
    if (!client || busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const approve = () =>
    run(() =>
      client!.sendMessage({
        jobId: job.id,
        kind: "concept_pick",
        payload: { candidate_id: selected, approve: true },
        replyTo: gate.latest.id,
        stage: "s03_concept",
      }),
    );

  // payloads.py: concept_pick(approve=false) marks the base; the tweak carries the text.
  const tweak = () =>
    run(async () => {
      const t = text.trim();
      if (!t) return;
      await client!.sendMessage({
        jobId: job.id,
        kind: "concept_pick",
        payload: { candidate_id: selected, approve: false },
        replyTo: gate.latest.id,
        stage: "s03_concept",
      });
      await client!.sendMessage({
        jobId: job.id,
        kind: "tweak",
        payload: { text: t, candidate_id: selected },
        replyTo: gate.latest.id,
        stage: "s03_concept",
      });
      setText("");
    });

  return (
    <>
      <h2 className={H2}>{name ? `Which one looks like ${name}?` : "Which one is closer?"}</h2>
      {showPhoto && <FirstPhoto job={job} />}
      {gate.pending ? (
        <p className="pulse text-base font-semibold">Rendering round {round.round + 1}</p>
      ) : (
        <button type="button" className={PRIMARY} disabled={busy || !gate.open} onClick={() => void approve()}>
          Go with {letter}
        </button>
      )}
      <label htmlFor="concept-tweak" className="flex items-baseline justify-between text-sm font-semibold">
        Change something
        <span className="num text-xs font-normal text-muted">
          {round.rounds_left} {round.rounds_left === 1 ? "round" : "rounds"} left
        </span>
      </label>
      <textarea
        id="concept-tweak"
        aria-label="Tweak"
        rows={2}
        placeholder={canTweak ? `Starting from ${letter}` : gate.pending ? "Rendering" : "No rounds left"}
        value={text}
        disabled={!canTweak || busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void tweak();
          }
        }}
        className={TEXTAREA}
      />
      <Button disabled={!canTweak || busy || !text.trim()} onClick={() => void tweak()} className="self-start">
        Send change
      </Button>
      <TweakThread thread={gate.thread} />
      <ErrorLine>{error}</ErrorLine>
    </>
  );
}

function TweakThread({ thread }: { thread: AnyMessage[] }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [thread.length]);
  if (!thread.length) return null;
  return (
    <div ref={box} className="max-h-56 space-y-2 overflow-y-auto border-t border-line-strong/40 pt-3" aria-label="Tweaks">
      {thread.map((m) =>
        m.kind === "tweak" ? (
          <p key={m.id} className="ml-6 rounded-lg rounded-br-sm bg-panel px-3 py-2 text-sm">
            {m.payload.text}
          </p>
        ) : m.kind === "tweak_decision" ? (
          <p key={m.id} className="text-sm text-muted">
            <Tag className="mr-1.5">{m.payload.route.replace(/_/g, " ")}</Tag>
            {m.payload.followup_question ?? m.payload.instruction}
          </p>
        ) : m.kind === "note" ? (
          <p key={m.id} className="text-xs text-muted">
            {m.payload.text}
          </p>
        ) : null,
      )}
    </div>
  );
}

// ------------------------------------------------------------------ result and status

function StatusBlock({ job }: { job: JobRow }) {
  const stage = job.stage ? STAGE_BY_ID[job.stage] : null;
  if (job.status === "paused") return <ResumeForm job={job} />;
  if (job.status === "failed") {
    return (
      <>
        <h2 className={H2}>Stopped</h2>
        <p className="text-base">{job.review_reason ?? "A stage failed"}</p>
      </>
    );
  }
  return (
    <>
      <h2 className={H2}>{stage ? `Working on ${stage.name.toLowerCase()}` : "Queued"}</h2>
      <p className="pulse text-sm text-muted">{stage ? `Stage ${stage.number} of 10` : "Waiting for the worker"}</p>
      <FirstPhoto job={job} />
    </>
  );
}

/** A paused job (budget cap, retry limit): resume it, optionally with higher caps for this job. */
export function ResumeForm({ job }: { job: JobRow }) {
  const client = useData();
  const [capUsd, setCapUsd] = useState(String(job.budget_usd));
  const [capCredits, setCapCredits] = useState(String(job.budget_credits));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const usdN = Number(capUsd);
  const crN = Number(capCredits);
  const valid = Number.isFinite(usdN) && usdN > 0 && Number.isInteger(crN) && crN > 0;

  const resume = async () => {
    if (!client || busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      await client.sendMessage({
        jobId: job.id,
        kind: "resume",
        payload: {
          cap_usd: usdN !== job.budget_usd ? usdN : null,
          cap_credits: crN !== job.budget_credits ? crN : null,
          note: note.trim() || null,
        },
        stage: job.stage,
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const input =
    "num h-11 w-full rounded-md border border-line-strong bg-panel px-3 text-[15px] focus:border-ink focus:outline-none";
  return (
    <>
      <h2 className={H2}>Paused</h2>
      <p className="text-base">{job.pause_reason ?? "Needs a look"}</p>
      <div className="grid grid-cols-2 gap-2.5">
        <label className="flex flex-col gap-1 text-sm font-semibold">
          Cap $
          <input aria-label="Cap USD" inputMode="decimal" value={capUsd} onChange={(e) => setCapUsd(e.target.value)} className={input} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-semibold">
          Cap credits
          <input aria-label="Cap credits" inputMode="numeric" value={capCredits} onChange={(e) => setCapCredits(e.target.value)} className={input} />
        </label>
      </div>
      <textarea aria-label="Resume note" rows={2} placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} className={TEXTAREA} />
      <button type="button" className={PRIMARY} disabled={busy || !valid} onClick={() => void resume()}>
        Resume
      </button>
      <ErrorLine>{error}</ErrorLine>
    </>
  );
}

export function CostRows({ cost }: { cost: CostView }) {
  return (
    <div className="num flex flex-col gap-2 text-[15px]">
      <div className="flex justify-between">
        <span className="text-muted">Spent</span>
        <span className="font-semibold">
          {usd(cost.usd)} of {usd(cost.capUsd, 0)}
        </span>
      </div>
      <div className="flex justify-between">
        <span className="text-muted">Credits</span>
        <span className="font-semibold">
          {cost.credits} of {cost.capCredits}
        </span>
      </div>
    </div>
  );
}

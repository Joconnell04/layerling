"use client";

import { useState } from "react";
import type { Answer, Question } from "@/lib/contracts";
import type { Gates } from "@/lib/derive";
import { useData } from "@/lib/hooks";
import { Button, ErrorLine, Input, Panel, Tag, Tip, cx } from "./ui";

type Draft = Record<string, { options: string[]; text: string }>;

export function buildAnswers(questions: Question[], draft: Draft): Answer[] {
  return questions.map((q) => {
    const d = draft[q.id] ?? { options: [], text: "" };
    const text = d.text.trim();
    return { question_id: q.id, option_ids: d.options, text: text ? text : null };
  });
}

export function QuestionGate({
  jobId,
  gate,
  bare,
}: {
  jobId: string;
  gate: NonNullable<Gates["questions"]>;
  /** Render only the questions (for the decision rail), without the panel frame. */
  bare?: boolean;
}) {
  const client = useData();
  const { message, answer, open } = gate;
  const questions = message.payload.questions;
  const subjects = message.payload.subjects;
  const [draft, setDraft] = useState<Draft>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(!open);

  const answered = (q: Question) => {
    const d = draft[q.id];
    return !!d && (d.options.length > 0 || d.text.trim().length > 0);
  };
  const complete = questions.every(answered);

  const toggle = (q: Question, optionId: string) => {
    setDraft((cur) => {
      const d = cur[q.id] ?? { options: [], text: "" };
      const has = d.options.includes(optionId);
      const options = q.multi
        ? has
          ? d.options.filter((o) => o !== optionId)
          : [...d.options, optionId]
        : has
          ? []
          : [optionId];
      return { ...cur, [q.id]: { ...d, options } };
    });
  };

  const send = async () => {
    if (!client || !complete || busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.sendMessage({
        jobId,
        kind: "answers",
        payload: { answers: buildAnswers(questions, draft) },
        replyTo: message.id,
        stage: "s01_intake",
      });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const given = answer?.payload.answers ?? [];

  const body = (
    <div className="space-y-4">
      {questions.map((q, qi) => {
        const prior = given.find((a) => a.question_id === q.id);
        const selected = open ? (draft[q.id]?.options ?? []) : (prior?.option_ids ?? []);
        return (
          <fieldset key={q.id} className="space-y-1.5" disabled={!open || busy}>
            <legend className="flex items-center gap-2 text-[15px] font-semibold text-fg">
              <span className="num text-xs font-normal text-faint">{qi + 1}</span>
              {q.text}
              {q.multi && <span className="text-xs font-normal text-faint">Multi</span>}
            </legend>
            <div className="flex flex-wrap gap-2 pt-0.5">
              {q.options.map((o) => {
                const on = selected.includes(o.id);
                return (
                  <button
                    key={o.id}
                    type="button"
                    role={q.multi ? "checkbox" : "radio"}
                    aria-checked={on}
                    onClick={() => toggle(q, o.id)}
                    className={cx(
                      "h-9 rounded-full border px-4 text-sm font-medium transition-colors disabled:cursor-default",
                      on ? "border-ink bg-ink text-bg" : "border-line-strong bg-panel text-fg enabled:hover:border-ink",
                      !open && !on && "opacity-50",
                    )}
                  >
                    {o.label}
                  </button>
                );
              })}
              {q.allow_text && open && (
                <Input
                  aria-label={`${q.text} other`}
                  placeholder="Other"
                  className="h-9 w-44 rounded-full px-4"
                  value={draft[q.id]?.text ?? ""}
                  onChange={(e) =>
                    setDraft((cur) => ({
                      ...cur,
                      [q.id]: { options: cur[q.id]?.options ?? [], text: e.target.value },
                    }))
                  }
                />
              )}
              {!open && prior?.text && <Tag>{prior.text}</Tag>}
            </div>
          </fieldset>
        );
      })}
      {open && (
        <div className="flex items-center gap-3 pt-1">
          <Button variant="primary" disabled={!complete || busy} onClick={() => void send()}>
            {busy ? "Sending" : "Send answers"}
          </Button>
          <span className="num text-xs text-faint">
            {questions.filter(answered).length}/{questions.length}
          </span>
        </div>
      )}
      <ErrorLine>{error}</ErrorLine>
    </div>
  );

  if (bare) return body;

  return (
    <Panel
      id="gate-questions"
      active={open}
      title={
        <>
          Intake
          <span className="num text-xs font-normal text-faint">{questions.length} questions</span>
          {!open && answer && (
            <span className="text-xs font-normal text-faint">{answer.consumed_at ? "Answered" : "Sent"}</span>
          )}
        </>
      }
      collapsed={!open && collapsed}
      onToggle={open ? undefined : () => setCollapsed((c) => !c)}
      right={
        subjects.length > 0 && (
          <div className="flex gap-1">
            {subjects.map((s) => (
              <Tip key={s.id} label={s.description}>
                <Tag>
                  {s.name}
                  {s.kind.toLowerCase() !== s.name.toLowerCase() && <span className="ml-1 text-faint">{s.kind}</span>}
                </Tag>
              </Tip>
            ))}
          </div>
        )
      }
    >
      {body}
    </Panel>
  );
}

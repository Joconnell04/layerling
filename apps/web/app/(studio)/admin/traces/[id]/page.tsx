"use client";

import { useParams } from "next/navigation";
import { TraceJob } from "../_components/TraceJob";

export default function Page() {
  const params = useParams<{ id: string }>();
  return <TraceJob jobId={params.id} />;
}

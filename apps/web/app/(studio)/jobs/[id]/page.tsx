"use client";

import { useParams } from "next/navigation";
import { JobPage } from "@/components/JobPage";

export default function Page() {
  const params = useParams<{ id: string }>();
  return <JobPage jobId={params.id} />;
}

// Browser side of signed uploads: PUT the file to the URL the server handed out.

export interface UploadTarget {
  key: string;
  url: string;
  method: "PUT";
  headers: Record<string, string>;
}

export async function putFile(target: UploadTarget, file: Blob, fetchImpl: typeof fetch = fetch): Promise<void> {
  const res = await fetchImpl(target.url, { method: target.method, headers: target.headers, body: file });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Upload failed (${res.status})${detail ? `: ${detail.slice(0, 120)}` : ""}`);
  }
}

export function photoMeta(file: File): { name: string; type: string; size: number } {
  return { name: file.name, type: file.type, size: file.size };
}

export async function sha256Hex(file: Blob): Promise<string | null> {
  try {
    const d = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

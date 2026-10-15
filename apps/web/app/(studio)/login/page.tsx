"use client";

import { useState } from "react";
import { Button, ErrorLine, Input, Label } from "@/components/ui";
import { useSession } from "@/lib/hooks";

export default function LoginPage() {
  const { client } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mock mode never shows login; AppShell redirects.
  if (!client || client.mode === "mock") return null;

  return (
    <form
      className="mx-auto mt-[18vh] w-full max-w-xs space-y-3 rounded-lg border border-line bg-panel p-4"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await client.signIn(email.trim(), password);
        } catch (err) {
          setError((err as Error).message);
          setBusy(false);
        }
      }}
    >
      <h1 className="font-display text-2xl font-bold">layerling</h1>
      <div>
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </div>
      <div>
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </div>
      <Button type="submit" variant="primary" className="w-full" disabled={busy || !email || !password}>
        {busy ? "Signing in" : "Sign in"}
      </Button>
      <ErrorLine>{error}</ErrorLine>
    </form>
  );
}

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminOrders, type AdminOrder } from "../AdminOrders";
import { AccessForm, safeNext } from "./AccessForm";
// OrderForm tests live in orderForm.test.tsx.

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams("src=flyer"),
  usePathname: () => "/order",
}));

type Call = { url: string; init?: RequestInit };

function fetchStub(routes: Record<string, (init?: RequestInit) => Response>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url);
    calls.push({ url: u, init });
    const key = Object.keys(routes).find((k) => u.startsWith(k));
    if (!key) return new Response("not found", { status: 404 });
    return routes[key](init);
  });
  return { fn: fn as unknown as typeof fetch, calls };
}

describe("AccessForm", () => {
  it("posts the code and reports wrong codes and lockouts", async () => {
    let status = 401;
    const { fn, calls } = fetchStub({
      "/api/access": () => Response.json({ error: "x", retry_after_s: 600 }, { status }),
    });
    render(<AccessForm fetchImpl={fn} />);
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("Access code"), "nope{Enter}");
    expect(await screen.findByText("Wrong code")).toBeInTheDocument();
    expect(JSON.parse(calls[0].init!.body as string)).toEqual({ code: "nope" });
    status = 429;
    await u.type(screen.getByLabelText("Access code"), "again{Enter}");
    expect(await screen.findByText("Too many attempts. Try again in 10 min")).toBeInTheDocument();
  });

  it("only redirects to studio paths", () => {
    expect(safeNext("/admin/orders")).toBe("/admin/orders");
    expect(safeNext("/jobs/abc?x=1")).toBe("/jobs/abc?x=1");
    expect(safeNext("//evil.example")).toBe("/jobs");
    expect(safeNext("https://evil.example")).toBe("/jobs");
    expect(safeNext("/order")).toBe("/jobs");
    expect(safeNext(null)).toBe("/jobs");
  });
});

describe("AdminOrders", () => {
  const order: AdminOrder = {
    id: "o1",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    status: "new",
    name: "TEST ORDER",
    email: "test@example.com",
    phone: null,
    product_type: "nameplate",
    size_tier: "m",
    size_mm: 120,
    quantity: 1,
    name_text: "PEPPER",
    request: "Beagle on a nameplate",
    photos: ["o1/photo_1.jpg"],
    source: "web",
    notified_at: null,
    notify_error: "no provider",
    job_id: null,
    photo_urls: ["data:image/png;base64,AAAA"],
  };

  it("lists orders, changes status and starts a job", async () => {
    const { fn, calls } = fetchStub({
      "/api/admin/orders/o1/job": () => Response.json({ job_id: "j1", order: { ...order, status: "accepted", job_id: "j1" } }),
      "/api/admin/orders/o1": () => Response.json({ order: { ...order, status: "contacted" } }),
      "/api/admin/orders": () => Response.json({ orders: [order] }),
    });
    render(<AdminOrders fetchImpl={fn} />);
    const row = await screen.findByTestId("order-o1");
    expect(within(row).getByText("PEPPER")).toBeInTheDocument();
    expect(within(row).getByText("No email")).toBeInTheDocument();
    const u = userEvent.setup();
    await u.selectOptions(within(row).getByLabelText("Status TEST ORDER"), "contacted");
    expect(calls.find((c) => c.init?.method === "PATCH")?.init?.body).toBe(JSON.stringify({ status: "contacted" }));
    await u.click(within(row).getByRole("button", { name: "Start job" }));
    expect(await within(row).findByRole("link", { name: "Open job" })).toHaveAttribute("href", "/jobs/j1");
  });
});

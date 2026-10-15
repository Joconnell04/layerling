import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { quote } from "@/lib/pricing";
import { OrderForm, acceptPhoto, COPY } from "./OrderForm";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
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

const okRoutes = () =>
  fetchStub({
    "/api/orders/start": () =>
      Response.json({
        order_id: "abcd1234-0000-4000-8000-000000000000",
        ticket: "t1",
        uploads: [{ key: "abcd/photo_1.jpg", url: "https://storage.example/up1", method: "PUT", headers: { "content-type": "image/jpeg" } }],
      }),
    "https://storage.example/up1": () => new Response("{}", { status: 200 }),
    "/api/orders/finish": () =>
      Response.json({ order_id: "abcd1234-0000-4000-8000-000000000000", status: "new", receipt: "r.sig" }),
  });

const photo = () => new File([new Uint8Array(10)], "dog.jpg", { type: "image/jpeg" });

beforeEach(() => {
  push.mockReset();
  // IntersectionObserver is not in jsdom; the typing hint only needs the constructor.
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("OrderForm", () => {
  it("sends a keychain with nothing typed in What you want, then opens the signed confirmation link", async () => {
    const { fn, calls } = okRoutes();
    render(<OrderForm fetchImpl={fn} />);
    const u = userEvent.setup();
    expect(screen.getByRole("radio", { name: "Keychain" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByLabelText("Name on it")).toBeNull();
    expect(screen.queryByLabelText("Quantity")).toBeNull();
    fireEvent.change(screen.getByTestId("order-photos"), { target: { files: [photo()] } });
    await u.type(screen.getByLabelText("Your name"), "TEST ORDER");
    await u.type(screen.getByLabelText(/^Email/), "test@example.com");
    await u.click(screen.getByRole("button", { name: "Send order" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/order/thanks?r=r.sig"));
    expect(calls.map((c) => c.url)).toEqual(["/api/orders/start", "https://storage.example/up1", "/api/orders/finish"]);
    const body = JSON.parse(calls[0].init!.body as string);
    expect(body).toMatchObject({
      name: "TEST ORDER",
      email: "test@example.com",
      phone: "",
      product_type: "keychain",
      size_tier: "m",
      size_mm: "52",
      price: quote("keychain", "m").amount,
      request: "",
      website: "",
      photos: [{ name: "dog.jpg", type: "image/jpeg", size: 10 }],
    });
    expect(body).not.toHaveProperty("name_text");
    expect(body).not.toHaveProperty("quantity");
    expect(calls[1].init?.method).toBe("PUT");
  });

  it("sends the picked size tier and a phone number without an email", async () => {
    const { fn, calls } = okRoutes();
    render(<OrderForm fetchImpl={fn} />);
    const u = userEvent.setup();
    await u.click(screen.getByRole("radio", { name: "Nameplate" }));
    await u.click(screen.getByRole("radio", { name: /^L, 150 millimeters wide/ }));
    expect(screen.getByTestId("summary-item")).toHaveTextContent("Nameplate, L");
    fireEvent.change(screen.getByTestId("order-photos"), { target: { files: [photo()] } });
    await u.type(screen.getByLabelText("Your name"), "TEST ORDER");
    await u.type(screen.getByLabelText(/^Phone/), "555 010 1234");
    await u.click(screen.getByRole("button", { name: "Send order" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const body = JSON.parse(calls[0].init!.body as string);
    expect(body).toMatchObject({ product_type: "nameplate", size_tier: "l", size_mm: "150", email: "", phone: "555 010 1234" });
  });

  it("needs an email or a phone and marks both invalid until one is filled", async () => {
    const { fn, calls } = okRoutes();
    render(<OrderForm fetchImpl={fn} />);
    const u = userEvent.setup();
    fireEvent.change(screen.getByTestId("order-photos"), { target: { files: [photo()] } });
    await u.type(screen.getByLabelText("Your name"), "TEST ORDER");
    await u.click(screen.getByRole("button", { name: "Send order" }));
    expect(await screen.findByText(COPY.contact)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Email/)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/^Phone/)).toHaveAttribute("aria-invalid", "true");
    expect(calls).toHaveLength(0);
    await u.type(screen.getByLabelText(/^Phone/), "5");
    expect(screen.queryByText(COPY.contact)).toBeNull();
  });

  it("Other hides sizes, asks what to make, and needs at least 10 characters", async () => {
    const { fn, calls } = okRoutes();
    render(<OrderForm fetchImpl={fn} />);
    const u = userEvent.setup();
    await u.click(screen.getByRole("radio", { name: "Other" }));
    expect(screen.queryByRole("radiogroup", { name: "Size" })).toBeNull();
    const request = screen.getByLabelText("What should we make?");
    expect(request).not.toHaveAttribute("placeholder");
    fireEvent.change(screen.getByTestId("order-photos"), { target: { files: [photo()] } });
    await u.type(request, "planter");
    await u.type(screen.getByLabelText("Your name"), "TEST ORDER");
    await u.type(screen.getByLabelText(/^Email/), "test@example.com");
    await u.click(screen.getByRole("button", { name: "Send order" }));
    expect(await screen.findByText(COPY.otherRequest)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
    await u.type(request, " shaped like my dog");
    await u.click(screen.getByRole("button", { name: "Send order" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const body = JSON.parse(calls[0].init!.body as string);
    expect(body).toMatchObject({ product_type: "other", size_tier: null, size_mm: "", request: "planter shaped like my dog" });
  });

  it("shows server field errors, a changed price, and rejects bad photos locally", async () => {
    const { fn } = fetchStub({
      "/api/orders/start": () =>
        Response.json({ error: "invalid", errors: { email: "Check the email address", price: "Price changed" } }, { status: 400 }),
    });
    render(<OrderForm fetchImpl={fn} />);
    fireEvent.change(screen.getByTestId("order-photos"), {
      target: { files: [new File([new Uint8Array(1)], "a.gif", { type: "image/gif" })] },
    });
    expect(await screen.findByText("JPEG, PNG, HEIC or WebP")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("order-photos"), { target: { files: [photo()] } });
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("Your name"), "TEST ORDER");
    await u.type(screen.getByLabelText(/^Email/), "a@b.co");
    await u.click(screen.getByRole("button", { name: "Send order" }));
    expect(await screen.findByText("Check the email address")).toBeInTheDocument();
    expect(screen.getByText("Price changed")).toBeInTheDocument();
    expect(acceptPhoto(new File([new Uint8Array(1)], "x.HEIC", { type: "" }))).toBeNull();
  });
});

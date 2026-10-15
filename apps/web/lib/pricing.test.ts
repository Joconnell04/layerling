import {
  OTHER_UNPRICED,
  PRICE_PLACEHOLDER,
  formatMoney,
  priceFor,
  priceFrom,
  priceFromLabel,
  priceRange,
  quote,
} from "./pricing";

describe("pricing", () => {
  it("shows from $X from the cost model", () => {
    const pricing = {
      currency: "USD",
      products: { keychain: { price: 18, from: 15, cost_floor: 4.2, basis: "45 mm" }, figurine: { price: 39.5 } },
    };
    expect(priceFrom("keychain", pricing)).toBe("from $15");
    expect(priceFrom("figurine", pricing)).toBe("from $39.50");
    expect(priceFrom("magnet", pricing)).toBe(PRICE_PLACEHOLDER);
  });

  it("reads `from: true` as a flag and `price` as the number (cost model shape)", () => {
    const pricing = { currency: "USD", products: { keychain: { price: 31.99, from: true }, magnet: { price: 34.99, from: false } } };
    expect(priceFrom("keychain", pricing)).toBe("from $31.99");
    expect(priceFrom("magnet", pricing)).toBe("$34.99");
  });

  it("falls back to [PRICE] when the file is missing", () => {
    expect(priceFrom("keychain", { missing: true })).toBe("[PRICE]");
    expect(priceFrom("keychain", { products: { keychain: { from: 0 } } })).toBe("[PRICE]");
    expect(formatMoney(12, "EUR")).toBe("€12");
  });

  it("public labels never say [PRICE]", () => {
    expect(priceFromLabel("keychain", { missing: true })).toBeNull();
    expect(priceFor("keychain", "m", { missing: true })).toEqual({ amount: null, label: OTHER_UNPRICED, kind: "unpriced" });
  });

  it("uses per-size prices as a number or {price}, else from $X", () => {
    const pricing = {
      currency: "USD",
      products: {
        keychain: { price: 31.99, from: true, sizes: { xs: 29.99, m: { price: 31.99, mm: 52 }, l: { price: 32.99 } } },
        magnet: { price: 34.99, from: true },
      },
    };
    expect(priceFor("keychain", "xs", pricing)).toEqual({ amount: 29.99, label: "$29.99", kind: "size" });
    expect(priceFor("keychain", "l", pricing).label).toBe("$32.99");
    // No price for S: the lowest price the product has.
    expect(priceFor("keychain", "s", pricing)).toEqual({ amount: 29.99, label: "from $29.99", kind: "from" });
    expect(priceFrom("keychain", pricing)).toBe("from $29.99");
    expect(priceFor("magnet", "m", pricing).label).toBe("from $34.99");
  });

  it("reads the Other range from any of its shapes", () => {
    const unpriced = { amount: null, label: OTHER_UNPRICED, kind: "unpriced" };
    expect(priceRange("other", { products: {} })).toEqual(unpriced);
    expect(priceRange("other", { missing: true, other: { min: 1, max: 2 } })).toEqual(unpriced);
    expect(priceRange("other", { products: { other: { range: { min: 31.99, max: 100.99 } } } }).label).toBe("$31.99 to $100.99");
    expect(priceRange("other", { products: { other: { price_min: 40, price_max: 90 } } }).label).toBe("$40 to $90");
    expect(priceRange("other", { other: { min: 31.99, max: 100.99 } })).toEqual({ amount: 31.99, label: "$31.99 to $100.99", kind: "range" });
    expect(priceRange("other", { other: { min: 50, max: 10 } })).toEqual(unpriced);
    expect(quote("other", "m", { other: { min: 31.99, max: 100.99 } }).kind).toBe("range");
    expect(quote("figurine", "m", { products: { figurine: { price: 57.99, sizes: { m: 57.99 } } } }).label).toBe("$57.99");
  });
});

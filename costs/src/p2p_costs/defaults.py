"""Default inputs, products and channels for a new workbook.

These seed the Inputs sheet the first time the workbook is built. After that the workbook is the
source: refresh reads Inputs from the workbook, never from here, and only appends keys that are
missing (for example a new input added in a later version of this file).

Provenance is one of SOURCE (with a link), FROM JACKSON, or ASSUMED. Every ASSUMED row says what
moves if it is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE = "SOURCE"
JACKSON = "FROM JACKSON"
ASSUMED = "ASSUMED"
PROVENANCES = (SOURCE, JACKSON, ASSUMED)

# Products priced in the workbook. large_figure is the Meshy-scale figure from the real beagle
# slice; it is a reference row, not a product on the web page.
PRODUCTS: list[str] = ["keychain", "nameplate", "figurine", "large_figure", "ornament", "magnet"]
_NAMES = {"keychain": "Keychain", "nameplate": "Nameplate", "figurine": "Figurine",
          "ornament": "Ornament", "magnet": "Magnet"}
# Labels carry the M size from contracts (filled in below, after SIZE_TIERS_MM loads).
PRODUCT_LABELS = {"large_figure": "Large figure (Meshy scale)"}
WEB_PRODUCTS = ["keychain", "nameplate", "figurine", "ornament", "magnet"]
# Market listings are grouped by these five; the large figure is compared with figurines.
MARKET_PRODUCTS = ["keychain", "nameplate", "figurine", "ornament", "magnet"]
MARKET_FOR = {p: p for p in MARKET_PRODUCTS} | {"large_figure": "figurine"}

CHANNELS: list[str] = ["own_site", "own_pickup", "etsy", "facebook", "ebay", "market"]
CHANNEL_LABELS = {
    "own_site": "Own site, shipped",
    "own_pickup": "Own site, NYC pickup",
    "etsy": "Etsy, shipped",
    "facebook": "Facebook Marketplace, local pickup",
    "ebay": "eBay, shipped",
    "market": "NYC market booth",
}
WEB_CHANNEL = "own_site"

# Size tiers the customer picks (contracts SIZE_TIERS_MM: longest visible dimension in mm).
TIERS: list[str] = ["xs", "s", "m", "l"]
M_TIER = "m"
_FALLBACK_SIZES = {
    "keychain": {"xs": 30, "s": 38, "m": 45, "l": 55},
    "nameplate": {"xs": 80, "s": 100, "m": 120, "l": 150},
    "figurine": {"xs": 50, "s": 70, "m": 90, "l": 120},
    "ornament": {"xs": 50, "s": 60, "m": 70, "l": 85},
    "magnet": {"xs": 40, "s": 50, "m": 60, "l": 75},
}


def _size_tiers() -> dict[str, dict[str, float]]:
    try:
        from p2p_contracts.job import SIZE_TIERS_MM

        return {str(getattr(pt, "value", pt)): {str(getattr(t, "value", t)): float(mm)
                                                for t, mm in tiers.items()}
                for pt, tiers in SIZE_TIERS_MM.items()}
    except Exception:  # noqa: BLE001
        return {p: {t: float(v) for t, v in d.items()} for p, d in _FALLBACK_SIZES.items()}


SIZE_TIERS_MM: dict[str, dict[str, float]] = _size_tiers()
for _p, _n in _NAMES.items():
    _mm = SIZE_TIERS_MM.get(_p, {}).get(M_TIER)
    PRODUCT_LABELS[_p] = f"{_n} {_mm:g} mm" if _mm else _n
PRODUCT_LABELS = {p: PRODUCT_LABELS[p] for p in PRODUCTS}
SIZE_PRODUCTS = [p for p in WEB_PRODUCTS if p in SIZE_TIERS_MM]


@dataclass(frozen=True)
class Input:
    section: str
    key: str
    label: str
    value: float | str | None
    unit: str
    provenance: str
    source: str = ""
    notes: str = ""
    moves: str = ""


MESHY_PRICING = "https://www.meshy.ai/pricing"
MESHY_API_PRICING = "https://docs.meshy.ai/api/pricing"
BAMBU_STORE = "https://us.store.bambulab.com"
PLAN = "PLAN.md (Cost per model; Changes from docs)"


def _general() -> list[Input]:
    g = "General"
    return [
        Input(g, "target_margin", "Target margin (profit share of price, after all costs and fees)",
              0.5, "share", ASSUMED,
              notes="Cost-based price = cost before fees plus fixed fees, divided by "
                    "(1 minus percent fees minus this).",
              moves="Every cost-based price. At 0.4 prices drop about a sixth; at 0.6 they rise "
                    "about a quarter."),
        Input(g, "buyer_tax_pct", "Sales tax rate buyers pay (NYC combined)", 0.08875, "share",
              SOURCE, "https://www.nyc.gov/site/finance/business/business-nys-sales-tax.page",
              notes="NY State 4% + NYC 4.5% + MCTD 0.375%. Added on top of the price and passed "
                    "through; it matters here only because card and platform fees are charged on "
                    "it. Shipping billed to a buyer is taxable too (tax.ny.gov TB-ST-838). Etsy, "
                    "eBay and Facebook collect and remit it; direct and booth sales need a NY "
                    "Certificate of Authority."),
    ]


def _meshy() -> list[Input]:
    s = "Meshy"
    return [
        Input(s, "meshy_plan_usd_month", "Meshy Pro plan price", 20.0, "USD/month", SOURCE,
              MESHY_PRICING, notes="Billed monthly. Yearly billing for new users is $192 the first "
                                   "year ($16/month)."),
        Input(s, "meshy_plan_credits_month", "Meshy Pro credits per month", 1000, "credits/month",
              SOURCE, MESHY_PRICING, notes="Credits reset monthly and do not roll over."),
        Input(s, "meshy_topup_usd_per_credit", "Meshy top-up credit price", 0.032, "USD/credit",
              SOURCE, MESHY_PRICING,
              notes="Packs list at $0.040/credit (250 for $10, 1,000 for $40, 3,000 for $120); Pro "
                    "monthly subscribers get 20% off, so $0.032."),
        Input(s, "meshy_use_topup_rate", "Price credits at the top-up rate (1) or plan rate (0)", 0,
              "flag", ASSUMED, notes="0 assumes monthly plan credits cover the orders.",
              moves="Meshy cost per order doubles (x1.6) if orders outrun the 1,000 plan credits."),
        Input(s, "meshy_rework_pct", "Extra credits for rejected concepts and regenerations", 0.10,
              "share", ASSUMED,
              notes="On top of the test-run credits. The keychain test run already includes one "
                    "tweak and one retexture.",
              moves="Meshy cost per order, about $0.17 per 10 points on a keychain."),
    ]


def _claude() -> list[Input]:
    s = "Claude and SAM 3"
    return [
        Input(s, "claude_marginal_usd_per_order", "Claude cost per order (marginal)", 0.0,
              "USD/order", JACKSON,
              notes="Runs on Jackson's Claude subscription through Claude Code, so $0 per order."),
        Input(s, "claude_list_usd_per_order", "Claude list-price equivalent per order", 1.80,
              "USD/order", ASSUMED, "https://claude.com/pricing",
              notes="Full pipeline at API list prices (Haiku 4.5 $1/$5, Sonnet 5 $2/$10, Opus 5.5 "
                    "$4/$20 per million tokens in/out): Haiku intake and Sonnet color spec $0.05 to "
                    "$0.10, Opus paint loop $1.00 to $2.50 (PLAN.md Cost per model). Midpoint. "
                    "Counted only if the flag below is 1.",
              moves="Nothing unless the flag is 1; then every price rises about $3.60 at a 50% "
                    "margin."),
        Input(s, "claude_count_list_price", "Count Claude at list price (1) or marginal (0)", 0,
              "flag", JACKSON, notes="0 while Claude runs on the subscription."),
        Input(s, "sam3_usd_per_order", "SAM 3 cost per order", 0.0, "USD/order", SOURCE, PLAN,
              notes="SAM 3 runs locally on the M5 (Changes from docs, 2026-09-24)."),
    ]


def _material() -> list[Input]:
    s = "Material"
    return [
        Input(s, "filament_bambu_pla_usd_kg", "Bambu PLA Basic, 1 kg refill", 15.99, "USD/kg",
              SOURCE, "https://us.store.bambulab.com/products/pla-basic-filament",
              notes="Refill on a reusable spool. With spool $18.99; 4+ rolls 10% off ($14.39); "
                    "10+ rolls $12.47."),
        Input(s, "filament_generic_pla_usd_kg", "Generic PLA, 1 kg with spool (Elegoo)", 14.49,
              "USD/kg", SOURCE,
              "https://us.elegoo.com/products/pla-basic-filament-1-75mm-colored-1kg",
              notes="Refill $13.49. 10 kg bundle $99.99 ($10.00/kg)."),
        Input(s, "filament_petg_usd_kg", "PETG, 1 kg (Elegoo Rapid PETG)", 13.99, "USD/kg",
              SOURCE, "https://us.elegoo.com/products/rapid-petg-filament-1-75mm-colored-1kg",
              notes="Bambu PETG HF: refill $15.99, with spool $18.99 "
                    "(https://us.store.bambulab.com/products/petg-hf)."),
        Input(s, "slice_grams_include_purge", "Slice report grams per filament already include purge",
              1, "flag", SOURCE, "threemf/src/p2p_threemf/gcode_stats.py",
              notes="Bambu's per-filament totals include flush and prime tower; purge_grams is the "
                    "part of them that is purge."),
        Input(s, "purge_scale", "Purge scale (1 = as sliced, 0.5 = purge cut by half)", 1.0,
              "multiplier", ASSUMED,
              notes="Set to 0.5 when the purge-reduction work (dual-nozzle mapping, flush into "
                    "infill) lands. The Per product sheet also shows a fixed purge-halved row.",
              moves="Material cost: purge is 45 to 80% of the plastic in every slice so far."),
        Input(s, "material_waste_pct", "Filament waste (spool ends, purge line, tests)", 0.05,
              "share", ASSUMED, moves="Material cost only; small in dollars."),
        Input(s, "failed_print_pct", "Failed print rate (reprints)", 0.10, "share", ASSUMED,
              notes="Applied to material and printer time.",
              moves="Material and machine cost; large for the large figure."),
    ]


def _machine() -> list[Input]:
    s = "Printer and power"
    return [
        Input(s, "printer_price_usd", "Bambu Lab X2D AMS Combo purchase price", 899.0, "USD",
              SOURCE, "https://us.store.bambulab.com/products/x2d",
              notes="Printer alone $649; the AMS Combo is what multicolor needs. Before sales tax."),
        Input(s, "printer_life_hours", "Printer life in print hours", 5000, "hours", ASSUMED,
              "https://forum.bambulab.com/t/how-many-printing-hours-on-your-x1c/22369?page=9",
              notes="No manufacturer rating. X1C owners report 3,000 to 6,000 h with wear parts "
                    "replaced (one at 14,136 h). Depreciation = price / life hours.",
              moves="Machine cost: at 2,500 h depreciation doubles (about +$0.18 per print hour)."),
        Input(s, "printer_upkeep_usd_hour", "Upkeep: hotends, belts, plates, lubricant", 0.10,
              "USD/print hour", ASSUMED,
              "https://us.store.bambulab.com/collections/spare-parts-for-x2d",
              notes="Hotend with nozzle $17.99, XY belt $11.99, heatbed $89.99; wiki asks for "
                    "monthly XY lube at 5+ hours a day.",
              moves="Machine cost per hour; about $0.70 on a 7-hour figurine."),
        Input(s, "printer_power_w", "Average power while printing PLA", 250, "W", SOURCE,
              "https://bambulab.com/en-us/x2d/specs",
              notes="Spec steady-state for PLA at 110 V. CNC Kitchen measured about 160 W "
                    "(https://www.cnckitchen.com/blog/bambu-lab-x2d-review-affordable-dual-nozzle-"
                    "printer). Max 1,100 W for a few minutes while heating."),
        Input(s, "electricity_usd_kwh", "Electricity price used (Con Edison residential, all-in)",
              0.3383, "USD/kWh", ASSUMED,
              "https://www.coned.com/-/media/files/coned/documents/save-energy-money/using-private-"
              "generation/historical-average-full-service-electric-rates.pdf",
              notes="Con Edison 2025 average at 300 kWh a month: delivery 18.32 + supply 13.75 + "
                    "other 1.75 cents. Assumes Jackson prints on a Con Edison residential meter.",
              moves="Electricity per hour ($0.085); a 20% change moves a figurine by cents."),
        Input(s, "eia_ny_residential_usd_kwh", "EIA New York residential average (reference)",
              0.2949, "USD/kWh", SOURCE,
              "https://www.eia.gov/electricity/monthly/epm_table_grapher.php?t=epmt_5_6_a",
              notes="Statewide, June 2026. Reference only, not used in formulas."),
    ]


def _labor() -> list[Input]:
    s = "Labor"
    return [
        Input(s, "labor_rate_jackson_usd_hour", "Jackson's hourly rate (leave blank to use default)",
              None, "USD/hour", JACKSON, notes="Fill in. Blank uses the default below."),
        Input(s, "labor_rate_default_usd_hour", "Default hourly rate", 25.0, "USD/hour", ASSUMED,
              "https://dol.ny.gov/minimum-wage-0",
              notes="NYC minimum wage is $17.00 from 2026-01-01; $25 is a skilled-craft rate.",
              moves="Every price: labor is the largest cost line. Each $5/hour moves a keychain "
                    "about $1.80 and a figurine about $3.40 at a 50% margin."),
    ]


def _shipping() -> list[Input]:
    s = "Packaging, shipping, hardware"
    return [
        Input(s, "packaging_mailer_usd", "Padded bubble mailer (Uline S-22392)", 0.216, "USD each",
              SOURCE, "https://www.uline.com/BL_2417/Uline-Economy-White-Poly-Bubble-Mailers",
              notes="$54 per 250; $0.184 each at 10+ cases."),
        Input(s, "packaging_box_usd", "Small box 4 x 4 x 4 in (Uline S-4040)", 0.34, "USD each",
              SOURCE, "https://www.uline.com/Product/Detail/S-4040/Corrugated-Boxes-200-Test/"
                      "4-x-4-x-4-Corrugated-Boxes",
              notes="Fits the 90 mm figurine and the ornament."),
        Input(s, "packaging_box_large_usd", "Larger box about 8 x 6 x 4 in", 0.60, "USD each",
              ASSUMED, notes="For the 120 mm nameplate and the large figure; not priced yet.",
              moves="Nameplate and large figure packaging; cents."),
        Input(s, "packaging_fill_usd", "Tissue, crinkle fill, sticker, card", 0.25, "USD/order",
              ASSUMED, "https://www.uline.com/BL_1962/Tissue-Paper-Sheets",
              notes="Two tissue sheets $0.04 + crinkle $0.06 + sticker $0.02 (Uline) + a printed "
                    "card about $0.10.",
              moves="Packaging per order; cents."),
        Input(s, "packaging_pickup_usd", "Pickup or booth bag and card", 0.20, "USD/order",
              ASSUMED, moves="Packaging for pickup and booth sales; cents."),
        Input(s, "usps_ga_4oz_usd", "USPS Ground Advantage commercial, up to 4 oz, zone 4", 7.46,
              "USD", SOURCE, "https://pe.usps.com/text/dmm300/Notice123.htm",
              notes="Commercial price (Pirate Ship, Etsy labels), September 2026. One price from "
                    "4 oz to 15.999 oz per zone: zone 1 $6.93, zone 4 $7.46, zone 8 $8.40. "
                    "Retail counter zone 4 is $8.30."),
        Input(s, "usps_ga_8oz_usd", "USPS Ground Advantage commercial, up to 8 oz, zone 4", 7.46,
              "USD", SOURCE, "https://pe.usps.com/text/dmm300/Notice123.htm",
              notes="Same as 4 oz at commercial prices."),
        Input(s, "usps_ga_16oz_usd", "USPS Ground Advantage commercial, under 1 lb, zone 4", 7.46,
              "USD", SOURCE, "https://pe.usps.com/text/dmm300/Notice123.htm",
              notes="Same price up to 15.999 oz; 1 lb and over costs more (zone 1 $7.61)."),
        Input(s, "keyring_usd", "Split key ring with chain", 0.07, "USD each", SOURCE,
              "https://www.amazon.com/s?k=split+key+rings+with+chain+bulk+100+pcs+keychain",
              notes="$6.99 per 100 (ASIN B08SJP1H8L)."),
        Input(s, "magnet_usd", "Neodymium disc magnet 10 x 3 mm", 0.12, "USD each", SOURCE,
              "https://www.amazon.com/s?k=10x3mm+neodymium+magnets+100+pack",
              notes="$11.99 per 100 (ASIN B0FDQJ3WR6)."),
        Input(s, "ornament_hook_usd", "Ornament hook", 0.015, "USD each", SOURCE,
              "https://www.amazon.com/s?k=ornament+hooks+500+pcs",
              notes="$4.50 per 300 wire hooks (ASIN B0DNTTC9JC)."),
    ]


def _fees() -> list[Input]:
    s = "Payment and platform fees"
    return [
        Input(s, "stripe_pct", "Stripe card fee, percent", 0.029, "share", SOURCE,
              "https://stripe.com/pricing",
              notes="Domestic cards. +1.5% international, +0.5% manually entered."),
        Input(s, "stripe_fixed_usd", "Stripe card fee, fixed", 0.30, "USD/order", SOURCE,
              "https://stripe.com/pricing"),
        Input(s, "etsy_listing_usd", "Etsy listing fee (renews on each sale)", 0.20, "USD/sale",
              SOURCE, "https://www.etsy.com/legal/fees",
              notes="Etsy's page returns 403 to scripts; figures confirmed in the 2026 guide at "
                    "https://craftybase.com/blog/the-complete-guide-to-etsy-fees."),
        Input(s, "etsy_transaction_pct", "Etsy transaction fee (price plus shipping)", 0.065,
              "share", SOURCE, "https://www.etsy.com/legal/fees",
              notes="On item price, shipping and gift wrap; not on sales tax."),
        Input(s, "etsy_payment_pct", "Etsy payment processing, percent (includes tax)", 0.03,
              "share", SOURCE, "https://www.etsy.com/legal/fees",
              notes="US: 3% + $0.25 on the total paid, sales tax included."),
        Input(s, "etsy_payment_fixed_usd", "Etsy payment processing, fixed", 0.25, "USD/order",
              SOURCE, "https://www.etsy.com/legal/fees"),
        Input(s, "etsy_offsite_ads_pct", "Etsy Offsite Ads fee when an ad makes the sale", 0.15,
              "share", SOURCE, "https://www.etsy.com/legal/fees",
              notes="15% for shops under $10k a year; 12% above, and then mandatory."),
        Input(s, "etsy_offsite_ads_share", "Share of Etsy orders that come through Offsite Ads",
              0.10, "share", ASSUMED, moves="Etsy prices; each 10 points adds 1.5% in fees."),
        Input(s, "ebay_fvf_pct", "eBay final value fee (includes shipping and tax)", 0.136,
              "share", SOURCE,
              "https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees?id=4822",
              notes="Most categories, no store. Charged on item, shipping and sales tax. eBay's "
                    "page shows a bot check to scripts; confirmed at "
                    "https://taxomate.com/blog/ebay-seller-fees."),
        Input(s, "ebay_fixed_usd", "eBay per-order fee", 0.40, "USD/order", SOURCE,
              "https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees?id=4822",
              notes="$0.30 when the order total is $10 or less."),
        Input(s, "facebook_pickup_pct", "Facebook Marketplace fee, local pickup", 0.0, "share",
              SOURCE, "https://www.facebook.com/help/449101635835192",
              notes="No selling fee for local pickup. Shipped orders pay 10% (minimum $0.80) on "
                    "item, shipping and tax."),
        Input(s, "square_pct", "Square in-person card fee, percent", 0.026, "share", SOURCE,
              "https://squareup.com/help/us/en/article/5068-what-are-square-s-fees",
              notes="Free plan, tap, dip or swipe. The fixed part rose from $0.10 to $0.15 in "
                    "March 2025."),
        Input(s, "square_fixed_usd", "Square in-person card fee, fixed", 0.15, "USD/sale", SOURCE,
              "https://squareup.com/help/us/en/article/5068-what-are-square-s-fees"),
        Input(s, "market_card_share", "Share of booth sales paid by card", 0.8, "share", ASSUMED,
              moves="Booth card fees; small."),
        Input(s, "market_booth_usd_day", "Market booth fee per day (Grand Bazaar 10 x 10 ft)",
              170.0, "USD/day", ASSUMED,
              "https://grandbazaarnyc.org/wp-content/uploads/2025/11/Updated-Pricing-Oct2025-5.pdf",
              notes="Grand Bazaar NYC, Sundays on the Upper West Side: $100 (5 x 10 ft) to $170 "
                    "(10 x 10 ft), $200 in holiday peak. Others: Queens Night Market $50 (items "
                    "capped at $5), BUST Craftacular about $220, Renegade $375 to $800 a weekend "
                    "(bars AI-generated designs). See docs/research/pricing.md.",
              moves="Booth prices: each $50 a day adds $2.50 per unit at 20 units."),
        Input(s, "market_units_per_day", "Units sold per market day", 20, "units/day", ASSUMED,
              moves="Booth prices, strongly: booth fee and staffing are spread over these units."),
        Input(s, "market_hours_per_day", "Hours at the booth per day, incl. setup", 9, "hours/day",
              ASSUMED, notes="Grand Bazaar runs 10 am to 5 pm, plus setup and travel.",
              moves="Booth prices: staffing time is spread over units sold."),
    ]


# Per product: (model grams, purge grams, print minutes) are placeholders used only until the
# Test data sheet has a slice run for that product with Use = 1.
_PRODUCT_TABLE = {
    #               credits model_g purge_g  min  plate review support pack  oz  box keyr mag hook
    "keychain":     (85,    11.1,   37.9,   240,  1,    5,     2,     3,   2.5, 0,  1,   0,  0),
    "nameplate":    (85,    10.9,    6.2,    58,  1,    6,     3,     4,   7.0, 2,  0,   0,  0),
    "figurine":     (85,    56.9,  136.6,   854,  1,    6,     8,     5,   7.0, 1,  0,   0,  0),
    "large_figure": (85,    36.2,  116.6,   693,  1,    6,    12,     5,  12.0, 2,  0,   0,  0),
    "ornament":     (85,     8.0,   10.0,    60,  1,    5,     2,     4,   5.0, 1,  0,   0,  1),
    "magnet":       (85,     7.0,    8.0,    50,  1,    5,     2,     4,   3.0, 0,  0,   2,  0),
}

# Placeholders follow the purge-lab slices at production settings (Saving prime, flush into
# infill; keychains and magnets flat). Every slice purges about 0.2 g per filament change.
_PLACEHOLDER_NOTES = {
    "keychain": "The real Meshy beagle at 45 mm, flat, production settings (49.0 g, 37.9 g "
                "purge, 240 min). Upright it was 83.7 g and 399 min.",
    "nameplate": "The procedural 120 mm nameplate at production settings (17.1 g, 6.2 g purge, "
                 "58 min). A real Meshy figure on the plate will add weight and time.",
    "figurine": "The real Meshy beagle at 90 mm, production settings (193.5 g, 136.6 g purge, "
                "854 min).",
    "large_figure": "The real Meshy beagle at Meshy's native 75 mm, production settings, "
                    "supports on (152.8 g, 116.6 g purge, 693 min).",
    "ornament": "No slice yet. A 70 mm relief about 6 mm thick at production settings.",
    "magnet": "No slice yet. A 60 mm relief about 5 mm thick, flat, at production settings.",
}


def _products() -> list[Input]:
    out: list[Input] = []
    for p, row in _PRODUCT_TABLE.items():
        (credits, model_g, purge_g, minutes, plate, review, support, pack, oz, box, keyrings,
         magnets, hooks) = row
        s = f"Product: {p}"
        note = _PLACEHOLDER_NOTES[p]
        out += [
            Input(s, f"{p}.credits", "Meshy credits per order (placeholder)", credits, "credits",
                  ASSUMED, notes="Used until Test data has a run with credits. Plan defaults are "
                                 "66; one tweak and one retexture make 85.",
                  moves="Meshy cost; about $0.02 per credit."),
            Input(s, f"{p}.model_g", "Model grams, without purge (placeholder)", model_g, "g",
                  ASSUMED, notes=note, moves="Material cost until a slice replaces it."),
            Input(s, f"{p}.purge_g", "Purge grams: flush and prime tower (placeholder)", purge_g,
                  "g", ASSUMED, notes=note,
                  moves="Material cost until a slice replaces it; purge is most of the plastic."),
            Input(s, f"{p}.print_min", "Print minutes (placeholder)", minutes, "min", ASSUMED,
                  notes=note, moves="Machine cost until a slice replaces it."),
            Input(s, f"{p}.units_per_plate", "Units printed per plate", plate, "units", ASSUMED,
                  notes="Purge per unit is divided by this. 1 = one order per plate.",
                  moves="Purge per unit: two per plate halves it."),
            Input(s, f"{p}.review_min", "Labor: review (concept, mesh, paint, slice check)", review,
                  "min", ASSUMED, moves="Labor cost."),
            Input(s, f"{p}.supports_min", "Labor: remove supports and clean up", support, "min",
                  ASSUMED, moves="Labor cost."),
            Input(s, f"{p}.packing_min", "Labor: pack, attach hardware, label", pack, "min",
                  ASSUMED, moves="Labor cost."),
            Input(s, f"{p}.ship_oz", "Packed weight", oz, "oz", ASSUMED,
                  notes="Picks the USPS tier: up to 4 oz, 8 oz or 16 oz.",
                  moves="Shipping label tier."),
            Input(s, f"{p}.uses_box", "Packaging: 0 mailer, 1 small box, 2 larger box", box,
                  "choice", ASSUMED, moves="Packaging cost; cents."),
            Input(s, f"{p}.keyrings", "Key rings per unit", keyrings, "count", ASSUMED,
                  moves="Hardware cost; cents."),
            Input(s, f"{p}.magnets", "Magnets per unit", magnets, "count", ASSUMED,
                  moves="Hardware cost; cents."),
            Input(s, f"{p}.hooks", "Ornament hooks per unit", hooks, "count", ASSUMED,
                  moves="Hardware cost; cents."),
        ]
    return out


_CHANNEL_TABLE = {
    #           ships  price ending
    "own_site": (1, 0.99),
    "own_pickup": (0, 0.99),
    "etsy":     (1, 0.99),
    "facebook": (0, 0.0),
    "ebay":     (1, 0.99),
    "market":   (0, 0.0),
}


def _channels() -> list[Input]:
    out: list[Input] = []
    for c, (ships, ending) in _CHANNEL_TABLE.items():
        s = f"Channel: {c}"
        out += [
            Input(s, f"{c}.ships", "Ships by USPS (1) or handed over (0)", ships, "flag", ASSUMED,
                  notes="Shipped channels price in free shipping: label and mailer are costs.",
                  moves="This channel's price by the label plus packaging."),
            Input(s, f"{c}.price_ending", "Price ending: 0.99 or 0 for whole dollars", ending,
                  "USD", ASSUMED, notes="Prices round up to the next price with this ending.",
                  moves="Rounding only, under $1."),
        ]
    return out


LAB_FIT = ("Fitted on the purge-lab Meshy beagle at 45, 75 and 90 mm with production settings "
           "(data/slice_reports/purge_lab.jsonl, z_combined/writer_defaults): time x size^1.09, "
           "purge x size^0.91, model grams x size^2.36.")


def _sizes() -> list[Input]:
    s = "Size tiers"
    return [
        Input(s, "size_model_g_exponent", "Model grams scale with size to this power", 3.0,
              "exponent", ASSUMED, notes="3 = by volume. " + LAB_FIT + " Used only for a tier "
              "with no slice yet.",
              moves="XS, S and L prices until their slices land; at 2.36 the L tiers drop."),
        Input(s, "size_purge_exponent", "Purge grams scale with size to this power", 0.91,
              "exponent", ASSUMED, "data/slice_reports/purge_lab.jsonl",
              notes="Purge follows layers with a colour change, so it grows about linearly. "
                    + LAB_FIT,
              moves="XS, S and L prices until their slices land."),
        Input(s, "size_time_exponent", "Print time scales with size to this power", 1.09,
              "exponent", ASSUMED, "data/slice_reports/purge_lab.jsonl", notes=LAB_FIT,
              moves="XS, S and L prices until their slices land; printer time is the biggest "
                    "cost that changes with size."),
        Input(s, "other_complexity_premium", "Custom (Other) orders: premium on the L figurine",
              0.5, "share", ASSUMED,
              notes="Top of the custom price range = L figurine price x (1 + this). Custom "
                    "work needs more review, modeling and test prints.",
              moves="The top of the custom price range only."),
    ]


def default_inputs() -> list[Input]:
    return (_general() + _meshy() + _claude() + _material() + _machine() + _labor()
            + _shipping() + _fees() + _products() + _channels() + _sizes())


INPUTS: list[Input] = default_inputs()

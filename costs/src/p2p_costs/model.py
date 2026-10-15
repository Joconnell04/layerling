"""The cost model in plain Python.

This is written independently of the workbook formulas (workbook.py) so refresh can check one
against the other: every number here must match what Excel or LibreOffice computes from the
formulas, to the cent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .defaults import (
    CHANNELS,
    M_TIER,
    MARKET_FOR,
    MARKET_PRODUCTS,
    PRODUCTS,
    SIZE_PRODUCTS,
    SIZE_TIERS_MM,
    TIERS,
)

# ------------------------------------------------------------------ Excel-compatible helpers


def xround(x: float, digits: int = 0) -> float:
    """Excel ROUND: half away from zero."""
    q = Decimal(1).scaleb(-digits)
    d = Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP)
    return float(d)


def xroundup(x: float, digits: int = 0) -> float:
    """Excel ROUNDUP: away from zero."""
    f = 10 ** digits
    v = abs(x) * f
    r = math.ceil(xround(v, 9)) / f
    return r if x >= 0 else -r


def round_price(x: float, ending: float) -> float:
    """Round up to the next price with this ending: 0.99 gives 12.99, 13.99; 0 gives 13, 14."""
    return xround(xroundup(xround(x - ending, 2), 0) + ending, 2)


def median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def quartile(vals: list[float], q: int) -> float:
    """Excel QUARTILE (inclusive, linear interpolation)."""
    s = sorted(vals)
    if not s:
        return 0.0
    pos = (len(s) - 1) * q / 4
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# ------------------------------------------------------------------ inputs


def num(inputs: dict, key: str) -> float:
    v = inputs.get(key)
    if v is None or v == "":
        return 0.0
    return float(v)


def labor_rate(inputs: dict) -> float:
    v = inputs.get("labor_rate_jackson_usd_hour")
    if v is None or v == "":
        return num(inputs, "labor_rate_default_usd_hour")
    return float(v)


@dataclass
class Result:
    rates: dict[str, float] = field(default_factory=dict)
    per_product: dict[str, dict[str, float | str]] = field(default_factory=dict)
    channels: dict[str, dict[str, float]] = field(default_factory=dict)
    prices: dict[tuple[str, str], dict[str, float | str]] = field(default_factory=dict)
    market_day: dict[str, dict[str, float]] = field(default_factory=dict)
    sizes: dict[tuple[str, str], dict[str, float | str]] = field(default_factory=dict)
    other: dict[str, float | str] = field(default_factory=dict)


def _avg(tests: list[dict], product: str, col: str, need: str | None = None,
         tier: str | None = M_TIER) -> tuple[int, float]:
    """Average of col over runs of this product with counts = 1 and a value in `need` (default col).
    Matches SUMIFS / COUNTIFS with a "<>" criterion: blank cells are skipped, blank values in col
    count as 0 when `need` is another column."""
    need = need or col
    rows = [t for t in tests if t.get("product") == product and _is_one(t.get("counts"))
            and (tier is None or (t.get("tier") or M_TIER) == tier)
            and t.get(need) not in (None, "")]
    if not rows:
        return 0, 0.0
    return len(rows), sum(float(t.get(col) or 0) for t in rows) / len(rows)


def _is_one(v) -> bool:
    try:
        return float(v) == 1
    except (TypeError, ValueError):
        return False


def market_stats(market: list[dict]) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {p: {"all": [], "ny": []} for p in MARKET_PRODUCTS}
    for m in market:
        p = m.get("product")
        if p not in out or not _is_one(m.get("use")):
            continue
        price = m.get("price_usd")
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            continue
        ship = m.get("shipping_usd")
        delivered = float(price) + (float(ship) if isinstance(ship, (int, float))
                                    and not isinstance(ship, bool) else 0.0)
        out[p]["all"].append(delivered)
        if m.get("region") == "NY":
            out[p]["ny"].append(delivered)
    return out


def compute(inputs: dict, tests: list[dict], market: list[dict],
            overrides: dict[tuple[str, str], float] | None = None,
            overrides_sizes: dict[tuple[str, str], float] | None = None) -> Result:
    overrides = overrides or {}
    i = lambda k: num(inputs, k)
    res = Result()

    # Rates
    usd_credit = (i("meshy_topup_usd_per_credit") if i("meshy_use_topup_rate") == 1
                  else (i("meshy_plan_usd_month") / i("meshy_plan_credits_month")
                        if i("meshy_plan_credits_month") else 0.0))
    pb = i("filament_bambu_pla_usd_kg") / 1000
    pg = i("filament_generic_pla_usd_kg") / 1000
    pp = i("filament_petg_usd_kg") / 1000
    power_h = i("printer_power_w") / 1000 * i("electricity_usd_kwh")
    life = i("printer_life_hours")
    dep_h = (i("printer_price_usd") / life if life else 0.0) + i("printer_upkeep_usd_hour")
    rate = labor_rate(inputs)
    tax = i("buyer_tax_pct")
    res.rates = {"usd_credit": usd_credit, "pla_bambu_g": pb, "pla_generic_g": pg, "petg_g": pp,
                 "power_h": power_h, "dep_h": dep_h, "labor_h": rate, "tax": tax}
    fail = i("failed_print_pct")
    waste = i("material_waste_pct")
    incl = i("slice_grams_include_purge")
    pscale = i("purge_scale")

    stats = market_stats(market)
    for p in PRODUCTS:
        r: dict[str, float | str] = {}
        n_cr, cr = _avg(tests, p, "credits", tier=None)  # credits do not depend on size
        r["runs_credits"] = n_cr
        r["credits_basis"] = "test data" if n_cr > 0 else "placeholder"
        r["credits"] = cr if n_cr > 0 else i(f"{p}.credits")
        r["meshy_usd"] = r["credits"] * (1 + i("meshy_rework_pct")) * usd_credit
        r["claude_usd"] = i("claude_marginal_usd_per_order") + (
            i("claude_list_usd_per_order") if i("claude_count_list_price") == 1 else 0.0)
        r["sam3_usd"] = i("sam3_usd_per_order")
        n_sl, _ = _avg(tests, p, "total_g")
        r["runs_slice"] = n_sl
        if n_sl > 0:
            g_b = _avg(tests, p, "pla_bambu_g", "total_g")[1]
            g_g = _avg(tests, p, "pla_generic_g", "total_g")[1]
            g_p = _avg(tests, p, "petg_g", "total_g")[1]
            g_o = _avg(tests, p, "other_g", "total_g")[1]
        else:
            g_b = i(f"{p}.model_g") + i(f"{p}.purge_g")
            g_g = g_p = g_o = 0.0
        n_pu, pu = _avg(tests, p, "purge_g")
        purge = pu if n_pu > 0 else i(f"{p}.purge_g")
        r.update(g_pla_bambu=g_b, g_pla_generic=g_g, g_petg=g_p, g_other=g_o, g_purge=purge)
        extra = purge if incl != 1 else 0.0
        g_total = g_b + g_g + g_p + g_o + extra
        r["g_total"] = g_total
        r["g_model"] = g_total - purge
        units = i(f"{p}.units_per_plate") or 1.0
        r["units_per_plate"] = units
        r["g_purge_charged"] = purge * pscale / units
        r["g_charged"] = r["g_model"] + r["g_purge_charged"]
        raw_cost = g_b * pb + g_g * pg + g_p * pp + g_o * pb + (purge * pb if incl != 1 else 0.0)
        r["usd_per_g"] = raw_cost / g_total if g_total else 0.0
        r["material_usd"] = r["g_charged"] * r["usd_per_g"] * (1 + waste) * (1 + fail)
        n_t, t = _avg(tests, p, "print_min")
        r["print_min"] = t if n_t > 0 else i(f"{p}.print_min")
        r["power_usd"] = r["print_min"] / 60 * power_h * (1 + fail)
        r["depreciation_usd"] = r["print_min"] / 60 * dep_h * (1 + fail)
        r["machine_usd"] = r["power_usd"] + r["depreciation_usd"]
        r["hardware_usd"] = (i(f"{p}.keyrings") * i("keyring_usd") + i(f"{p}.magnets")
                             * i("magnet_usd") + i(f"{p}.hooks") * i("ornament_hook_usd"))
        r["review_min"] = i(f"{p}.review_min")
        r["supports_min"] = i(f"{p}.supports_min")
        r["packing_min"] = i(f"{p}.packing_min")
        r["labor_min"] = r["review_min"] + r["supports_min"] + r["packing_min"]
        r["labor_usd"] = r["labor_min"] / 60 * rate
        r["production_usd"] = (r["meshy_usd"] + r["claude_usd"] + r["sam3_usd"]
                               + r["material_usd"] + r["machine_usd"] + r["hardware_usd"]
                               + r["labor_usd"])
        box = i(f"{p}.uses_box")
        r["pack_ship_usd"] = ((i("packaging_box_large_usd") if box == 2 else
                               i("packaging_box_usd") if box == 1 else
                               i("packaging_mailer_usd")) + i("packaging_fill_usd"))
        oz = r["ship_oz"] = i(f"{p}.ship_oz")
        r["label_usd"] = (i("usps_ga_4oz_usd") if oz <= 4 else
                          i("usps_ga_8oz_usd") if oz <= 8 else i("usps_ga_16oz_usd"))
        r["pack_pickup_usd"] = i("packaging_pickup_usd")
        s = stats[MARKET_FOR[p]]
        r["market_n"] = len(s["all"])
        r["market_q1"] = quartile(s["all"], 1)
        r["market_median"] = median(s["all"])
        r["market_q3"] = quartile(s["all"], 3)
        r["ny_n"] = len(s["ny"])
        r["ny_median"] = median(s["ny"])
        r["own_market_median"] = r["market_median"]
        r["ny_q3"] = quartile(s["ny"], 3)
        # Sensitivity: purge cut by half (material only)
        half_g = r["g_model"] + purge * pscale * 0.5 / units
        r["material_half_usd"] = half_g * r["usd_per_g"] * (1 + waste) * (1 + fail)
        r["production_half_usd"] = r["production_usd"] - r["material_usd"] + r["material_half_usd"]
        res.per_product[p] = r

    # Channels
    tax1 = 1 + tax
    booth_units = i("market_units_per_day")
    ch = {
        "own_site": (i("stripe_pct") * tax1, i("stripe_fixed_usd"), 0.0),
        "own_pickup": (i("stripe_pct") * tax1, i("stripe_fixed_usd"), 0.0),
        "etsy": (i("etsy_transaction_pct") + i("etsy_payment_pct") * tax1
                 + i("etsy_offsite_ads_pct") * i("etsy_offsite_ads_share"),
                 i("etsy_listing_usd") + i("etsy_payment_fixed_usd"), 0.0),
        "facebook": (i("facebook_pickup_pct"), 0.0, 0.0),
        "ebay": (i("ebay_fvf_pct") * tax1, i("ebay_fixed_usd"), 0.0),
        "market": (i("square_pct") * tax1 * i("market_card_share"),
                   i("square_fixed_usd") * i("market_card_share"),
                   ((i("market_booth_usd_day") + i("market_hours_per_day") * rate) / booth_units
                    if booth_units else 0.0)),
    }
    for c in CHANNELS:
        pct, fixed, extra = ch[c]
        res.channels[c] = {"ships": i(f"{c}.ships"), "pct": pct, "fixed": fixed, "extra": extra,
                           "ending": i(f"{c}.price_ending")}

    margin = i("target_margin")
    for p in PRODUCTS:
        r = res.per_product[p]
        for c in CHANNELS:
            cc = res.channels[c]
            x: dict[str, float | str] = {}
            x["production"] = r["production_usd"]
            x["pack_ship"] = (r["pack_ship_usd"] + r["label_usd"] if cc["ships"] == 1
                              else r["pack_pickup_usd"])
            x["extra"] = cc["extra"]
            x["cost"] = x["production"] + x["pack_ship"] + x["extra"]
            x["pct"] = cc["pct"]
            x["fixed"] = cc["fixed"]
            x["break_even"] = (x["cost"] + x["fixed"]) / (1 - x["pct"])
            x["target"] = (x["cost"] + x["fixed"]) / (1 - x["pct"] - margin)
            x["market_q1"] = r["market_q1"]
            x["market_median"] = r["market_median"]
            x["formula_price"] = round_price(max(x["target"], x["market_q1"]), cc["ending"])
            ov = overrides.get((p, c))
            x["override"] = ov if ov is not None else ""
            x["price"] = float(ov) if ov is not None else x["formula_price"]
            x["basis"] = ("override" if ov is not None else
                          "cost" if x["target"] >= x["market_q1"] else "market")
            x["fees"] = x["price"] * x["pct"] + x["fixed"]
            x["profit"] = x["price"] - x["cost"] - x["fees"]
            x["margin"] = x["profit"] / x["price"] if x["price"] else 0.0
            x["buyer_pays"] = x["price"] * (1 + tax)
            res.prices[(p, c)] = x
        # Purge-halved own-site price (sensitivity)
        o = res.channels["own_site"]
        cost_h = (r["production_half_usd"] + r["pack_ship_usd"] + r["label_usd"]
                  if o["ships"] == 1 else r["production_half_usd"] + r["pack_pickup_usd"])
        target_h = (cost_h + o["fixed"]) / (1 - o["pct"] - margin)
        r["own_price_half"] = round_price(max(target_h, r["market_q1"]), o["ending"])
        r["own_price_half_change"] = (r["own_price_half"]
                                      - res.prices[(p, "own_site")]["formula_price"])
        r["own_break_even"] = res.prices[(p, "own_site")]["break_even"]
        r["own_target"] = res.prices[(p, "own_site")]["target"]
        own = res.prices[(p, "own_site")]
        r["own_price"] = own["price"]
        r["own_fees"] = own["fees"]
        r["own_total_cost"] = own["cost"] + own["fees"]
        r["own_flag"] = ("above NY high quartile"
                         if r["ny_q3"] > 0 and own["price"] > r["ny_q3"] else "")
        for key, m in (("own_margin_at_median", r["market_median"]),
                       ("own_margin_at_ny_median", r["ny_median"])):
            r[key] = (m - own["cost"] - (m * own["pct"] + own["fixed"])) / m if m else 0.0
        m = res.prices[(p, "market")]
        contrib = m["price"] - m["production"] - m["pack_ship"] - m["fees"]
        fixed_day = i("market_booth_usd_day") + i("market_hours_per_day") * rate
        res.market_day[p] = {"contribution": contrib,
                             "units": fixed_day / contrib if contrib > 0 else 0.0}
    _sizes(res, inputs, tests, overrides_sizes or {})
    return res


OTHER_BASIS = ("Custom (Other) range: the cheapest XS own-site price, up to the L figurine "
               "price plus the complexity premium in Inputs.")


def _sizes(res: Result, inputs: dict, tests: list[dict],
           overrides: dict[tuple[str, str], float]) -> None:
    i = lambda k: num(inputs, k)
    fail = 1 + i("failed_print_pct")
    waste = 1 + i("material_waste_pct")
    incl = i("slice_grams_include_purge")
    per_h = res.rates["power_h"] + res.rates["dep_h"]
    margin = i("target_margin")
    ending = res.channels["own_site"]["ending"]
    for p in SIZE_PRODUCTS:
        r = res.per_product[p]
        own = res.prices[(p, "own_site")]
        m_mm = SIZE_TIERS_MM[p][M_TIER]
        for t in TIERS:
            z: dict[str, float | str] = {"mm": SIZE_TIERS_MM[p][t]}
            z["scale"] = z["mm"] / m_mm
            if t == M_TIER:
                z["runs"] = r["runs_slice"]
                z["basis"] = "slice" if z["runs"] > 0 else "placeholder (ASSUMED)"
                z["model_g"], z["purge_g"], z["print_min"] = (r["g_model"], r["g_purge"],
                                                              r["print_min"])
            else:
                n, tot = _avg(tests, p, "total_g", tier=t)
                z["runs"] = n
                if n > 0:
                    _, pur = _avg(tests, p, "purge_g", "total_g", tier=t)
                    _, mins = _avg(tests, p, "print_min", "total_g", tier=t)
                    z["basis"] = "slice"
                    z["model_g"] = tot - (pur if incl == 1 else 0.0)
                    z["purge_g"], z["print_min"] = pur, mins
                else:
                    sc = z["scale"]
                    z["basis"] = "scaled from M (ASSUMED)"
                    z["model_g"] = r["g_model"] * sc ** i("size_model_g_exponent")
                    z["purge_g"] = r["g_purge"] * sc ** i("size_purge_exponent")
                    z["print_min"] = r["print_min"] * sc ** i("size_time_exponent")
            z["material"] = ((z["model_g"] + z["purge_g"] * i("purge_scale")
                              / r["units_per_plate"]) * r["usd_per_g"] * waste * fail)
            z["machine"] = z["print_min"] / 60 * per_h * fail
            z["production"] = (r["production_usd"] - r["material_usd"] - r["machine_usd"]
                               + z["material"] + z["machine"])
            z["cost"] = z["production"] + own["pack_ship"] + own["extra"]
            z["break_even"] = (z["cost"] + own["fixed"]) / (1 - own["pct"])
            z["target"] = (z["cost"] + own["fixed"]) / (1 - own["pct"] - margin)
            if t == M_TIER:
                z["formula_price"] = own["price"]
                z["price"] = own["price"]
            else:
                z["formula_price"] = round_price(z["target"] * own["price"] / own["target"],
                                                 ending)
                ov = overrides.get((p, t))
                z["override"] = ov if ov is not None else ""
                z["price"] = float(ov) if ov is not None else z["formula_price"]
            z["margin"] = ((z["price"] - z["cost"] - (z["price"] * own["pct"] + own["fixed"]))
                           / z["price"] if z["price"] else 0.0)
            res.sizes[(p, t)] = z
    xs = [res.sizes[(p, "xs")]["price"] for p in SIZE_PRODUCTS]
    lfig = res.sizes[("figurine", "l")]["price"]
    res.other = {"min": min(xs),
                 "max": round_price(lfig * (1 + i("other_complexity_premium")), ending),
                 "basis": OTHER_BASIS, "premium": i("other_complexity_premium")}

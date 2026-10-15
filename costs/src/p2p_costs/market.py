"""Which research listings count as comparables by default.

A comparable is a 3D likeness made from a customer photo (printed, painted, sculpted). Name-only
keychains, flat photo prints, acrylic, lithophanes and plush start with Use = 0 and the reason in
Notes; Jackson can flip Use in the Market sheet.
"""

from __future__ import annotations

RULES: list[tuple[tuple[str, ...], str]] = [
    (("name only", "name-only", "no figure", "name (plus title line) only"), "name only, no figure"),
    (("lithophane",), "lithophane"),
    (("plush",), "plush, not a hard figure"),
    (("acrylic",), "acrylic photo print"),
    (("heat-pressed", "not a 3d", "not a sculpt", "photo silhouette"), "not a 3D likeness"),
    (("flat photo", "flat acrylic", "flat outline", "flat printed", "flat 2d",
      "appears to be a flat"), "flat photo item, not a 3D likeness"),
    (("stock designs",), "stock design, not custom"),
    (("not a pet or photo product",), "not made from a photo"),
]


def default_use(d: dict) -> tuple[int, str]:
    price = d.get("price_usd")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return 0, "no price shown"
    text = f" {d.get('title') or ''} {d.get('notes') or ''} ".lower()
    for keys, reason in RULES:
        if any(k in text for k in keys):
            return 0, reason
    return 1, ""

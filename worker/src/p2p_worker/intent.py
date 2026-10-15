"""What a short, casual request says outright: the product form and the custom asks.

Customers write "keychain of my frenchie" or "make my dog wear a sombrero". Intake (Haiku) lists
the asks; the code here is the deterministic part:

- form_from_text: a keychain or magnet form only when the request names it ("3d", "figurine",
  "flat", "relief"). Anything else is asked, so a guess never picks the form.
- asks_from_text: props, outfits, poses, expressions and names in common phrasings. MockClaude
  uses it as its whole answer; the real backends merge it under the model's list as a safety
  net (merge_asks), so "wearing a sombrero" survives even when the model drops it.
"""

from __future__ import annotations

import re

from p2p_contracts.job import CustomAsk, ProductForm

FIGURE_WORDS = re.compile(
    r"\b(?:3\s*-?\s*d|three[\s-]*d(?:imensional)?|figur(?:e|ine)s?|sculpt(?:ed|ure)?|statue)\b",
    re.IGNORECASE)
FLAT_WORDS = re.compile(
    r"\b(?:flat(?!\s*-?\s*back)|2\s*-?\s*d|two[\s-]*d|relief|cut\s*-?\s*out|badge|medallion|"
    r"silhouette)\b", re.IGNORECASE)


def form_from_text(text: str) -> ProductForm | None:
    """The form the request states outright, else None (then intake asks)."""
    fig, flat = bool(FIGURE_WORDS.search(text or "")), bool(FLAT_WORDS.search(text or ""))
    if fig == flat:
        return None
    return ProductForm.FIGURE if fig else ProductForm.FLAT


# Words that name a subject, not an ask ("holding my cat" is two subjects, not a prop).
SUBJECT_WORDS = {
    "dog", "dogs", "puppy", "pup", "cat", "cats", "kitten", "kitty", "rabbit", "bunny", "bird",
    "parrot", "horse", "hamster", "ferret", "turtle", "lizard", "baby", "me", "him", "her",
    "them", "us", "kid", "son", "daughter", "wife", "husband", "boyfriend", "girlfriend", "mom",
    "dad", "chihuahua", "frenchie", "beagle", "golden", "terrier", "spaniel", "hound", "lab",
    "poodle", "pug", "corgi", "husky", "calico", "tabby",
}
STOP = r"(?=\s*(?:[,.;!?]|$|\s(?:and|with|holding|wearing|while|on\s+(?:a|the|his|her)|in|for|"\
       r"please|pls|plz|lol)\b))"
DET = r"(?:(?:a|an|the|his|her|their|its|my|our|some|little|lil|tiny|cute|big)\s+)*"
# Things worn: "with sunglasses" or "give him a bow tie" is an outfit ("wearing ...").
CLOTHES = re.compile(r"(?:hat|sombrero|cap|beanie|bow ?tie|bow|bandana|scarf|sweater|shirt|"
                     r"jersey|hoodie|costume|outfit|tux(?:edo)?|crown|cape|tiara|glasses|"
                     r"sunglasses|goggles|collar|jacket|coat|dress|tutu|boots|shoes|socks|"
                     r"harness|necklace|helmet)$")
THINGS = (r"ball|toy|bone|stick|frisbee|flower|balloon|cake|guitar|book|mug|leash|pumpkin|"
          r"present|gift|heart|sign|carrot|fish|mouse|yarn|pizza|donut|coffee|"
          + CLOTHES.pattern[3:-2])
PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("outfit", r"\bwear(?:ing|s)?\s+" + DET + r"([a-z][a-z' -]{1,40}?)" + STOP, "wearing a {}"),
    ("outfit", r"\bdressed\s+(?:up\s+)?as\s+" + DET + r"([a-z][a-z' -]{1,40}?)" + STOP,
     "dressed as a {}"),
    ("outfit", (r"\bin\s+" + DET + r"((?:[a-z][a-z' -]{0,29}?\s)?(?:" + CLOTHES.pattern[3:-2]
                + r")s?)" + STOP), "wearing a {}"),
    ("prop", r"\bholding\s+" + DET + r"([a-z][a-z' -]{1,40}?)" + STOP, "holding a {}"),
    ("prop", (r"\b(?:with|give\s+(?:him|her|them|it))\s+" + DET
              + r"((?:[a-z][a-z' -]{0,29}?\s)?(?:" + THINGS + r")s?)" + STOP), "with a {}"),
    ("expression", (r"\b(tongue\s+out|smiling|winking|sticking\s+(?:his|her|its)\s+tongue\s+"
                    r"out|happy\s+face|grumpy\s+face)\b"), "{}"),
    ("pose", (r"\b(sitting(?:\s+up)?|standing(?:\s+up)?|(?:lying|laying)(?:\s+down)?|sleeping|"
              r"asleep|curled\s+up|hugging|cuddling|snuggl(?:ed|ing)\s+up|playing|jumping|"
              r"running|waving|begging|rolling\s+over|on\s+(?:his|her|its)\s+back)\b"), "{}"),
)
TEXT_PATTERNS = (
    (r"\bwith\s+(?:his|her|their|its|the|my)\s+names?\s+[\"']?([A-Za-z][\w' &-]{0,23}?)[\"']?"
     r"(?=\s*(?:[,.;!?]|$|\s(?:and|on|in|please|pls)\b))"),
    r"\bthat\s+says\s+[\"']([^\"']{1,24})[\"']",
    r"\bwith\s+[\"']([^\"']{1,24})[\"']\s+on\s+it\b",
)


def _article(phrase: str, thing: str) -> str:
    """ "wearing a {thing}" with the right article: none for plurals, "an" before a vowel."""
    if " a " + thing not in " " + phrase:
        return phrase
    plural = thing.endswith("s") and not thing.endswith(("ss", "us"))
    art = "" if plural else ("an " if thing[:1] in "aeiou" else "a ")
    return phrase.replace("a " + thing, art + thing, 1)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -'")


def asks_from_text(text: str) -> list[CustomAsk]:
    """Props, outfits, poses, expressions and names in common phrasings of the request."""
    out: list[CustomAsk] = []
    seen: set[str] = set()
    low = (text or "").lower()
    for kind, pat, fmt in PATTERNS:
        for m in re.finditer(pat, low):
            thing = _clean(m.group(1))
            words = thing.split()
            if not words or words[-1] in SUBJECT_WORDS or thing in seen:
                continue
            k, f = kind, fmt
            if kind == "prop" and fmt == "with a {}" and CLOTHES.search(thing.rstrip("s")):
                k, f = "outfit", "wearing a {}"
            phrase = _article(f.format(thing), thing)
            seen.add(thing)
            out.append(CustomAsk(kind=k, text=phrase[:120]))  # type: ignore[arg-type]
    for pat in TEXT_PATTERNS:
        for m in re.finditer(pat, text or "", flags=re.IGNORECASE):
            name = _clean(m.group(1))
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(CustomAsk(kind="text", text=name[:24]))
    return out


def ask_key(ask: CustomAsk) -> str:
    """The word that identifies an ask for de-duplication: its last word, or the name."""
    if ask.kind == "text":
        return ask.text.strip().lower()
    words = re.findall(r"[a-z]+", ask.text.lower())
    return words[-1] if words else ask.text.lower()


def merge_asks(model: list[CustomAsk], code: list[CustomAsk],
               subject_words: set[str] | None = None) -> list[CustomAsk]:
    """The model's asks, plus each code ask whose key word none of them mention and that does not
    name a subject."""
    out = list(model)
    said = " ".join(a.text.lower() for a in model)
    for a in code:
        key = ask_key(a)
        if key in said or (a.kind != "text" and subject_words and key in subject_words):
            continue
        out.append(a)
        said += " " + a.text.lower()
    return out


def parse_ask_lines(lines: list[str], subject_ids: set[str]) -> list[CustomAsk]:
    """ "outfit|s1|wearing a sombrero" lines from intake -> CustomAsk. Bad lines are dropped."""
    kinds = {"prop", "outfit", "pose", "expression", "text", "other"}
    out = []
    for line in lines:
        f = [x.strip() for x in str(line).split("|")]
        if len(f) < 3 or not f[2]:
            continue
        kind = f[0].lower() if f[0].lower() in kinds else "other"
        sid = f[1] if f[1] in subject_ids else None
        out.append(CustomAsk(kind=kind, text=f[2][:120], subject_id=sid))  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------- mesh review text
# A change to the 3D model's colors or markings only: Retexture (10 credits) repaints the same
# shape, where a new model is 35 (SOURCE: https://docs.meshy.ai/api/pricing). Any word about
# shape, size, pose or parts sends it to a regenerate instead.
COLOR_WORDS = re.compile(
    r"\b(?:colou?rs?|colou?red|shades?|darker|lighter|brighter|paler|duller|tint|hue|"
    r"textures?|paint(?:ed)?|markings?|spots?|spotted|stripes?|striped|patch(?:es)?|"
    r"black|white|gr[ae]y|brown|tan|cream|beige|ginger|golden|gold|silver|red|orange|yellow|"
    r"green|blue|purple|pink|caramel|chocolate|brindle|tabby|merle|tricolou?r)\b",
    re.IGNORECASE)
SHAPE_WORDS = re.compile(
    r"\b(?:shape[sd]?|pose[sd]?|sit(?:ting)?|stand(?:ing)?|ly(?:ing)|lie|bigger|smaller|larger|"
    r"taller|shorter|longer|wider|narrower|thinner|thicker|fatter|slimmer|floppy|floppier|"
    r"pointy|pointier|rounder|flatter|sizes?|proportions?|scale|rotate|turn(?:ed)?|tilt(?:ed)?|"
    r"remove|add|extra|missing|broken|hole|gap|attach(?:ed)?|detach(?:ed)?|merge|separate|"
    r"geometry|mesh|sculpt(?:ed)?|ears? (?:up|down)|open|closed|"
    r"face (?:left|right|forward)|angle)\b",
    re.IGNORECASE)


def color_only_fix(text: str | None) -> bool:
    """True when a mesh review note asks only for a color or marking change ("make the ears
    black", "the saddle should be darker"), so Retexture can do it on the same shape."""
    t = (text or "").strip()
    return bool(t) and bool(COLOR_WORDS.search(t)) and not SHAPE_WORDS.search(t)

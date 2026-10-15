// What a short request says outright: the product form and the custom asks (props, outfits,
// poses, names). A port of worker/src/p2p_worker/intent.py so the new-job form shows what intake
// will carry into the concept, the concept check and the views. lib/intent.test.ts runs the
// worker's own examples; intake (Haiku plus this parser) stays the source of truth.

import type { CustomAsk, ProductForm, ProductType } from "./contracts.gen";

// contracts job.py FORM_CHOICE_PRODUCTS: keychains and magnets are a flat charm or a 3D figure,
// the customer's choice. Every other product is a 3D figure.
export const FORM_CHOICE_PRODUCTS: readonly ProductType[] = ["keychain", "magnet"];

export const formIsChoice = (p: ProductType) => FORM_CHOICE_PRODUCTS.includes(p);

/** "Flat charm" / "Flat relief" (magnet) / "3D figure", the words the intake question uses. */
export function formLabel(form: ProductForm, product?: ProductType): string {
  if (form === "figure") return "3D figure";
  return product === "magnet" ? "Flat relief" : "Flat charm";
}

const FIGURE_WORDS = /\b(?:3\s*-?\s*d|three[\s-]*d(?:imensional)?|figur(?:e|ine)s?|sculpt(?:ed|ure)?|statue)\b/i;
const FLAT_WORDS = /\b(?:flat(?!\s*-?\s*back)|2\s*-?\s*d|two[\s-]*d|relief|cut\s*-?\s*out|badge|medallion|silhouette)\b/i;

/** The form the request states outright, else null (then intake asks). */
export function formFromText(text: string): ProductForm | null {
  const fig = FIGURE_WORDS.test(text ?? "");
  const flat = FLAT_WORDS.test(text ?? "");
  if (fig === flat) return null;
  return fig ? "figure" : "flat";
}

const SUBJECT_WORDS = new Set([
  "dog", "dogs", "puppy", "pup", "cat", "cats", "kitten", "kitty", "rabbit", "bunny", "bird",
  "parrot", "horse", "hamster", "ferret", "turtle", "lizard", "baby", "me", "him", "her",
  "them", "us", "kid", "son", "daughter", "wife", "husband", "boyfriend", "girlfriend", "mom",
  "dad", "chihuahua", "frenchie", "beagle", "golden", "terrier", "spaniel", "hound", "lab",
  "poodle", "pug", "corgi", "husky", "calico", "tabby",
]);
const STOP =
  String.raw`(?=\s*(?:[,.;!?]|$|\s(?:and|with|holding|wearing|while|on\s+(?:a|the|his|her)|in|for|please|pls|plz|lol)\b))`;
const DET = String.raw`(?:(?:a|an|the|his|her|their|its|my|our|some|little|lil|tiny|cute|big)\s+)*`;
const CLOTHES_WORDS =
  "hat|sombrero|cap|beanie|bow ?tie|bow|bandana|scarf|sweater|shirt|jersey|hoodie|costume|outfit|tux(?:edo)?|crown|cape|tiara|glasses|sunglasses|goggles|collar|jacket|coat|dress|tutu|boots|shoes|socks|harness|necklace|helmet";
const CLOTHES = new RegExp(`(?:${CLOTHES_WORDS})$`);
const THINGS =
  "ball|toy|bone|stick|frisbee|flower|balloon|cake|guitar|book|mug|leash|pumpkin|present|gift|heart|sign|carrot|fish|mouse|yarn|pizza|donut|coffee|" +
  CLOTHES_WORDS;

const PATTERNS: [CustomAsk["kind"], string, string][] = [
  ["outfit", String.raw`\bwear(?:ing|s)?\s+` + DET + String.raw`([a-z][a-z' -]{1,40}?)` + STOP, "wearing a {}"],
  ["outfit", String.raw`\bdressed\s+(?:up\s+)?as\s+` + DET + String.raw`([a-z][a-z' -]{1,40}?)` + STOP, "dressed as a {}"],
  ["outfit", String.raw`\bin\s+` + DET + String.raw`((?:[a-z][a-z' -]{0,29}?\s)?(?:` + CLOTHES_WORDS + ")s?)" + STOP, "wearing a {}"],
  ["prop", String.raw`\bholding\s+` + DET + String.raw`([a-z][a-z' -]{1,40}?)` + STOP, "holding a {}"],
  [
    "prop",
    String.raw`\b(?:with|give\s+(?:him|her|them|it))\s+` + DET + String.raw`((?:[a-z][a-z' -]{0,29}?\s)?(?:` + THINGS + ")s?)" + STOP,
    "with a {}",
  ],
  [
    "expression",
    String.raw`\b(tongue\s+out|smiling|winking|sticking\s+(?:his|her|its)\s+tongue\s+out|happy\s+face|grumpy\s+face)\b`,
    "{}",
  ],
  [
    "pose",
    String.raw`\b(sitting(?:\s+up)?|standing(?:\s+up)?|(?:lying|laying)(?:\s+down)?|sleeping|asleep|curled\s+up|hugging|cuddling|snuggl(?:ed|ing)\s+up|playing|jumping|running|waving|begging|rolling\s+over|on\s+(?:his|her|its)\s+back)\b`,
    "{}",
  ],
];
const TEXT_PATTERNS = [
  String.raw`\bwith\s+(?:his|her|their|its|the|my)\s+names?\s+["']?([A-Za-z][\w' &-]{0,23}?)["']?(?=\s*(?:[,.;!?]|$|\s(?:and|on|in|please|pls)\b))`,
  String.raw`\bthat\s+says\s+["']([^"']{1,24})["']`,
  String.raw`\bwith\s+["']([^"']{1,24})["']\s+on\s+it\b`,
];

/** "wearing a {thing}" with the right article: none for plurals, "an" before a vowel. */
function article(phrase: string, thing: string): string {
  if (!(" " + phrase).includes(" a " + thing)) return phrase;
  const plural = thing.endsWith("s") && !thing.endsWith("ss") && !thing.endsWith("us");
  const art = plural ? "" : /^[aeiou]/.test(thing) ? "an " : "a ";
  return phrase.replace("a " + thing, art + thing);
}

const clean = (s: string) => s.replace(/\s+/g, " ").replace(/^[\s'-]+|[\s'-]+$/g, "");

/** Props, outfits, poses, expressions and names in common phrasings of the request. */
export function asksFromText(text: string): CustomAsk[] {
  const out: CustomAsk[] = [];
  const seen = new Set<string>();
  const low = (text ?? "").toLowerCase();
  for (const [kind, pat, fmt] of PATTERNS) {
    for (const m of low.matchAll(new RegExp(pat, "g"))) {
      const thing = clean(m[1]);
      const words = thing.split(" ").filter(Boolean);
      if (!words.length || SUBJECT_WORDS.has(words.at(-1)!) || seen.has(thing)) continue;
      let k = kind;
      let f = fmt;
      if (kind === "prop" && fmt === "with a {}" && CLOTHES.test(thing.replace(/s+$/, ""))) {
        k = "outfit";
        f = "wearing a {}";
      }
      seen.add(thing);
      out.push({ kind: k, text: article(f.replace("{}", thing), thing).slice(0, 120) });
    }
  }
  for (const pat of TEXT_PATTERNS) {
    for (const m of (text ?? "").matchAll(new RegExp(pat, "gi"))) {
      const name = clean(m[1]);
      if (name && !seen.has(name.toLowerCase())) {
        seen.add(name.toLowerCase());
        out.push({ kind: "text", text: name.slice(0, 24) });
      }
    }
  }
  return out;
}

const ASK_KIND_LABELS: Record<CustomAsk["kind"], string> = {
  prop: "Prop",
  outfit: "Outfit",
  pose: "Pose",
  expression: "Face",
  text: "Name",
  other: "Ask",
};

/** One chip per ask: "Outfit" and "wearing a sombrero"; a name reads as it will print. */
export function askChip(a: CustomAsk): { kind: string; text: string } {
  return { kind: ASK_KIND_LABELS[a.kind] ?? "Ask", text: a.text };
}

/** The word that names an ask in a check result: "Sombrero", the name itself, "Sitting". */
export function askKeyWord(text: string): string {
  const words = text.match(/[A-Za-z][A-Za-z'-]*/g) ?? [];
  const skip = /^(wearing|holding|with|a|an|the|his|her|their|its|dressed|as|up|on|in)$/i;
  const w = [...words].reverse().find((x) => !skip.test(x)) ?? text;
  return w.charAt(0).toUpperCase() + w.slice(1);
}

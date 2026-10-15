
layerling makes small multicolor 3D prints of people's pets and people from their photos: keychains, nameplates, figures, ornaments and magnets, printed in New York. The system is "Paper": the ground is paper, text is ink, the one accent is brick, and the only other colors are the real filaments on the printer. The subject is the object, so the page is built around real renders of real prints and the real units they come in (millimeters, layers, colors).

## Voice

- Plain and specific. Name the thing and give the number: "About 45 mm tall", "Up to 5 colors", "Checked by a person before it prints".
- Short sentences, sentence case, no exclamation marks, no emoji, no em dashes. Use a colon, a comma or a full stop.
- "We" is the shop (Jackson). "You" is the customer. Never "our AI", "our team of experts" or "crafted with love".
- Say what happens next, not how we feel about it: "We'll reply by email or text with a preview. Nothing is charged until you approve it."
- Missing facts stay visible placeholders: [TURNAROUND], [DELIVERY]. Never invent a number, a review or a stat.
- UI copy is labels and numbers. Buttons are verbs: "Send order", "See sizes", "See a first look". Labels are nouns: "What you want", "Size".
- Do not name the machine or the vendor ("Bambu Lab X2D", "Meshy") on the public site.

Real copy to reuse:

| Where | Copy |
|---|---|
| Hero | Your pet, printed in color. / Send a photo. We print it in up to five colors as a keychain, nameplate, figure, ornament or magnet. |
| Facts | Up to 5 colors · Checked by a person before it prints · Made in New York |
| Contact | We'll reply by email or text. (both fields Optional; error: Add an email or a phone number) |
| Summary | Keychain, M · 45 mm tall · $31.99 · Quantity at checkout |
| Other | Tell us what to make (a sentence or two) · Priced after we see it: [OTHER RANGE] from the cost model |
| First look | Rough first look. The real preview comes after a person checks your photos. |

## Color

- Page ground is `paper`. Lay the order form, the order summary and dialogs on `sheet` with `elev-sheet`. Use `tint` for at most one band per page (How it works).
- Text is `ink` on paper, sheet, tint and field. Secondary text is `ink-2`. Faint text (Optional tags, helper lines, the typing hint, credits) is `ink-3`, never on `tint`.
- `brick` is the only accent. It marks the one primary action per view, the current step, the typing caret and the print line. Text on brick is `on-brick`. Never brick body text, never a brick band or wash behind content.
- `fil-*` colors exist only to depict filament: swatches, filament dots, a colored layer stack in the printing state. Red is never an error, blue is never a link.
- Errors are `danger` text with words, and the field border turns `danger`. No red backgrounds.
- Controls have a `line-strong` edge (3:1 or better). `line` is for decorative rules only.

## Type

- `display` is Fraunces with the SOFT axis at 100 and WONK 0 at every display size: the rounded terminals read like extruded plastic. Weight 600. Headings are sentence case and end with a full stop only when they are a sentence ("Your pet, printed in color.").
- The `wordmark` style (Fraunces 700, SOFT 0) is only the logo.
- `sans` (Instrument Sans) is everything else: `lede` under a heading, `body` for text, `label` above fields.
- `mono` (Geist Mono) is only for measurements and references with a unit or an id: "45 mm", "24.26 mm", "Order 98edb3f8". Never for prose or buttons.
- Prices use `display-s` in the order summary with tabular numbers; elsewhere `body-strong`.
- No uppercase letterspaced eyebrows above headings. The `slug` style stays in the studio proof sheet header only.

## Layout and spacing

- Max width 1440 with `space-18` gutters on desktop, `space-4` on phones. Section padding `space-24` desktop, `space-16` phones.
- Left-aligned, asymmetric compositions. Headings sit on the left edge of the grid; nothing is centered except a dialog.
- Lay sibling groups out with flex or grid and `gap` from the spacing scale.
- Not everything is a card. The product lineup stands on a shelf line, not in boxes. Only the order sheet, the summary and dialogs are sheets.

## Radius, borders, elevation

- `radius-md` on fields, size cells, product tiles and the dropzone; `radius-lg` on sheets and dialogs; `radius-pill` on buttons and the segmented control only; `radius-sm` on thumbnails.
- `border-hair` everywhere; `border-select` (1.5px `ink`) marks the selected tile or size cell.
- Shadows: `elev-sheet` (offset, no blur) under sheets, `elev-photo` under the photo prints in the How it works trio, `elev-float` under dialogs. Nothing else casts a shadow.
- Focus: `focus-ring` (a sheet gap, then 2px ink) on every interactive element, always visible on keyboard focus. Never brick, so focus never reads as an error.

## Imagery

- Products are shown only as real renders of real printed models (the sliced 3MF, in the loaded filament colors), rendered with one camera, one lens and one light on paper, transparent PNG with a soft contact shadow. Sources: apps/web/public/products/ and renders.json.
- Size comparisons use the `-front` renders, which are orthographic at 10 px per mm, so relative scale is exact.
- Photos are real customer-style phone photos with their credit line. Concept images are labelled as concepts.
- No hand-drawn stand-ins for products, no stock photos, no illustrations of people, no 3D blobs or gradients.

## Iconography

- Icons are 1.75px stroke outlines on a 24px grid, round caps, drawn in `ink` or `currentColor`: upload, close, lock, arrow, ruler. Only where a word alone is slower.
- Reference objects in the size visualizer (quarter, card, AirPods case, hand) are single-weight ink outlines with their real dimensions.
- No icon grids, no emoji, no decorative icons beside headings.

## Motifs

- Layer lines: 1px `ink` at 5% every 7px. Only behind the landing hero and inside empty or loading render areas. One per viewport.
- Ruler: 1 mm ticks in `line-strong`, labels in `measure-s`. Only where the scale is real (the lineup shelf, the size visualizer).
- Print bed: the brick bar under the logo mark. Not reused as a decoration.

## Motion

- Signature: print-in. A render builds from the bottom up in 18 visible layers as it scrolls into view (`ease-print`), tied to scroll where the browser supports scroll timelines, else once over `dur-print`. A 1px brick line rides the top edge while it builds.
- Headings and ledes rise 12px and fade in over `dur-reveal` with `ease-out`, once.
- The How it works trio drifts at three parallax rates (at most 24px).
- Prices and mm values tick to the new number over `dur-base`.
- The typing hint in What you want types, holds and erases rotating ideas; it stops the moment the field has focus or text.
- Only transform, opacity and clip-path animate. Nothing moves layout. Under `prefers-reduced-motion: reduce` everything is shown in its final state, the typing hint shows one static example, and parallax is off.

## Not AI-generated: do and don't

| Do | Don't |
|---|---|
| Real renders of the actual printed model | Hand-drawn clip art standing in for a product |
| Numbers with units: 45 mm, 5 colors, $31.99 | Filler taglines: "Bring your memories to life" |
| One accent, used for the one action | Gradient washes, glass, glows |
| Left-aligned, asymmetric, a shelf line | Centered hero plus three icon cards |
| Sentence-case headings | Uppercase letterspaced eyebrows on every section |
| Numbered steps only when order matters | Giant accent numerals as decoration |
| Plain facts in one quiet line | A dark marquee band of slogans |
| Placeholders like [TURNAROUND] | Invented reviews, stats or "trusted by" rows |
| Line icons only where a word is slower | Emoji, icon grids, sparkles |
| The machine stays backstage | "Printed on a Bambu Lab X2D", "AI-powered" |

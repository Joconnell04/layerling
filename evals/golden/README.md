# Golden set

20 photo-to-print jobs, re-run after every meaningful change. `sets.json` holds each job's prompt, product, subjects and photo sources. The photos live in `photos/<set_id>/` and are gitignored; `contact_sheet.jpg` (also local) shows one row per set.

**Subjects:** person with pet (5), two pets (3), single pet (3), black-and-white animal (2), calico or tabby (2), person alone (2), object (2), hardest case (1: two people and a dog on a nameplate).

**Products:** keychain 45 mm (6), figurine on round base (5), nameplate with raised name (5), ornament (2), fridge magnet (2). `product` follows `AddOn` and `PRODUCT_DEFAULTS` in `contracts/src/p2p_contracts/job.py`; a size in the prompt ("4 inch") overrides the default.

**Prompts** are written the way customers type them: short, lowercase, casual ("keychain of my frenchie wearing a sombrero lol"). The pipeline adds the detail. Four sets carry custom asks that must survive into the concept and the 3D model: a sombrero (`bw_frenchie`), a bow tie (`sp_beagle`), holding a ball (`sp_golden_face`, and a ball of yarn on `pat_calico`), and a name on a nameplate (`sp_beagle`, `sp_rabbit`, `pat_tabby`, `tp_two_terriers`, `hard_couple_dog`). `obj_teddy` asks for a name on a keychain, which only a flat charm can engrave.

**`expect`** per set, for scoring intake and the concept check:
- `form`: `flat` or `figure` when the request says it ("3d", "flat"); `null` when intake must ask (keychains and magnets only; every other product is `figure`). `form_asked` is true exactly when the form question should appear.
- `custom_asks`: what intake must list and the concept must show, in the customer's words (poses count: "hugging", "curled up").
- `name`: the exact text a nameplate must read, case included.

**Sources:** 32 photos from Wikimedia Commons (CC BY, CC BY-SA or CC0; author and license per photo, credit them if a result is shared) and 1 product photo that is not included (`obj_eames_birds`: supply your own). Names on nameplates come from the Commons file title where it had one (Bronco, Blossom & Laysha); the rest are invented.

**Add or replace a set:** put 1 to 3 photos of the same subjects in `photos/<set_id>/` (JPEG or PNG, long side at most 2048 px; `sips -s format jpeg` for HEIC, `sips -Z 2048` to shrink), add an entry to `sets.json` with the same fields, and keep the product mix above. To rebuild `photos/` on a new machine, download each photo's `url`; for `obj_eames_birds`, use any product photo of an Eames House Bird into its `file` path.

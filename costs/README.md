# costs

Cost model and prices. `cost_model.xlsx` is the model; `pricing.json` is what the web page shows: the own-site price per product, plus every channel's price.

## Refresh

```bash
uv run python costs/refresh.py                      # after new test runs or an edit in the workbook
uv run python costs/refresh.py --reason "new filament price"   # the reason goes in the Changelog
uv run python costs/refresh.py --jobs-dir /path/to/jobs        # also ingest another jobs folder
uv run python -m p2p_costs.sensitivity              # which ASSUMED inputs move price most
uv run pytest costs/tests
```

Refresh reads the workbook, re-ingests test outputs, rebuilds the formula sheets, computes the same
numbers in Python, evaluates the formulas with LibreOffice headless, and stops without saving if
any number differs by more than half a cent. Then it writes `pricing.json` and adds a Changelog
row when a price moves. It needs LibreOffice (`brew install --cask libreoffice`). Close the
workbook in LibreOffice before refreshing.

Edit the workbook in Excel or Numbers. From Numbers, export back to `costs/cost_model.xlsx`
(File > Export To > Excel) so refresh can read it.

## Sheets

| Sheet | What | Who edits |
|---|---|---|
| Inputs | Every assumption: value, unit, provenance (SOURCE, FROM JACKSON, ASSUMED), link, notes, what moves if it is wrong | Jackson. Refresh never changes it; it only adds new keys at the bottom |
| Test data | One row per test run: config (settings, variant, orientation), Auto, Use, Counts, credits, grams per filament, purge, print minutes, source file | Refresh updates it in place. Use is Jackson's override (1 in, 0 out, blank follows Auto); refresh never writes to it |
| Market | Comparable listings with delivered price; helper columns feed the quartiles | Jackson (add rows, set Use 0). Refresh adds new research listings |
| Per product | Formulas: Meshy, material (purge explicit), printer time, hardware, labor, packaging, market quartiles, own-site price options, purge-halved sensitivity | Refresh |
| Prices | Channel fees, then price per product and channel (own site shipped, own site NYC pickup, Etsy, Facebook pickup, eBay, market booth): break-even, target-margin price, market low quartile, rounded price, Override, margin; then booth break-even units per day | Refresh, except Override (yellow), which refresh keeps |
| Sizes | Own-site price per size tier (XS, S, M, L from contracts SIZE_TIERS_MM): a tier's own slices when Test data has them, else the M slice scaled by size (exponents in Inputs, ASSUMED); M equals the Prices own-site price; then the custom (Other) range | Refresh, except Override (yellow, non-M tiers), which refresh keeps |
| Changelog | Date, what changed, why | Refresh appends |

## Test data sources

Refresh scans the main checkout, this checkout and every worktree under `.claude/worktrees/`:
`data/slice_reports/*.slice_report.json`, `threemf/_out/`, `geometry/_out/`, `data/ledger.jsonl`
(real Meshy spend) and `jobs/<id>/` (job.json, ledger.jsonl, any slice report). Every run it finds
is kept in `test_data.jsonl`, so a run stays in the model after its folder is gone.

Auto is 1 when a run matches what production does now: the slicer settings the purge lab
recorded equal the writer's defaults (`p2p_threemf.writer.DEFAULT_PROCESS_OVERRIDES`), the layer
height is the production one, and the orientation is the geometry stage's (keychains and magnets
flat, `p2p_geometry.product.FLAT_PRODUCTS`). Real Meshy slices win over procedural shapes of the
same product. Finished, non-dev worker jobs count for credits. A product with no counted run
falls back to its placeholders in Inputs (ASSUMED, marked for replacement).

## Pricing rule

Price = the higher of the target-margin price and the market low quartile, rounded up to the
channel's price ending (.99 online, whole dollars in person). Target-margin price = (cost before
fees + fixed fees) / (1 - percent fees - target margin). An Override replaces the formula price.

Market listings go in `research/market_*.json` (not included in the public repo; refresh works without them).

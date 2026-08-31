# Screenshot capture guide

The images in [img/](img/) are real captures of the local dashboard, taken
2026-08-31. To refresh one, save a new capture over it — **same path, same
filename**. The README references these paths directly, so nothing else needs
editing.

Captures are downscaled to ~1800px wide (half of a Retina grab) so the repo stays
light. If you are converting from JPG, re-save as PNG at the same name.

## Before you start

```bash
./start.command          # builds if needed, serves on http://127.0.0.1:8000
```

Give it a run's worth of data first, or most views will be empty:

```bash
.venv/bin/python orchestrator.py --mode recommend --account individual
```

**Capture settings**

- Browser window ~1600×1000 (the placeholders' size), zoom at 100%.
- macOS: `Cmd-Shift-4`, then `Space`, then click the window for a clean shot with
  no desktop behind it.
- Save as PNG.

## ⚠️ Redact before committing

These views show a **real brokerage account**. Before saving, crop or blur:

- The **account number** (appears in Portfolio and Trade).
- Balances and P&L, if you would rather not publish them — the README is public.

A quick check for a leaked account number once the files are in — the pattern
comes from your own `config.local.yaml`, so it never gets written down here:

```bash
grep -oE '[0-9]{8,}' config.local.yaml | sort -u | while read -r n; do
  grep -rln "$n" README.md docs/ 2>/dev/null && echo "LEAK: $n"
done; echo "check complete"
```

## The five shots

| File | View | What should be on screen |
|------|------|--------------------------|
| `img/overview.png` | **Overview** | Landing state: run status, last brief, quick actions and the top ideas list. This is the README's hero image — make it the tidiest one. |
| `img/ideas.png` | **Ideas** | The ranked list with score bars visible, and the per-stock **"why" drawer open** on one name so the technicals / fundamentals / guardrail breakdown shows. |
| `img/deepdive.png` | **Deep dive** | A completed briefing for a well-known ticker (NVDA reads well), scrolled to show the verdict and the scenario levels. |
| `img/trade.png` | **Trade** | The **plan** step with proposed orders listed and per-order approve controls visible — not a live-confirm dialog. Redact the account number. |
| `img/activity.png` | **Activity** | The decisions/orders/fills table with a few real rows, ideally including a guardrail `RESIZE` line. |

## Regenerating the placeholders

If you need the placeholders back, the generator is a short PIL script — see the
commit that added this file, or just delete the PNGs and the README will show
broken images until real ones land.

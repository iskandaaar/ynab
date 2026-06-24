# TNG eWallet → YNAB importer

Parses a Touch 'n Go eWallet transaction PDF and bulk-uploads it into YNAB.
Re-runnable every month; safe to re-run on overlapping dates (deduped).

## One-time setup
```bash
cd "/Users/sharlynesimon/Documents/Iskandar's Code/YNAB"
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit .env and fill in TNG_PDF_PASSWORD (+ token if rotated)
```

`.env` holds your secrets (token, budget id, account id, PDF password) and is gitignored.
Destination is preset to budget **2026 🚀** → account **TNGo E-Wallet**.

## Monthly use
1. Export your transactions from the TNG eWallet app as PDF and drop it in this folder.
2. Preview (no changes pushed):
   ```bash
   ./.venv/bin/python import_tng.py --dry-run
   ```
   Check `import_preview.csv` and the "Unmatched payees" list.
3. Add any new merchants to `rules.json` to auto-categorize them, then re-run the dry-run.
4. Push for real:
   ```bash
   ./.venv/bin/python import_tng.py
   ```
   (add `--yes` to skip the confirm prompt). Re-running is safe — duplicates are skipped.
5. **Reconcile:** the last "Wallet Balance" in the PDF = your real TNG balance. After import,
   the YNAB TNGo E-Wallet account balance should equal it. If it does, you're done.

> If you also enter some TNG transactions manually in YNAB, those have no `import_id`, so the
> importer can't dedup against them — use `--since=YYYY-MM-DD` to import only from the day after
> your last manual entry and avoid overlap duplicates.

## Flags
- `--dump` — print the raw PDF text/tables (use when a new statement layout breaks parsing).
- `--dry-run` — parse + categorize + write `import_preview.csv`, but don't touch YNAB.
- `--yes` — skip the confirmation prompt.
- `--since=YYYY-MM-DD` — only import transactions on/after this date (avoid overlap with data
  you already entered manually).
- `--include-transfers` — keep wallet top-ups. By default DuitNow-receive / reload **inflows
  are skipped** because they're internal transfers from a bank account that YNAB tracks on the
  other side (importing them here would double-count).
- A PDF path can be passed explicitly: `... import_tng.py path/to/file.pdf`.

## How dedup works
Each transaction gets a stable `import_id` (`TNG:<md5 of the full reference>`), so YNAB skips
any it has already imported — even across overlapping date ranges. The push summary prints
`Created: N | Skipped duplicates: M`. (Deleted transactions stay deduped too, so a re-run
won't resurrect ones you removed.)

## Categorization
`rules.json` is an ordered list of `{ "match": "<text>", "category": "<exact YNAB category>" }`.
First case-insensitive substring match (on payee + type) wins. Edit it freely — no code changes
needed. The dry-run flags any rule category name that doesn't exist in the budget.

## Notes
- Currency is MYR; amounts convert to YNAB milliunits (RM 1.00 = 1000).
- Outflows are negative, reloads/refunds positive (left in Ready-to-Assign).
- Rotate the YNAB token if it was ever shared; put the new one in `.env`.

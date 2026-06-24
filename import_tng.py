#!/usr/bin/env python3
"""
TNG eWallet PDF -> YNAB bulk importer.

Usage:
    python import_tng.py [PDF] [--dry-run] [--dump] [--yes]
                         [--since=YYYY-MM-DD] [--include-transfers]

    PDF                  Path to the TNG eWallet PDF. Defaults to the newest
                         tng_*.pdf in this folder.
    --dump               Print raw extracted text + tables from the PDF and exit.
                         Use once to confirm/tune the parser for a new layout.
    --dry-run            Parse + categorize, write a preview CSV and print a
                         report. Does NOT touch YNAB.
    --yes                Skip the confirmation prompt before the live push.
    --since=YYYY-MM-DD   Only import transactions on/after this date (avoids
                         overlapping data you've already entered manually).
    --include-transfers  Keep wallet top-ups (DuitNow receive / reloads). By
                         default these internal transfers are skipped because
                         they're tracked on the bank-account side in YNAB.

Config is read from .env (see .env.example): YNAB_TOKEN, YNAB_BUDGET_ID,
YNAB_ACCOUNT_ID, TNG_PDF_PASSWORD.
"""
import csv
import glob
import hashlib
import json
import os
import re
import sys

import pdfplumber
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
YNAB_BASE = "https://api.ynab.com/v1"

MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


# ----------------------------------------------------------------------------- config
def load_env():
    env = {}
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # allow real environment to override
    for k in ("YNAB_TOKEN", "YNAB_BUDGET_ID", "YNAB_ACCOUNT_ID", "TNG_PDF_PASSWORD"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def newest_pdf():
    pdfs = sorted(glob.glob(os.path.join(HERE, "tng_*.pdf")) +
                  glob.glob(os.path.join(HERE, "*.pdf")),
                  key=os.path.getmtime, reverse=True)
    return pdfs[0] if pdfs else None


# ----------------------------------------------------------------------------- parsing helpers
def parse_date(s):
    """Return ISO YYYY-MM-DD from common TNG date formats, or None."""
    s = s.strip()
    # 26 May 2026  /  26 May 2026, 10:30 AM
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", s)
    if m:
        d, mon, y = m.group(1), m.group(2)[:3].lower(), m.group(3)
        if mon in MONTHS:
            return f"{y}-{MONTHS[mon]}-{int(d):02d}"
    # 26/05/2026 or 26-05-2026
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    # 2026-05-26
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    return None


def parse_amount(s):
    """Return (rm_float_signed) from a TNG amount cell, or None.
    Outflow negative, inflow positive. Handles 'RM', +/-, parentheses, commas."""
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    neg = False
    if "(" in raw and ")" in raw:   # (12.50) = negative
        neg = True
    if re.search(r"^\s*-|−", raw):  # leading - or unicode minus
        neg = True
    plus = bool(re.search(r"^\s*\+", raw))
    num = re.search(r"(\d[\d,]*\.?\d*)", raw)
    if not num:
        return None
    val = float(num.group(1).replace(",", ""))
    if val == 0:
        return None
    if neg and not plus:
        val = -val
    return val


# ----------------------------------------------------------------------------- TNG parser
# TNG export column order (header appears on page 1 only; later pages are
# headerless continuation tables, so we keep this as the fallback mapping).
DEFAULT_COLS = {"date": 0, "status": 1, "type": 2, "ref": 3,
                "desc": 4, "details": 5, "amount": 6, "balance": 7}


def parse_tng(pdf):
    """Extract normalized transactions: list of dicts {date, payee, amount, ref, type}.

    Tables on page 1 carry a header; continuation pages don't. We detect the
    header when present and reuse that column mapping for headerless tables.
    """
    rows = []
    cols = None
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            if not table:
                continue
            detected = _detect_cols(table[0])
            if detected:
                cols = detected
                data = table[1:]
            else:
                data = table  # headerless continuation table
            rows.extend(_rows_with_cols(data, cols or DEFAULT_COLS))
    if rows:
        return rows
    # text fallback
    for page in pdf.pages:
        text = page.extract_text() or ""
        rows.extend(_rows_from_text(text))
    return rows


def _find_col(header, *keywords):
    for i, cell in enumerate(header):
        c = (cell or "").lower()
        if any(k in c for k in keywords):
            return i
    return None


# TNG amounts in the PDF carry NO sign — direction is inferred from the type.
# Keywords are spaceless because the type cell can wrap mid-word across lines
# (e.g. "DUITNOW_RECEI\nVEFROM").
INFLOW_KW = ("receive", "reload", "refund", "cashback", "topup",
             "reward", "incoming", "transferin", "received")


def direction(ttype):
    """+1 for money in (received/reload/refund), -1 for payments (money out)."""
    t = re.sub(r"[\s_]+", "", (ttype or "").lower())
    return 1 if any(k in t for k in INFLOW_KW) else -1


# Wallet top-ups (DuitNow receive / reloads) are internal transfers from a bank
# account that's tracked separately in YNAB — importing them here would double
# count, so they're skipped by default (use --include-transfers to keep them).
TRANSFER_KW = ("receive", "reload", "topup", "transferin", "incoming", "duitnow")


def is_internal_transfer(txn):
    t = re.sub(r"[\s_]+", "", (txn.get("type") or "").lower())
    return txn["amount"] > 0 and any(k in t for k in TRANSFER_KW)


def _detect_cols(row):
    """Return a column-index mapping if `row` looks like the TNG header, else None."""
    header = [(c or "").strip().lower() for c in row]
    i_date = _find_col(header, "date")
    i_amt = _find_col(header, "amount")
    if i_date is None or i_amt is None:
        return None
    return {
        "date": i_date,
        "status": _find_col(header, "status"),
        "type": _find_col(header, "transaction type", "type"),
        "ref": _find_col(header, "reference"),
        "desc": _find_col(header, "description", "merchant"),
        "amount": i_amt,
    }


def _rows_with_cols(data, cols):
    out = []

    def cell(r, key):
        i = cols.get(key)
        return r[i].strip() if i is not None and i < len(r) and r[i] else ""

    for r in data:
        if not r or all((c or "").strip() == "" for c in r):
            continue
        date = parse_date(cell(r, "date"))
        amount = parse_amount(cell(r, "amount"))
        if not date or amount is None:
            continue
        status = cell(r, "status")
        if status and "success" not in status.lower() and "complete" not in status.lower():
            continue  # skip failed/pending
        ttype = cell(r, "type").replace("\n", " ").strip()
        desc = cell(r, "desc").replace("\n", " ").strip()
        ref = cell(r, "ref")
        amount = abs(amount) * direction(ttype)   # PDF has no sign; infer from type
        out.append({"date": date, "payee": desc or ttype or "TNG eWallet",
                    "amount": amount, "ref": ref, "type": ttype})
    return out


def _rows_from_text(text):
    """Fallback: each line that has a date and a trailing amount."""
    out = []
    for line in text.splitlines():
        date = parse_date(line)
        if not date:
            continue
        amt_m = re.search(r"([+\-−(]?\s*RM?\s*\d[\d,]*\.\d{2}\)?)\s*$", line, re.I)
        if not amt_m:
            continue
        amount = parse_amount(amt_m.group(1))
        if amount is None:
            continue
        desc = line[:amt_m.start()]
        desc = re.sub(r"^\s*\d{1,2}\s+[A-Za-z]{3,}\s+\d{4}[, ]*", "", desc)
        desc = re.sub(r"\d{1,2}:\d{2}\s*(AM|PM)?", "", desc, flags=re.I).strip()
        ref_m = re.search(r"\b([A-Z0-9]{10,})\b", desc)
        ref = ref_m.group(1) if ref_m else ""
        out.append({"date": date, "payee": desc.strip(" -|"),
                    "amount": amount, "ref": ref, "type": ""})
    return out


# ----------------------------------------------------------------------------- categorization
def load_rules():
    with open(os.path.join(HERE, "rules.json")) as f:
        return json.load(f).get("rules", [])


def categorize(txn, rules):
    hay = f"{txn['payee']} {txn.get('type','')}".lower()
    for rule in rules:
        if rule["match"].lower() in hay:
            return rule["category"]
    return None


# ----------------------------------------------------------------------------- YNAB
def ynab_get(env, path):
    r = requests.get(f"{YNAB_BASE}{path}",
                     headers={"Authorization": f"Bearer {env['YNAB_TOKEN']}"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def category_name_to_id(env):
    data = ynab_get(env, f"/budgets/{env['YNAB_BUDGET_ID']}/categories")
    out = {}
    for g in data["category_groups"]:
        for c in g["categories"]:
            if not c.get("deleted"):
                out[c["name"].strip()] = c["id"]
    return out


def to_ynab_txn(txn, env, cat_id, seen):
    milli = int(round(txn["amount"] * 1000))
    ref = re.sub(r"[^A-Za-z0-9]", "", txn.get("ref", ""))
    if ref:
        # Reference's unique part is at the END, so hash the whole thing
        # (stable per transaction) to a fixed-length, collision-safe import_id.
        import_id = "TNG:" + hashlib.md5(ref.encode()).hexdigest()[:28]
    else:
        key = f"YNAB:{milli}:{txn['date']}"
        seen[key] = seen.get(key, 0) + 1
        import_id = f"{key}:{seen[key]}"[:36]
    payee = (txn["payee"] or "TNG eWallet")[:50]
    memo_bits = [b for b in [txn.get("type", ""), txn.get("ref", "")] if b]
    obj = {
        "account_id": env["YNAB_ACCOUNT_ID"],
        "date": txn["date"],
        "amount": milli,
        "payee_name": payee,
        "memo": " | ".join(memo_bits)[:200],
        "cleared": "cleared",
        "approved": True,
        "import_id": import_id,
    }
    if cat_id:
        obj["category_id"] = cat_id
    return obj


def push(env, txns):
    r = requests.post(
        f"{YNAB_BASE}/budgets/{env['YNAB_BUDGET_ID']}/transactions",
        headers={"Authorization": f"Bearer {env['YNAB_TOKEN']}",
                 "Content-Type": "application/json"},
        data=json.dumps({"transactions": txns}), timeout=60)
    if r.status_code >= 400:
        print("YNAB ERROR", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["data"]


# ----------------------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    dump = "--dump" in args
    dry = "--dry-run" in args
    yes = "--yes" in args
    include_transfers = "--include-transfers" in args
    since = next((a.split("=", 1)[1] for a in args if a.startswith("--since=")), None)
    positional = [a for a in args if not a.startswith("--")]
    env = load_env()

    pdf_path = positional[0] if positional else newest_pdf()
    if not pdf_path or not os.path.exists(pdf_path):
        sys.exit(f"PDF not found: {pdf_path}")
    print(f"PDF: {pdf_path}")

    pw = env.get("TNG_PDF_PASSWORD") or None
    try:
        pdf = pdfplumber.open(pdf_path, password=pw)
    except Exception as e:
        sys.exit(f"Could not open PDF (wrong/missing TNG_PDF_PASSWORD in .env?): {e}")

    if dump:
        with pdf:
            for n, page in enumerate(pdf.pages, 1):
                print(f"\n===== PAGE {n} TEXT =====")
                print(page.extract_text() or "(no text)")
                for t, table in enumerate(page.extract_tables() or [], 1):
                    print(f"\n----- PAGE {n} TABLE {t} -----")
                    for row in table:
                        print(row)
        return

    with pdf:
        txns = parse_tng(pdf)
    if not txns:
        sys.exit("No transactions parsed. Run with --dump to inspect the layout, "
                 "then tune parse_tng().")

    # Filtering: skip internal-transfer inflows and anything before --since.
    skipped_transfers = [t for t in txns if not include_transfers and is_internal_transfer(t)]
    txns = [t for t in txns if include_transfers or not is_internal_transfer(t)]
    skipped_since = []
    if since:
        skipped_since = [t for t in txns if t["date"] < since]
        txns = [t for t in txns if t["date"] >= since]
    if skipped_transfers:
        print(f"Skipped {len(skipped_transfers)} internal-transfer inflow(s) "
              f"(RM {sum(t['amount'] for t in skipped_transfers):,.2f}) — tracked as bank "
              f"transfers. Use --include-transfers to keep them.")
    if skipped_since:
        print(f"Skipped {len(skipped_since)} transaction(s) before {since}.")
    if not txns:
        sys.exit("Nothing left to import after filtering.")

    rules = load_rules()
    name_to_id = None if (dry or dump) else None
    # fetch category map (needed for live; also nice for dry-run validation)
    try:
        name_to_id = category_name_to_id(env)
    except Exception as e:
        print(f"(warning) could not fetch categories: {e}")
        name_to_id = {}

    seen = {}
    prepared, unmatched, missing_cat = [], set(), set()
    for t in txns:
        cat_name = categorize(t, rules)
        cat_id = None
        if cat_name:
            cat_id = name_to_id.get(cat_name)
            if not cat_id:
                missing_cat.add(cat_name)
        else:
            unmatched.add(t["payee"])
        prepared.append((t, cat_name, to_ynab_txn(t, env, cat_id, seen)))

    # report
    inflow = sum(t["amount"] for t in txns if t["amount"] > 0)
    outflow = sum(t["amount"] for t in txns if t["amount"] < 0)
    print(f"\nParsed {len(txns)} transactions | "
          f"{prepared and prepared[0][0]['date']} .. {prepared[-1][0]['date']}")
    print(f"Inflow RM {inflow:,.2f} | Outflow RM {outflow:,.2f} | Net RM {inflow+outflow:,.2f}")
    print(f"Categorized: {sum(1 for _,c,_ in prepared if c)} | "
          f"Uncategorized: {sum(1 for _,c,_ in prepared if not c)}")
    if missing_cat:
        print("\n  ! Rule categories NOT found in budget (fix names in rules.json):")
        for c in sorted(missing_cat):
            print("    -", c)
    if unmatched:
        print("\n  Unmatched payees (add to rules.json if you want them categorized):")
        for p in sorted(unmatched):
            print("    -", p)

    # preview CSV
    csv_path = os.path.join(HERE, "import_preview.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "payee", "amount_RM", "milliunits", "category", "import_id", "memo"])
        for t, cat_name, y in prepared:
            w.writerow([t["date"], y["payee_name"], f"{t['amount']:.2f}",
                        y["amount"], cat_name or "", y["import_id"], y["memo"]])
    print(f"\nPreview written: {csv_path}")

    if dry:
        print("\nDRY RUN — nothing sent to YNAB.")
        return

    if not yes:
        ans = input(f"\nPush {len(prepared)} transactions to YNAB account "
                    f"{env['YNAB_ACCOUNT_ID']}? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    result = push(env, [y for _, _, y in prepared])
    created = result.get("transaction_ids", [])
    dupes = result.get("duplicate_import_ids", [])
    print(f"\nDone. Created: {len(created)} | Skipped duplicates: {len(dupes)}")


if __name__ == "__main__":
    main()

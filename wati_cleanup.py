#!/usr/bin/env python3
"""
wati_cleanup.py

Interactive tool to turn a Bigin CRM contact export (CSV) into
WATI campaign import files.

Flow:
  1. Load the Bigin export CSV (any columns).
  2. Interactively filter rows by field values (keep OR remove; repeatable).
  3. Map a Name column and a Phone column to the WATI format.
  4. Clean phones to 10-digit Indian numbers (CountryCode = 91) and de-dupe.
  5. Split into batch files of a chosen size (100 / 250 / 500 / custom).

Output format (WATI campaign):
  Name,CountryCode,Phone,AllowCampaign,AllowSMS

Standard library only. Run:  python3 wati_cleanup.py
"""

import csv
import os
import re
import sys

WATI_HEADER = ["Name", "CountryCode", "Phone", "AllowCampaign", "AllowSMS"]
DEFAULT_COUNTRY_CODE = "91"

# Header-name hints used to auto-suggest which columns to map.
NAME_HINTS = ["full name", "contact name", "name", "first name"]
LAST_NAME_HINTS = ["last name"]
PHONE_HINTS = ["mobile", "phone", "mobile phone", "whatsapp", "contact number", "number"]


# --------------------------------------------------------------------------- #
# small input helpers
# --------------------------------------------------------------------------- #
def ask(prompt, default=None):
    """Prompt for a line of input; blank falls back to `default`."""
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val if val else (default if default is not None else "")


def ask_yes_no(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        val = ask(f"{prompt} ({d})").lower()
        if val == "":
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_int_in_range(prompt, lo, hi, default=None):
    while True:
        val = ask(prompt, default=str(default) if default is not None else None)
        if val.isdigit() and lo <= int(val) <= hi:
            return int(val)
        print(f"  Enter a number between {lo} and {hi}.")


def pick_column(headers, prompt):
    """Show numbered headers, return the chosen header string."""
    for i, h in enumerate(headers, 1):
        print(f"  {i:>2}. {h}")
    idx = ask_int_in_range(prompt, 1, len(headers))
    return headers[idx - 1]


def suggest_column(headers, hints):
    """Return the first header whose lowercase name contains any hint, else None."""
    lowered = {h: h.lower().strip() for h in headers}
    for hint in hints:
        for h in headers:
            if lowered[h] == hint:
                return h
    for hint in hints:
        for h in headers:
            if hint in lowered[h]:
                return h
    return None


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #
def load_csv(path):
    # utf-8-sig strips a BOM if present (the existing WATI files have one).
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("The file appears to be empty.")
        headers = [h for h in reader.fieldnames if h is not None]
        rows = [dict(r) for r in reader]
    return headers, rows


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #
def filter_rows(headers, rows):
    """Interactive, repeatable filtering. Returns the surviving rows."""
    while True:
        print(f"\nContacts currently: {len(rows)}")
        if not ask_yes_no("Filter by a field?", default=False):
            break

        col = pick_column(headers, "Pick the field number to filter on")

        # distinct values with counts
        counts = {}
        for r in rows:
            v = (r.get(col) or "").strip()
            counts[v] = counts.get(v, 0) + 1
        distinct = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

        print(f"\nField '{col}' has {len(distinct)} distinct value(s).")
        use_substring = len(distinct) > 40
        if use_substring:
            print("  (many distinct values — using text match instead of a list)")
            needle = ask("  Type text to match (substring, case-insensitive)").lower()
            selected = {v for v in counts if needle and needle in v.lower()}
        else:
            for i, (v, c) in enumerate(distinct, 1):
                shown = v if v != "" else "(blank)"
                print(f"  {i:>2}. {shown}  ({c})")
            raw = ask("  Enter value number(s) to match, comma-separated")
            selected = set()
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(distinct):
                    selected.add(distinct[int(part) - 1][0])

        if not selected:
            print("  Nothing matched — filter skipped.")
            continue

        n_match = sum(counts[v] for v in selected if v in counts)
        print(f"  {n_match} row(s) match the selected value(s).")
        keep = ask_yes_no("  Keep matching rows? (No = remove them instead)", default=True)

        def matches(r):
            return (r.get(col) or "").strip() in selected

        new_rows = [r for r in rows if matches(r) == keep]

        if not new_rows:
            print("  That would leave 0 contacts — filter NOT applied.")
            continue

        rows = new_rows
        print(f"  Applied. Remaining: {len(rows)}")

    return rows


# --------------------------------------------------------------------------- #
# mapping + phone cleaning
# --------------------------------------------------------------------------- #
def choose_mapping(headers):
    print("\n--- Map columns to the WATI format ---")

    # Name
    name_suggest = suggest_column(headers, NAME_HINTS)
    print("\nName column:")
    if name_suggest and ask_yes_no(f"  Use '{name_suggest}' as the Name?", default=True):
        name_cols = [name_suggest]
    else:
        name_cols = [pick_column(headers, "  Pick the Name column")]

    # optional second name column (first + last)
    last_suggest = suggest_column(headers, LAST_NAME_HINTS)
    if last_suggest and last_suggest not in name_cols:
        if ask_yes_no(f"  Also append '{last_suggest}' (last name) to the Name?", default=True):
            name_cols.append(last_suggest)

    # Phone
    phone_suggest = suggest_column(headers, PHONE_HINTS)
    print("\nPhone column:")
    if phone_suggest and ask_yes_no(f"  Use '{phone_suggest}' as the Phone?", default=True):
        phone_col = phone_suggest
    else:
        phone_col = pick_column(headers, "  Pick the Phone column")

    country = ask("\nCountry code", default=DEFAULT_COUNTRY_CODE)
    return name_cols, phone_col, country


def clean_phone(raw):
    """
    Return a clean 10-digit Indian number, or None if it can't be resolved.

    Accepts the number written any of the usual ways — with or without +91,
    with 0091 or 091, with a trunk 0, and with any spaces, dashes or brackets:

        9876543210      +919876543210     +91 98765 43210
        919876543210    0091 9876543210   (+91) 98765-43210
        09876543210     +91 09876543210

    Non-Indian numbers are deliberately rejected: this tool only emits
    CountryCode 91, so a +971 or +60 number is not ours to send to.
    """
    if raw is None:
        return None
    # [^0-9] rather than \D — \D is Unicode-aware and would let Arabic-Indic
    # or Devanagari digits through, producing a 10-character "number" that is
    # not actually digits.
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return None
    # Strip an international prefix, longest form first.
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("091"):
        digits = digits[3:]
    elif len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    # A trunk 0 can survive the country code (e.g. "+91 09876543210").
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return None


def build_wati_rows(rows, name_cols, phone_col, country):
    """Return (good_rows, skipped_rows). De-dupes on phone, keeps first."""
    good, skipped, seen = [], [], set()
    for r in rows:
        name = " ".join((r.get(c) or "").strip() for c in name_cols).strip()
        phone = clean_phone(r.get(phone_col))
        if not phone:
            skipped.append({**r, "_skip_reason": "invalid/missing phone"})
            continue
        if phone in seen:
            skipped.append({**r, "_skip_reason": "duplicate phone"})
            continue
        seen.add(phone)
        good.append({
            "Name": name if name else "Contact",
            "CountryCode": country,
            "Phone": phone,
            "AllowCampaign": "True",
            "AllowSMS": "True",
        })
    return good, skipped


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def choose_batch_size():
    print("\nBatch size (contacts per file):")
    print("  1. 100")
    print("  2. 250")
    print("  3. 500")
    print("  4. custom")
    choice = ask_int_in_range("Choose", 1, 4, default=2)
    presets = {1: 100, 2: 250, 3: 500}
    if choice in presets:
        return presets[choice]
    return ask_int_in_range("Enter custom size", 1, 100000, default=250)


def next_output_dir(base):
    d = os.path.join(base, "wati_output")
    n = 1
    while os.path.exists(d):
        n += 1
        d = os.path.join(base, f"wati_output_{n}")
    os.makedirs(d)
    return d


def write_batches(good, size, out_dir):
    paths = []
    for i in range(0, len(good), size):
        chunk = good[i:i + size]
        path = os.path.join(out_dir, f"wati_batch_{i // size + 1}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=WATI_HEADER)
            w.writeheader()
            w.writerows(chunk)
        paths.append((path, len(chunk)))
    return paths


def write_skipped(skipped, out_dir):
    if not skipped:
        return None
    path = os.path.join(out_dir, "skipped.csv")
    fields = list(skipped[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(skipped)
    return path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    print("=" * 60)
    print(" Bigin  ->  WATI campaign CSV cleaner")
    print("=" * 60)

    # 1. input file
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = ask("\nPath to the Bigin export CSV")
    path = path.strip().strip('"').strip("'")
    if not path or not os.path.isfile(path):
        print(f"File not found: {path!r}")
        sys.exit(1)

    headers, rows = load_csv(path)
    total_in = len(rows)
    print(f"\nLoaded {total_in} contacts with {len(headers)} columns:")
    for i, h in enumerate(headers, 1):
        print(f"  {i:>2}. {h}")

    # 2. filter
    rows = filter_rows(headers, rows)
    after_filter = len(rows)
    if not rows:
        print("No contacts left after filtering. Nothing to write.")
        sys.exit(0)

    # 3. map + 4. clean/de-dupe
    name_cols, phone_col, country = choose_mapping(headers)
    good, skipped = build_wati_rows(rows, name_cols, phone_col, country)
    if not good:
        print("No valid phone numbers found. Nothing to write.")
        sys.exit(0)

    # 5. batch + write
    size = choose_batch_size()
    out_dir = next_output_dir(os.path.dirname(os.path.abspath(path)))
    batches = write_batches(good, size, out_dir)
    skipped_path = write_skipped(skipped, out_dir)

    # summary
    print("\n" + "=" * 60)
    print(" DONE")
    print("=" * 60)
    print(f"  Loaded from CSV      : {total_in}")
    print(f"  After filtering      : {after_filter}")
    print(f"  Skipped (bad/dupe)   : {len(skipped)}")
    print(f"  Valid WATI contacts  : {len(good)}")
    print(f"  Batch size           : {size}")
    print(f"  Batches written      : {len(batches)}")
    print(f"  Output folder        : {out_dir}")
    for p, n in batches:
        print(f"    - {os.path.basename(p)}  ({n})")
    if skipped_path:
        print(f"  Skipped rows saved to: {os.path.basename(skipped_path)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)

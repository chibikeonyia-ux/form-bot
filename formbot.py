#!/usr/bin/env python3
"""
formbot.py - fill your own (public, single-page) Google Form in bulk.

Setup:
    pip install requests google-genai     # google-genai only needed for `generate`

Commands:
    python formbot.py inspect  FORM_URL
        Show every question, its type, options, and entry ID.
        Also writes template.csv (headers = question labels) for you to fill.

    python formbot.py send     FORM_URL responses.csv [--dry-run] [--limit N] [--delay 1.5]
        Submit one response per CSV row. Use --dry-run first, then --limit 3.

    python formbot.py generate FORM_URL out.csv --count 90 --context "who the respondents are"
        Use Gemini to create `count` varied rows (needs GEMINI_API_KEY, free at
        https://aistudio.google.com/apikey).
        Review out.csv, then run `send`.

CSV rules:
    - Column headers must match the question labels exactly (inspect writes them for you).
    - Choice/dropdown cells: exact option text.
    - Checkbox cells: separate multiple options with ";"  e.g.  Red;Blue
"""
import argparse
import csv
import json
import re
import sys
import time

import requests

TYPE_NAMES = {0: "short text", 1: "paragraph", 2: "multiple choice", 3: "dropdown",
              4: "checkboxes", 5: "linear scale", 9: "date", 10: "time"}


# ---------- reading the form ----------

def normalize(url: str) -> str:
    """Accept viewform / formResponse / edit-less links and return the /viewform URL."""
    url = url.strip()
    if "forms.gle" in url:
        url = requests.get(url, timeout=15, allow_redirects=True).url
    return url.split("?")[0].replace("/formResponse", "/viewform")


SKIPPED = []  # questions the parser can't fill (grids, file uploads); filled by load_form


def _get(lst, i, default=None):
    """Safe lst[i] that never raises."""
    try:
        return lst[i]
    except (IndexError, TypeError, KeyError):
        return default


def load_form(view_url: str):
    html = requests.get(view_url, timeout=15).text
    m = re.search(r"FB_PUBLIC_LOAD_DATA_ = (.*?);</script>", html, re.S)
    if not m:
        sys.exit("Could not read the form. Use the fill-out link (ends in /viewform), "
                 "not the edit link, and make sure the form needs no sign-in.")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        sys.exit("Found the form data but could not decode it.")

    try:
        items = _get(_get(data, 1), 1) or []
        fields = []
        SKIPPED.clear()
        for item in items:
            label = _get(item, 1) or "(untitled)"
            qtype = _get(item, 3)
            questions = _get(item, 4)
            if qtype in (6, 8, 11, 12) or not questions:  # video, section, image, page break
                continue
            if qtype == 13:
                SKIPPED.append(f"{label} (file upload)")
                continue
            if qtype == 7 or len(questions) > 1:
                SKIPPED.append(f"{label} (grid / multi-part)")
                continue
            q = questions[0]
            raw_opts = _get(q, 1)
            options = [_get(o, 0) for o in raw_opts if _get(o, 0) is not None] if raw_opts else None
            fields.append({
                "label": label,
                "entry": f"entry.{_get(q, 0)}",
                "type": qtype,
                "options": options or None,
                "required": bool(_get(q, 2)),
            })
    except Exception as e:  # unexpected layout: save it so it can be inspected
        with open("form_debug.json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        sys.exit(f"Could not parse this form's layout ({e}). Saved form_debug.json - send it to me.")

    if not fields:
        with open("form_debug.json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        sys.exit("The form loaded but no fillable questions were found. Saved form_debug.json.")
    return fields


def to_payload(fields, row: dict) -> dict:
    payload = {}
    for f in fields:
        val = (row.get(f["label"]) or "").strip()
        if not val:
            continue
        if f["type"] == 4:
            payload[f["entry"]] = [v.strip() for v in val.split(";") if v.strip()]
        elif f["type"] == 9:  # date: YYYY-MM-DD -> three fields
            y, mo, d = val.split("-")
            payload[f["entry"] + "_year"] = y
            payload[f["entry"] + "_month"] = str(int(mo))
            payload[f["entry"] + "_day"] = str(int(d))
        elif f["type"] == 10:  # time: HH:MM
            h, mi = val.split(":")[:2]
            payload[f["entry"] + "_hour"] = str(int(h))
            payload[f["entry"] + "_minute"] = str(int(mi))
        else:
            payload[f["entry"]] = val
    return payload


def check_row(fields, row: dict, i: int):
    """Return a list of problems with this row (missing required, bad option text)."""
    problems = []
    for f in fields:
        val = (row.get(f["label"]) or "").strip()
        if f["required"] and not val:
            problems.append(f"row {i}: missing required '{f['label']}'")
        if val and f["options"] and f["type"] in (2, 3, 4):
            chosen = [v.strip() for v in val.split(";")] if f["type"] == 4 else [val]
            for c in chosen:
                if c not in f["options"]:
                    problems.append(f"row {i}: '{c}' is not an option for '{f['label']}'")
    return problems


# ---------- commands ----------

def cmd_inspect(args):
    url = normalize(args.form_url)
    fields = load_form(url)
    for n, f in enumerate(fields, 1):
        req = " *required*" if f["required"] else ""
        print(f"{n}. {f['label']}  [{TYPE_NAMES.get(f['type'], f['type'])}]{req}")
        print(f"   id: {f['entry']}")
        if f["options"]:
            print(f"   options: {f['options']}")
    for sk in SKIPPED:
        print(f"SKIPPED (not supported): {sk}")
    with open("template.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow([f["label"] for f in fields])
    print("\nWrote template.csv - fill it in, one row per response.")


def post_row(post_url: str, payload: dict):
    """POST one response. Raises on failure."""
    r = requests.post(post_url, data=payload, timeout=15)
    r.raise_for_status()
    ok = "response has been recorded" in r.text.lower() or r.url.endswith("formResponse")
    if not ok:
        raise RuntimeError("form did not confirm the response")


def cmd_send(args):
    url = normalize(args.form_url)
    fields = load_form(url)
    post_url = url.replace("/viewform", "/formResponse")

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if args.limit:
        rows = rows[: args.limit]

    problems = [p for i, r in enumerate(rows, 1) for p in check_row(fields, r, i)]
    if problems:
        print("Fix these before sending:")
        print("\n".join(problems[:30]))
        if len(problems) > 30:
            print(f"...and {len(problems) - 30} more")
        sys.exit(1)

    failed = []
    for i, row in enumerate(rows, 1):
        payload = to_payload(fields, row)
        if args.dry_run:
            print(f"{i}/{len(rows)} DRY RUN: {json.dumps(payload, ensure_ascii=False)}")
            continue
        try:
            post_row(post_url, payload)
            print(f"{i}/{len(rows)} ok")
        except Exception as e:
            print(f"{i}/{len(rows)} FAILED: {e}")
            failed.append(i)
        time.sleep(args.delay)

    if not args.dry_run:
        print(f"\nDone. {len(rows) - len(failed)} sent, {len(failed)} failed.")
        if failed:
            print(f"Failed rows: {failed}  (re-run with a CSV of just these rows)")


def generate_rows(fields, count, context, model, key, on_progress=print, on_batch=None):
    """Ask Gemini for `count` varied response rows. Returns a list of dicts keyed by label."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    spec = [{"label": f["label"], "type": TYPE_NAMES.get(f["type"]), "options": f["options"],
             "required": f["required"]} for f in fields]
    system = (
        "You generate realistic, varied form responses. Return ONLY a JSON array of objects. "
        "Each object maps every question label to an answer string. For choice and dropdown "
        "questions use the exact option text. For checkboxes join chosen options with ';'. "
        "Vary the answers across respondents; do not repeat the same values."
    )
    rows, batch = [], 20
    while len(rows) < count:
        n = min(batch, count - len(rows))
        prompt = (f"Respondent context: {context}\n\nForm questions:\n{json.dumps(spec)}\n\n"
                  f"Generate {n} responses. Already generated {len(rows)} - make these different.")
        for attempt in range(4):  # retry on free-tier rate limits
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        temperature=1.0,
                    ),
                )
                rows.extend(json.loads(resp.text))
                # Call on_batch callback after each successful batch
                if on_batch:
                    labels = [f["label"] for f in fields]
                    on_batch([{k: str(r.get(k, "")) for k in labels} for r in rows])
                break
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f"Gemini failed: {e}")
                on_progress(f"retrying after error: {e}")
                time.sleep(15 * (attempt + 1))
        on_progress(f"generated {len(rows)}/{count}")
        time.sleep(4)  # stay under free-tier requests-per-minute
    labels = [f["label"] for f in fields]
    return [{k: str(r.get(k, "")) for k in labels} for r in rows[:count]]


def cmd_generate(args):
    import os
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY first (free key: https://aistudio.google.com/apikey)")
    fields = load_form(normalize(args.form_url))
    try:
        rows = generate_rows(fields, args.count, args.context, args.model, key)
    except RuntimeError as e:
        sys.exit(str(e))
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[f["label"] for f in fields])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {args.out}. Review it, then run: python formbot.py send FORM_URL {args.out} --dry-run")


def main():
    p = argparse.ArgumentParser(description="Fill your own Google Form in bulk.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("inspect"); a.add_argument("form_url"); a.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("send")
    s.add_argument("form_url"); s.add_argument("csv")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--delay", type=float, default=1.5)
    s.set_defaults(fn=cmd_send)

    g = sub.add_parser("generate")
    g.add_argument("form_url"); g.add_argument("out")
    g.add_argument("--count", type=int, default=90)
    g.add_argument("--context", default="General respondents")
    g.add_argument("--model", default="gemini-3.8-flash", choices=["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])
    g.set_defaults(fn=cmd_generate)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

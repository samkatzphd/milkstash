#!/usr/bin/env python3
"""Milk Stash: a friendly tracker for a frozen breast milk supply.

Run it with no arguments for a simple menu:

    python3 milkstash.py

Needs only Python 3.8+. Your data stays on your computer, in a plain CSV file
that opens in any spreadsheet app.
"""

import argparse
import calendar
import csv
import html
import json
import math
import os
import random
import re
import sys
import tempfile
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

ML_PER_OZ = 29.5735
FIELDS = ["id", "pumped", "volume_ml", "status", "status_date", "notes"]
DEFAULTS = {
    "unit": "oz",            # "oz" or "ml"
    "daily_oz": 24.0,        # how much milk baby drinks per day
    "target_days": 90,       # how many days of milk to keep
    "freezer_months": 6,     # how long a bag is good for
    "warn_days": 30,         # warn when a bag is this close to its use-by date
    "birth_date": None,      # optional: charts can count days since delivery
}


# ---------------------------------------------------------------- storage

def home():
    return Path(os.environ.get("MILKSTASH_HOME", Path.home() / ".milkstash"))


def load_settings():
    path = home() / "settings.json"
    settings = dict(DEFAULTS)
    if path.exists():
        settings.update(json.loads(path.read_text(encoding="utf-8")))
    return settings


def atomic_write(path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            write(handle)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_settings(settings):
    for key, value in settings.items():
        if isinstance(value, (int, float)) and value <= 0:
            raise ValueError(f"{key.replace('_', ' ')} must be more than zero.")
    atomic_write(home() / "settings.json",
                 lambda f: json.dump(settings, f, indent=2))


def load_bags():
    path = home() / "stash.csv"
    if not path.exists():
        return []
    bags = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bags.append({
                "id": int(row["id"]),
                "pumped": date.fromisoformat(row["pumped"]),
                "volume_ml": float(row["volume_ml"]),
                "status": row.get("status") or "stored",
                "status_date": (date.fromisoformat(row["status_date"])
                                if row.get("status_date") else None),
                "notes": row.get("notes") or "",
            })
    return bags


def save_bags(bags):
    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for bag in sorted(bags, key=lambda b: b["id"]):
            writer.writerow({
                "id": bag["id"],
                "pumped": bag["pumped"].isoformat(),
                "volume_ml": f"{bag['volume_ml']:.1f}",
                "status": bag["status"],
                "status_date": (bag["status_date"].isoformat()
                                if bag["status_date"] else ""),
                "notes": bag["notes"],
            })
    atomic_write(home() / "stash.csv", write)


# ---------------------------------------------------------------- parsing

def parse_date(text):
    text = (text or "today").strip().lower()
    if text == "today":
        return date.today()
    if text == "yesterday":
        return date.today() - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"I couldn't read the date '{text}'. Try 3/14/2025.")


def parse_volume(text, unit):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ml|oz)?\s*", text.lower())
    if not match or float(match.group(1)) <= 0:
        raise ValueError(f"I couldn't read the amount '{text}'. Try 5 or 150ml.")
    amount = float(match.group(1))
    return amount * ML_PER_OZ if (match.group(2) or unit) == "oz" else amount


def parse_birth_date(text):
    """Turn a typed birthday into a stored date. Blank or 'none' clears it."""
    if not text or text.strip().lower() == "none":
        return None
    birth = parse_date(text)
    if birth > date.today():
        raise ValueError("The birthday can't be in the future.")
    return birth.isoformat()


def add_months(day, months):
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


# ---------------------------------------------------------------- the math

def use_by(bag, settings):
    return add_months(bag["pumped"], int(settings["freezer_months"]))


def fmt(ml, settings):
    if settings["unit"] == "ml":
        return f"{ml:,.0f} ml"
    return f"{ml / ML_PER_OZ:,.1f} oz"


def nice(day):
    return f"{calendar.month_abbr[day.month]} {day.day}, {day.year}"


def daily_ml(settings):
    return float(settings["daily_oz"]) * ML_PER_OZ


def target_ml(settings):
    return daily_ml(settings) * float(settings["target_days"])


def good_bags(bags, settings, today=None):
    """Bags in the freezer that are still in date, oldest first."""
    today = today or date.today()
    return sorted((b for b in bags if b["status"] == "stored"
                   and use_by(b, settings) >= today),
                  key=lambda b: (b["pumped"], b["id"]))


def expired_bags(bags, settings, today=None):
    today = today or date.today()
    return sorted((b for b in bags if b["status"] == "stored"
                   and use_by(b, settings) < today),
                  key=lambda b: (b["pumped"], b["id"]))


def donation_plan(bags, settings, today=None):
    """Oldest whole bags that can go while still keeping the target supply."""
    good = good_bags(bags, settings, today)
    spare = sum(b["volume_ml"] for b in good) - target_ml(settings)
    picks = []
    for bag in good:
        if bag["volume_ml"] <= spare:
            picks.append(bag)
            spare -= bag["volume_ml"]
    return picks


def at_risk(bags, settings, today=None):
    """Bags that would expire before baby reached them, drinking oldest first."""
    today = today or date.today()
    used_ml, risky = 0.0, []
    for bag in good_bags(bags, settings, today):
        reached = today + timedelta(days=int(used_ml / daily_ml(settings)))
        if reached > use_by(bag, settings):
            risky.append(bag)
        else:
            used_ml += bag["volume_ml"]
    return risky


# ---------------------------------------------------------------- commands

def show_bags(bags, settings):
    print(f"  {'Bag':>5}  {'Pumped':<13} {'Amount':>10}  {'Use by':<13} Notes")
    for bag in bags:
        print(f"  {bag['id']:>5}  {nice(bag['pumped']):<13} "
              f"{fmt(bag['volume_ml'], settings):>10}  "
              f"{nice(use_by(bag, settings)):<13} {bag['notes']}")


def cmd_status(_args=None):
    settings, bags = load_settings(), load_bags()
    today = date.today()
    good = good_bags(bags, settings, today)
    total = sum(b["volume_ml"] for b in good)
    target = target_ml(settings)
    days = total / daily_ml(settings)
    filled = min(20, int(20 * total / target)) if target else 20

    print("\nYour milk stash")
    print("-" * 44)
    print(f"  In the freezer  {fmt(total, settings)} in {len(good)} bags")
    print(f"  Lasts about     {days:.0f} days "
          f"({fmt(daily_ml(settings), settings)} a day)")
    print(f"  Your goal       {fmt(target, settings)} "
          f"({settings['target_days']} days)")
    print(f"  [{'#' * filled}{'.' * (20 - filled)}] "
          f"{100 * total / target:.0f}% of goal")

    soon = [b for b in good if (use_by(b, settings) - today).days
            <= int(settings["warn_days"])]
    expired = expired_bags(bags, settings, today)
    risky = at_risk(bags, settings, today)
    plan = donation_plan(bags, settings, today)

    print()
    if expired:
        print(f"  ! {len(expired)} bags are past their use-by date. "
              "See them with: list --expired")
    if soon:
        print(f"  ! {len(soon)} bags expire within {settings['warn_days']} "
              f"days, first on {nice(use_by(soon[0], settings))}.")
    if risky:
        print(f"  ! {len(risky)} bags may expire before baby can drink them.")
    if plan:
        spare = sum(b["volume_ml"] for b in plan)
        print(f"  You can donate {len(plan)} bags ({fmt(spare, settings)}) "
              "and still meet your goal. See: plan")
    elif total < target:
        print(f"  {fmt(target - total, settings)} to go until your goal.")
    else:
        print("  You're right at your goal.")
    print()


def cmd_add(args):
    settings, bags = load_settings(), load_bags()
    pumped = parse_date(args.date)
    if pumped > date.today():
        raise ValueError("That date is in the future.")
    volumes = [parse_volume(v, settings["unit"]) for v in args.amounts]
    next_id = max((b["id"] for b in bags), default=0) + 1
    for offset, volume in enumerate(volumes):
        bag = {"id": next_id + offset, "pumped": pumped, "volume_ml": volume,
               "status": "stored", "status_date": None,
               "notes": args.note or ""}
        bags.append(bag)
        print(f"Added bag {bag['id']}: {fmt(volume, settings)}, "
              f"use by {nice(use_by(bag, settings))}. Label it #{bag['id']}.")
    save_bags(bags)


def change_status(ids, status, when, verb):
    settings, bags = load_settings(), load_bags()
    by_id = {b["id"]: b for b in bags}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError(f"No bag numbered {missing[0]}.")
    taken = [i for i in ids if by_id[i]["status"] != "stored"]
    if taken:
        raise ValueError(f"Bag {taken[0]} is already marked "
                         f"{by_id[taken[0]]['status']}. Use 'restore' to undo.")
    for i in ids:
        by_id[i]["status"], by_id[i]["status_date"] = status, when
    save_bags(bags)
    total = sum(by_id[i]["volume_ml"] for i in ids)
    print(f"{verb} {len(ids)} bag(s), {fmt(total, settings)}.")


def cmd_use(args):
    ids = args.ids
    if not ids:
        settings = load_settings()
        good = good_bags(load_bags(), settings)
        if not good:
            raise ValueError("There are no in-date bags to use.")
        ids = [good[0]["id"]]
        print(f"Oldest bag is #{ids[0]}.")
    else:
        settings = load_settings()
        expired = {b["id"] for b in expired_bags(load_bags(), settings)}
        for bag_id in ids:
            if bag_id in expired:
                print(f"Heads up: bag {bag_id} is past its use-by date.")
    change_status(ids, "used", parse_date(args.date), "Used")


def cmd_donate(args):
    ids = args.ids
    if not ids:
        settings = load_settings()
        plan = donation_plan(load_bags(), settings)
        if not plan:
            print("Nothing extra to donate right now.")
            return
        show_bags(plan, settings)
        ids = [b["id"] for b in plan]
        if not args.yes:
            reply = input(f"Mark these {len(ids)} bags as donated? [y/N] ")
            if reply.strip().lower() not in ("y", "yes"):
                print("No changes made.")
                return
    change_status(ids, "donated", parse_date(args.date), "Donated")


def cmd_toss(args):
    change_status(args.ids, "discarded", parse_date(args.date), "Discarded")


def cmd_restore(args):
    bags = load_bags()
    by_id = {b["id"]: b for b in bags}
    for i in args.ids:
        if i not in by_id:
            raise ValueError(f"No bag numbered {i}.")
    for i in args.ids:
        by_id[i]["status"], by_id[i]["status_date"] = "stored", None
    save_bags(bags)
    print(f"Put {len(args.ids)} bag(s) back in the freezer.")


def cmd_list(args):
    settings, bags = load_settings(), load_bags()
    chosen = expired_bags(bags, settings) if args.expired \
        else good_bags(bags, settings)
    if not chosen:
        print("No bags to show.")
        return
    show_bags(chosen, settings)
    print(f"\n  {len(chosen)} bags, "
          f"{fmt(sum(b['volume_ml'] for b in chosen), settings)}")


def cmd_plan(_args=None):
    settings, bags = load_settings(), load_bags()
    plan = donation_plan(bags, settings)
    if not plan:
        print("Nothing extra to donate. Your stash is at or under your goal.")
        return
    print(f"These {len(plan)} bags are extra. They're the oldest, so your "
          "freshest milk stays home:\n")
    show_bags(plan, settings)
    print(f"\n  Total: {fmt(sum(b['volume_ml'] for b in plan), settings)}")
    print("  When they've gone, run: donate")


def cmd_settings(args):
    settings = load_settings()
    changed = False
    for key in DEFAULTS:
        value = getattr(args, key, None)
        if value is not None:
            settings[key] = (parse_birth_date(value) if key == "birth_date"
                             else value)
            changed = True
    if changed:
        save_settings(settings)
        print("Saved.")
    print(f"  Units           {settings['unit']}")
    print(f"  Baby drinks     {settings['daily_oz']:g} oz a day")
    print(f"  Keep on hand    {settings['target_days']} days "
          f"({fmt(target_ml(settings), settings)})")
    print(f"  Freezer life    {settings['freezer_months']} months")
    print(f"  Warn me         {settings['warn_days']} days before expiry")
    birth = settings.get("birth_date")
    print(f"  Baby's birthday {nice(date.fromisoformat(birth)) if birth else 'not set'}")


def cmd_import(args):
    bags = load_bags()
    if bags and not args.append:
        raise ValueError("You already have bags saved. To add this file to "
                         "them anyway, run import again with --append.")
    used_ids = {b["id"] for b in bags}
    added = skipped = 0
    with open(args.file, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {re.sub(r"[^a-z]", "", (c or "").lower()): c
                   for c in reader.fieldnames or []}
        date_col = columns.get("date") or columns.get("pumped")
        ml_col = columns.get("volumeml") or columns.get("ml")
        oz_col = columns.get("volumeoz") or columns.get("oz")
        if not date_col or not (ml_col or oz_col):
            raise ValueError("The file needs a Date column and a "
                             "'Volume (ml)' or 'Volume (oz)' column.")
        flag = lambda row, name: bool((row.get(columns.get(name, ""), "")
                                       or "").strip())
        for row in reader:
            try:
                pumped = parse_date(row[date_col])
                if ml_col and (row.get(ml_col) or "").strip():
                    volume = float(row[ml_col])
                else:
                    volume = float(row[oz_col]) * ML_PER_OZ
            except (ValueError, TypeError):
                skipped += 1
                continue
            status, when = "stored", None
            if flag(row, "donated"):
                status = "donated"
                raw = row.get(columns.get("donationdate", ""), "")
                when = parse_date(raw) if (raw or "").strip() else pumped
            elif flag(row, "used"):
                status, when = "used", pumped
            label = (row.get(columns.get("name", ""), "") or "").strip()
            bag_id = int(label) if label.isdigit() else 0
            if not bag_id or bag_id in used_ids:
                bag_id = max(used_ids, default=0) + 1
            used_ids.add(bag_id)
            notes = (row.get(columns.get("specialnotes", ""), "")
                     or row.get(columns.get("notes", ""), "") or "").strip()
            bags.append({"id": bag_id, "pumped": pumped, "volume_ml": volume,
                         "status": status, "status_date": when, "notes": notes})
            added += 1
    save_bags(bags)
    print(f"Imported {added} bags." +
          (f" Skipped {skipped} rows I couldn't read." if skipped else ""))


def history(bags, settings):
    changes = {}
    for bag in bags:
        changes[bag["pumped"]] = changes.get(bag["pumped"], 0) + bag["volume_ml"]
        leaves = bag["status_date"] if bag["status"] != "stored" else None
        if bag["status"] == "stored" and use_by(bag, settings) < date.today():
            leaves = use_by(bag, settings)
        if leaves:
            changes[leaves] = changes.get(leaves, 0) - bag["volume_ml"]
    level, points = 0.0, []
    for day in sorted(changes):
        level += changes[day]
        points.append((day, max(level, 0.0)))
    if points and points[-1][0] < date.today() and points[-1][1] > 0.01:
        points.append((date.today(), points[-1][1]))
    return points


CHART_W, CHART_H, CHART_L, CHART_R, CHART_T, CHART_B = 760, 300, 62, 28, 12, 50
AVERAGE_DAYS = 4


def nice_ticks(low, high, count=5):
    raw = max(high - low, 1) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return [i * step for i in range(math.floor(low / step),
                                    math.ceil(high / step) + 1)]


def time_ticks(start, end, settings):
    """Tick marks along the bottom: days since delivery, or calendar months."""
    birth = settings.get("birth_date")
    if birth:
        birth = date.fromisoformat(birth)
        first, last = (start - birth).days, (end - birth).days
        step = 30 * max(1, math.ceil((last - first) / 240))
        ticks = [(birth + timedelta(days=n * step), str(n * step))
                 for n in range(math.ceil(first / step), last // step + 1)]
        return ticks, "Days since delivery"
    months = (end.year - start.year) * 12 + end.month - start.month
    step = max(1, math.ceil(months / 6))
    ticks = []
    for n in range(1, months + 1, step):
        day = add_months(start.replace(day=1), n)
        ticks.append((day, f"{calendar.month_abbr[day.month]} {day.year}"))
    return ticks, "Date"


def scales(start, end, low, high):
    span = max((end - start).days, 1)
    x = lambda d: CHART_L + (CHART_W - CHART_L - CHART_R) * (d - start).days / span
    y = lambda v: CHART_T + (CHART_H - CHART_B - CHART_T) * (high - v) / (high - low)
    return x, y


def axes(x, y, value_ticks, start, end, settings):
    right = CHART_W - CHART_R
    parts = [
        f'<line x1="{CHART_L}" x2="{right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
        f'class="{"zero" if v == 0 else "grid"}"/>'
        f'<text x="{CHART_L - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:,.0f}</text>'
        for v in value_ticks]
    ticks, title = time_ticks(start, end, settings)
    baseline = CHART_H - CHART_B + 18
    parts += [f'<text x="{x(d):.1f}" y="{baseline}" text-anchor="middle">{label}</text>'
              for d, label in ticks]
    parts.append(f'<text x="{(CHART_L + right) / 2}" y="{CHART_H - 6}" '
                 f'text-anchor="middle">{title}</text>')
    return "".join(parts)


def unit_size(settings):
    return 1 if settings["unit"] == "ml" else ML_PER_OZ


def chart_svg(points, settings):
    divisor = unit_size(settings)
    goal = target_ml(settings) / divisor
    values = [(d, v / divisor) for d, v in points]
    ticks = nice_ticks(0, max([v for _, v in values] + [goal]) * 1.05)
    start, end = values[0][0], values[-1][0]
    x, y = scales(start, end, ticks[0], ticks[-1])
    path = []
    previous = None
    for day, value in values:
        if previous is not None:
            path.append(f"{x(day):.1f},{y(previous):.1f}")
        path.append(f"{x(day):.1f},{y(value):.1f}")
        previous = value
    return (
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
        f'aria-label="Stash size over time compared with your goal">'
        f'{axes(x, y, ticks, start, end, settings)}'
        f'<line x1="{CHART_L}" x2="{CHART_W - CHART_R}" y1="{y(goal):.1f}" '
        f'y2="{y(goal):.1f}" class="goal"/>'
        f'<text x="{CHART_L + 4}" y="{y(goal) - 6:.1f}" class="goaltext">Goal</text>'
        f'<polyline points="{" ".join(path)}" class="line"/></svg>')


def daily_totals(bags):
    totals = {}
    for bag in bags:
        day = totals.setdefault(bag["pumped"], {})
        day["pumped"] = day.get("pumped", 0) + bag["volume_ml"]
        if bag["status"] != "stored" and bag["status_date"]:
            day = totals.setdefault(bag["status_date"], {})
            day[bag["status"]] = day.get(bag["status"], 0) + bag["volume_ml"]
    return totals


def daily_svg(bags, settings):
    """Bars for milk in (up) and out (down), with a rolling average of pumping."""
    divisor = unit_size(settings)
    totals = daily_totals(bags)
    start, end = min(totals), max(max(totals), history(bags, settings)[-1][0])
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    outgoing = ("used", "donated", "discarded")
    high = max(t.get("pumped", 0) for t in totals.values()) / divisor
    low = -max(sum(t.get(k, 0) for k in outgoing) for t in totals.values()) / divisor
    ticks = nice_ticks(low, high)
    x, y = scales(start, end, ticks[0], ticks[-1])
    bar = min(24.0, max(1.0, (CHART_W - CHART_L - CHART_R) / len(days) * 0.85))
    rects = []
    for day, amounts in totals.items():
        left = x(day) - bar / 2
        pumped = amounts.get("pumped", 0) / divisor
        if pumped:
            rects.append(f'<rect x="{left:.1f}" y="{y(pumped):.1f}" width="{bar:.1f}" '
                         f'height="{y(0) - y(pumped):.1f}" class="pumped"/>')
        depth = 0.0
        for kind in outgoing:
            amount = amounts.get(kind, 0) / divisor
            if amount:
                rects.append(f'<rect x="{left:.1f}" y="{y(-depth):.1f}" width="{bar:.1f}" '
                             f'height="{y(-depth - amount) - y(-depth):.1f}" class="{kind}"/>')
                depth += amount
    pumped_by_day = [totals.get(d, {}).get("pumped", 0) / divisor for d in days]
    average = []
    for i in range(AVERAGE_DAYS - 1, len(days)):
        level = sum(pumped_by_day[i - AVERAGE_DAYS + 1:i + 1]) / AVERAGE_DAYS
        average.append(f"{x(days[i]):.1f},{y(level):.1f}")
    line = f'<polyline points="{" ".join(average)}" class="avg"/>' if average else ""
    return (
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="Milk pumped, '
        f'used, donated, and discarded each day, with a {AVERAGE_DAYS}-day average">'
        f'{axes(x, y, ticks, start, end, settings)}{"".join(rects)}{line}</svg>')


def daily_legend(bags):
    present = {k for t in daily_totals(bags).values() for k in t}
    names = [("pumped", "Pumped"), ("used", "Used"), ("donated", "Donated"),
             ("discarded", "Tossed")]
    items = [f'<span><i class="{k}"></i>{label}</span>'
             for k, label in names if k in present]
    items.append(f'<span><i class="avgkey"></i>{AVERAGE_DAYS}-day average pumped</span>')
    return f'<div class=legend>{"".join(items)}</div>'


def cmd_report(args):
    settings, bags = load_settings(), load_bags()
    if not bags:
        raise ValueError("Add a bag first, then I can draw your report.")
    good = good_bags(bags, settings)
    total = sum(b["volume_ml"] for b in good)
    donated = sum(b["volume_ml"] for b in bags if b["status"] == "donated")
    plan = donation_plan(bags, settings)
    rows = "".join(
        f"<tr><td>#{b['id']}</td><td>{nice(b['pumped'])}</td>"
        f"<td>{fmt(b['volume_ml'], settings)}</td>"
        f"<td>{nice(use_by(b, settings))}</td>"
        f"<td>{html.escape(b['notes'])}</td></tr>" for b in good[:10])
    table = ("<table><tr><th>Bag<th>Pumped<th>Amount<th>Use by<th>Notes</tr>"
             f"{rows}</table>" if rows else
             "<p>There are no in-date bags in the freezer right now.</p>")
    tiles = [("In the freezer", fmt(total, settings)),
             ("Days of milk", f"{total / daily_ml(settings):.0f}"),
             ("Ready to donate", fmt(sum(b["volume_ml"] for b in plan), settings)),
             ("Donated so far", fmt(donated, settings))]
    tile_html = "".join(f"<div class=tile><b>{v}</b><span>{k}</span></div>"
                        for k, v in tiles)
    page = f"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Milk Stash</title><style>
:root{{--bg:#fffaf5;--card:#fff;--ink:#3b3330;--soft:#7d716b;--line:#eadfd6;--main:#c8693f;--goal:#44707f;--pumped:#eeb896;--used:#d19a12;--donated:#7c58a6;--tossed:#8f8580}}
@media(prefers-color-scheme:dark){{:root{{--bg:#211d1b;--card:#2c2724;--ink:#f3ebe5;--soft:#b5a9a2;--line:#463e39;--main:#ee9b72;--goal:#8cc0d1;--pumped:#9a6245;--used:#e8c35a;--donated:#b795dc;--tossed:#a59b95}}}}
body{{margin:0 auto;max-width:800px;padding:24px 16px;background:var(--bg);color:var(--ink);font:16px system-ui,sans-serif}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.tile b{{display:block;font-size:22px}}.tile span{{color:var(--soft);font-size:14px}}
.card{{margin-top:14px;overflow-x:auto}}svg{{width:100%;height:auto}}svg text{{fill:var(--soft);font-size:12px}}
.grid{{stroke:var(--line)}}.goal{{stroke:var(--goal);stroke-width:2;stroke-dasharray:6 5}}svg .goaltext{{fill:var(--goal);font-weight:600}}
.line{{fill:none;stroke:var(--main);stroke-width:2.5}}.zero{{stroke:var(--soft)}}.avg{{fill:none;stroke:var(--ink);stroke-width:2}}
rect.pumped{{fill:var(--pumped)}}rect.used{{fill:var(--used)}}rect.donated{{fill:var(--donated)}}rect.discarded{{fill:var(--tossed)}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin-bottom:8px;color:var(--soft);font-size:14px}}.legend span{{display:inline-flex;align-items:center;gap:6px}}
.legend i{{width:12px;height:12px;border-radius:3px}}i.pumped{{background:var(--pumped)}}i.used{{background:var(--used)}}i.donated{{background:var(--donated)}}i.discarded{{background:var(--tossed)}}.legend i.avgkey{{height:2px;width:18px;border-radius:0;background:var(--ink)}}
table{{border-collapse:collapse;width:100%}}
td,th{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}h2{{font-size:17px;margin:0 0 10px}}
</style><h1>Milk Stash</h1><div class=tiles>{tile_html}</div>
<div class=card><h2>Stash over time ({settings['unit']})</h2>{chart_svg(history(bags, settings), settings)}</div>
<div class=card><h2>Daily milk ({settings['unit']})</h2>{daily_legend(bags)}{daily_svg(bags, settings)}</div>
<div class=card><h2>Use these first</h2>{table}</div>
</html>"""
    path = home() / "report.html"
    atomic_write(path, lambda f: f.write(page))
    print(f"Report saved to {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())


def cmd_demo(_args=None):
    """Fill a separate, made-up stash so people can look around safely."""
    random.seed(7)
    today = date.today()
    bags, next_id = [], 1
    for back in range(150, 0, -1):
        for _ in range(random.choice([3, 4, 4, 5])):
            bags.append({"id": next_id,
                         "pumped": today - timedelta(days=back),
                         "volume_ml": float(random.randrange(90, 190)),
                         "status": "stored", "status_date": None, "notes": ""})
            next_id += 1
    with tempfile.TemporaryDirectory(prefix="milkstash-demo-") as folder:
        os.environ["MILKSTASH_HOME"] = folder
        save_bags(bags)
        print("This is made-up data. Your real stash wasn't touched.")
        cmd_status()
        cmd_plan()


# ---------------------------------------------------------------- menu

def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def ask_ids(prompt):
    return [int(p) for p in re.split(r"[,\s]+", ask(prompt)) if p]


def first_run():
    print("\nWelcome to Milk Stash! A few quick questions. Press Enter to "
          "accept the suggestion in [brackets].\n")
    settings = dict(DEFAULTS)
    unit = ask("Do you measure in oz or ml?", "oz").lower()
    settings["unit"] = "ml" if unit.startswith("m") else "oz"
    settings["daily_oz"] = float(ask("How many oz does baby drink a day?", "24"))
    settings["target_days"] = int(ask("How many days of milk do you want to keep?", "90"))
    settings["birth_date"] = parse_birth_date(
        ask("Baby's birthday, for charts (optional, Enter to skip)"))
    save_settings(settings)
    print("\nAll set. You can change these any time.\n")


def menu():
    if not (home() / "settings.json").exists():
        first_run()
    ns = argparse.Namespace
    while True:
        cmd_status()
        print("  1 Add milk      2 Use a bag     3 Donate extra")
        print("  4 See bags      5 Open report   6 Settings    q Quit")
        choice = input("\nWhat would you like to do? ").strip().lower()
        try:
            if choice == "1":
                amounts = ask("Amount of each bag, separated by spaces").split()
                cmd_add(ns(amounts=amounts, date=ask("Date pumped", "today"),
                           note=ask("Note (optional)")))
            elif choice == "2":
                raw = ask("Bag number (Enter for the oldest)")
                cmd_use(ns(ids=[int(raw)] if raw else [], date="today"))
            elif choice == "3":
                cmd_donate(ns(ids=[], date="today", yes=False))
            elif choice == "4":
                cmd_list(ns(expired=False))
            elif choice == "5":
                cmd_report(ns(no_open=False))
            elif choice == "6":
                current = load_settings()
                current["daily_oz"] = float(ask("Oz per day", f"{current['daily_oz']:g}"))
                current["target_days"] = int(ask("Days to keep", str(current["target_days"])))
                current["freezer_months"] = int(ask("Freezer months", str(current["freezer_months"])))
                current["birth_date"] = parse_birth_date(
                    ask("Baby's birthday (or none)", current.get("birth_date") or "none"))
                save_settings(current)
            elif choice in ("q", "quit", "exit"):
                return
        except ValueError as error:
            print(f"\nHmm: {error}")


# ---------------------------------------------------------------- command line

def build_parser():
    parser = argparse.ArgumentParser(
        prog="milkstash",
        description="Track your frozen milk: what you have, what's expiring, "
                    "and what you can donate. Run with no command for a menu.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="see your stash at a glance").set_defaults(func=cmd_status)

    p = sub.add_parser("add", help="add bags, e.g. add 5 4.5 150ml")
    p.add_argument("amounts", nargs="+")
    p.add_argument("--date", default="today")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("use", help="mark bags used (oldest if none given)")
    p.add_argument("ids", nargs="*", type=int)
    p.add_argument("--date", default="today")
    p.set_defaults(func=cmd_use)

    sub.add_parser("plan", help="see which bags are extra").set_defaults(func=cmd_plan)

    p = sub.add_parser("donate", help="mark bags donated (the plan if none given)")
    p.add_argument("ids", nargs="*", type=int)
    p.add_argument("--date", default="today")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_donate)

    p = sub.add_parser("toss", help="mark bags thrown away")
    p.add_argument("ids", nargs="+", type=int)
    p.add_argument("--date", default="today")
    p.set_defaults(func=cmd_toss)

    p = sub.add_parser("restore", help="undo: put bags back in the freezer")
    p.add_argument("ids", nargs="+", type=int)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("list", help="list bags in the freezer, oldest first")
    p.add_argument("--expired", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("report", help="make a chart page")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("settings", help="view or change your settings")
    p.add_argument("--unit", choices=["oz", "ml"])
    p.add_argument("--daily-oz", dest="daily_oz", type=float)
    p.add_argument("--target-days", dest="target_days", type=int)
    p.add_argument("--freezer-months", dest="freezer_months", type=int)
    p.add_argument("--warn-days", dest="warn_days", type=int)
    p.add_argument("--birth-date", dest="birth_date",
                   help="baby's birthday, or 'none' to clear it")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("import", help="bring in bags from a CSV file")
    p.add_argument("file")
    p.add_argument("--append", action="store_true",
                   help="add to the bags you already have")
    p.set_defaults(func=cmd_import)

    sub.add_parser("demo", help="try it with made-up data").set_defaults(func=cmd_demo)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command:
            args.func(args)
        else:
            menu()
    except (ValueError, OSError) as error:
        print(f"Hmm: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nBye! Your stash is saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Krillion analytics for an iMessage group chat.

Usage:
    python3 krillion.py --list-chats       # chats that contain Krillion results
    python3 krillion.py --chat "My Group"  # today, by chat display name or chat_identifier
    python3 krillion.py 2026-10-01         # specific day (needs KRILLION_CHAT or --chat)

Config (env vars):
    KRILLION_CHAT   default chat (chat_identifier or display name from --list-chats)
    KRILLION_NAME   your name, shown for your own results (default "Me")

Share format:
    Krillion #82 🦐
    305

    🐟🦑🫧🐟🦑🐟🏮

Emoji tiers (krillion.wiki):
    🫧 Plankton +10 | 🤡 Too Clever 0 | 🐟 Schooler +30 | 🦑 Rare +60
    🏮 Deep Cut +85 | 🌟 One in a Krillion +100 | ⬛ Missed 0
"""

import argparse
import glob
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import date, timedelta

# Default chat: chat_identifier (or display name) from `--list-chats`.
DEFAULT_CHAT = os.environ.get("KRILLION_CHAT")
MY_NAME = os.environ.get("KRILLION_NAME", "Me")
# Optional manual overrides: handle (phone/email) -> name. Contacts are used otherwise.
NAME_OVERRIDES = {}

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)
# Krillion #N is played on PUZZLE_ZERO + N days (#82 = 2026-10-05).
PUZZLE_ZERO = date(2026, 7, 15)

TIERS = {
    "🫧": ("Plankton", 10),
    "🤡": ("Too Clever", 0),
    "🐟": ("Schooler", 30),
    "🦑": ("Rare", 60),
    "🏮": ("Deep Cut", 85),
    "🌟": ("One in a Krillion", 100),
    "⬛": ("Missed", 0),
}

RESULT_RE = re.compile(
    r"Krillion\s*#(\d+)[^\n]*\n\s*(\d+)\s*\n\s*([" + "".join(TIERS) + r"️\s]+)"
)


def normalize_phone(s):
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) >= 10 else (s or "").lower()


def load_contacts():
    names = {}
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER FROM ZABCDPHONENUMBER p "
                "JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER"
            ).fetchall()
            rows += con.execute(
                "SELECT r.ZFIRSTNAME, r.ZLASTNAME, e.ZADDRESS FROM ZABCDEMAILADDRESS e "
                "JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for first, last, handle in rows:
            name = " ".join(x for x in (first, last) if x)
            if name and handle:
                names.setdefault(normalize_phone(handle), name)
    return names


def decode_attributed_body(blob):
    """Pull plain text out of an NSAttributedString typedstream blob."""
    if not blob:
        return None
    try:
        body = blob.split(b"NSString", 1)[1][5:]
        if body[0] == 0x81:
            length = int.from_bytes(body[1:3], "little")
            body = body[3 : 3 + length]
        else:
            length = body[0]
            body = body[1 : 1 + length]
        return body.decode("utf-8", errors="ignore")
    except (IndexError, ValueError):
        return None


def find_chat(con, chat):
    row = con.execute(
        "SELECT ROWID, chat_identifier, display_name FROM chat "
        "WHERE chat_identifier = ? OR display_name = ? COLLATE NOCASE",
        (chat, chat),
    ).fetchone()
    if not row:
        raise SystemExit(f"Chat not found: {chat!r}. Try --list-chats.")
    return row


def list_chats(con):
    rows = con.execute(
        "SELECT c.chat_identifier, c.display_name, COUNT(*) n FROM chat c "
        "JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID "
        "JOIN message m ON m.ROWID = cmj.message_id "
        "WHERE m.text LIKE '%Krillion #%' OR CAST(m.attributedBody AS TEXT) LIKE '%Krillion #%' "
        "GROUP BY c.ROWID ORDER BY n DESC"
    ).fetchall()
    for ident, name, n in rows:
        members = [
            h for (h,) in con.execute(
                "SELECT h.id FROM chat c JOIN chat_handle_join chj ON chj.chat_id = c.ROWID "
                "JOIN handle h ON h.ROWID = chj.handle_id WHERE c.chat_identifier = ?",
                (ident,),
            )
        ]
        print(f"{n:4d} results  {ident}  {name or '(no name)'}  [{', '.join(members)}]")


def puzzle_day(puzzle):
    return PUZZLE_ZERO + timedelta(days=puzzle)


def load_results(con, contacts):
    """Results from every chat, one per player per puzzle. Each has a set of chats it was posted in."""
    rows = con.execute(
        "SELECT cmj.chat_id, m.is_from_me, h.id, m.text, m.attributedBody FROM chat_message_join cmj "
        "JOIN message m ON m.ROWID = cmj.message_id "
        "LEFT JOIN handle h ON h.ROWID = m.handle_id "
        "WHERE m.associated_message_type = 0 "  # skip tapbacks
        "AND (m.text LIKE '%Krillion%' OR m.text IS NULL) "
        "ORDER BY m.date"
    ).fetchall()

    seen = {}
    results = []
    for chat_id, is_from_me, handle, text, body in rows:
        text = text or decode_attributed_body(body)
        if not text or "Krillion" not in text:
            continue
        m = RESULT_RE.search(text)
        if not m:
            continue
        if is_from_me:
            player = MY_NAME
        else:
            player = NAME_OVERRIDES.get(handle) or contacts.get(normalize_phone(handle)) or handle
        puzzle = int(m.group(1))
        if (player, puzzle) in seen:  # first post wins; remember other chats it was shared to
            seen[(player, puzzle)]["chats"].add(chat_id)
            continue
        emojis = [c for c in m.group(3) if c in TIERS]
        emoji_score = sum(TIERS[e][1] for e in emojis)
        posted = int(m.group(2))
        seen[(player, puzzle)] = r = {
            "player": player,
            "puzzle": puzzle,
            "score": posted,
            "emoji_score": emoji_score,
            "emojis": "".join(emojis),
            "day": puzzle_day(puzzle),
            "chats": {chat_id},
        }
        results.append(r)
    return results


def best_combined(day_results):
    """Sum over questions of the best tile anyone got on that question."""
    n = max(len(r["emojis"]) for r in day_results)
    tiles = []
    for i in range(n):
        tiles.append(max(
            (r["emojis"][i] for r in day_results if i < len(r["emojis"])),
            key=lambda e: TIERS[e][1],
        ))
    return sum(TIERS[e][1] for e in tiles), "".join(tiles)


def avg(xs):
    return sum(xs) / len(xs) if xs else 0


def print_table(rows, headers):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    print("  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  " + "  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


def report(results, day):
    today = [r for r in results if r["day"] == day]
    week_start = day - timedelta(days=day.weekday())  # Monday
    week = [r for r in results if week_start <= r["day"] <= day]
    history = [r for r in results if r["day"] <= day]

    print(f"\n🦐 Krillion report for {day:%a %Y-%m-%d}")
    puzzles = sorted({r["puzzle"] for r in today})
    if puzzles:
        print(f"   Puzzle #{', #'.join(map(str, puzzles))}")

    # 1. Highest for the day
    print("\n1. Today's scores")
    if today:
        ranked = sorted(today, key=lambda r: -r["score"])
        rows = []
        for i, r in enumerate(ranked, 1):
            flag = "" if r["score"] == r["emoji_score"] else f" (emoji sum {r['emoji_score']})"
            rows.append([i, r["player"], f"{r['score']}{flag}", r["emojis"]])
        print_table(rows, ["#", "Player", "Score", "Tiles"])
        top = ranked[0]["score"]
        winners = [r["player"] for r in ranked if r["score"] == top]
        print(f"   🏆 Top: {' & '.join(winners)} with {top}")
    else:
        print("   No results yet.")

    # 2. Running (all-time) average through this day
    print("\n2. Running average (all-time through today)")
    by_player = defaultdict(list)
    for r in history:
        by_player[r["player"]].append(r["score"])
    if by_player:
        rows = sorted(
            ([p, f"{avg(s):.1f}", len(s), max(s)] for p, s in by_player.items()),
            key=lambda x: -float(x[1]),
        )
        print_table([[i, *r] for i, r in enumerate(rows, 1)], ["#", "Player", "Avg", "Games", "Best"])
        print(f"   🏆 Best average: {rows[0][0]} ({rows[0][1]})")

    # 3. Weekly leaderboard (Mon -> day)
    print(f"\n3. Week leaderboard ({week_start:%b %d} – {day:%b %d})")
    wk = defaultdict(list)
    for r in week:
        wk[r["player"]].append(r["score"])
    if wk:
        rows = sorted(
            ([p, sum(s), len(s), f"{avg(s):.1f}"] for p, s in wk.items()),
            key=lambda x: -x[1],
        )
        print_table([[i, *r] for i, r in enumerate(rows, 1)], ["#", "Player", "Total", "Games", "Avg"])
    else:
        print("   No results this week.")

    # 4. Combined group score for the day, vs. history
    print("\n4. Combined group score")
    daily = defaultdict(int)
    daily_n = defaultdict(int)
    for r in history:
        daily[r["day"]] += r["score"]
        daily_n[r["day"]] += 1
    if today:
        total = daily[day]
        rank = 1 + sum(1 for t in daily.values() if t > total)
        best_day = max(daily, key=daily.get)
        print(f"   Today: {total} from {daily_n[day]} players (avg {total / daily_n[day]:.1f})")
        print(f"   Rank: #{rank} of {len(daily)} days")
        print(f"   🏆 Record: {daily[best_day]} on {best_day:%a %Y-%m-%d} ({daily_n[best_day]} players)")
    else:
        print("   No results yet.")

    if today:
        best, tiles = best_combined(today)
        print(f"\n5. Best combined score (best answer per question)\n   {best} {tiles}")

    # Bonus: tier counts for the day
    if today:
        counts = defaultdict(int)
        for r in today:
            for e in r["emojis"]:
                counts[e] += 1
        print("\n   Tiers today: " + "  ".join(
            f"{e} {TIERS[e][0]} x{counts[e]}" for e in TIERS if counts[e]
        ))
    print()


def krill(name, prefix="Krill"):
    """Swap a player's first name for prefix: 'Krill' for the leader, 'Shrimp' for last place."""
    parts = name.split(" ", 1)
    return f"{prefix} {parts[1]}" if len(parts) > 1 else prefix


def nickname(name, i, n):
    """Krill for 1st, Shrimp for last (when more than one player)."""
    if i == 1:
        return krill(name)
    if i == n:
        return krill(name, "Shrimp")
    return name


def recap(results, day, chat_name=None):
    """Short chat-friendly recap."""
    medals = ["🥇", "🥈", "🥉"]
    today = sorted((r for r in results if r["day"] == day), key=lambda r: -r["score"])
    if not today:
        return None
    week_start = day - timedelta(days=day.weekday())
    history = [r for r in results if r["day"] <= day]

    puzzles = sorted({r["puzzle"] for r in today})
    title = f"recap for {chat_name}" if chat_name else "recap"
    lines = [f"🦐 Krillion #{', #'.join(map(str, puzzles))} {title} ({day:%a %b %d})", ""]
    for i, r in enumerate(today):
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {nickname(r['player'], i + 1, len(today))} {r['score']} {r['emojis']}")

    by_player = defaultdict(list)
    for r in history:
        by_player[r["player"]].append(r["score"])
    avgs = sorted(((avg(s), p, len(s)) for p, s in by_player.items()), reverse=True)
    best, tiles = best_combined(today)
    lines += ["", f"🧩 Best combined {best} {tiles}"]

    lines += ["", "📈 All-time avg"]
    lines += [f"{i}. {nickname(p, i, len(avgs))} {a:.1f} ({n} game{'s' if n != 1 else ''})" for i, (a, p, n) in enumerate(avgs, 1)]

    wk = defaultdict(int)
    for r in history:
        if r["day"] >= week_start:
            wk[r["player"]] += r["score"]
    lines += ["", f"📅 Week ({week_start:%b %d}–)"]
    lines += [f"{i}. {nickname(p, i, len(wk))} {t}" for i, (t, p) in enumerate(sorted(((t, p) for p, t in wk.items()), reverse=True), 1)]

    daily = defaultdict(int)
    for r in history:
        daily[r["day"]] += r["score"]
    total = daily[day]
    rank = 1 + sum(1 for t in daily.values() if t > total)
    record = " — NEW RECORD 🔥" if rank == 1 else f" (record {max(daily.values())})"
    lines += ["", f"🌊 Group total {total}, #{rank} of {len(daily)} days{record}"]
    return "\n".join(lines)


def copy_to_clipboard(text):
    subprocess.run(["pbcopy"], input=text.encode(), check=True)


def send_imessage(guid, text):
    script = (
        "on run argv\n"
        '  tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)\n'
        "end run"
    )
    # Newer macOS stores guids as "any;+;..." but AppleScript may want "iMessage;+;..."
    candidates = [guid]
    if guid.startswith("any;"):
        candidates.append("iMessage;" + guid.split(";", 1)[1])
    for g in candidates:
        res = subprocess.run(["osascript", "-e", script, text, g], capture_output=True, text=True)
        if res.returncode == 0:
            return
    raise SystemExit(f"Send failed: {res.stderr.strip()}")


def main():
    ap = argparse.ArgumentParser(description="Krillion group chat analytics")
    ap.add_argument("day", nargs="?", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--chat", default=DEFAULT_CHAT, help="chat_identifier or display name (default: $KRILLION_CHAT)")
    ap.add_argument("--list-chats", action="store_true", help="list chats with Krillion results")
    ap.add_argument("--recap", action="store_true", help="print short chat-friendly recap only")
    ap.add_argument("--copy", action="store_true", help="copy recap to clipboard")
    ap.add_argument("--send", action="store_true", help="send recap to the chat via iMessage")
    ap.add_argument("--chat-only", action="store_true",
                    help="only count results posted in this chat (default: all chats, for this chat's players)")
    ap.add_argument("-y", "--yes", action="store_true", help="send without confirmation")
    args = ap.parse_args()

    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error as e:
        raise SystemExit(f"Can't read {CHAT_DB} ({e}). Grant Full Disk Access to your terminal.")

    if args.list_chats:
        list_chats(con)
        return

    if not args.chat:
        raise SystemExit("No chat given. Pass --chat or set KRILLION_CHAT (see --list-chats).")
    day = date.fromisoformat(args.day) if args.day else date.today()
    chat_rowid, ident, name = find_chat(con, args.chat)
    results = load_results(con, load_contacts())
    if args.chat_only:
        results = [r for r in results if chat_rowid in r["chats"]]
    else:
        roster = {r["player"] for r in results if chat_rowid in r["chats"]}
        results = [r for r in results if r["player"] in roster]
    if not (args.recap or args.copy or args.send):
        print(f"Chat: {name or ident}  ({len(results)} results total)")
        report(results, day)
        return

    text = recap(results, day, name)
    if not text:
        raise SystemExit(f"No Krillion results on {day}.")
    print(text)
    if args.copy:
        copy_to_clipboard(text)
        print("\n📋 Copied to clipboard.")
    if args.send:
        guid = con.execute("SELECT guid FROM chat WHERE ROWID = ?", (chat_rowid,)).fetchone()[0]
        if not args.yes and input(f"\nSend to {name or ident}? [y/N] ").strip().lower() != "y":
            print("Not sent.")
            return
        send_imessage(guid, text)
        print("✅ Sent.")


if __name__ == "__main__":
    main()

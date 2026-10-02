"""Grounding enforcement. The model's reply is checked in code before it is sent.

Rule: every price in the reply must be a rent the tools returned in this
session (or a number the user typed, such as their budget). A unit with no
rent on file must never have a number next to it, and must never be left out.
"""
import re

# $1,850  $1850  $1,850.00  1,850 dollars  1850/mo  1850 per month
_PRICE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
    r"|\b(\d{1,3}(?:,\d{3})+|\d{3,5})\s*(?:dollars\b|usd\b|/\s?mo\b|/\s?month\b|per month\b|a month\b)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"\bID[\s:#]*(\d+)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")


def amounts(text):
    out = []
    for m in _PRICE_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        out.append(int(raw.replace(",", "")))
    return out


def numbers_in(text):
    """Every number the user typed, so "under $2,000" lets the reply say $2,000."""
    return {int(n.replace(",", "")) for n in _NUM_RE.findall(text or "")}


def check(reply, unit_rents, user_numbers):
    """Return a list of problems. Empty list means the reply is grounded.

    unit_rents: {unit_id: rent or None} for every unit seen in tool results.
    user_numbers: numbers the user has typed in this session.
    """
    problems = []
    known_rents = {r for r in unit_rents.values() if r is not None}

    for a in amounts(reply):
        if a not in known_rents and a not in user_numbers:
            problems.append(f"${a:,} does not match any rent returned by the database.")

    # Line-level attribution: a line about one unit may only state that unit's rent.
    for line in reply.splitlines():
        ids = {int(i) for i in _ID_RE.findall(line) if int(i) in unit_rents}
        all_amounts = amounts(line)
        if len(ids) != 1 or not all_amounts:
            continue
        uid = ids.pop()
        rent = unit_rents[uid]
        if rent is None:
            # Even the user's own number may not be presented as this unit's price.
            if "not on file" not in line.lower():
                problems.append(f"Unit ID {uid} has no rent on file but the reply gives it a price.")
        elif any(a != rent and a not in user_numbers for a in all_amounts):
            problems.append(f"Unit ID {uid} rent is ${rent:,} but the reply states a different amount.")
    return problems


def mentioned_ids(reply):
    return {int(i) for i in _ID_RE.findall(reply or "")}


def missing_unpriced(reply, unpriced_units):
    """Units with no rent that the tools returned this turn but the reply never mentions."""
    mentioned = {int(i) for i in _ID_RE.findall(reply or "")}
    return [u for u in unpriced_units if u["unit_id"] not in mentioned]


def unpriced_footer(units):
    lines = ["", "Also matching, but with no price on file (I'll check and confirm):"]
    for u in units:
        lines.append(f"- Unit {u['unit_number']} (ID {u['unit_id']}), {u['building_name']}, "
                     f"{u['address']}: {u['beds']} bed / {u['baths']:g} bath, price not on file")
    return "\n".join(lines)


def fallback_reply(units):
    """Deterministic reply built only from tool data, used if the model keeps getting prices wrong."""
    if not units:
        return ("I want to make sure I only quote prices from our records, and I couldn't verify "
                "that just now. Could you ask again, or tell me which unit you're interested in?")
    lines = ["Here is what our records show:"]
    for u in units:
        lines.append(f"- Unit {u['unit_number']} (ID {u['unit_id']}), {u['building_name']}, "
                     f"{u['address']}: {u['beds']} bed / {u['baths']:g} bath, {u['price_display']}")
    return "\n".join(lines)

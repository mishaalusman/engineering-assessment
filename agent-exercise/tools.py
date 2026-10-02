"""The three tools the agent can call. All business rules live here, in code.

Every tool returns a plain dict that is JSON-serialisable, so it can be sent
back to Gemini as a function response and written to the log unchanged.
"""
import os
import sqlite3
from datetime import datetime, time

DB_PATH = os.environ.get(
    "LISTINGS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "listings.db"),
)

TOUR_OPEN = time(9, 0)
TOUR_CLOSE = time(18, 0)
NO_PRICE = "Price not on file (I'll check)"
MAX_PRICED_RESULTS = 25


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def price_display(rent):
    return f"${rent:,}/month" if rent is not None else NO_PRICE


def _unit(row, include_status=False):
    d = {
        "unit_id": row["id"],
        "unit_number": row["unit_number"],
        "building_name": row["building_name"],
        "address": f'{row["address"]}, {row["city"]}, {row["state"]} {row["zip"]}',
        "beds": row["beds"],
        "baths": row["baths"],
        "rent": row["rent"],                 # None means not on file
        "price_display": price_display(row["rent"]),
        "available_from": row["available_from"],
    }
    if include_status:
        d["is_active"] = bool(row["is_active"])
        d["status"] = "active" if row["is_active"] else "inactive: not available for tours"
    return d


_UNIT_SQL = """
SELECT u.id, u.unit_number, u.beds, u.baths, u.rent, u.available_from, u.is_active,
       b.name AS building_name, b.address, b.city, b.state, b.zip
FROM units u JOIN buildings b ON b.id = u.building_id
"""


def _as_int(value, name):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}")


# --------------------------------------------------------------------------
def search_units(city=None, max_rent=None, min_beds=None):
    """Active units matching the filters.

    Units whose rent is NULL are never dropped by the max_rent filter, because
    we cannot know they are over budget. They are returned in their own list
    so the agent has to present them as "price not on file".
    """
    try:
        max_rent = _as_int(max_rent, "max_rent")
        min_beds = _as_int(min_beds, "min_beds")
    except ValueError as e:
        return {"error": str(e)}

    where, params = ["u.is_active = 1"], []
    if city:
        where.append("LOWER(b.city) = LOWER(?)")
        params.append(str(city).strip())
    if min_beds is not None:
        where.append("u.beds >= ?")
        params.append(min_beds)
    if max_rent is not None:
        where.append("(u.rent <= ? OR u.rent IS NULL)")
        params.append(max_rent)

    sql = _UNIT_SQL + " WHERE " + " AND ".join(where) + " ORDER BY u.rent IS NULL, u.rent, u.id"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    priced = [_unit(r) for r in rows if r["rent"] is not None]
    unpriced = [_unit(r) for r in rows if r["rent"] is None]
    result = {
        "filters": {"city": city, "max_rent": max_rent, "min_beds": min_beds},
        "units_with_price": priced[:MAX_PRICED_RESULTS],
        "units_price_not_on_file": unpriced,
        "total_matches": len(rows),
        "instructions": ("Quote prices only from price_display. Every unit in "
                         "units_price_not_on_file must be mentioned as 'price not on file, I'll check'."),
    }
    if len(priced) > MAX_PRICED_RESULTS:
        result["note"] = f"{len(priced) - MAX_PRICED_RESULTS} more priced units not shown; ask the user to narrow the search."
    return result


def get_unit_details(unit_id):
    try:
        unit_id = _as_int(unit_id, "unit_id")
    except ValueError as e:
        return {"error": str(e)}
    with _conn() as conn:
        row = conn.execute(_UNIT_SQL + " WHERE u.id = ?", (unit_id,)).fetchone()
    if row is None:
        return {"error": f"No unit with ID {unit_id}."}
    return {"unit": _unit(row, include_status=True)}


def request_tour(unit_id, tour_time, client_name, *, now=None):
    """Books a tour. Refuses, whatever anyone says, when the unit does not exist,
    is inactive, or the time is outside 09:00-18:00. There is deliberately no
    parameter that can override these checks.

    tour_time is ISO 8601 local time, e.g. 2026-10-05T14:30. A timezone suffix,
    if present, is ignored and the wall-clock time is used.
    """
    now = now or datetime.now()

    def refuse(reason):
        return {"booked": False, "refused": True, "reason": reason}

    try:
        unit_id = _as_int(unit_id, "unit_id")
    except ValueError as e:
        return refuse(str(e))
    if unit_id is None:
        return refuse("A unit ID is required.")

    name = (client_name or "").strip()
    if not name:
        return refuse("The client's name is required to book a tour.")
    if len(name) > 100:
        return refuse("Client name is too long.")

    try:
        when = datetime.fromisoformat(str(tour_time).strip().replace("Z", ""))
    except ValueError:
        return refuse(f"Could not read the tour time {tour_time!r}. Use a format like 2026-10-05T14:30.")
    when = when.replace(tzinfo=None, second=0, microsecond=0)

    if not (TOUR_OPEN <= when.time() <= TOUR_CLOSE):
        return refuse(f"Tours can only be booked between 09:00 and 18:00. "
                      f"{when.strftime('%H:%M')} is outside those hours.")
    if when <= now:
        return refuse("That time is in the past.")

    with _conn() as conn:
        unit = conn.execute("SELECT id, is_active FROM units WHERE id = ?", (unit_id,)).fetchone()
        if unit is None:
            return refuse(f"No unit with ID {unit_id} exists.")
        if not unit["is_active"]:
            return refuse(f"Unit {unit_id} is inactive and cannot be toured.")
        cur = conn.execute(
            "INSERT INTO tours (unit_id, tour_at, client_name, created_at) VALUES (?, ?, ?, ?)",
            (unit_id, when.isoformat(timespec="minutes"), name, now.isoformat(timespec="seconds")),
        )
        tour_id = cur.lastrowid
    return {"booked": True, "tour_id": tour_id, "unit_id": unit_id,
            "tour_at": when.isoformat(timespec="minutes"), "client_name": name}


# --------------------------------------------------------------------------
REGISTRY = {
    "search_units": (search_units, {"city", "max_rent", "min_beds"}),
    "get_unit_details": (get_unit_details, {"unit_id"}),
    "request_tour": (request_tour, {"unit_id", "tour_time", "client_name"}),
}


def dispatch(name, args):
    """Run a tool by name. Unknown arguments are dropped and reported, never passed on."""
    if name not in REGISTRY:
        return {"error": f"Unknown tool {name}."}, []
    fn, allowed = REGISTRY[name]
    args = dict(args or {})
    ignored = sorted(set(args) - allowed)
    clean = {k: v for k, v in args.items() if k in allowed}
    try:
        return fn(**clean), ignored
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}, ignored

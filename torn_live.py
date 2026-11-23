"""
torn_live.py - keeps Chopper's Torn knowledge current from the official API.

Pulls game-reference selections from Torn API v2 (/v2/torn/...) using a key
(a "Public Only" key is enough) and renders each one into note lines in the
same "- Subject: body [source: ...]" format as the hand-written .md notes, so
retrieval treats them identically.

The Torn *wiki* is deliberately NOT fetched: it sits behind an anti-bot
challenge, so wiki-derived notes stay as hand-maintained .md files.

Used two ways:
  * by chopper_service.py - refresh() on startup and every TORN_REFRESH_HOURS,
    results kept in STATE (and cached to LIVE_CACHE so a restart while the API
    is down still has data);
  * from the command line to test / snapshot:
        python torn_live.py --dump out_dir     (reads TORN_API_KEY from env/.env)

The API key is only ever sent in the Authorization header. It is never logged,
printed, written to the cache, or put in a URL.
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

API_BASE = "https://api.torn.com/v2"
USER_AGENT = "ChopperBot/1.0 (Torn faction Discord bot)"
LIVE_CACHE = os.environ.get("LIVE_CACHE", "/data/live-cache.json")
REQUEST_GAP = 0.7          # seconds between calls -> ~85/min max, under the 100/min limit
MAX_PAGES = 20             # safety cap when following _metadata.links.next

# Static note files that are pure API dumps. When the live version of the
# selection loads successfully, the static file is skipped so retrieval
# doesn't see two (possibly disagreeing) copies.
REPLACES = {
    "calendar": "torn-calendar",
    "honors": "torn-honors",
    "medals": "torn-medals",
    "merits": "torn-merits",
    "stocks": "torn-stocks",
    "organizedcrimes": "torn-oc-scenarios",
    "education": "torn-education",
    "factiontree": "torn-faction-tree",
    "companies": "torn-company-details",
}


# Selections whose stale data should be discarded when the API returns nothing.
CLEAR_WHEN_EMPTY = {"elimination"}


class TornApiError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Torn API error {code}: {message}")
        self.code = code


# --------------------------------------------------------------------------- #
# small formatting helpers
# --------------------------------------------------------------------------- #
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _src(sel: str) -> str:
    return f"[source: Torn API ({sel}) | {_today()}]"


def money(n) -> str:
    if n is None:
        return "?"
    try:
        return "${:,}".format(int(n))
    except (TypeError, ValueError):
        return f"${n}"


def num(n) -> str:
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)


def clean(text, limit=None) -> str:
    """Strip HTML tags / whitespace runs; optionally trim to `limit` chars."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    t = re.sub(r"\s+", " ", t).strip()
    if limit and len(t) > limit:
        t = t[: limit - 3].rsplit(" ", 1)[0] + "..."
    return t


def sentence(text) -> str:
    t = clean(text)
    if t and t[-1] not in ".!?":
        t += "."
    return t


def ts_date(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%b %d, %Y")


def duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if s >= 86400:
        d = s / 86400
        return "1 day" if d == 1 else f"{d:g} days"
    if s >= 3600:
        return f"{s / 3600:g} hours"
    return f"{s // 60} minutes"


def chunked(items, per):
    for i in range(0, len(items), per):
        yield items[i:i + per]


# --------------------------------------------------------------------------- #
# renderers: raw API JSON -> list of note lines
# --------------------------------------------------------------------------- #
def r_calendar(data, ctx):
    cal = data.get("calendar", {}) or {}
    events = [dict(e, kind="event") for e in cal.get("events", []) or []]
    events += [dict(e, kind="competition") for e in cal.get("competitions", []) or []]
    ctx["calendar"] = events
    s = _src("calendar")
    good = []
    for e in events:
        st, en = e.get("start"), e.get("end")
        if isinstance(st, int) and isinstance(en, int) and st <= en:
            good.append(e)
    good.sort(key=lambda e: e["start"])
    lines = []
    if good:
        listing = "; ".join(
            f"{e['title']} ({ts_date(e['start'])}"
            + ("" if ts_date(e['start']) == ts_date(e['end']) else f" - {ts_date(e['end'])}")
            + ")" for e in good)
        lines.append(f"- Torn calendar (all events and competitions this year, in order): {listing}. "
                     f"Dates are Torn City Time (UTC). {s}")
    for e in good:
        when = ts_date(e["start"])
        if ts_date(e["end"]) != when:
            when += f" to {ts_date(e['end'])}"
        kind = "competition" if e.get("kind") == "competition" else "event"
        lines.append(f"- Torn {kind} \"{e['title']}\": runs {when} (TCT). {sentence(e.get('description'))} {s}")
    return lines


def r_items(data, ctx):
    items = data.get("items", []) or []
    ctx["items_by_id"] = {i.get("id"): i.get("name") for i in items}
    s = _src("items")
    lines, by_type, abroad, city = [], {}, {}, {}
    for it in items:
        if it.get("is_masked") or it.get("type") == "Unused":
            continue
        name = it.get("name", "?")
        typ = it.get("type") or "Item"
        sub = it.get("sub_type")
        by_type.setdefault(typ, []).append(name)
        parts = [f"{typ}" + (f" ({sub})" if sub and sub != typ else "") + f", item id {it.get('id')}."]
        desc = clean(it.get("description"), 240)
        if desc:
            parts.append(sentence(desc))
        if it.get("effect"):
            parts.append("Effect: " + sentence(clean(it["effect"], 200)))
        if it.get("requirement"):
            parts.append("Requirement: " + sentence(clean(it["requirement"], 140)))
        det = it.get("details") or {}
        if isinstance(det, dict):
            bs = det.get("base_stats") or {}
            if det.get("category"):
                w = f"{det['category']} weapon"
                if bs:
                    w += f", base damage {bs.get('damage')}, accuracy {bs.get('accuracy')}"
                ammo = det.get("ammo")
                if isinstance(ammo, dict) and ammo.get("name"):
                    rof = ammo.get("rate_of_fire") or {}
                    w += (f", uses {ammo['name']} ({ammo.get('magazine_rounds')}-round magazine, "
                          f"{rof.get('minimum')}-{rof.get('maximum')} shots per turn)")
                parts.append(w + ".")
            elif bs.get("armor"):
                cov = det.get("coverage") or []
                top = sorted(cov, key=lambda c: c.get("value", 0), reverse=True)[:4]
                cv = ", ".join(f"{c.get('name')} {c.get('value'):g}%" for c in top if c.get("value"))
                parts.append(f"Armor rating {bs.get('armor')}" + (f"; covers {cv}" if cv else "") + ".")
        val = it.get("value") or {}
        price_bits = []
        if val.get("market_price"):
            price_bits.append(f"market value ~{money(val['market_price'])}")
        if val.get("sell_price"):
            price_bits.append(f"sells to shops for {money(val['sell_price'])}")
        where = []
        for sh in val.get("shops") or []:
            country, shop, bp = sh.get("country"), sh.get("shop"), sh.get("buy_price")
            if not bp:
                continue
            where.append(f"{shop}" + (f" in {country}" if country and country != "Torn" else "") + f" ({money(bp)})")
            bucket = abroad if country and country != "Torn" else city
            bucket.setdefault(country if country and country != "Torn" else shop, []).append(f"{name} {money(bp)}")
        if where:
            price_bits.append("buy at " + ", ".join(where[:4]))
        if price_bits:
            pb = "; ".join(price_bits)
            parts.append(sentence(pb[0].upper() + pb[1:]))
        flags = []
        if it.get("circulation") is not None:
            flags.append(f"{num(it['circulation'])} in circulation")
        if it.get("is_tradable") is False:
            flags.append("not tradable")
        if it.get("is_found_in_city"):
            flags.append("can be found in the city")
        if flags:
            parts.append(sentence(", ".join(flags)))
        lines.append(f"- Item \"{name}\": " + " ".join(parts) + f" {s}")
    # one browsable list line per type, so "what drugs are there" works
    for typ, names in sorted(by_type.items()):
        for n, chunk in enumerate(chunked(sorted(names), 60), 1):
            tag = f" (part {n})" if len(names) > 60 else ""
            lines.append(f"- Items - {typ} list{tag}: {', '.join(chunk)}. {s}")
    # what each foreign country sells (travel shopping) and each city shop
    for country, goods in sorted(abroad.items()):
        for n, chunk in enumerate(chunked(goods, 40), 1):
            tag = f" (part {n})" if len(goods) > 40 else ""
            lines.append(f"- Travel shopping - items you can buy in {country}{tag}: {', '.join(chunk)}. {s}")
    for shop, goods in sorted(city.items()):
        for n, chunk in enumerate(chunked(goods, 40), 1):
            tag = f" (part {n})" if len(goods) > 40 else ""
            lines.append(f"- City shop - {shop} sells{tag}: {', '.join(chunk)}. {s}")
    return lines


def r_itemmods(data, ctx):
    mods = data.get("itemmods", []) or []
    s = _src("itemmods")
    lines = []
    if mods:
        lines.append(f"- Weapon mods (overview): weapon mods are upgrades you attach to guns (primary and "
                     f"secondary weapons - melee and temporary weapons can't take mods). There are "
                     f"{len(mods)} mods: {', '.join(m.get('name', '?') for m in mods)}. {s}")
    for m in mods:
        fits = ", ".join(m.get("weapons") or []) or "?"
        dual = " It can be fitted alongside another mod of the same kind (dual fit)." if m.get("dual_fit") else ""
        lines.append(f"- Weapon mod \"{m.get('name')}\": {sentence(m.get('description'))} "
                     f"Fits: {fits}.{dual} {s}")
    return lines


def r_itemammo(data, ctx):
    ammo = data.get("itemammo", []) or []
    s = _src("itemammo")
    lines = []
    for a in ammo:
        types = ", ".join(a.get("types") or [])
        lines.append(f"- Ammo \"{a.get('name')}\": costs {money(a.get('price'))} per round"
                     + (f"; available as {types}" if types else "") + f". {s}")
    if ammo:
        lines.append(f"- Ammo (overview): guns use ammo bought per round; ammo types: "
                     f"{', '.join(a.get('name', '?') for a in ammo)}. {s}")
    return lines


def r_gyms(data, ctx):
    gyms = data.get("gyms", []) or []
    s = _src("gyms")
    lines = []
    for g in gyms:
        mod = g.get("modifiers") or {}
        gains = ", ".join(f"{k} {v:g}" for k, v in mod.items() if v) or "no stat gains"
        note = f" {sentence(g.get('note'))}" if g.get("note") else ""
        lines.append(f"- Gym \"{g.get('name')}\": {g.get('class', '')} gym, {g.get('energy_cost')} energy "
                     f"per train, membership {money(g.get('cost'))}. Gym gains (dots) per stat: {gains}.{note} {s}")
    if gyms:
        order = " > ".join(g.get("name", "?") for g in gyms)
        lines.append(f"- Gyms (overview, all {len(gyms)} gyms in unlock order): {order}. {s}")
    return lines


def r_properties(data, ctx):
    props = data.get("properties", []) or []
    s = _src("properties")
    lines = []
    for p in props:
        mods = ", ".join(p.get("modifications") or []) or "none"
        staff = ", ".join(p.get("staff") or []) or "none"
        lines.append(f"- Property \"{p.get('name')}\": costs {money(p.get('cost'))}, base happy "
                     f"{num(p.get('happy'))}, upkeep {money(p.get('upkeep'))}/day. Allowed upgrades: {mods}. "
                     f"Staff: {staff}. {s}")
    return lines


def r_companies(data, ctx):
    comps = data.get("companies", []) or []
    s = _src("companies")
    lines = []
    if comps:
        lines.append(f"- Company types (overview): there are {len(comps)} company types: "
                     f"{', '.join(c.get('name', '?') for c in comps)}. {s}")
    for c in comps:
        name = c.get("name", "?")
        lines.append(f"- Company \"{name}\": costs {money(c.get('cost'))} to start, up to "
                     f"{c.get('employees')} employees. {s}")
        specials = c.get("specials") or []
        if specials:
            sp = "; ".join(
                f"{x.get('rating')}-star \"{x.get('name')}\" - {clean(x.get('effect'))}"
                + (f" ({x.get('cost')} job points)" if x.get("cost") else " (passive)")
                for x in sorted(specials, key=lambda x: x.get("rating") or 0))
            lines.append(f"- Company \"{name}\" specials (by star rating): {sp}. {s}")
        positions = c.get("positions") or []
        if positions:
            ps = "; ".join(f"{p.get('name')} ({clean(p.get('ability'))})" if p.get("ability")
                           else f"{p.get('name')}" for p in positions)
            lines.append(f"- Company \"{name}\" positions: {ps}. {s}")
        stock = c.get("stock") or []
        if stock:
            st = ", ".join(f"{x.get('name')} (cost {money(x.get('cost'))}, RRP {money(x.get('rrp'))})" for x in stock)
            lines.append(f"- Company \"{name}\" products/stock: {st}. {s}")
    return lines


def r_crimes(data, ctx):
    crimes = data.get("crimes", []) or []
    ctx["crime_ids"] = [(c.get("id"), c.get("name")) for c in crimes]
    s = _src("crimes")
    lines = []
    for c in crimes:
        notes = " ".join(sentence(n) for n in (c.get("notes") or [])[:3])
        lines.append(f"- Crime \"{c.get('name')}\" ({c.get('category_name')}): enhancer item "
                     f"{c.get('enhancer_name')}; {c.get('unique_outcomes_count')} unique outcomes. "
                     f"{notes} {s}".replace("  ", " "))
    return lines


def r_subcrimes(crime_name, data):
    subs = data.get("subcrimes", []) or []
    if not subs:
        return []
    s = _src("subcrimes")
    lst = ", ".join(f"{x.get('name')} ({x.get('nerve_cost')} nerve)" for x in subs)
    return [f"- Crime \"{crime_name}\" subcrimes and nerve costs: {lst}. {s}"]


def r_education(data, ctx):
    cats = data.get("education", []) or []
    s = _src("education")
    code_by_id = {}
    for cat in cats:
        for co in cat.get("courses") or []:
            code_by_id[co.get("id")] = co.get("code")
    lines = []
    for cat in cats:
        courses = cat.get("courses") or []
        if courses:
            listing = ", ".join("{} {}".format(c.get("code"), c.get("name")) for c in courses)
            lines.append(f"- Education category \"{cat.get('name')}\": {len(courses)} courses - "
                         f"{listing}. {s}")
        for co in courses:
            rw = co.get("rewards") or {}
            bits = []
            if rw.get("effect"):
                bits.append("Reward: " + sentence(clean(rw["effect"])))
            ws = rw.get("working_stats") or {}
            wsb = ", ".join(f"+{v} {k.replace('_', ' ')}" for k, v in ws.items() if v)
            if wsb:
                bits.append(f"Working stats: {wsb}.")
            if rw.get("honor"):
                bits.append("Awards an honor.")
            pre = co.get("prerequisites") or {}
            req = [code_by_id.get(i, str(i)) for i in pre.get("courses") or []]
            if req:
                bits.append(f"Requires: {', '.join(req)}.")
            if pre.get("cost"):
                bits.append(f"Cost {money(pre['cost'])}.")
            lines.append(f"- Education course {co.get('code')} \"{co.get('name')}\" ({cat.get('name')}): "
                         f"{sentence(clean(co.get('description'), 200))} Takes {duration(co.get('duration'))}. "
                         + (" ".join(bits) + " " if bits else "") + s)
    return lines


def r_factiontree(data, ctx):
    tree = data.get("factionTree", []) or data.get("factiontree", []) or []
    s = _src("factiontree")
    lines = []
    for branch in tree:
        bname = branch.get("name", "?")
        for up in branch.get("branches") or []:
            levels = sorted(up.get("upgrades") or [], key=lambda u: u.get("level") or 0)
            if not levels:
                continue
            first, last = levels[0], levels[-1]
            ladder = "; ".join(f"L{u.get('level')}: {clean(u.get('ability'))} ({num(u.get('cost'))} respect)"
                               for u in levels)
            ch = next((u.get("challenge") for u in levels if u.get("challenge")), None)
            chal = f" Unlock challenge: {clean(ch.get('description'))}." if isinstance(ch, dict) and ch.get("description") else ""
            lines.append(f"- Faction upgrade \"{up.get('name')}\" ({bname} branch, {len(levels)} levels, "
                         f"max: {clean(last.get('ability'))}): {ladder}.{chal} {s}")
    return lines


def _award_lines(items, label, sel):
    s = _src(sel)
    lines = []
    for h in items:
        typ = (h.get("type") or {}).get("title")
        bits = [sentence(h.get("description"))]
        meta = []
        if h.get("rarity"):
            meta.append(str(h["rarity"]))
        if typ:
            meta.append(f"{typ} category")
        if h.get("circulation") is not None:
            meta.append(f"held by {num(h['circulation'])} players")
        if meta:
            bits.append(sentence(", ".join(meta)))
        lines.append(f"- {label} \"{h.get('name')}\": " + " ".join(bits) + f" {s}")
    return lines


def r_honors(data, ctx):
    return _award_lines(data.get("honors", []) or [], "Honor", "honors")


def r_medals(data, ctx):
    return _award_lines(data.get("medals", []) or [], "Medal", "medals")


def r_merits(data, ctx):
    s = _src("merits")
    merits = data.get("merits", []) or []
    lines = [f"- Merit \"{m.get('name')}\": {sentence(m.get('description'))} "
             f"(Merit upgrades are bought with merits, max 10 levels each.) {s}" for m in merits]
    if merits:
        lines.append(f"- Merits (overview): the {len(merits)} merit upgrades are: "
                     f"{', '.join(m.get('name', '?') for m in merits)}. {s}")
    return lines


def r_museum(data, ctx):
    s = _src("museum")
    names = ctx.get("items_by_id", {})
    lines = []
    for m in data.get("museum", []) or []:
        its = ", ".join(names.get(i, f"item {i}") for i in m.get("items") or [])
        lines.append(f"- Museum set \"{m.get('name')}\": exchange a full set ({its}) at the Museum "
                     f"for {num(m.get('points'))} points. {s}")
    return lines


def r_organizedcrimes(data, ctx):
    s = _src("organizedcrimes")
    ocs = data.get("organizedcrimes", []) or []
    lines = []
    if ocs:
        lines.append(f"- Organized crimes (OC 2.0 scenarios overview): {len(ocs)} scenarios by difficulty: "
                     + ", ".join(f"{o.get('name')} (level {o.get('difficulty')})"
                                 for o in sorted(ocs, key=lambda o: o.get("difficulty") or 0)) + f". {s}")
    for o in ocs:
        slots = []
        for sl in o.get("slots") or []:
            label = (sl.get("position_info") or {}).get("label") or sl.get("name")
            ri = sl.get("required_item")
            if isinstance(ri, dict) and ri.get("name"):
                label += f" (needs {ri['name']}{', consumed' if ri.get('is_used') else ', not consumed'})"
            slots.append(label)
        sc = o.get("scope") or {}
        pre = f" Prerequisite: {clean(o['prerequisite'])}." if o.get("prerequisite") else ""
        lines.append(f"- OC scenario \"{o.get('name')}\" (difficulty level {o.get('difficulty')}): "
                     f"{sentence(clean(o.get('description'), 220))} Scope cost {sc.get('cost')}, scope return "
                     f"{sc.get('return')}.{pre} Roles ({len(slots)}): {', '.join(slots)}. {s}")
    return lines


def r_stocks(data, ctx):
    s = _src("stocks")
    lines = []
    for st in data.get("stocks", []) or []:
        mk, bn = st.get("market") or {}, st.get("bonus") or {}
        if bn:
            kind = "passive" if bn.get("passive") else f"active, pays out every {bn.get('frequency')} days"
            benefit = (f" Benefit block ({num(bn.get('requirement'))} shares, {kind}): "
                       f"{sentence(clean(bn.get('description')))}")
        else:
            benefit = ""
        price = mk.get("price")
        lines.append(f"- Stock {st.get('acronym')} \"{st.get('name')}\": share price ${price:,.2f} "
                     f"at last refresh, {num(mk.get('investors'))} investors.{benefit} {s}"
                     if isinstance(price, (int, float)) else
                     f"- Stock {st.get('acronym')} \"{st.get('name')}\":{benefit} {s}")
    return lines


def r_bank(data, ctx):
    rates = data.get("bank", []) or []
    if not rates:
        return []
    r = "; ".join(f"{x.get('days')} days: {x.get('rate')}%" for x in rates)
    return [f"- Bank investment rates (City Bank, current): {r}. {_src('bank')}"]


def r_cityshops(data, ctx):
    s = _src("cityshops")
    lines = []
    for sh in data.get("cityshops", []) or []:
        its = ", ".join(f"{i.get('name')} {money(i.get('price'))}" for i in sh.get("items") or [])
        if its:
            lines.append(f"- City shop \"{sh.get('name')}\" price list: {its}. {s}")
    return lines


def r_halloween(data, ctx):
    s = _src("halloween")
    ups = (data.get("halloween") or {}).get("basket_upgrades") or []
    return [f"- Halloween basket upgrade \"{u.get('name')}\": {sentence(u.get('description'))} "
            f"Costs {num(u.get('cost'))} treats" + (f", requires {num(u['requirement'])}" if u.get("requirement") else "")
            + f". {s}" for u in ups]


def r_elimination(data, ctx):
    teams = data.get("elimination", []) or []
    if not teams:
        return []
    teams = sorted(teams, key=lambda t: t.get("position") or 99)
    board = "; ".join(
        f"#{t.get('position')} {t.get('name')} - score {num(t.get('score'))}, {t.get('lives')} lives, "
        f"{t.get('participants_left')}/{t.get('participants')} left" + (" (ELIMINATED)" if t.get("eliminated") else "")
        for t in teams)
    return [f"- Elimination standings (live, as of last refresh): {board}. {_src('elimination')}"]


# (selection, path, renderer). Order matters: items before museum (item names).
SELECTIONS = [
    ("calendar", "/torn/calendar", r_calendar),
    ("items", "/torn/items", r_items),
    ("itemmods", "/torn/itemmods", r_itemmods),
    ("itemammo", "/torn/itemammo", r_itemammo),
    ("gyms", "/torn/gyms", r_gyms),
    ("properties", "/torn/properties", r_properties),
    ("companies", "/torn/companies", r_companies),
    ("crimes", "/torn/crimes", r_crimes),
    ("education", "/torn/education", r_education),
    ("factiontree", "/torn/factiontree", r_factiontree),
    ("honors", "/torn/honors", r_honors),
    ("medals", "/torn/medals", r_medals),
    ("merits", "/torn/merits", r_merits),
    ("museum", "/torn/museum", r_museum),
    ("organizedcrimes", "/torn/organizedcrimes", r_organizedcrimes),
    ("stocks", "/torn/stocks", r_stocks),
    ("bank", "/torn/bank", r_bank),
    ("cityshops", "/torn/cityshops", r_cityshops),
    ("halloween", "/torn/halloween", r_halloween),
    ("elimination", "/torn/elimination", r_elimination),
]


# --------------------------------------------------------------------------- #
# state + fetching
# --------------------------------------------------------------------------- #
class LiveState:
    def __init__(self):
        self.lines = {}          # selection -> [note lines]
        self.calendar = []       # raw event dicts for calendar_status()
        self.updated = None      # unix ts of last successful refresh
        self.errors = {}         # selection -> last error (no secrets)
        self.running = False

    def topic_lines(self):
        """(line, topic) pairs, topic prefixed 'live-' so they're recognisable."""
        out = []
        for sel, lines in self.lines.items():
            for ln in lines:
                out.append((ln, f"live-{sel}"))
        return out

    def replaced_topics(self):
        return {REPLACES[sel] for sel in self.lines if sel in REPLACES and self.lines[sel]}

    def summary(self):
        return {
            "enabled": bool(api_key()),
            "updated": (datetime.fromtimestamp(self.updated, timezone.utc).isoformat()
                        if self.updated else None),
            "selections": {k: len(v) for k, v in self.lines.items()},
            "replaces_static": sorted(self.replaced_topics()),
            "errors": self.errors,
        }


STATE = LiveState()


def api_key():
    return (os.environ.get("TORN_API_KEY") or "").strip()


async def _get(client, path, follow=True):
    """GET an API path, following pagination unless follow=False; raises TornApiError."""
    url, merged, pages = f"{API_BASE}{path}", None, 0
    while url and pages < MAX_PAGES:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"] or {}
            raise TornApiError(err.get("code"), err.get("error"))
        if merged is None:
            merged = data
        else:  # append list payloads from subsequent pages
            for k, v in data.items():
                if isinstance(v, list) and isinstance(merged.get(k), list):
                    merged[k].extend(v)
        nxt = ((data.get("_metadata") or {}).get("links") or {}).get("next") if follow else None
        # only follow links back to the API host, and never carry a key in the URL
        url = nxt if (nxt and nxt.startswith(API_BASE) and "key=" not in nxt) else None
        pages += 1
        if url:
            await asyncio.sleep(REQUEST_GAP)
    return merged or {}


async def refresh(state: LiveState = STATE, log=print):
    """Fetch every selection; on a per-selection failure keep the previous data."""
    key = api_key()
    if not key:
        state.errors = {"_": "TORN_API_KEY not set - live data disabled"}
        return state
    if state.running:
        return state
    state.running = True
    headers = {"Authorization": f"ApiKey {key}", "User-Agent": USER_AGENT}
    ctx, new_lines, errors = {}, {}, {}
    try:
        async with httpx.AsyncClient(timeout=45, headers=headers) as client:
            for sel, path, render in SELECTIONS:
                try:
                    data = await _get(client, path)
                    new_lines[sel] = render(data, ctx)
                except TornApiError as e:
                    errors[sel] = str(e)
                    if e.code in (1, 2, 10, 13, 18):   # key problem: stop hammering
                        errors["_"] = f"key problem ({e}); stopping this refresh"
                        break
                except Exception as e:  # network/parse - keep going
                    errors[sel] = f"{type(e).__name__}: {e}"
                await asyncio.sleep(REQUEST_GAP)
            # subcrimes (one call per crime)
            sub_lines = []
            for cid, cname in ctx.get("crime_ids", []):
                try:
                    sub_lines += r_subcrimes(cname, await _get(client, f"/torn/{cid}/subcrimes"))
                except Exception as e:
                    errors[f"subcrimes:{cid}"] = f"{type(e).__name__}: {e}"
                await asyncio.sleep(REQUEST_GAP)
            if sub_lines:
                new_lines["subcrimes"] = sub_lines
    finally:
        state.running = False
    # merge: replace only selections that succeeded with content. Seasonal
    # live boards (elimination) are dropped when they come back empty so last
    # season's standings don't linger.
    for sel, lines in new_lines.items():
        if lines:
            state.lines[sel] = lines
        elif sel in CLEAR_WHEN_EMPTY:
            state.lines.pop(sel, None)
    if ctx.get("calendar"):
        state.calendar = ctx["calendar"]
    state.errors = errors
    if new_lines:
        state.updated = time.time()
        save_cache(state)
    log(f"[torn_live] refresh: {sum(len(v) for v in new_lines.values())} lines from "
        f"{len(new_lines)} selections; {len(errors)} errors")
    return state


def save_cache(state: LiveState = STATE, path: str = None):
    path = path or LIVE_CACHE
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": state.updated, "lines": state.lines,
                       "calendar": state.calendar}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load_cache(state: LiveState = STATE, path: str = None):
    path = path or LIVE_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        state.lines = d.get("lines") or {}
        state.calendar = d.get("calendar") or []
        state.updated = d.get("updated")
    except (OSError, ValueError):
        pass
    return state


async def refresh_loop(hours: float, state: LiveState = STATE, log=print):
    load_cache(state)
    while True:
        try:
            await refresh(state, log=log)
        except Exception as e:  # never let the loop die
            log(f"[torn_live] refresh crashed: {type(e).__name__}: {e}")
        await asyncio.sleep(max(0.25, hours) * 3600)


# --------------------------------------------------------------------------- #
# live player lookups ("who is flirt", "is bob [123] online", "who is @someone")
# --------------------------------------------------------------------------- #
_NAME = r"[A-Za-z0-9_-]{2,16}"
_LOOKUP_RE = re.compile(
    r"(?i)(?:\bwho\s*(?:is|'s|’s|s|was)\b|\bwhois\b|\btell\s+me\s+about\b|\blook\s*up\b"
    r"|\bprofile\s+(?:of|for)\b|\bstats\s+(?:of|for|on)\b|\bwhat\s+(?:level|faction|rank)\s+is\b"
    r"|\bis\b(?=\s+@?" + _NAME + r"(?:\s*\[\d+\])?\s+(?:online|offline|active|idle|traveling|travelling"
    r"|abroad|in\s+(?:hosp|hospital|jail)|alive|okay)\b))"
    r"\s+@?(?P<name>" + _NAME + r")(?:\s*\[(?P<id>\d{1,8})\])?")
_ID_RE = re.compile(r"(?i)(?:\bXID=|\[)(\d{1,8})\]?")
# words that follow "who is" but aren't player names
NOT_PLAYERS = {
    "the", "a", "an", "this", "that", "it", "he", "she", "they", "them", "you", "u", "ur", "me",
    "i", "we", "us", "our", "your", "my", "his", "her", "their", "online", "offline", "best",
    "top", "strongest", "richest", "weakest", "here", "there", "chopper", "everyone", "everybody",
    "anyone", "anybody", "someone", "somebody", "nobody", "staff", "admin", "admins", "leader",
    "boss", "in", "on", "at", "of", "for", "to", "and", "or", "not", "going", "gonna", "playing",
    "winning", "next", "first", "last", "better", "worse", "more", "most", "faction", "torn",
}
_PLAYER_CACHE = {}           # cache key -> (time, lines)
PLAYER_CACHE_SECONDS = 300


def parse_lookup_target(question, mentions=None):
    """Work out whether the message asks about a specific player.
    Returns {"name", "pid", "discord_id", "strong"} or None. "strong" means the
    phrasing is clearly about a player ("who is X", "is X online", a ping, an ID);
    weak phrasings ("tell me about X", "look up X") may be about an item/faction."""
    q = question or ""
    m = _LOOKUP_RE.search(q)
    mentions = mentions or []
    # a Discord ping: the cog rewrites <@id> to '@DisplayName' and sends the ids
    for men in mentions:
        disp = (men.get("name") or "").strip()
        base = re.sub(r"\s*\[\d+\]\s*$", "", disp)
        if disp and (("@" + disp).lower() in q.lower() or ("@" + base).lower() in q.lower()):
            dm = re.match(r"\s*(" + _NAME + r")\s*\[(\d{1,8})\]", disp)
            if dm:
                return {"name": dm.group(1), "pid": dm.group(2), "discord_id": None, "strong": True}
            if m or len(q.split()) <= 4:
                return {"name": disp if re.fullmatch(_NAME, disp) else None,
                        "pid": None, "discord_id": str(men.get("discord_id") or "") or None,
                        "strong": True}
    if not m:
        return None
    name, pid = m.group("name"), m.group("id")
    strong = not re.match(r"(?i)(tell|look|profile|stats)", m.group(0).strip())
    if not pid:
        im = _ID_RE.search(q[m.start():])
        pid = im.group(1) if im else None
    if name.isdigit():
        return {"name": None, "pid": name, "discord_id": None, "strong": True}
    if name.upper() == "XID":
        return {"name": None, "pid": pid, "discord_id": None, "strong": True} if pid else None
    if name.lower() in NOT_PLAYERS and not pid:
        return None
    return {"name": name, "pid": pid, "discord_id": None, "strong": strong or bool(pid)}


def _ago(ts):
    try:
        s = int(time.time()) - int(ts)
    except (TypeError, ValueError):
        return None
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def render_profile(p, fac):
    name, pid = p.get("name", "?"), p.get("id")
    s = f"[source: Torn API (live player profile) | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} TCT]"
    bits = [f"level {p.get('level')}"]
    if p.get("rank"):
        bits.append(f"rank \"{clean(p['rank'])}\"")
    if p.get("role") and p["role"] != "Civilian":
        bits.append(f"Torn role: {p['role']}")
    if p.get("age") is not None:
        joined = f", signed up {ts_date(p['signed_up'])}" if p.get("signed_up") else ""
        bits.append(f"account age {num(p['age'])} days{joined}")
    out = [f"- Player {name} [{pid}] (live Torn profile, looked up just now): " + ", ".join(bits) + "."]
    if fac and fac.get("name"):
        tag = f" [{fac['tag']}]" if fac.get("tag") else ""
        out.append(f"Faction: {fac['name']}{tag} (faction ID {fac.get('id')}), position {fac.get('position')}, "
                   f"{num(fac.get('days_in_faction'))} days in the faction.")
    elif p.get("faction_id") in (None, 0):
        out.append("Not in a faction.")
    st = p.get("status") or {}
    if st.get("description"):
        det = f" ({clean(st['details'])})" if st.get("details") else ""
        out.append(f"Current status: {clean(st['description'])}{det}.")
    la = p.get("last_action") or {}
    if la.get("status"):
        rel = la.get("relative") or _ago(la.get("timestamp")) or ""
        out.append(f"Last action: {la['status']}" + (f", {rel}" if rel else "") + ".")
    if (p.get("property") or {}).get("name"):
        out.append(f"Lives in a {p['property']['name']}.")
    sp = p.get("spouse")
    if isinstance(sp, dict) and sp.get("name"):
        out.append(f"Married to {sp['name']} [{sp.get('id')}] for {num(sp.get('days_married'))} days.")
    extra = []
    if p.get("awards") is not None:
        extra.append(f"{num(p['awards'])} awards")
    if p.get("donator_status"):
        extra.append(f"donator status: {p['donator_status']}")
    if p.get("revivable") is not None:
        extra.append("accepting revives" if p.get("revivable") else "not accepting revives")
    if extra:
        ex = sentence(", ".join(extra))
        out.append(ex[0].upper() + ex[1:])
    return " ".join(out) + f" {s}"


async def lookup_player(name=None, pid=None, discord_id=None, strong=True):
    """Live public-profile lookup. Returns note lines (possibly a 'not found' line),
    or [] if live data is disabled or the API call failed."""
    key = api_key()
    if not key or not (name or pid or discord_id):
        return []
    ck = ((name or "").lower(), pid, discord_id)
    hit = _PLAYER_CACHE.get(ck)
    if hit and time.time() - hit[0] < PLAYER_CACHE_SECONDS:
        return hit[1]
    headers = {"Authorization": f"ApiKey {key}", "User-Agent": USER_AGENT}
    s = f"[source: Torn API (player search) | {_today()}]"
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            target = pid
            if not target and discord_id:
                # Torn resolves Discord ids for accounts linked to Discord; if this
                # one isn't linked, fall back to searching the display name.
                try:
                    prof = (await _get(client, f"/user/{discord_id}/profile", follow=False)).get("profile") or {}
                    target = prof.get("id")
                except TornApiError:
                    if not name:
                        raise
            if not target:
                q = httpx.QueryParams({"name": name})
                res = (await _get(client, f"/user/search?{q}", follow=False)).get("search") or []
                exact = [r for r in res if (r.get("name") or "").lower() == name.lower()]
                if not exact:
                    near = ", ".join(f"{r.get('name')} [{r.get('id')}] (level {r.get('level')})" for r in res[:5])
                    lines = [f"- Player lookup \"{name}\": no Torn player with exactly that name exists."
                             + (f" Similar names: {near}." if near else "") + f" {s}"]
                    _PLAYER_CACHE[ck] = (time.time(), lines)
                    return lines
                target = exact[0].get("id")
            prof = (await _get(client, f"/user/{target}/profile", follow=False)).get("profile") or {}
            if not prof.get("id"):
                return []
            fac = {}
            try:
                fac = (await _get(client, f"/user/{prof['id']}/faction", follow=False)).get("faction") or {}
            except Exception:
                pass
    except TornApiError as e:
        if e.code == 6 or (discord_id and not pid):   # unknown id / unlinked Discord account
            who = name or (f"player ID {pid}" if pid else "that Discord user")
            lines = [f"- Player lookup \"{who}\": Torn couldn't find a player for that "
                     f"{'ID' if pid else 'Discord account (it may not be linked to Torn)'}. {s}"]
            _PLAYER_CACHE[ck] = (time.time(), lines)
            return lines
        return []
    except Exception:
        return []
    lines = [render_profile(prof, fac)]
    _PLAYER_CACHE[ck] = (time.time(), lines)
    return lines


# --------------------------------------------------------------------------- #
# CLI: python torn_live.py --dump out_dir
# --------------------------------------------------------------------------- #
def _load_dotenv(path=".env"):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$", line)
                if m and m.group(1) not in os.environ:
                    os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass


if __name__ == "__main__":
    _load_dotenv()
    out = sys.argv[sys.argv.index("--dump") + 1] if "--dump" in sys.argv else None
    st = asyncio.run(refresh(LiveState()))
    print(json.dumps(st.summary(), indent=2))
    if out:
        os.makedirs(out, exist_ok=True)
        for sel, lines in st.lines.items():
            with open(os.path.join(out, f"live-{sel}.md"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        print(f"wrote {len(st.lines)} files to {out}")

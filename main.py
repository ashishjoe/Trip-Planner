import sqlite3, html, secrets, os
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

DB = os.environ.get("DB_PATH", "trips_v2.db")


def initialize_db():
    db_dir = os.path.dirname(DB)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trips(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, traveller TEXT, budget INTEGER,
          max_hotel_stars INTEGER DEFAULT 3, allow_business INTEGER DEFAULT 0,
          status TEXT DEFAULT 'draft', share_id TEXT, approved_at TEXT);
        CREATE TABLE IF NOT EXISTS options(
          id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER, leg TEXT, mode TEXT,
          provider TEXT, price INTEGER, duration_min INTEGER, comfort INTEGER,
          depart TEXT, arrive TEXT, hotel_stars INTEGER DEFAULT 0, is_business INTEGER DEFAULT 0,
          link TEXT, reason TEXT, checked_on TEXT, picked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS price_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT, option_id INTEGER, price INTEGER, at TEXT);
        """)


def db():
    initialize_db()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- MCP tools (what Claude uses) ----------------
mcp = FastMCP(
    "trip-planner",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def create_trip(title: str, traveller: str, budget: int,
                max_hotel_stars: int = 3, allow_business_class: bool = False) -> dict:
    """Create a business trip. budget is the total in INR. Returns trip_id and page paths."""
    sid = secrets.token_urlsafe(6)
    with db() as c:
        cur = c.execute(
            "INSERT INTO trips(title,traveller,budget,max_hotel_stars,allow_business,share_id) VALUES(?,?,?,?,?,?)",
            (title, traveller, budget, max_hotel_stars, int(allow_business_class), sid))
        tid = cur.lastrowid
    return {"trip_id": tid, "edit_path": f"/trip/{tid}", "share_path": f"/share/{sid}"}


@mcp.tool()
def add_option(trip_id: int, leg: str, mode: str, provider: str, price: int,
               duration_minutes: int, comfort_score: int, reason: str,
               link: str = "", depart: str = "", arrive: str = "",
               hotel_stars: int = 0, is_business_class: bool = False,
               checked_on: str = "", recommended: bool = False) -> dict:
    """Add one option to a leg of a trip. mode is flight, train, bus or hotel.
    leg is a label such as 'Chennai to Bangalore' or 'Hotel in Bangalore'.
    price is in INR (for hotels, the total for the stay). comfort_score is 1 to 10.
    duration_minutes is 0 for hotels. reason is one sentence on why this option is good or bad.
    link is the source page. Set recommended=True for your single best pick in each leg."""
    with db() as c:
        if recommended:
            c.execute("UPDATE options SET picked=0 WHERE trip_id=? AND leg=?", (trip_id, leg))
        cur = c.execute(
            """INSERT INTO options(trip_id,leg,mode,provider,price,duration_min,comfort,depart,arrive,
               hotel_stars,is_business,link,reason,checked_on,picked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trip_id, leg, mode.lower(), provider, price, duration_minutes, comfort_score, depart, arrive,
             hotel_stars, int(is_business_class), link, reason, checked_on or now(), int(recommended)))
        oid = cur.lastrowid
        c.execute("INSERT INTO price_history(option_id,price,at) VALUES(?,?,?)", (oid, price, now()))
    return {"option_id": oid}


@mcp.tool()
def update_price(option_id: int, new_price: int, checked_on: str = "") -> str:
    """Update an option's price after re-checking it. The app keeps the price history."""
    with db() as c:
        c.execute("UPDATE options SET price=?, checked_on=? WHERE id=?", (new_price, checked_on or now(), option_id))
        c.execute("INSERT INTO price_history(option_id,price,at) VALUES(?,?,?)", (option_id, new_price, now()))
    return "price updated"


@mcp.tool()
def get_trip(trip_id: int) -> dict:
    """Get a trip with all its options (use this before refreshing prices)."""
    with db() as c:
        t = c.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        opts = c.execute("SELECT * FROM options WHERE trip_id=?", (trip_id,)).fetchall()
    return {"trip": dict(t) if t else None, "options": [dict(o) for o in opts]}


@mcp.tool()
def list_trips() -> list[dict]:
    """List all saved trips."""
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,title,traveller,budget,status FROM trips")]


# ---------------- Web app (what a person sees) ----------------
@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)

CSS = """<meta name=viewport content='width=device-width,initial-scale=1'><style>
body{font-family:system-ui;max-width:900px;margin:30px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:10px;padding:12px;margin:10px 0}
.picked{border:2px solid #2e7d32;background:#f3faf3}
.flag{color:#b71c1c;font-size:13px}.muted{color:#666;font-size:13px}
.badge{background:#2e7d32;color:#fff;border-radius:6px;padding:2px 6px;font-size:12px}
.best{background:#1565c0;color:#fff;border-radius:6px;padding:2px 6px;font-size:12px}
.box{background:#f5f5f5;border-radius:10px;padding:12px;margin:12px 0}
button{cursor:pointer;margin:4px 4px 0 0}input[type=number]{width:90px}
</style>"""

JS = """<script>
function rank(){
 var wp=+document.getElementById('wp').value, wt=+document.getElementById('wt').value, wc=+document.getElementById('wc').value;
 var sum=(wp+wt+wc)||1;
 document.querySelectorAll('.leg').forEach(function(leg){
  var cards=[].slice.call(leg.querySelectorAll('.card'));
  function norm(vals,v){var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);return mx==mn?0.5:(v-mn)/(mx-mn);}
  var P=cards.map(function(c){return +c.dataset.price}),T=cards.map(function(c){return +c.dataset.dur}),C=cards.map(function(c){return +c.dataset.comfort});
  cards.forEach(function(c,i){c.score=(wp*(1-norm(P,P[i]))+wt*(1-norm(T,T[i]))+wc*norm(C,C[i]))/sum;});
  cards.sort(function(a,b){return b.score-a.score});
  cards.forEach(function(c,i){leg.appendChild(c);
   c.querySelector('.rank').innerHTML=(i==0?'<span class="best">Best match for your priorities</span> ':'#'+(i+1)+' ')+'(score '+Math.round(c.score*100)+')';});
 });
}
window.onload=rank;
</script>"""


def now():
    return datetime.now().strftime("%d %b %Y %H:%M")


def esc(x):
    return html.escape(str(x if x is not None else ""))


def flags(t, o):
    f = []
    if o["mode"] == "hotel" and o["hotel_stars"] > t["max_hotel_stars"]:
        f.append(f"Hotel is {o['hotel_stars']} star, policy max is {t['max_hotel_stars']} star")
    if o["is_business"] and not t["allow_business"]:
        f.append("Business class is not allowed by policy")
    return f


def card(t, o, hist):
    fl = "".join(f"<div class=flag>⚠ {esc(f)}</div>" for f in flags(t, o))
    h = ""
    if len(hist) > 1:
        d = hist[-1]["price"] - hist[0]["price"]
        h = f"<div class=muted>Price moved {'+' if d >= 0 else '-'}₹{abs(d):,} since first check ({len(hist)} checks)</div>"
    link = f" · <a href='{esc(o['link'])}' target=_blank>source</a>" if str(o["link"]).startswith("http") else ""
    dur = f"{o['duration_min']//60}h {o['duration_min']%60}m · " if o["duration_min"] else ""
    stars = f", {o['hotel_stars']} star" if o["hotel_stars"] else ""
    times = f" · {esc(o['depart'])} → {esc(o['arrive'])}" if o["depart"] or o["arrive"] else ""
    base = f"/trip/{t['id']}/option/{o['id']}"
    return f"""<div class="card {'picked' if o['picked'] else ''}" data-price="{o['price']}" data-dur="{o['duration_min']}" data-comfort="{o['comfort']}">
<div><b>{esc(o['provider'])}</b> <span class=muted>({esc(o['mode'])}{stars})</span>
{'<span class=badge>Picked</span>' if o['picked'] else ''} <span class=rank></span></div>
<div>₹{o['price']:,} · {dur}comfort {o['comfort']}/10{times}</div>
<div>{esc(o['reason'])}</div>{fl}{h}
<div class=muted>Checked: {esc(o['checked_on'])} · verify price before booking{link}</div>
<form method=post action="{base}/pick" style="display:inline"><button>Make this my pick</button></form>
<form method=post action="{base}/price" style="display:inline"><input type=number name=price value="{o['price']}"><button>Update price</button></form>
<form method=post action="{base}/delete" style="display:inline"><button>Delete</button></form>
</div>"""


@app.get("/", response_class=HTMLResponse)
def home():
    with db() as c:
        rows = c.execute("SELECT * FROM trips ORDER BY id DESC").fetchall()
    items = "".join(
        f"<li><a href='/trip/{r['id']}'><b>{esc(r['title'])}</b></a> · {esc(r['traveller'])} · budget ₹{r['budget']:,} · {esc(r['status'])}</li>"
        for r in rows)
    return CSS + f"<h1>My Trips</h1><ul>{items or '<li>No trips yet. Ask Claude to plan one.</li>'}</ul>"


@app.get("/trip/{tid}", response_class=HTMLResponse)
def trip_page(tid: int):
    with db() as c:
        t = c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
        if not t:
            return HTMLResponse("Trip not found", 404)
        opts = c.execute("SELECT * FROM options WHERE trip_id=? ORDER BY id", (tid,)).fetchall()
        hist = {o["id"]: c.execute("SELECT * FROM price_history WHERE option_id=? ORDER BY id", (o["id"],)).fetchall() for o in opts}
    total = sum(o["price"] for o in opts if o["picked"])
    colour = "#b71c1c" if total > t["budget"] else "#2e7d32"
    legs = {}
    for o in opts:
        legs.setdefault(o["leg"], []).append(o)
    body = f"<p><a href='/'>← All trips</a></p><h1>{esc(t['title'])}</h1>"
    body += f"<p class=muted>Traveller: {esc(t['traveller'])} · Policy: hotels up to {t['max_hotel_stars']} star, business class {'allowed' if t['allow_business'] else 'not allowed'} · Status: {esc(t['status'])}</p>"
    body += f"<div class=box><b>What matters to you?</b> (drag, options re-rank instantly)<br>Price <input id=wp type=range min=0 max=10 value=5 oninput=rank()> Speed <input id=wt type=range min=0 max=10 value=5 oninput=rank()> Comfort <input id=wc type=range min=0 max=10 value=5 oninput=rank()></div>"
    body += f"<h3 style='color:{colour}'>Picked total: ₹{total:,} / budget ₹{t['budget']:,}{' (OVER BUDGET)' if total > t['budget'] else ''}</h3>"
    body += f"<p>Share with your manager: <a href='/share/{t['share_id']}' target=_blank>/share/{esc(t['share_id'])}</a></p>"
    for leg, items in legs.items():
        body += f"<h2>{esc(leg)}</h2><div class=leg>" + "".join(card(t, o, hist[o["id"]]) for o in items) + "</div>"
    if not legs:
        body += "<p>No options yet. Ask Claude to add some.</p>"
    return CSS + body + JS


@app.post("/trip/{tid}/option/{oid}/pick")
def pick(tid: int, oid: int):
    with db() as c:
        o = c.execute("SELECT leg FROM options WHERE id=?", (oid,)).fetchone()
        if o:
            c.execute("UPDATE options SET picked=0 WHERE trip_id=? AND leg=?", (tid, o["leg"]))
            c.execute("UPDATE options SET picked=1 WHERE id=?", (oid,))
    return RedirectResponse(f"/trip/{tid}", status_code=303)


@app.post("/trip/{tid}/option/{oid}/price")
def edit_price(tid: int, oid: int, price: int = Form(...)):
    with db() as c:
        c.execute("UPDATE options SET price=?, checked_on=? WHERE id=?", (price, now() + " (edited)", oid))
        c.execute("INSERT INTO price_history(option_id,price,at) VALUES(?,?,?)", (oid, price, now()))
    return RedirectResponse(f"/trip/{tid}", status_code=303)


@app.post("/trip/{tid}/option/{oid}/delete")
def delete(tid: int, oid: int):
    with db() as c:
        c.execute("DELETE FROM options WHERE id=?", (oid,))
    return RedirectResponse(f"/trip/{tid}", status_code=303)


@app.get("/share/{sid}", response_class=HTMLResponse)
def share(sid: str):
    with db() as c:
        t = c.execute("SELECT * FROM trips WHERE share_id=?", (sid,)).fetchone()
        if not t:
            return HTMLResponse("Not found", 404)
        picks = c.execute("SELECT * FROM options WHERE trip_id=? AND picked=1 ORDER BY id", (t["id"],)).fetchall()
    total = sum(o["price"] for o in picks)
    rows = ""
    for o in picks:
        fl = "".join(f"<div class=flag>⚠ {esc(f)}</div>" for f in flags(t, o))
        rows += f"<div class=card><b>{esc(o['leg'])}</b><br>{esc(o['provider'])} ({esc(o['mode'])}) · ₹{o['price']:,}<br>{esc(o['reason'])}{fl}<div class=muted>Checked: {esc(o['checked_on'])}</div></div>"
    over = total > t["budget"]
    status = (f"<h3 style='color:#2e7d32'>Approved on {esc(t['approved_at'])}</h3>" if t["status"] == "approved"
              else f"<form method=post action='/share/{esc(sid)}/approve'><button style='font-size:18px;padding:8px 18px'>Approve this trip</button></form>")
    return CSS + f"<h1>{esc(t['title'])}</h1><p class=muted>Traveller: {esc(t['traveller'])}</p>{rows or '<p>No picks yet.</p>'}<h3 style='color:{'#b71c1c' if over else '#2e7d32'}'>Total ₹{total:,} / budget ₹{t['budget']:,}{' (OVER BUDGET)' if over else ''}</h3>{status}"


@app.post("/share/{sid}/approve")
def approve(sid: str):
    with db() as c:
        c.execute("UPDATE trips SET status='approved', approved_at=? WHERE share_id=?", (now(), sid))
    return RedirectResponse(f"/share/{sid}", status_code=303)


app.mount("/mcp", mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

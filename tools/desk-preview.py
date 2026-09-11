"""Start a seeded mplabel server so the desk portal can be looked at.

    python tools/desk-preview.py            # prints a URL and a password
    python tools/desk-preview.py --port 8099

Nothing here touches a real database, a real mailbox or a real printer.
It builds a throwaway home directory, fills it with invented rows in the
house style, and serves `/desk` off it until Ctrl-C.

This exists for the same reason `ios/screenshots.sh` does. Looking at
the iOS app found two things its assertions did not - a photograph that
sat on a spinner for ever, and a save button that had drifted under two
optional panels - and neither is visible to a test that asks whether a
string is on screen. The desk portal is a denser screen than either, so
the same argument applies harder.

It seeds more than `tests/make_ios_fixtures.py` does on purpose. Three
listings is enough to prove a payload decodes and nowhere near enough to
show whether a forty-row table is legible, whether the sort arrows read,
or whether the bulk bar covers the last row. The awkward cases are kept
from that seed though - a sale with no tracking, a sold item with no
cost, a trip with no till total - because a screen full of complete rows
proves only the happy path.
"""

import argparse
import datetime
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mplabel import cli, listings, web  # noqa: E402

PASSWORD = "preview"

# Invented, in the house style. Real titles carry a buyer's taste and a
# real database carries their address; neither belongs in a repo.
STOCK = [
    ("Walnut secretary desk, drop front", "1940s", 340, 85, "FLOOR"),
    ("Oil portrait of a woman in blue, unsigned", "c. 1910", 225, 40, "B4"),
    ("Set of 6 pressed glass tumblers", "1950s", 45, 6, "B2"),
    ("Brass student lamp, green shade", "1930s", 130, 28, "B4"),
    ("Watercolor, barn in winter, framed", "1960s", 85, 12, "B1"),
    ("Hobnail milk glass vase", "1950s", 28, 3, "B2"),
    ("Cast iron skillet, No. 8, smooth bottom", "1940s", 65, 10, "B5"),
    ("Danish teak side table", "1960s", 210, 55, "FLOOR"),
    ("Pair of Staffordshire spaniels", "c. 1890", 180, 45, "B4"),
    ("Cut glass decanter with stopper", "c. 1910", 55, 8, "B2"),
    ("Singer 99K hand crank machine", "1920s", 165, 40, "ATTIC"),
    ("Oak barley twist plant stand", "1930s", 78, 20, "FLOOR"),
    ("Ironstone platter, blue transferware", "c. 1880", 60, 9, "B1"),
    ("Set of 4 Windsor chairs", "1950s", 260, 70, "FLOOR"),
    ("Silver plate tea service, 5 pc", "1930s", 140, 35, "B6"),
    ("Bakelite radio, working", "1940s", 190, 48, "B5"),
    ("Framed botanical prints, set of 3", "1970s", 70, 12, "B1"),
    ("Copper kettle, dovetailed seam", "c. 1900", 95, 22, "B5"),
    ("Mahogany mirror, beveled glass", "1920s", 120, 30, "FLOOR"),
    ("Jadeite mugs, set of 6", "1950s", 84, 14, "B3"),
    ("Enamel bread bin, cream", "1940s", 52, 9, "B5"),
    ("Brass candlesticks, pair", "c. 1900", 48, 7, "B6"),
    ("Landscape oil, gilt frame, unsigned", "c. 1920", 275, 65, "B4"),
    ("Wicker sewing basket, fitted", "1930s", 42, 5, "ATTIC"),
    ("Blue Willow dinner plates, set of 8", "c. 1900", 88, 16, "B1"),
    ("Mid-century brass floor lamp", "1960s", 145, 32, "FLOOR"),
    ("Depression glass pitcher, pink", "1930s", 58, 8, "B2"),
    ("Carved oak blanket chest", "c. 1890", 380, 95, "FLOOR"),
    ("Rosewood mantel clock, chiming", "c. 1900", 240, 60, "B6"),
    ("Chenille bedspread, white", "1950s", 45, 6, "ATTIC"),
    ("Stoneware crock, 3 gallon", "c. 1890", 110, 25, "B5"),
    ("Etching, harbor scene, pencil signed", "1930s", 130, 22, "B4"),
    ("Nesting tables, set of 3, walnut", "1950s", 175, 42, "FLOOR"),
    ("Hull pottery vase, matte green", "1940s", 62, 9, "B3"),
    ("Wool camp blanket, striped", "1940s", 68, 10, "ATTIC"),
    ("Silver tray, monogrammed", "1920s", 105, 24, "B6"),
    ("Pressed tin ceiling panel, framed", "c. 1900", 95, 18, "FLOOR"),
    ("Aluminum Christmas tree, 4 ft", "1960s", 125, 30, "ATTIC"),
    ("Occupied Japan figurines, pair", "1940s", 34, 4, "B3"),
    ("Coca-Cola serving tray, litho", "1950s", 38, None, "B6"),
]

# The queue. One overdue, one due today, one with no label at all, one
# local pickup with no tracking - the states the screen exists to tell
# apart, rather than five rows of the same parcel.
PARCELS = [
    # Long on purpose. Her real titles run past a hundred characters and
    # share their opening, and a queue row drawn against a forty-character
    # invention looks fine right up until it meets one of these.
    ("Original 1944 WWII Army Air Forces Officer Candidate School "
     "Panoramic Photograph Miami Beach Florida", "Ellis Navarro", 215.0,
     0, True, "9405 5118 9922 3197 4284 90", "4 lb 6 oz"),
    ("Vintage Japanese 1980s Black Otagiri “Crown Iris” lacquer "
     "music box, working", "Bhavneet Chaudhary", 28.0, -2, False,
     None, "2 lb 4 oz"),
    ("Still life with pears, chip at corner", "Moses Okafor", 140.0, 0, False,
     None, "3 lb 1 oz"),
    ("Mid-century brass floor lamp, rewired", "Dana Haas", 95.0, 1, True,
     "9405 5118 9922 3197 4211 07", "11 lb 2 oz"),
    ("Pyrex Primary Colors, 4 pc", "Sam Bright", 68.0, 3, True,
     "9405 5118 9922 3197 4302 55", "6 lb 8 oz"),
    ("Edwardian cut glass decanter", "Sam Bright", 52.0, -1, False,
     None, "5 lb 0 oz"),
    ("Set of 6 jadeite mugs, local pickup", "Nina Alder", 84.0, 2, False,
     None, None),
]


def _swatch(path, caption, n):
    """A stand-in photograph. Not a real one, and it should not look like
    one - a preview that ships with plausible pictures of objects invites
    somebody to think the data is real."""
    from PIL import Image, ImageDraw

    tint = [(122, 108, 86), (96, 104, 92), (110, 96, 104)][n % 3]
    img = Image.new("RGB", (640, 480), tint)
    draw = ImageDraw.Draw(img)
    for x in range(0, 640, 24):
        draw.line([(x, 0), (x, 480)], fill=(tint[0] + 12, tint[1] + 12,
                                            tint[2] + 12), width=1)
    draw.text((24, 24), caption[:34], fill=(240, 236, 228))
    draw.text((24, 44), f"stand-in photograph {n + 1}", fill=(220, 214, 200))
    img.save(path)


def seed(conn, home):
    rng = random.Random(20260910)
    today = datetime.date.today()

    for name in ("FLOOR", "ATTIC", "B1", "B2", "B3", "B4", "B5", "B6"):
        listings.create_bin(conn, name)
    codes = {name: listings.find_bin(conn, name)["code"]
             for name in ("FLOOR", "ATTIC", "B1", "B2", "B3",
                          "B4", "B5", "B6")}

    trip = conn.execute(
        "INSERT INTO trips (store, occurred_at, receipt_total) "
        "VALUES ('GOODWILL 214', ?, 148.50)",
        ((today - datetime.timedelta(days=19)).isoformat(),)).lastrowid
    # A trip nobody wrote the till total for. `unassigned` comes back
    # null rather than zero, and the Today card has to say so in words -
    # "nothing left to attribute" and "we never recorded what the till
    # said" are different answers and only one of them is a job.
    untotalled = conn.execute(
        "INSERT INTO trips (store, occurred_at) VALUES ('ESTATE SALE, MAIN ST', ?)",
        ((today - datetime.timedelta(days=6)).isoformat(),)).lastrowid

    for i, (title, era, ask, paid, bin_name) in enumerate(STOCK):
        listed = today - datetime.timedelta(days=rng.randint(3, 120))
        # A third of the shelf has sold, spread across the last few
        # months so the net-by-month bars have something to draw.
        sold = None
        state = "active"
        if i % 3 == 0:
            sold = listed + datetime.timedelta(days=rng.randint(4, 70))
            if sold <= today:
                state = "sold"
            else:
                sold = None
        conn.execute(
            "INSERT INTO listings (listing_id, title, price, paid, era, "
            "state, source, listed_at, sold_at, category, bin_code, trip_id) "
            "VALUES (?,?,?,?,?,?,'manual',?,?,'Home',?,?)",
            (f"preview-{i}", title, float(ask),
             None if paid is None else float(paid), era, state,
             listed.isoformat(), sold.isoformat() if sold else None,
             codes[bin_name], trip if i % 4 else untotalled))
    conn.commit()

    labels = home / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    for i, (item, buyer, price, offset, printed, track, weight) in \
            enumerate(PARCELS):
        pdf = labels / f"preview{i}_4x6.pdf"
        # Enough of a PDF for the panel to have something to show. The
        # real archive is whatever Facebook attached.
        pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 288 432]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n")
        conn.execute(
            "INSERT INTO sales (message_id, order_id, item, buyer, price, "
            "ship_by, code, ship_to, tracking, weight, service, status, "
            "label_pdf, printed_at, print_count, postage, postage_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'to_ship',?,?,?,?,?)",
            (f"<preview{i}>", f"FB-44{60 + i}-08", item, buyer, price,
             (today + datetime.timedelta(days=offset)).isoformat(),
             cli.allocate_code(conn),
             "2 FICTION RD, SHELBYVILLE IN 46176",
             track, weight, "USPS Ground Advantage", str(pdf),
             (today.isoformat() + "T09:14:00") if printed else None,
             1 if printed else 0,
             # One confirmed, one estimated, the rest unknown - the three
             # things the detail pane has to phrase differently.
             8.95 if i == 0 else (12.40 if i == 2 else None),
             "confirmed" if i == 0 else ("estimated" if i == 2 else None)))
    conn.commit()

    photos = home / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO photos (path, sha256, taken_at, created_at) "
                 "VALUES ('photos/receipt.jpg', 'deadbeef', ?, ?)",
                 (today.isoformat(), today.isoformat()))

    # Three drafts, one of them already written up, so the writer screen
    # has a filled body and two empty ones to move between. Drafts have
    # no `listed_at`: they were never for sale, which is the whole reason
    # `v_listing_perf` keeps them out of sell-through.
    drafts = conn.execute(
        "SELECT id, title FROM listings WHERE state='active' "
        "ORDER BY id DESC LIMIT 3").fetchall()
    for n, row in enumerate(drafts):
        conn.execute(
            "UPDATE listings SET state='draft', listed_at=NULL, "
            "description=? WHERE id=?",
            ("Sound condition with age-appropriate wear, no chips or "
             "cracks. Measures roughly 14 by 9 inches. Collection from "
             "the north side, or I can meet partway. No holds."
             if n == 0 else None, row["id"]))
        for shot in range(3 if n == 0 else 1):
            path = photos / f"draft{row['id']}-{shot}.png"
            _swatch(path, row["title"], shot)
            conn.execute(
                "INSERT INTO photos (path, sha256, taken_at, created_at, "
                "listing_id) VALUES (?,?,?,?,?)",
                (str(path), f"draft{row['id']}{shot}",
                 today.isoformat(), today.isoformat(), row["id"]))
    conn.commit()

    listings.refresh(conn)
    cli.ensure_inventory_codes(conn)
    conn.commit()


class PreviewHandler(web.Handler):
    """The real handler, plus one door that only exists here.

    `GET /preview-login` issues the session cookie and redirects to the
    desk, so a headless browser can be pointed at a screen without a
    form to fill in. It is a preview affordance and it lives in this
    file on purpose - `web.py` has exactly one way in and it is a POST
    with a password, which is the whole point of it.

    This server only ever holds invented rows in a temporary directory,
    and the password is printed to the terminal a line above the URL.
    """

    def do_GET(self):
        if self.path.split("#")[0].split("?")[0] != "/preview-login":
            return web.Handler.do_GET(self)
        token = web.issue_token(self.cfg, days=1)
        self.send_response(302)
        self.send_header("Location", "/desk")
        # `_cookie_header` returns the (name, value) pair `_send` passes
        # through as an extra header; only the value goes here.
        self.send_header(*self._cookie_header(token, 86400))
        self.send_header("Content-Length", "0")
        self.end_headers()


class PreviewServer(web.Server):
    def __init__(self, addr, cfg):
        super().__init__(addr, cfg)
        self.RequestHandlerClass = PreviewHandler


def main():
    import tempfile

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=0,
                    help="default 0, meaning any free port")
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = pathlib.Path(tmp)
        conn = cli.connect_db(home)
        seed(conn, home)

        cfg = dict(cli.DEFAULTS)
        cfg.update({"home": str(home),
                    "web_password_hash": web.hash_password(PASSWORD),
                    "web_secure_cookie": "no"})
        srv = PreviewServer((args.bind, args.port), cfg)
        port = srv.server_address[1]
        print(f"    desk    http://{args.bind}:{port}/desk")
        print(f"    (auto)  http://{args.bind}:{port}/preview-login")
        print(f"    phone   http://{args.bind}:{port}/")
        print(f"    password: {PASSWORD}")
        print("    Ctrl-C to stop. Nothing here is real.", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()


if __name__ == "__main__":
    main()

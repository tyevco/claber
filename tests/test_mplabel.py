"""Regression tests for the parts that were verified against real data.

Every fixture here is synthetic. The real label PDF carries a buyer's
home address and the real database carries customer names, so neither
belongs in version control - see tests/fixtures/make_label.py, which
reproduces the exact page geometry of a real Marketplace label with
invented names and an unused tracking number.
"""

import email
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from mplabel import label, listings, mailparse, savedpage, sheets

FIXTURES = Path(__file__).parent / "fixtures"
LABEL_PDF = FIXTURES / "label_sample.pdf"
EMAIL_EML = FIXTURES / "label_email.eml"


@pytest.fixture
def msg():
    return email.message_from_bytes(EMAIL_EML.read_bytes())


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sales (id INTEGER PRIMARY KEY, listing_id TEXT,
            item TEXT, price REAL, received_at TEXT, buyer TEXT,
            tracking TEXT, ship_to TEXT, weight TEXT, service TEXT,
            status TEXT, printed_at TEXT, ship_by TEXT);
    """)
    conn.executescript(listings.SCHEMA)
    return conn


# ----------------------------------------------------------------- label

def test_output_is_exactly_4x6(tmp_path):
    """Not 4.06x6.06. At 203dpi the raw backends clip anything wider than
    812 dots, and an extra row of pixels ejects a second blank label."""
    out = tmp_path / "out.pdf"
    info = label.to_4x6(LABEL_PDF, out)
    assert info["size_in"] == (4.0, 6.0)


def test_rotation_detected_from_text_matrix(tmp_path):
    info = label.to_4x6(LABEL_PDF, tmp_path / "o.pdf")
    assert info["rotation"] == 90


def test_crop_bbox_matches_real_label_geometry(tmp_path):
    """(90,450)-(522,738) is where Facebook's labels actually sit on the
    letter page - 432x288pt. If this moves, the detector is guessing."""
    info = label.to_4x6(LABEL_PDF, tmp_path / "o.pdf")
    assert info["crop_bbox"] == (90.0, 450.0, 522.0, 738.0)


def test_label_fields_extracted_after_rotation(tmp_path):
    """Text extraction on the un-rotated source comes back mirrored, so
    these must be read from the cropped output."""
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    got = label.extract_label_fields(out)
    assert got["tracking"] == "9400100000000000000000"
    assert got["weight"] == "1 lb 15 oz"
    assert got["service"] == "USPS Ground Advantage"
    assert "SHELBYVILLE IN 46176-0002" in got["ship_to"]
    assert "SAM SAMPLE" in got["ship_to"]


def test_ship_to_is_recipient_not_sender(tmp_path):
    """Two address blocks are on the label. Picking the wrong one ships
    every parcel back to her."""
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    got = label.extract_label_fields(out)
    assert "JANE TESTER" not in got["ship_to"]


def test_oversized_content_rejected(tmp_path):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    big = tmp_path / "big.pdf"
    c = canvas.Canvas(str(big), pagesize=letter)
    c.rect(20, 20, 550, 700)
    c.showPage()
    c.save()
    with pytest.raises(ValueError, match="larger than"):
        label.to_4x6(big, tmp_path / "o.pdf")


# ------------------------------------------------------------- rasterise

@pytest.mark.parametrize("dpi,w,h", [(203, 812, 1218), (300, 1200, 1800)])
def test_bitmap_pinned_to_exact_dots(tmp_path, dpi, w, h):
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    data, width_px, width_bytes, height = printers.render_bitmap(out, dpi)
    assert (width_px, height) == (w, h)
    assert len(data) == width_bytes * height


def test_tspl_job_structure(tmp_path):
    import re
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    job = printers.build_tspl(out, 203, darkness=8, speed=4, media="gap")

    assert b"GAP 0.12,0" in job, "die-cut stock needs gap detection"
    assert b"DENSITY 8" in job
    assert job.rstrip().endswith(b"PRINT 1,1")

    m = re.search(rb"BITMAP (\d+),(\d+),(\d+),(\d+),(\d+),", job)
    width_bytes, height = int(m.group(3)), int(m.group(4))
    payload = job[m.end():-len(b"\r\nPRINT 1,1\r\n")]
    assert len(payload) == width_bytes * height


def test_tspl_continuous_media_differs(tmp_path):
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    assert b"GAP 0,0" in printers.build_tspl(out, 203, media="continuous")
    assert b"BLINE" in printers.build_tspl(out, 203, media="blackmark")


def test_tspl_and_zpl_bit_polarity_are_opposite(tmp_path):
    """TSPL prints on a clear bit; ZPL prints on a set bit. Getting this
    backwards produces a solid black label."""
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    normal, _, _, _ = printers.render_bitmap(out, 203, invert=False)
    inverted, _, _, _ = printers.render_bitmap(out, 203, invert=True)
    assert normal != inverted
    ink = sum(bin(b).count("1") for b in normal)
    assert 0.02 < ink / (len(normal) * 8) < 0.5, "should be mostly white"


# --------------------------------------------------------------- esc/pos

def _escpos_rasters(job):
    """Pull every GS v 0 raster block out of a job.

    Returns [(mode, width_bytes, rows, data), ...]. Walking the blocks by
    their own declared sizes is the point: if a header disagrees with its
    payload the walk desynchronises and the assertions below fail."""
    blocks, i = [], 0
    while True:
        i = job.find(b"\x1dv0", i)
        if i < 0:
            return blocks
        mode = job[i + 3]
        xl, xh, yl, yh = job[i + 4:i + 8]
        width_bytes = xl + (xh << 8)
        rows = yl + (yh << 8)
        start = i + 8
        end = start + width_bytes * rows
        blocks.append((mode, width_bytes, rows, job[start:end]))
        i = end


def test_escpos_job_structure(tmp_path):
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    job = printers.build_escpos(out, 203)

    assert job.startswith(b"\x1b@"), "job must reset the printer first"
    blocks = _escpos_rasters(job)
    assert blocks, "no GS v 0 raster blocks in the job"
    for mode, width_bytes, rows, data in blocks:
        assert mode == 0
        assert len(data) == width_bytes * rows, "header disagrees with payload"
    assert job.endswith(b"\x0c"), "die-cut stock advances with a form feed"


def test_escpos_bands_cover_every_row_exactly_once(tmp_path):
    """Banding is the risky part - a slip drops or repeats whole rows."""
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    expected, _px, width_bytes, height = printers.render_bitmap(out, 203)

    for band_rows in (128, 256, 1, height, height * 2):
        blocks = _escpos_rasters(printers.build_escpos(out, 203,
                                                       band_rows=band_rows))
        assert sum(b[2] for b in blocks) == height, band_rows
        assert all(b[1] == width_bytes for b in blocks), band_rows
        assert b"".join(b[3] for b in blocks) == expected, band_rows


def test_escpos_prints_on_a_set_bit_like_zpl(tmp_path):
    """ESC/POS and ZPL print on a set bit; TSPL prints on a clear one.
    Getting this backwards produces a solid black label."""
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    normal, _, _, _ = printers.render_bitmap(out, 203, invert=False)
    inverted, _, _, _ = printers.render_bitmap(out, 203, invert=True)

    raster = b"".join(b[3] for b in
                      _escpos_rasters(printers.build_escpos(out, 203)))
    assert raster == normal
    assert raster != inverted


def test_escpos_right_edge_padding_is_white(tmp_path):
    """812 dots is not a byte boundary. The 4 spare bits per row must stay
    clear, or every label carries a black stripe down its right edge."""
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    _data, width_px, width_bytes, height = printers.render_bitmap(out, 203)
    spare = width_bytes * 8 - width_px
    assert spare == 4
    mask = (1 << spare) - 1

    raster = b"".join(b[3] for b in
                      _escpos_rasters(printers.build_escpos(out, 203)))
    for y in range(height):
        assert not raster[y * width_bytes + width_bytes - 1] & mask, y


def test_escpos_continuous_media_does_not_form_feed(tmp_path):
    from mplabel import printers
    out = tmp_path / "o.pdf"
    label.to_4x6(LABEL_PDF, out)
    job = printers.build_escpos(out, 203, media="continuous")
    assert not job.endswith(b"\x0c")
    with pytest.raises(ValueError, match="unknown media"):
        printers.build_escpos(out, 203, media="nonsense")


@pytest.mark.parametrize("cmd", [
    ["selftest"],
    # -o keeps the converted PDF in tmp_path: without it cmd_file writes
    # label_sample_4x6.pdf next to the fixture, and .gitignore's
    # !tests/fixtures/** exception means it lands in the next commit.
    ["file", str(LABEL_PDF), "-o"],
])
def test_printer_commands_do_not_open_the_database(monkeypatch, tmp_path,
                                                   cmd):
    """selftest and file never touch the database, so they must not open
    one. They used to, which meant an unwritable home directory stopped
    you testing the printer:

        sqlite3.OperationalError: unable to open database file
    """
    from mplabel import cli, printers

    def boom(home):
        pytest.fail("opened the database for a command that does not need it")

    monkeypatch.setattr(cli, "connect_db", boom)
    monkeypatch.setattr(cli, "load_config", lambda p: dict(cli.DEFAULTS))
    # Stub both, so this keeps testing dispatch order rather than whichever
    # language happens to be the default today.
    for fn in ("escpos_selftest", "tspl_selftest"):
        monkeypatch.setattr(printers, fn, lambda *a, **k: None)
    argv = ["mplabel"] + cmd
    if cmd[-1] == "-o":
        argv.append(str(tmp_path / "out.pdf"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert not list(FIXTURES.glob("*_4x6.pdf")), "wrote into tests/fixtures"


def test_write_raw_survives_fsync_failing(tmp_path, monkeypatch):
    """Observed on the G4: the label prints, then fsync on /dev/usb/lp0
    raises OSError 22, and the caller records a successful print as a
    failure. Character devices do not implement fsync."""
    from mplabel import printers

    dev = tmp_path / "lp0"
    dev.write_bytes(b"")

    def einval(fd):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(printers.os, "fsync", einval)
    printers._write_raw(str(dev), b"SIZE 4,6", settle=0)
    assert dev.read_bytes() == b"SIZE 4,6", "the job must still be written"


def test_probe_only_suggests_backends_that_exist():
    """probe used to print `set printer_backend = esc/pos`, which is not a
    backend - following its own advice exited with 'Unknown backend'."""
    from mplabel import printers
    for lang, backend in printers.LANGUAGE_BACKENDS.items():
        assert backend in printers.BACKENDS, f"{lang} -> {backend!r} missing"
    assert printers.LANGUAGE_BACKENDS["ESC/POS"] == "escpos"


# ------------------------------------------------------------ mailparse

def test_email_is_recognised(msg):
    assert mailparse.is_label_email(msg)


def test_email_fields(msg):
    got = mailparse.parse(msg)
    assert got["buyer"] == "Sam Sample"
    assert got["item"] == "Hand-thrown stoneware vase"
    assert got["price"] == 15.0
    assert got["listing_id"] == "2379911152536775"
    assert got["order_id"] == "1094882736451203"


def test_item_title_is_not_boilerplate(msg):
    """The block before the price must not be 'To be shipped' or a
    numbered instruction from the packing advice."""
    got = mailparse.parse(msg)
    assert got["item"].lower() not in ("to be shipped", "shipped")


def test_attachment_round_trips(msg):
    name, blob = mailparse.attachment(msg)
    assert name.endswith(".pdf")
    assert blob.startswith(b"%PDF")


@pytest.mark.parametrize("fragment,base,expected", [
    ("Fri, Sep 4", datetime(2026, 8, 28), "2026-09-04"),
    ("Thu, Jan 2", datetime(2026, 12, 26), "2027-01-02"),   # year rollover
    ("Mon, Dec 29", datetime(2026, 12, 26), "2026-12-29"),
    ("Jan 2", datetime(2026, 12, 26), "2027-01-02"),        # no weekday
    ("Wed, March 4", datetime(2027, 2, 25), "2027-03-04"),  # full month
    ("Sunday, Jun 7", datetime(2026, 6, 1), "2026-06-07"),
    ("Sat, Feb 29", datetime(2028, 2, 20), "2028-02-29"),   # leap day
])
def test_ship_by_year_inference(fragment, base, expected):
    """Facebook writes 'Fri, Sep 4' with no year."""
    assert mailparse._resolve_ship_by(fragment, base) == expected


# ------------------------------------------------------------- listings

@pytest.mark.parametrize("subject,kind", [
    ("Shipping label for your Marketplace order", "shipping_label"),
    ("You sold Oak side table", "sold"),
    ("Your listing is live", "listed"),
    ("New message about Wool rug", "inquiry"),
    ("Your listing has expired", "expired"),
    ("Completely unrelated newsletter", None),
    # Subject shapes taken from the real mailbox. Titles are invented.
    ("New Marketplace order for Brass Candlestick Pair", "sold"),
    ("\U0001f4ec Sam sent you a message", "inquiry"),
    # ...and the buyer side, which must not read as a sale.
    ("You placed an order: Blue Ceramic Vase", "purchase"),
    ("Offer submitted: Blue Ceramic Vase", "purchase"),
    ("Confirm if you received your order: Blue Ceramic Vase", "purchase"),
])
def test_subject_classification(subject, kind):
    assert listings.classify(subject) == kind


def test_her_purchases_do_not_become_listings(db):
    """The same mailbox carries what she buys. Those emails hold the
    *seller's* listing id, so counting them would invent listings that were
    never for sale and drag sell-through down."""
    listings.record_event(db, "<buy1>", "2026-08-01T10:00:00", "purchase",
                          "You placed an order: Blue Ceramic Vase",
                          listing_id="7777")
    listings.record_event(db, "<sale1>", "2026-08-02T10:00:00", "sold",
                          "New Marketplace order for Brass Candlestick Pair",
                          listing_id="1234")
    listings.apply_events(db)

    ids = [r[0] for r in db.execute("SELECT listing_id FROM listings")]
    assert ids == ["1234"], "a purchase leaked into the listings table"
    state = db.execute("SELECT state FROM listings WHERE listing_id='1234'"
                       ).fetchone()[0]
    assert state == "sold", "a Marketplace order is a sale"


def test_upsert_fills_blanks_without_clobbering(db):
    listings.upsert_listing(db, "L1", "email", title="Lamp", price=40.0)
    listings.upsert_listing(db, "L1", "dyi", title="Different", category="Home")
    row = db.execute("SELECT * FROM listings WHERE listing_id='L1'").fetchone()
    assert row["title"] == "Lamp", "existing value must win"
    assert row["category"] == "Home", "blank must be filled"


def test_sold_state_is_terminal(db):
    listings.upsert_listing(db, "L1", "email", state="sold")
    listings.upsert_listing(db, "L1", "email", state="active")
    row = db.execute("SELECT state FROM listings WHERE listing_id='L1'").fetchone()
    assert row["state"] == "sold"


def test_days_to_sell_and_sell_through(db):
    listings.upsert_listing(db, "A", "t", title="Fast", price=20.0,
                            listed_at="2026-01-01", sold_at="2026-01-04",
                            state="sold")
    listings.upsert_listing(db, "B", "t", title="Slow", price=22.0,
                            listed_at="2026-01-01", state="active")
    listings.build_views(db)
    perf = {r["listing_id"]: r for r in db.execute("SELECT * FROM v_listing_perf")}
    assert perf["A"]["days_to_sell"] == 3
    assert perf["B"]["days_to_sell"] is None
    band = db.execute("SELECT * FROM v_price_band WHERE price_band='$10-25'").fetchone()
    assert band["listed"] == 2 and band["sold"] == 1
    assert band["sell_through_pct"] == 50.0


def test_csv_import(db):
    n = listings.import_csv(db, FIXTURES / "listings.csv")
    assert n == 2
    row = db.execute("SELECT * FROM listings WHERE listing_id='CSV001'").fetchone()
    assert row["state"] == "sold" and row["price"] == 25.0


def test_dyi_import_walks_unknown_shape(db):
    n, examined = listings.import_dyi(db, FIXTURES / "dyi_export.zip")
    assert examined == 1, "must ignore non-marketplace files"
    assert n == 2
    titles = {r[0] for r in db.execute("SELECT title FROM listings")}
    assert "Brass candlesticks" in titles
    assert "Rattan basket" in titles, "listing without an id must still import"


# ------------------------------------------------------------ savedpage

def test_saved_page_extracts_all_listings():
    rows, stats = savedpage.extract(FIXTURES / "selling_page.html")
    assert stats["listings"] == 4
    assert stats["unparseable_blocks"] == 1, "broken block counted, not hidden"


def test_saved_page_states():
    rows, _ = savedpage.extract(FIXTURES / "selling_page.html")
    by_id = {r["listing_id"]: r for r in rows}
    assert by_id["2379911152536775"]["state"] == "sold"
    assert by_id["1188273645009112"]["state"] == "active"
    assert by_id["9922110088776655"]["state"] == "expired"


def test_price_offset_not_read_as_dollars():
    """amount_with_offset is in cents. Reading it raw turns $15 into
    $1500 and wrecks every average downstream."""
    rows, _ = savedpage.extract(FIXTURES / "selling_page.html")
    by_id = {r["listing_id"]: r for r in rows}
    assert by_id["2379911152536775"]["price"] == 15.0
    assert by_id["9922110088776655"]["price"] == 95.5


def test_price_offset_helper_directly():
    assert savedpage._price({"amount_with_offset": "1500"}) == 15.0
    assert savedpage._price({"amount": "15", "amount_with_offset": "1500"}) == 15.0
    assert savedpage._price("$95.50") == 95.5
    assert savedpage._price(None) is None


def test_unrelated_json_not_treated_as_listing():
    """The page has a blob with 'price' and 'title' keys that is not a
    listing. Strong-key guard must exclude it."""
    rows, _ = savedpage.extract(FIXTURES / "selling_page.html")
    assert all(r["title"] != "nope" for r in rows)


def test_console_snippet_json_imports(tmp_path, db):
    """CONSOLE_SNIPPET downloads a bare .json array, not a page. That has
    no <script> tags, so it used to parse to zero listings and blame the
    scrolling. Its `listed_at` key is not one of Facebook's own creation
    time spellings either."""
    import json as _json

    rows = [
        {"listing_id": "111", "title": "Brass Candlestick Pair",
         "price": 38.0, "listed_at": 1756382400,
         "is_sold": False, "is_live": True},
        {"listing_id": "222", "title": "Walnut Mantel Clock",
         "price": 120.0, "listed_at": 1753704000,
         "is_sold": True, "is_live": False},
    ]
    src = tmp_path / "marketplace-listings.json"
    src.write_text(_json.dumps(rows), encoding="utf-8")

    found, stats = savedpage.extract(src)
    assert stats["listings"] == 2, stats
    by_id = {r["listing_id"]: r for r in found}
    assert by_id["111"]["title"] == "Brass Candlestick Pair"
    assert by_id["111"]["state"] == "active"
    assert by_id["222"]["state"] == "sold"
    assert by_id["111"]["price"] == 38.0
    assert by_id["111"]["listed_at"], "the listed_at key must survive"

    savedpage.import_saved(db, src)
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2


def test_card_shaped_records_import(tmp_path, db):
    """What the snippet gets off a rendered card: no creation time, and
    state only from a Sold badge. It still has to import."""
    import json as _json

    src = tmp_path / "cards.json"
    src.write_text(_json.dumps([
        {"listing_id": "333", "title": "Oak Bookcase", "price": 65.0,
         "is_sold": False, "is_live": True},
    ]), encoding="utf-8")

    found, stats = savedpage.extract(src)
    assert stats["listings"] == 1, stats
    assert found[0]["state"] == "active"
    savedpage.import_saved(db, src)
    row = db.execute("SELECT title, price, state FROM listings").fetchone()
    assert tuple(row) == ("Oak Bookcase", 65.0, "active")


def test_state_override_for_a_single_tab_capture(tmp_path, db):
    """A capture from the Sold tab may carry no per-card Sold badge, so
    every row would import as active and sell-through would come out
    backwards. --state says what the file is."""
    import json as _json

    src = tmp_path / "sold.json"
    src.write_text(_json.dumps([
        {"title": "Walnut Mantel Clock", "price": 120.0,
         "is_sold": False, "is_live": True},
    ]), encoding="utf-8")

    savedpage.import_saved(db, src, state="sold")
    assert db.execute("SELECT state FROM listings").fetchone()[0] == "sold"

    # And the terminal-state rule still holds: a later active-tab capture
    # of the same item must not resurrect it.
    savedpage.import_saved(db, src)
    assert db.execute("SELECT state FROM listings").fetchone()[0] == "sold"


def test_snippet_and_parser_agree_on_key_names():
    """Nothing at runtime checks that CONSOLE_SNIPPET emits what extract()
    reads - a rename on either side would just quietly yield 0 listings."""
    snippet = savedpage.CONSOLE_SNIPPET
    for key in ("listing_id", "title", "price", "listed_at",
                "is_sold", "is_live"):
        assert key in snippet, key
    # is_sold / is_live are what _looks_like_listing keys off, so a card
    # with no marketplace_* fields still registers as a listing.
    assert {"is_sold", "is_live"} <= set(savedpage.STRONG_KEYS)


def test_saved_page_import_populates_analytics(db):
    savedpage.import_saved(db, FIXTURES / "selling_page.html")
    listings.build_views(db)
    unsold_priced = db.execute(
        "SELECT COUNT(*) FROM v_listing_perf "
        "WHERE state != 'sold' AND price IS NOT NULL").fetchone()[0]
    assert unsold_priced == 2, "sell-through needs prices on unsold stock"


# --------------------------------------------------------------- sheets

def test_sheet_payload_builds_without_credentials(db):
    listings.upsert_listing(db, "A", "t", title="Lamp", price=40.0,
                            listed_at="2026-01-01", state="active")
    listings.build_views(db)
    counts = sheets.sync(db, None, dry_run=True)
    assert set(counts) == {"Sales", "Listings", "By price band",
                           "Monthly", "Aging"}


def test_sheet_tabs_have_matching_header_widths(db):
    listings.build_views(db)
    for name, (sql, headers) in sheets.TABS.items():
        cur = db.execute(sql)
        assert len(cur.description) == len(headers), f"{name} column mismatch"

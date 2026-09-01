"""Regression tests for the parts that were verified against real data.

Every fixture here is synthetic. The real label PDF carries a buyer's
home address and the real database carries customer names, so neither
belongs in version control - see tests/fixtures/make_label.py, which
reproduces the exact page geometry of a real Marketplace label with
invented names and an unused tracking number.
"""

import csv
import email
import importlib.util
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from mplabel import label, listings, mailparse, savedpage, sheets, supvan

FIXTURES = Path(__file__).parent / "fixtures"
LABEL_PDF = FIXTURES / "label_sample.pdf"
EMAIL_EML = FIXTURES / "label_email.eml"


@pytest.fixture
def msg():
    return email.message_from_bytes(EMAIL_EML.read_bytes())


@pytest.fixture
def db():
    # The real schemas, not a hand-written subset: a trimmed copy drifted
    # from cli.SCHEMA and lost message_id, so tests passed against a table
    # the code would never meet.
    from mplabel import cli
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(cli.SCHEMA)
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


# ----------------------------------------------------------- parcel code

def _quadrant_ink(pdf, dpi=203):
    """Ink per corner of the page *as printed*, as a fraction of each area.

    Rendered, not reasoned about: the page is stored landscape with
    /Rotate 90, so page space and printed space disagree, and the only
    honest way to know where the code lands is to look at the output."""
    from mplabel import printers
    data, width_px, width_bytes, height = printers.render_bitmap(pdf, dpi)
    half_w, half_h = width_px // 2, height // 2

    def dark(x_from, x_to, y_from, y_to):
        n = 0
        for y in range(y_from, y_to):
            row = y * width_bytes
            for x in range(x_from, x_to):
                if data[row + (x >> 3)] & (0x80 >> (x & 7)):
                    n += 1
        return n / max(1, (x_to - x_from) * (y_to - y_from))

    # Rendered images put y=0 at the top.
    return {"tl": dark(0, half_w, 0, half_h),
            "tr": dark(half_w, width_px, 0, half_h),
            "bl": dark(0, half_w, half_h, height),
            "br": dark(half_w, width_px, half_h, height)}


def test_code_lands_in_the_printed_top_right(tmp_path):
    """The whole point of the feature: the code has to be findable in the
    corner of the paper, not merely present in the file somewhere."""
    plain = tmp_path / "plain.pdf"
    stamped = tmp_path / "stamped.pdf"
    label.to_4x6(LABEL_PDF, plain)
    label.stamp_code(plain, stamped, "042")

    before, after = _quadrant_ink(plain), _quadrant_ink(stamped)
    assert after["tr"] > before["tr"], "nothing was added to the top right"
    for corner in ("tl", "bl", "br"):
        assert abs(after[corner] - before[corner]) < 0.001, \
            f"the stamp bled into the {corner} corner"


def test_stamped_label_is_still_exactly_4x6(tmp_path):
    """An oversized page is 824 dots at 203dpi, wider than the print head,
    and the overflow ejects a second near-blank label."""
    plain = tmp_path / "plain.pdf"
    stamped = tmp_path / "stamped.pdf"
    label.to_4x6(LABEL_PDF, plain)
    label.stamp_code(plain, stamped, "042")

    from mplabel import printers
    for pdf in (plain, stamped):
        _d, w, _wb, h = printers.render_bitmap(pdf, 203)
        assert (w, h) == (812, 1218)


def test_stamp_does_not_disturb_the_label_fields(tmp_path):
    """The code is three digits on a page full of numbers. If it ever gets
    stamped before extraction, this is what catches it."""
    plain = tmp_path / "plain.pdf"
    stamped = tmp_path / "stamped.pdf"
    label.to_4x6(LABEL_PDF, plain)
    label.stamp_code(plain, stamped, "042")
    assert label.extract_label_fields(stamped) == \
        label.extract_label_fields(plain)


def _pending_args(**kw):
    import argparse
    return argparse.Namespace(**{"since": None, "all": False,
                                 "dry_run": True, **kw})


def _pending_rows(db, tmp_path, capsys, **kw):
    from mplabel import cli
    cli.cmd_pending({}, db, _pending_args(**kw))
    return capsys.readouterr().out


def test_pending_defaults_to_today(db, tmp_path, capsys):
    """The poller looks back days. Older labels may already have been
    printed and posted by hand, and reprinting those wastes stock and puts
    a second label on a parcel that has gone."""
    from mplabel import cli

    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    today = datetime.now().strftime("%Y-%m-%d")
    for mid, item, when in (("<old>", "Last Week Lamp", "2026-08-21T09:00:00-07:00"),
                            ("<new>", "Today Vase", f"{today}T09:00:00-07:00")):
        db.execute("INSERT INTO sales (message_id, item, received_at, "
                   "label_pdf) VALUES (?,?,?,?)", (mid, item, when, str(pdf)))
    db.commit()

    out = _pending_rows(db, tmp_path, capsys)
    assert "Today Vase" in out
    assert "Last Week Lamp" not in out

    out = _pending_rows(db, tmp_path, capsys, all=True)
    assert "Last Week Lamp" in out and "Today Vase" in out

    out = _pending_rows(db, tmp_path, capsys, since="2026-08-20")
    assert "Last Week Lamp" in out


def test_pending_ignores_printed_and_shipped(db, tmp_path, capsys):
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    today = datetime.now().strftime("%Y-%m-%d")
    db.executemany(
        "INSERT INTO sales (message_id, item, received_at, label_pdf, "
        "printed_at, status) VALUES (?,?,?,?,?,?)",
        [("<a>", "Already Printed", f"{today}T09:00:00-07:00", str(pdf),
          "2026-08-28T10:00:00", "printed"),
         ("<b>", "Already Shipped", f"{today}T09:00:00-07:00", str(pdf),
          None, "shipped"),
         ("<c>", "Still Waiting", f"{today}T09:00:00-07:00", str(pdf),
          None, "to_ship")])
    db.commit()

    out = _pending_rows(db, tmp_path, capsys)
    assert "Still Waiting" in out
    assert "Already Printed" not in out and "Already Shipped" not in out


def test_pending_dry_run_prints_nothing(db, tmp_path, capsys, monkeypatch):
    from mplabel import cli

    sent = []
    monkeypatch.setattr(cli, "print_label",
                        lambda *a, **k: sent.append(a))
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute("INSERT INTO sales (message_id, item, received_at, label_pdf) "
               "VALUES ('<a>','Vase',?,?)",
               (f"{today}T09:00:00-07:00", str(pdf)))
    db.commit()

    cli.cmd_pending({}, db, _pending_args(dry_run=True))
    assert sent == [], "a dry run must not reach the printer"

    cli.cmd_pending({}, db, _pending_args(dry_run=False))
    assert len(sent) == 1
    assert db.execute("SELECT printed_at FROM sales").fetchone()[0]


def _tiny_alphabet(monkeypatch, alphabet="AB", length=1):
    """Shrink the code space so exhaustion is testable at all. With the
    real 32^3 the interesting cases never come up by chance."""
    from mplabel import cli
    monkeypatch.setattr(cli, "CODE_ALPHABET", alphabet)
    monkeypatch.setattr(cli, "CODE_LENGTH", length)


def test_code_never_collides_with_an_unshipped_parcel(db, monkeypatch):
    """Two boxes in the hall with the same code on them is the one outcome
    that makes the whole feature worse than useless."""
    from mplabel import cli

    _tiny_alphabet(monkeypatch, "ABCD", 1)
    for n, code in enumerate("ABC"):
        db.execute("INSERT INTO sales (message_id, code, status) "
                   "VALUES (?,?,'printed')", (f"<m{n}>", code))
    db.commit()
    # Only one code left, so the random guesses all miss and the
    # exhaustive walk has to be the thing that answers.
    for _ in range(20):
        assert cli.allocate_code(db) == "D"


def test_a_shipped_parcel_frees_its_code(db, monkeypatch):
    """Codes are scoped to what is still going out, or they would run out."""
    from mplabel import cli

    _tiny_alphabet(monkeypatch, "AB", 1)
    db.execute("INSERT INTO sales (message_id, code, status) "
               "VALUES ('<m1>','A','printed')")
    db.execute("INSERT INTO sales (message_id, code, status) "
               "VALUES ('<m2>','B','shipped')")
    db.commit()
    assert cli.allocate_code(db) == "B"


def test_code_avoids_characters_that_get_misread(db):
    """I, L, O and U read as 1, 1, 0 and V on thermal stock across a room,
    and the code is meant to be read off a box, not squinted at."""
    from mplabel import cli

    assert not set("ILOU") & set(cli.CODE_ALPHABET)
    for _ in range(50):
        code = cli.allocate_code(db)
        assert len(code) == cli.CODE_LENGTH
        assert set(code) <= set(cli.CODE_ALPHABET)


def test_letters_are_stamped_and_measured(tmp_path):
    """Helvetica letters are not one width - W is nearly twice I - so the
    white patch has to be measured or a wide code spills off it."""
    assert label._text_width("WWW", 8) > label._text_width("111", 8) * 1.5, \
        "letter widths are not being measured"

    plain = tmp_path / "plain.pdf"
    label.to_4x6(LABEL_PDF, plain)
    before = _quadrant_ink(plain)
    for code in ("W7X", "042", "WWW"):
        out = tmp_path / f"{code}.pdf"
        label.stamp_code(plain, out, code)
        after = _quadrant_ink(out)
        assert after["tr"] > before["tr"], code
        for corner in ("tl", "bl", "br"):
            assert abs(after[corner] - before[corner]) < 0.001, \
                f"{code} bled into the {corner} corner"


def test_stamp_uppercases_a_lowercase_code(tmp_path):
    plain = tmp_path / "plain.pdf"
    label.to_4x6(LABEL_PDF, plain)
    assert label.stamp_code(plain, tmp_path / "o.pdf", "w7x")["code"] == "W7X"


def test_a_relisted_item_can_sell_twice(db):
    """A buyer cancelled and someone else bought the same item. The second
    label email carries the same listing_id and a new order_id - and was
    being discarded, so the second buyer's label never printed and the
    record still named the first buyer."""
    from mplabel import cli

    first = {"message_id": "<m1>", "order_id": "111", "listing_id": "L1",
             "buyer": "Alice", "item": "Brass Lamp"}
    assert not cli.already_seen(db, first["message_id"], first["order_id"])
    cli.upsert(db, first)

    second = {"message_id": "<m2>", "order_id": "222", "listing_id": "L1",
              "buyer": "Bob", "item": "Brass Lamp"}
    assert not cli.already_seen(db, second["message_id"], second["order_id"]), \
        "a new order on the same listing is a new sale"
    cli.upsert(db, second)

    buyers = [r[0] for r in db.execute(
        "SELECT buyer FROM sales ORDER BY id")]
    assert buyers == ["Alice", "Bob"], "the second sale was dropped"


def test_the_same_order_is_still_only_printed_once(db):
    """Dropping the listing_id check must not let a resent label email
    print a second time."""
    from mplabel import cli

    rec = {"message_id": "<m1>", "order_id": "111", "listing_id": "L1",
           "buyer": "Alice"}
    cli.upsert(db, rec)
    assert cli.already_seen(db, "<m1>", "111"), "same message"
    assert cli.already_seen(db, "<resent>", "111"), "same order, new email"
    assert not cli.already_seen(db, "<m9>", None), "no order id to match on"


def test_find_sale_prefers_the_live_sale(db):
    """`reprint L1` must not print the cancelled buyer's label - that is a
    parcel posted to the wrong person."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, listing_id, order_id, buyer, "
               "status) VALUES ('<m1>','L1','111','Alice','cancelled')")
    db.execute("INSERT INTO sales (message_id, listing_id, order_id, buyer) "
               "VALUES ('<m2>','L1','222','Bob')")
    db.commit()
    assert cli.find_sale(db, "L1")["buyer"] == "Bob"


def test_a_cancelled_sale_drops_off_and_frees_its_code(db, monkeypatch):
    import argparse
    from mplabel import cli

    _tiny_alphabet(monkeypatch, "AB", 1)
    db.execute("INSERT INTO sales (message_id, listing_id, buyer, code) "
               "VALUES ('<m1>','L1','Alice','A')")
    db.commit()
    cli.cmd_cancel({}, db, argparse.Namespace(ref="L1"))

    assert db.execute("SELECT status FROM sales").fetchone()[0] == "cancelled"
    assert cli.allocate_code(db) in ("A", "B"), "its code is free again"
    rows = db.execute("SELECT 1 FROM sales WHERE status NOT IN "
                      f"({','.join('?' * len(cli.CLOSED_STATUSES))})",
                      cli.CLOSED_STATUSES).fetchall()
    assert not rows, "a cancelled sale is not outstanding"


def test_a_mismatched_label_is_not_printed(db, tmp_path, monkeypatch):
    """The reprint that started this: the row said Opera Glasses, the PDF
    was addressed to someone who had ordered something else. Printing that
    posts a parcel to the wrong person."""
    import argparse
    from mplabel import cli

    pdf = tmp_path / "l.pdf"
    label.to_4x6(LABEL_PDF, pdf)
    real = label.extract_label_fields(pdf)["ship_to"]

    db.execute("INSERT INTO sales (message_id, item, buyer, ship_to, "
               "label_pdf, code) VALUES ('<m1>','Opera Glasses','Alice',"
               "'SOMEONE ELSE, 9 OTHER ST',?,'W7X')", (str(pdf),))
    db.commit()
    row = cli.find_sale(db, "W7X")
    ok, detail = cli.label_belongs_to(row)
    assert not ok and "addressed to" in detail

    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    with pytest.raises(SystemExit, match="refusing to print"):
        cli.cmd_reprint({}, db, argparse.Namespace(ref="W7X", force=False))
    assert sent == []

    # ...and the matching case still prints.
    db.execute("UPDATE sales SET ship_to=? WHERE code='W7X'", (real,))
    db.commit()
    cli.cmd_reprint({}, db, argparse.Namespace(ref="W7X", force=False))
    assert len(sent) == 1


def test_two_orders_for_one_listing_keep_separate_labels(db, tmp_path):
    """End to end on the case that started this. Both emails carry the same
    listing_id; the second is a different order. Before, the second was
    discarded *and* its PDF overwrote the first one's file, so the surviving
    row pointed at the other buyer's label."""
    from mplabel import cli

    raw = EMAIL_EML.read_bytes()
    second = raw.replace(b"1094882736451203", b"2222222222222222") \
                .replace(b"fixture-label-0001", b"fixture-label-0002")
    assert second != raw

    (tmp_path / "labels").mkdir()
    cfg = {"home": str(tmp_path)}
    a = cli.process_message(cfg, db, email.message_from_bytes(raw), False)
    b = cli.process_message(cfg, db, email.message_from_bytes(second), False)
    assert a and b, "the second order was dropped"
    assert a["listing_id"] == b["listing_id"], "same listing, by construction"
    assert a["label_pdf"] != b["label_pdf"], "one label overwrote the other"

    rows = db.execute("SELECT order_id, label_pdf FROM sales "
                      "ORDER BY id").fetchall()
    assert len(rows) == 2
    assert len({r["label_pdf"] for r in rows}) == 2
    for r in rows:
        assert Path(r["label_pdf"]).exists()


# ------------------------------------------------------- inventory labels

def _inv_args(**kw):
    import argparse
    return argparse.Namespace(**{"output": None, "state": "active",
                                 "all": False, **kw})


def test_inventory_codes_are_stable_and_never_reused(db):
    """A parcel code comes back once the parcel ships. An inventory code is
    stuck to a thing on a shelf, so it must stay true for as long as that
    thing exists - including after it sells, or the label on the box in the
    loft starts naming something else."""
    from mplabel import cli

    for i in range(6):
        listings.upsert_listing(db, f"L{i}", "t", title=f"Item {i}",
                                price=10.0 + i, state="active")
    assert cli.ensure_inventory_codes(db) == 6

    codes = {r[0] for r in db.execute(
        "SELECT inventory_code FROM listings")}
    assert len(codes) == 6, "codes collided"
    for c in codes:
        assert len(c) == cli.INVENTORY_CODE_LENGTH
        assert set(c) <= set(cli.CODE_ALPHABET)

    # Running again changes nothing...
    assert cli.ensure_inventory_codes(db) == 0
    assert {r[0] for r in db.execute(
        "SELECT inventory_code FROM listings")} == codes

    # ...and a sold listing keeps its code rather than releasing it.
    db.execute("UPDATE listings SET state='sold' WHERE listing_id='L1'")
    db.commit()
    listings.upsert_listing(db, "NEW", "t", title="Later", state="active")
    cli.ensure_inventory_codes(db)
    new = db.execute("SELECT inventory_code FROM listings WHERE "
                     "listing_id='NEW'").fetchone()[0]
    assert new not in codes, "a sold listing's code was handed out again"


def test_inventory_csv_survives_her_titles(tmp_path, db, capsys):
    """Her titles carry accents and curly quotes. Excel on Windows reads a
    plain utf-8 CSV as mojibake, and whatever it shows is what gets printed
    onto the label."""
    from mplabel import cli

    title = "The Gleaners by Jean-François Millet — Otagiri “Crown”"
    listings.upsert_listing(db, "L1", "t", title=title, price=28.0,
                            state="active")
    out = tmp_path / "inv.csv"
    cli.cmd_inventory({}, db, _inv_args(output=str(out)))

    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "no BOM: Excel will mangle this"
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == ["code", "barcode", "short_title", "title", "price",
                       "state", "listing_id"]
    assert rows[1][3] == title
    assert rows[1][0] == rows[1][1], "barcode column mirrors the code"
    assert len(rows[1][2]) <= 38, "short_title must fit a 48mm label"
    assert rows[1][4] == "28.00"


def test_inventory_defaults_to_what_is_on_the_shelf(tmp_path, db, capsys):
    from mplabel import cli

    listings.upsert_listing(db, "A", "t", title="Still here", state="active")
    listings.upsert_listing(db, "B", "t", title="Gone", state="sold")
    out = tmp_path / "inv.csv"

    cli.cmd_inventory({}, db, _inv_args(output=str(out)))
    body = out.read_text(encoding="utf-8-sig")
    assert "Still here" in body and "Gone" not in body

    cli.cmd_inventory({}, db, _inv_args(output=str(out), all=True))
    body = out.read_text(encoding="utf-8-sig")
    assert "Still here" in body and "Gone" in body


def test_inventory_leaves_a_missing_price_blank(tmp_path, db):
    """Better an empty field on the label than the word None."""
    from mplabel import cli

    listings.upsert_listing(db, "A", "t", title="No price", state="active")
    out = tmp_path / "inv.csv"
    cli.cmd_inventory({}, db, _inv_args(output=str(out)))
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[1][4] == ""


def test_find_sale_by_parcel_code(db):
    """The code is the only handle that is printed on the box, so it has to
    be typeable back in - `list` shows it and nothing else you could use."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, item, listing_id, tracking, "
               "code) VALUES ('<m1>','Lamp','123','9400abc','W7X')")
    db.commit()
    for ref in ("W7X", "w7x", "123", "9400abc"):
        row = cli.find_sale(db, ref)
        assert row is not None and row["item"] == "Lamp", ref
    assert cli.find_sale(db, "nope") is None


def test_ship_by_parcel_code(db):
    import argparse
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, item, code) "
               "VALUES ('<m1>','Lamp','W7X')")
    db.commit()
    cli.cmd_ship({}, db, argparse.Namespace(ref="w7x"))
    assert db.execute("SELECT status FROM sales").fetchone()[0] == "shipped"


def test_the_old_decimal_codes_are_still_valid(db, tmp_path):
    """The decimal codes are a subset of the alphabet, which is what makes
    this a change with no migration - the ~15 parcels already carrying one
    keep working."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, code) VALUES ('<old>', '042')")
    db.commit()
    assert cli.ensure_code(db, "<old>") == "042"

    plain = tmp_path / "plain.pdf"
    label.to_4x6(LABEL_PDF, plain)
    label.stamp_code(plain, tmp_path / "x.pdf", "042")


def test_reprint_keeps_the_same_code(db):
    """The paper, the screen and the sheet have to agree, so allocation
    has to be idempotent."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id) VALUES ('<m1>')")
    db.commit()
    first = cli.ensure_code(db, "<m1>")
    assert first and cli.ensure_code(db, "<m1>") == first
    assert db.execute("SELECT code FROM sales WHERE message_id='<m1>'"
                      ).fetchone()[0] == first


def test_migration_adds_code_to_an_existing_database(tmp_path):
    """Her database already holds real sales, and CREATE TABLE IF NOT
    EXISTS will not add a column to it."""
    from mplabel import cli

    home = tmp_path / "marketplace"
    (home / "labels").mkdir(parents=True)
    old = sqlite3.connect(home / "sales.db")
    old.executescript(
        cli.SCHEMA.replace("code         TEXT,\n", ""))
    old.execute("INSERT INTO sales (message_id, item) VALUES ('<m1>','Lamp')")
    old.commit()
    old.close()

    conn = cli.connect_db(home)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sales)")}
    assert "code" in cols
    row = conn.execute("SELECT item, code FROM sales").fetchone()
    assert row["item"] == "Lamp" and row["code"] is None


@pytest.mark.parametrize("bad", ["04-", "hi there", "", "4.2", "é"])
def test_stamp_rejects_an_unprintable_code(tmp_path, bad):
    """Anything outside the width table would be drawn at a guessed width
    and spill off its own background."""
    plain = tmp_path / "plain.pdf"
    label.to_4x6(LABEL_PDF, plain)
    with pytest.raises(ValueError, match="capital letters"):
        label.stamp_code(plain, tmp_path / "x.pdf", bad)


def test_the_widest_code_still_fits_on_the_label(tmp_path):
    """812 dots is the print head. A code that runs past the edge is
    clipped, and a clipped code is worse than none - it still looks like
    a number."""
    from mplabel import printers

    plain = tmp_path / "plain.pdf"
    label.to_4x6(LABEL_PDF, plain)
    for code in ("WWW", "000", "MQW"):
        out = tmp_path / f"{code}.pdf"
        label.stamp_code(plain, out, code)
        _d, w, _wb, h = printers.render_bitmap(out, 203)
        assert (w, h) == (812, 1218), f"{code} changed the page size"



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


@pytest.mark.parametrize("subject,expected", [
    ("New Marketplace order for Brass Candlestick Pair",
     "Brass Candlestick Pair"),
    ("You sold Oak side table", "Oak side table"),
    ("Shipping label for your Marketplace order", None),
    ("You placed an order: Blue Ceramic Vase", None),
])
def test_title_from_subject(subject, expected):
    assert listings.title_from_subject(subject) == expected


@pytest.mark.parametrize("from_header,ok", [
    ("Facebook Marketplace <noreply@marketplace.facebook.com>", True),
    ("Facebook <notification@facebookmail.com>", True),
    ("Facebook <NoReply@Marketplace.Facebook.Com>", True),
    # Substring matching used to accept all of these.
    ("Facebook Marketplace <noreply@marketplace.facebook.com.example.net>",
     False),
    ("Facebook Marketplace <billing@facebookmail.com.attacker.io>", False),
    ("\"Facebook Marketplace\" <sales@notfacebookmail.com>", False),
    ("Facebook Marketplace <hello@example.com>", False),
])
def test_sender_domain_must_be_facebook(from_header, ok):
    """The IMAP search matches the From header as text, so a display name
    alone gets a message fetched. Everything downstream trusts this: a
    subject reading "New Marketplace order for <item>" becomes a sold
    listing."""
    msg = email.message_from_string(
        f"From: {from_header}\n"
        "Subject: New Marketplace order for Brass Lamp\n\n")
    assert mailparse.is_from_facebook(msg) is ok


class _FakeIMAP:
    """Enough IMAP to test which messages a poll considers.

    `seen` records every message the caller marked read, so a test can
    assert that peeking does not."""

    def __init__(self, ids, gmail_ok=True):
        self.ids = ids
        self.gmail_ok = gmail_ok
        self.queries = []
        self.fetched = []

    def search(self, charset, query):
        self.queries.append(query)
        if query.startswith("(X-GM-RAW") and not self.gmail_ok:
            import imaplib
            raise imaplib.IMAP4.error("unsupported")
        return "OK", [b" ".join(self.ids)]

    def fetch(self, num, spec):
        self.fetched.append((num, spec))
        mid = b"<msg-" + num + b"@marketplace.facebook.com>"
        head = (b"Message-ID: " + mid + b"\r\n"
                b"From: Facebook Marketplace <noreply@marketplace.facebook.com>\r\n"
                b"Subject: Shipping label for your Marketplace order\r\n\r\n")
        return "OK", [(b"1 (BODY[HEADER]", head)]


def test_poll_does_not_filter_on_read_state():
    """She sold nine things at once, Gmail threaded them, and opening the
    conversation marked all nine read - so UNSEEN returned none of them
    and eight labels never printed. Read state cannot gate printing."""
    from mplabel import cli

    imap = _FakeIMAP([b"1", b"2", b"3"])
    ids = cli.candidate_ids(imap, {"lookback_days": "7"}, "imap.gmail.com")
    assert ids == [b"1", b"2", b"3"]
    assert "UNSEEN" not in imap.queries[0]
    assert "newer_than:7d" in imap.queries[0]
    # Deliberately not filtered on the processed label: Gmail's search is
    # thread-aware in places, and labelling one message must not hide its
    # eight siblings.
    assert "label:" not in imap.queries[0]


def test_poll_falls_back_when_gmail_search_is_unavailable():
    from mplabel import cli

    imap = _FakeIMAP([b"7"], gmail_ok=False)
    assert cli.candidate_ids(imap, {}, "imap.example.com") == [b"7"]
    assert not imap.queries[0].startswith("(X-GM-RAW")
    assert "SINCE" in imap.queries[0]


def test_peek_does_not_mark_mail_read(db):
    from mplabel import cli, mailparse

    imap = _FakeIMAP([b"5"])
    hdr = cli.peek_headers(imap, b"5")
    assert mailparse._decode(hdr.get("Message-ID")) == \
        "<msg-5@marketplace.facebook.com>"
    assert "BODY.PEEK" in imap.fetched[0][1]
    assert mailparse.is_label_email(hdr), "triage needs From and Subject too"


def test_a_catalogued_label_is_not_treated_as_printed(db):
    """backfill records every classified Facebook message in mail_events,
    shipping_label included. Treating that as "handled" skipped fifteen
    labels that had never been printed: catalogued is not printed."""
    from mplabel import cli

    mid = "<label-1@marketplace.facebook.com>"
    listings.record_event(db, mid, "2026-08-28T10:00:00", "shipping_label",
                          "Shipping label for your Marketplace order")

    assert cli.already_recorded(db, mid, is_label=True) is False, \
        "a label in mail_events but not sales still needs printing"

    db.execute("INSERT INTO sales (message_id) VALUES (?)", (mid,))
    db.commit()
    assert cli.already_recorded(db, mid, is_label=True) is True


def test_non_label_mail_dedupes_on_mail_events(db):
    from mplabel import cli

    mid = "<order-1@marketplace.facebook.com>"
    assert cli.already_recorded(db, mid, is_label=False) is False
    listings.record_event(db, mid, "2026-08-28T10:00:00", "sold",
                          "New Marketplace order for Brass Lamp")
    assert cli.already_recorded(db, mid, is_label=False) is True


def test_spoofed_sender_cannot_post_a_sale(db, monkeypatch):
    from mplabel import cli
    msg = email.message_from_string(
        "From: Facebook Marketplace <noreply@marketplace.facebook.com.evil.ru>\n"
        "Subject: New Marketplace order for Brass Lamp\n"
        "Date: Fri, 28 Aug 2026 11:00:00 +0000\n\n")
    assert cli.record_event(db, msg) == 0
    assert db.execute("SELECT COUNT(*) FROM mail_events").fetchone()[0] == 0


def test_a_local_pickup_sale_is_counted(tmp_path, db):
    """A local pickup sale never produces a shipping label, so the order
    subject is the only record of it. Those used to be dropped: the poller
    kept label mail and put everything else back, so the database only ever
    knew about items that shipped."""
    import json as _json

    title = "Vintage Swedish Full Lead Crystal Owl Sculpture"
    src = tmp_path / "active.json"
    src.write_text(_json.dumps([
        {"title": title, "price": 65.0, "is_sold": False, "is_live": True},
    ]), encoding="utf-8")
    savedpage.import_saved(db, src)

    # No sales row at all - only the order email.
    listings.record_event(db, "<order1>", "2026-08-28T11:00:00", "sold",
                          f"New Marketplace order for {title}")
    listings.apply_events(db)

    rows = db.execute("SELECT title, state, sold_at FROM listings").fetchall()
    assert len(rows) == 1, "the order created a duplicate instead of linking"
    assert rows[0]["state"] == "sold"
    assert rows[0]["sold_at"] == "2026-08-28T11:00:00"


def test_a_sale_of_something_never_captured_still_counts(db):
    """If the item was never in a saved-page capture there is nothing to
    link to, and dropping it would undercount sales."""
    listings.record_event(db, "<order2>", "2026-08-28T12:00:00", "sold",
                          "New Marketplace order for Copper Jelly Mould")
    listings.apply_events(db)
    row = db.execute("SELECT title, state FROM listings").fetchone()
    assert row["title"] == "Copper Jelly Mould"
    assert row["state"] == "sold"


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


@pytest.mark.parametrize("sale_listing_id", [None, "88887777"])
def test_a_sale_marks_the_saved_page_listing_sold(tmp_path, db, sale_listing_id):
    """What she actually hit: items sold today stayed in the active
    columns. Saved-page listings are keyed by title because the cards
    carry no Facebook id, and some label emails carry no listing id
    either - so matching on listing_id alone left the item 'active' and
    put a duplicate row next to it."""
    import json as _json

    title = "Vintage Swedish Full Lead Crystal Owl Sculpture"
    src = tmp_path / "active.json"
    src.write_text(_json.dumps([
        {"title": title, "price": 65.0, "is_sold": False, "is_live": True},
    ]), encoding="utf-8")
    savedpage.import_saved(db, src)

    db.execute("INSERT INTO sales (listing_id, item, price, received_at) "
               "VALUES (?,?,?,?)",
               (sale_listing_id, title, 65.0, "2026-08-28T13:54:00"))
    db.commit()
    listings.link_sales(db)

    rows = db.execute("SELECT title, state, sold_at FROM listings").fetchall()
    assert len(rows) == 1, "the sale created a duplicate instead of linking"
    assert rows[0]["state"] == "sold"
    assert rows[0]["sold_at"] == "2026-08-28T13:54:00"

    # And sold_at is what the monthly view groups on.
    listings.build_views(db)
    assert db.execute("SELECT COUNT(*) FROM v_monthly").fetchone()[0] == 1


def test_long_similar_titles_stay_separate(tmp_path, db):
    """Her titles are long and share prefixes. A 60-character slug merged
    two real listings into one, quietly under-counting the denominator
    that sell-through is measured against."""
    import json as _json

    a = "Antique 1900-1915 American Edwardian Late Victorian Carved Oak Hall Stand"
    b = "Antique 1900-1915 American Edwardian Late Victorian Carved Oak Side Chair"
    assert a[:60] == b[:60], "the fixture must actually collide on 60 chars"

    src = tmp_path / "similar.json"
    src.write_text(_json.dumps([
        {"title": a, "price": 650.0, "is_sold": False, "is_live": True},
        {"title": b, "price": 180.0, "is_sold": False, "is_live": True},
    ]), encoding="utf-8")

    rows, _stats = savedpage.extract(src)
    assert len({r["listing_id"] for r in rows}) == 2, "ids collided"
    savedpage.import_saved(db, src)
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2


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


def test_sales_tab_carries_the_parcel_code(db):
    """The code is only useful if it can be read off the sheet against the
    number written on the box."""
    db.execute("INSERT INTO sales (message_id, item, code) "
               "VALUES ('<m1>', 'Brass Lamp', '042')")
    db.commit()
    sql, headers = sheets.TABS["Sales"]
    assert headers[0] == "Code"
    assert db.execute(sql).fetchone()[0] == "042"


def test_sheet_tabs_have_matching_header_widths(db):
    listings.build_views(db)
    for name, (sql, headers) in sheets.TABS.items():
        cur = db.execute(sql)
        assert len(cur.description) == len(headers), f"{name} column mismatch"


# ------------------------------------------------- two processes, one Pi

def test_connect_db_turns_on_wal_and_a_busy_timeout(tmp_path):
    """The poll loop is no longer the only writer.

    Under the default rollback journal, a web request overlapping a poll
    gives `database is locked`, and it would surface as a failed print in
    the middle of a batch. journal_mode lives in the file; busy_timeout is
    per-connection, so every opener has to ask for it."""
    from mplabel import cli

    conn = cli.connect_db(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_two_connections_can_write_the_same_database(tmp_path):
    from mplabel import cli

    a = cli.connect_db(tmp_path)
    b = cli.connect_db(tmp_path)
    for i in range(20):
        conn = a if i % 2 == 0 else b
        conn.execute("INSERT INTO sales (message_id, item) VALUES (?, ?)",
                     (f"<m{i}>", "Stoneware vase"))
        conn.commit()
    assert a.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 20


def test_migration_adds_columns_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS will not touch a table that already
    exists, so a database holding real sales never gains a new column
    unless MIGRATIONS says so. The row has to survive it."""
    from mplabel import cli

    # The real schema minus the column, rather than a hand-written subset:
    # a trimmed copy is how the db fixture drifted until it had no
    # message_id and tests passed against a table the code never meets.
    old_schema = cli.SCHEMA.replace("    code         TEXT,\n", "")
    assert "code" not in old_schema, "SCHEMA reformatted - fix this test"

    old = sqlite3.connect(tmp_path / "sales.db")
    old.executescript(old_schema)
    old.execute("INSERT INTO sales (message_id, item) "
                "VALUES ('<real>', 'Stoneware vase')")
    old.commit()
    old.close()

    conn = cli.connect_db(tmp_path)
    assert "code" in {r[1] for r in conn.execute("PRAGMA table_info(sales)")}
    assert conn.execute(
        "SELECT item FROM sales WHERE message_id='<real>'"
    ).fetchone()[0] == "Stoneware vase"


def _lock_cfg(tmp_path, home=None):
    return {"printer_backend": "zpl", "printer_dpi": "203",
            "printer_darkness": "8", "printer_device": str(tmp_path / "lp0"),
            "home": str(tmp_path if home is None else home),
            "label_code": "no"}


# The print lock is flock, so these two only mean anything where flock
# exists. Skipping is honest: off-target there is no poll loop to contend
# with either, and the Pi is where this has to hold.
needs_flock = pytest.mark.skipif(
    importlib.util.find_spec("fcntl") is None,
    reason="fcntl is Unix-only; the print lock is a deployment-target concern")


@needs_flock
def test_print_label_serialises_concurrent_jobs(tmp_path, monkeypatch):
    """_write_raw hands the whole job over in one write because this
    firmware drops bytes arriving while the head moves. Two writers
    interleaved is a garbage label - so the poll loop and the web app have
    to queue, not race."""
    import threading
    import time as _time

    from mplabel import cli, printers

    events = []

    def fake_send(pdf_path, backend, **kwargs):
        events.append("start")
        _time.sleep(0.05)
        events.append("end")

    monkeypatch.setattr(printers, "send", fake_send)
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cfg = _lock_cfg(tmp_path)

    threads = [threading.Thread(target=cli.print_label, args=(cfg, pdf))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert events == ["start", "end", "start", "end"], \
        "the second job started before the first finished"


@needs_flock
def test_print_label_still_prints_when_the_lock_cannot_be_made(
        tmp_path, monkeypatch, caplog):
    """probe/selftest/file run above connect_db on purpose, so a printer
    test keeps working when the data directory is missing or unwritable -
    which is exactly when you need one. A lock we cannot take must not
    become the thing that stops a label."""
    from mplabel import cli, printers

    sent = []
    monkeypatch.setattr(printers, "send",
                        lambda *a, **k: sent.append(a))
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    # /dev/null is not a directory, so mkdir underneath it cannot succeed.
    cfg = _lock_cfg(tmp_path, home="/dev/null/nope")
    cli.print_label(cfg, pdf)

    assert len(sent) == 1, "the label must still print"
    assert "without a lock" in caplog.text


# ------------------------------------------------------------ the web app

def _http(url, method="GET", data=None, cookie=None, headers=None):
    """One request through the real server. Returns (status, headers, body)."""
    import json as _json
    import urllib.error
    import urllib.request

    body = _json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


@pytest.fixture
def app(tmp_path):
    """A real server on an ephemeral port, against a real database."""
    import threading

    from mplabel import cli, web

    conn = cli.connect_db(tmp_path)
    conn.execute(
        "INSERT INTO sales (message_id, item, buyer, price, ship_by, code, "
        "ship_to, tracking, status) VALUES "
        "('<m1>', 'Stoneware vase', 'Sam Sample', 15.0, '2026-09-04', '042',"
        " '2 FICTION RD, SHELBYVILLE IN 46176', '9400100000000000000000',"
        " 'to_ship')")
    conn.commit()

    cfg = {"home": str(tmp_path),
           "web_password_hash": web.hash_password("hunter2"),
           "web_session_days": "30", "web_secure_cookie": "no"}
    srv = web.Server(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", conn
    finally:
        srv.shutdown()
        srv.server_close()


def _login(base, password="hunter2"):
    status, headers, _ = _http(f"{base}/api/login", "POST",
                               {"password": password})
    return status, (headers.get("Set-Cookie") or "").split(";")[0]


def test_password_hash_round_trips_and_rejects(tmp_path):
    from mplabel import web

    stored = web.hash_password("hunter2")
    assert web.verify_password("hunter2", stored)
    assert not web.verify_password("hunter3", stored)
    # A mangled config line must lock her out, not raise.
    assert not web.verify_password("hunter2", "garbage")
    assert not web.verify_password("hunter2", "")


def test_session_token_is_signed_and_expires():
    from mplabel import web

    cfg = {"web_password_hash": "scrypt$1$2$3$aaaa$bbbb"}
    token = web.issue_token(cfg, days=1)
    assert web.valid_token(cfg, token)
    # Tampering with the payload breaks the signature.
    payload, sig = token.split(".")
    assert not web.valid_token(cfg, payload[:-2] + "xx." + sig)
    # Expiry is enforced.
    assert not web.valid_token(cfg, token, now=time.time() + 2 * 86400)


def test_changing_the_password_invalidates_old_sessions():
    """The signing key is derived from the password hash, so there is no
    separate secret to manage and a password change logs every phone out."""
    from mplabel import web

    old = {"web_password_hash": web.hash_password("hunter2")}
    token = web.issue_token(old, days=30)
    new = {"web_password_hash": web.hash_password("something-else")}
    assert web.valid_token(old, token)
    assert not web.valid_token(new, token)


@pytest.mark.parametrize("attempt", [
    "../../etc/passwd",
    "/../../etc/passwd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..%2f..%2fetc%2fpasswd",
    "/labels/../../../../etc/passwd",
])
def test_static_paths_cannot_escape(attempt):
    from mplabel import web

    assert web.safe_static_path(attempt) is None


def test_the_dot_filter_bypass_stays_inside():
    """`....//` is the bypass for filters that strip `../` exactly once -
    strip it and what is left is `../`. This resolves the path instead of
    editing it, so `....` is only an odd directory name and the result is
    still under static/: a 404, not a file read."""
    from mplabel import web

    got = web.safe_static_path("....//....//etc/passwd")
    assert got is not None
    assert got.is_relative_to(web.STATIC.resolve())


def test_label_path_must_live_under_home(tmp_path):
    """The path comes from the database, but a row edited by hand must not
    turn into an arbitrary file read."""
    from mplabel import web

    outside = tmp_path.parent / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    assert web.safe_label_path(tmp_path, str(outside)) is None

    inside = tmp_path / "labels" / "ok_4x6.pdf"
    inside.parent.mkdir(exist_ok=True)
    inside.write_bytes(b"%PDF-1.4\n")
    assert web.safe_label_path(tmp_path, str(inside)) == inside


def test_endpoints_require_authentication(app):
    base, _ = app
    for path in ("/api/orders", "/api/orders/1", "/api/pending", "/api/stats"):
        status, _, _ = _http(base + path)
        assert status == 401, f"{path} served data unauthenticated"


def test_health_is_open_but_says_nothing(app):
    base, _ = app
    status, _, body = _http(f"{base}/healthz")
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_login_then_read_the_queue(app):
    base, _ = app
    assert _login(base, "wrong")[0] == 401

    status, cookie = _login(base)
    assert status == 200 and cookie

    status, _, body = _http(f"{base}/api/orders", cookie=cookie)
    assert status == 200
    orders = json.loads(body)["orders"]
    assert len(orders) == 1
    assert orders[0]["code"] == "042"
    assert orders[0]["printed"] is False


def test_the_queue_does_not_carry_addresses(app):
    """She is looking at a list in a kitchen. The buyer's home address
    belongs on the one screen that needs it."""
    base, _ = app
    _, cookie = _login(base)

    _, _, body = _http(f"{base}/api/orders", cookie=cookie)
    assert "ship_to" not in json.loads(body)["orders"][0]
    assert json.loads(body)["orders"][0]["buyer"] == "Sam"

    _, _, detail = _http(f"{base}/api/orders/1", cookie=cookie)
    assert "SHELBYVILLE" in json.loads(detail)["ship_to"]


def test_mutations_need_the_csrf_header(app):
    """SameSite=Lax plus a header no cross-origin form can set. Logout is
    the only mutation until phase 3, but the gate is on the dispatcher."""
    from mplabel import web

    base, _ = app
    _, cookie = _login(base)

    web.Handler.ROUTES.append(("POST", r"^/api/_probe$", "h_session", True))
    web.Handler._COMPILED = [(m, re.compile(p), h, a)
                             for m, p, h, a in web.Handler.ROUTES]
    try:
        status, _, _ = _http(f"{base}/api/_probe", "POST", {}, cookie=cookie)
        assert status == 400
        status, _, _ = _http(f"{base}/api/_probe", "POST", {}, cookie=cookie,
                             headers={"X-Mplabel": "1"})
        assert status == 200
    finally:
        web.Handler.ROUTES.pop()
        web.Handler._COMPILED = [(m, re.compile(p), h, a)
                                 for m, p, h, a in web.Handler.ROUTES]


def test_traversal_over_http_is_refused(app):
    base, _ = app
    status, _, _ = _http(f"{base}/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    assert status in (400, 404), "traversal must not reach the filesystem"


def test_login_locks_out_after_repeated_failures():
    from mplabel import web

    t = web.Throttle(limit=3, window=60)
    assert not t.locked("1.2.3.4")
    for _ in range(3):
        t.record_failure("1.2.3.4")
    assert t.locked("1.2.3.4")
    # A different client is unaffected, and the window expires.
    assert not t.locked("5.6.7.8")
    assert not t.locked("1.2.3.4", now=time.time() + 61)


def test_serve_refuses_to_run_without_a_password():
    """Better to fail loudly at startup than to publish customer addresses
    to the internet because a config key was missed on an upgrade."""
    from mplabel import web

    with pytest.raises(SystemExit) as exc:
        web.serve({"home": "/tmp", "web_password_hash": ""})
    assert "passwd" in str(exc.value)


def test_head_is_answered_not_501(app):
    """curl -I and health checkers use HEAD; BaseHTTPRequestHandler
    answers 501 unless it is wired up."""
    base, _ = app
    status, headers, body = _http(f"{base}/healthz", method="HEAD")
    assert status == 200
    assert body == b""
    assert headers.get("Content-Length") == "12"


# ------------------------------------------------------- the write actions

def test_ship_and_undo_round_trip(app):
    """Undo exists because marking shipped hides a parcel from the queue,
    and a mis-tap on a box that has not gone is expensive."""
    base, conn = app
    _, cookie = _login(base)
    hdr = {"X-Mplabel": "1"}

    status, _, _ = _http(f"{base}/api/orders/1/ship", "POST", {},
                         cookie=cookie, headers=hdr)
    assert status == 200
    assert conn.execute("SELECT status FROM sales WHERE id=1").fetchone()[0] \
        == "shipped"

    _http(f"{base}/api/orders/1/unship", "POST", {}, cookie=cookie, headers=hdr)
    # Never printed, so it goes back to to_ship rather than printed.
    assert conn.execute("SELECT status FROM sales WHERE id=1").fetchone()[0] \
        == "to_ship"


def test_unship_returns_a_printed_parcel_to_printed(app):
    base, conn = app
    _, cookie = _login(base)
    conn.execute("UPDATE sales SET printed_at='2026-08-28T09:00:00' WHERE id=1")
    conn.commit()
    hdr = {"X-Mplabel": "1"}
    _http(f"{base}/api/orders/1/ship", "POST", {}, cookie=cookie, headers=hdr)
    _http(f"{base}/api/orders/1/unship", "POST", {}, cookie=cookie, headers=hdr)
    assert conn.execute("SELECT status FROM sales WHERE id=1").fetchone()[0] \
        == "printed"


def test_fields_correct_the_record(app):
    base, conn = app
    _, cookie = _login(base)
    hdr = {"X-Mplabel": "1"}

    status, _, body = _http(f"{base}/api/orders/1/fields", "POST",
                            {"item": "Corrected title", "price": "19.50",
                             "notes": "packed"},
                            cookie=cookie, headers=hdr)
    assert status == 200
    assert json.loads(body)["item"] == "Corrected title"
    row = conn.execute("SELECT item, price, notes FROM sales WHERE id=1").fetchone()
    assert (row[0], row[1], row[2]) == ("Corrected title", 19.5, "packed")


def test_fields_rejects_a_bad_price_and_an_empty_patch(app):
    base, _ = app
    _, cookie = _login(base)
    hdr = {"X-Mplabel": "1"}
    for payload in ({"price": "twenty quid"}, {}):
        status, _, _ = _http(f"{base}/api/orders/1/fields", "POST", payload,
                             cookie=cookie, headers=hdr)
        assert status == 400


def test_fields_ignores_columns_it_was_not_offered(app):
    """The request never names a column - the allow-list does."""
    base, conn = app
    _, cookie = _login(base)
    _http(f"{base}/api/orders/1/fields", "POST",
          {"item": "ok", "status": "shipped", "code": "999"},
          cookie=cookie, headers={"X-Mplabel": "1"})
    row = conn.execute("SELECT status, code FROM sales WHERE id=1").fetchone()
    assert row[0] == "to_ship" and row[1] == "042"


def test_printing_goes_through_the_same_path_as_the_cli(app, tmp_path, monkeypatch):
    """The endpoint must not reimplement printing - cli.print_label is
    what takes the flock that keeps it from interleaving with the poller."""
    from mplabel import cli

    base, conn = app
    _, cookie = _login(base)
    pdf = tmp_path / "labels" / "x_4x6.pdf"
    pdf.parent.mkdir(exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    conn.execute("UPDATE sales SET label_pdf=? WHERE id=1", (str(pdf),))
    conn.commit()

    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    status, _, _ = _http(f"{base}/api/orders/1/print", "POST", {},
                         cookie=cookie, headers={"X-Mplabel": "1"})
    assert status == 200
    assert len(sent) == 1
    assert conn.execute("SELECT printed_at FROM sales WHERE id=1").fetchone()[0]


def test_batch_dry_run_prints_nothing(app, tmp_path, monkeypatch):
    from mplabel import cli

    base, conn = app
    _, cookie = _login(base)
    pdf = tmp_path / "labels" / "x_4x6.pdf"
    pdf.parent.mkdir(exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    conn.execute("UPDATE sales SET label_pdf=? WHERE id=1", (str(pdf),))
    conn.commit()

    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    status, _, body = _http(f"{base}/api/print/pending", "POST",
                            {"ids": [1], "dry_run": True},
                            cookie=cookie, headers={"X-Mplabel": "1"})
    assert status == 200
    assert sent == [], "a dry run must not reach the printer"
    assert len(json.loads(body)["would_print"]) == 1


def test_one_bad_label_does_not_abandon_the_batch(app, tmp_path, monkeypatch):
    """Recovering a jammed batch is the whole point of that screen; a
    single missing file must not strand the rest."""
    from mplabel import cli

    base, conn = app
    _, cookie = _login(base)
    good = tmp_path / "labels" / "good_4x6.pdf"
    good.parent.mkdir(exist_ok=True)
    good.write_bytes(b"%PDF-1.4\n")
    conn.execute("UPDATE sales SET label_pdf=? WHERE id=1", (str(good),))
    conn.execute("INSERT INTO sales (message_id, item, label_pdf, code) "
                 "VALUES ('<m2>', 'Missing file', ?, '077')",
                 (str(tmp_path / "labels" / "gone_4x6.pdf"),))
    conn.commit()

    monkeypatch.setattr(cli, "print_label", lambda *a, **k: None)
    _, _, body = _http(f"{base}/api/print/pending", "POST",
                       {"ids": [1, 2]}, cookie=cookie,
                       headers={"X-Mplabel": "1"})
    out = json.loads(body)
    assert len(out["printed"]) == 1 and len(out["failed"]) == 1


def test_system_endpoint_leaks_no_secrets(app):
    """imap_password, sheets_key and the password hash all live in the
    same config dict this reads from."""
    base, _ = app
    _, cookie = _login(base)
    _, _, body = _http(f"{base}/api/system", cookie=cookie)
    blob = body.decode().lower()
    for secret in ("password", "scrypt", "sheets_key", "imap"):
        assert secret not in blob, f"{secret} reached the client"


def test_the_app_shell_is_served(app):
    base, _ = app
    for path, needle in (("/", b"<title>mplabel</title>"),
                         ("/app.js", b"esc("),
                         ("/manifest.json", b"standalone")):
        status, _, body = _http(base + path)
        assert status == 200, path
        assert needle in body, path


def test_the_client_escapes_what_facebook_sends():
    """Item titles come from Marketplace listings, so their text is chosen
    by someone else. app.js must route every one through esc()."""
    # encoding, not the platform default: cp1252 chokes on this file, so
    # without it the test only passes where the locale happens to be UTF-8.
    js = (Path(__file__).parent.parent / "src" / "mplabel" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "function esc(" in js

    # Anything concatenated straight into an HTML string is unescaped by
    # definition. o/d/p/a/b/s/t/m are the loop variables holding server
    # data, so a raw `+ o.title` is the bug this is looking for. Numeric
    # ids are the only safe exception - they cannot carry markup.
    numeric_ok = {"id", "print_count", "length"}
    raw = [f"{v}.{f}" for v, f in
           re.findall(r"\+\s*\b([odpabstm])\.(\w+)", js)
           if f not in numeric_ok]
    assert not raw, f"interpolated into HTML without esc(): {sorted(set(raw))}"


# ------------------------------------- the web app meets the label backstop

def test_the_web_reprint_refuses_a_mismatched_label(app, tmp_path, monkeypatch):
    """`label_belongs_to` exists because an archived label once pointed at
    another buyer, and printing it posts a parcel to a stranger. Reprinting
    from a phone is the easy path, so it is the one that most needs the
    check - going straight to print_label would route around it."""
    from mplabel import cli

    base, conn = app
    _, cookie = _login(base)

    pdf = tmp_path / "labels" / "x_4x6.pdf"
    pdf.parent.mkdir(exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    conn.execute("UPDATE sales SET label_pdf=? WHERE id=1", (str(pdf),))
    conn.commit()

    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cli, "label_belongs_to",
                        lambda row: (False, "addressed to someone else"))

    status, _, body = _http(f"{base}/api/orders/1/print", "POST", {},
                            cookie=cookie, headers={"X-Mplabel": "1"})
    assert status == 400
    assert "someone else" in json.loads(body)["error"]
    assert sent == [], "a mismatched label reached the printer"

    # ...and --force still gets through, the same as the CLI.
    status, _, _ = _http(f"{base}/api/orders/1/print", "POST", {"force": True},
                         cookie=cookie, headers={"X-Mplabel": "1"})
    assert status == 200 and len(sent) == 1


def test_a_cancelled_order_leaves_the_phone_queue(app):
    """CLOSED_STATUSES, not `!= shipped`. A cancelled order is closed too,
    and would otherwise sit in her queue forever asking to be posted."""
    base, conn = app
    _, cookie = _login(base)

    _, _, body = _http(f"{base}/api/orders", cookie=cookie)
    assert len(json.loads(body)["orders"]) == 1

    conn.execute("UPDATE sales SET status='cancelled' WHERE id=1")
    conn.commit()
    _, _, body = _http(f"{base}/api/orders", cookie=cookie)
    assert json.loads(body)["orders"] == []

    _, _, body = _http(f"{base}/api/pending", cookie=cookie)
    assert json.loads(body)["pending"] == []

# ------------------------------------------------ the inventory label maker
#
# The SUPVAN/KATA T50M Pro is a vendor-defined HID device, not a printer,
# and none of this has ever run against the hardware - it is written from
# docs/supvan-t50m-protocol.md alone. So these tests pin the two things a
# document can settle: the exact bytes on the wire, and the meaning of the
# bytes coming back. There is no /dev/hidraw0 here and there must never
# need to be; the "device" is a temp file or a stub.



def _status_report(*values):
    """A well-formed status reply: the device's length byte, then flags.

    Building one by hand without the length byte is how these tests broke
    when decode_status started honouring it - the payload came back empty
    and every flag read false."""
    report = bytearray(supvan.REPORT_SIZE)
    report[0] = 8                      # eight bytes of payload follow
    for offset, value in values:
        report[supvan.STATUS_PREFIX_LEN + offset] = value
    return bytes(report)


class _FakeDevice(supvan.SupvanDevice):
    """A T50M Pro that records what it was told and answers with a canned
    report. Bypasses os entirely, so it runs anywhere."""

    def __init__(self, reply=b"\x00" * supvan.REPORT_SIZE):
        super().__init__(path="fake", timeout=0)
        self.reply = reply
        self.sent = []

    def open(self):
        return self

    def close(self):
        pass

    def write(self, payload):
        self.sent.append(bytes(payload))
        return len(supvan.split_reports(payload))

    def read_report(self, timeout=None):
        return self.reply


def _fake_node(tmp_path):
    """A file standing in for the hidraw node. It has to exist already:
    the real one does, and open() deliberately does not pass O_CREAT."""
    node = tmp_path / "hidraw0"
    node.write_bytes(b"")
    return node


def test_supvan_write_carries_the_report_id_byte(tmp_path):
    """65 bytes per report, not 64. The descriptor declares no Report ID,
    but a Linux hidraw write still needs the leading 0x00 - and without it
    the device ignores the write silently rather than complaining, so
    there is nothing to notice at runtime."""
    node = _fake_node(tmp_path)
    with supvan.SupvanDevice(node) as dev:
        assert dev.write(b"\xAA\xBB") == 1

    raw = node.read_bytes()
    assert len(raw) == supvan.WRITE_SIZE == 65
    assert raw[0] == 0x00
    assert raw[1:3] == b"\xAA\xBB"
    assert raw[3:] == b"\x00" * (supvan.REPORT_SIZE - 2)   # zero-padded


def test_supvan_splits_a_long_payload_across_reports(tmp_path):
    """A payload longer than one report becomes consecutive whole reports,
    each with its own id byte, and only the last one is padded."""
    node = _fake_node(tmp_path)
    payload = bytes(range(100))                 # 64 + 36
    with supvan.SupvanDevice(node) as dev:
        assert dev.write(payload) == 2

    raw = node.read_bytes()
    assert len(raw) == 2 * supvan.WRITE_SIZE
    first, second = raw[:65], raw[65:]
    assert first[0] == 0x00 and second[0] == 0x00
    assert first[1:] == payload[:64]
    assert second[1:37] == payload[64:]
    assert second[37:] == b"\x00" * 28
    # And the framing helper agrees with what actually reached the node.
    assert supvan.wire_bytes(payload) == raw


def test_supvan_empty_payload_is_still_one_report():
    """Sending nothing must not look like sending something."""
    assert len(supvan.split_reports(b"")) == 1
    assert supvan.split_reports(b"")[0] == b"\x00" * 64


def test_supvan_command_frame_is_eight_bytes():
    frame = supvan.build_command(supvan.OP_INQUIRY_STATUS)
    assert frame == bytes([0xC0, 0x40, 0x00, 0x00, 0x11, 0x00, 0x08, 0x00])


def test_supvan_wvalue_goes_out_high_byte_first():
    """The opposite order to the page counter in the status report. That
    is what the document records for each; do not make them agree."""
    frame = supvan.build_command(supvan.OP_NEXT_FRAME_IS_BULK, 0x1234)
    assert frame[2] == 0x12 and frame[3] == 0x34
    assert frame[4] == 0x5C


def test_supvan_two_value_frame_appends_ten_bytes():
    """The ten-byte variant appends a second 16-bit value and changes
    nothing else - wLength stays 8."""
    frame = supvan.build_command(supvan.OP_BUFFER_FULL, 0x0102, 0x0304)
    assert len(frame) == 10
    assert frame[:8] == bytes([0xC0, 0x40, 0x01, 0x02, 0x10, 0x00, 0x08, 0x00])
    assert frame[8:] == b"\x03\x04"


def test_supvan_a_command_pads_to_exactly_one_report(tmp_path):
    node = _fake_node(tmp_path)
    with supvan.SupvanDevice(node) as dev:
        dev.command(supvan.OP_BUFFER_FULL, 1, 2)
    raw = node.read_bytes()
    assert len(raw) == supvan.WRITE_SIZE
    assert raw[11:] == b"\x00" * (supvan.WRITE_SIZE - 11)   # id + 10 bytes


def test_supvan_refuses_to_build_the_firmware_opcode():
    """0xc6 is the firmware update path. The document says do not send it,
    so it cannot be built by accident either."""
    with pytest.raises(ValueError, match="firmware"):
        supvan.build_command(supvan.OP_NEXT_FRAME_IS_FIRMWARE, 1)


def test_supvan_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        supvan.build_command(supvan.OP_START_PRINT, 0x10000)
    with pytest.raises(ValueError):
        supvan.build_command(supvan.OP_BUFFER_FULL, 1, -1)


@pytest.mark.parametrize("name,offset,mask", supvan.STATUS_FLAGS)
def test_supvan_status_flags_decode_one_bit_at_a_time(name, offset, mask):
    """Each flag, alone, against an otherwise clear report - so a wrong
    byte offset or a mask shared between two names shows up as two flags
    lighting at once rather than as a plausible-looking status line."""
    status = supvan.decode_status(_status_report((offset, mask)))
    assert status[name] is True
    lit = [n for n, _o, _m in supvan.STATUS_FLAGS if status[n]]
    assert lit == [name]


def test_supvan_a_clear_report_raises_nothing():
    status = supvan.decode_status(_status_report())
    assert status["pages_printed"] == 0
    assert status["errors"] == []
    assert not any(status[n] for n, _o, _m in supvan.STATUS_FLAGS)


def test_supvan_page_counter_is_little_endian():
    """Bytes 4 and 5, byte 5 the high byte. Read the other way round, one
    printed page reads as 256 and the mistake stays plausible for a long
    time."""
    def pages(low, high):
        return supvan.decode_status(
            _status_report((4, low), (5, high)))["pages_printed"]

    assert pages(0x01, 0x00) == 1
    assert pages(0x00, 0x01) == 256
    assert pages(0x34, 0x12) == 0x1234


def test_supvan_errors_are_separate_from_warnings():
    """Out of media stops a job; a low battery does not. The document says
    to abort on 'any error condition' without listing them, so this split
    is ours - keep it visible rather than folding warnings into errors."""
    status = supvan.decode_status(_status_report((0, 0x04 | 0x40)))
    assert status["out_of_media"] and status["battery_low"]
    assert status["errors"] == ["out_of_media"]


def test_supvan_padding_past_byte_six_is_not_interpreted():
    """Only the first six bytes are documented. Whatever the device puts
    in the other 58 must not change the decode."""
    clear = supvan.decode_status(_status_report())
    padded = bytearray(_status_report())
    padded[supvan.STATUS_MIN_LEN:] = b"\xFF" * (supvan.REPORT_SIZE
                                                - supvan.STATUS_MIN_LEN)
    noisy = supvan.decode_status(bytes(padded))
    assert {k: v for k, v in noisy.items() if k != "raw"} == \
           {k: v for k, v in clear.items() if k != "raw"}


def test_supvan_a_truncated_report_raises():
    """A short read is a transport fault. Padding it out would report a
    healthy device with an empty page count."""
    with pytest.raises(supvan.SupvanError):
        supvan.decode_status(b"\x00\x00\x00")


def test_supvan_status_poll_sends_the_inquiry_and_decodes_the_reply():
    dev = _FakeDevice(_status_report((2, 0x10), (4, 0x09)))

    status = dev.status()

    assert dev.sent == [supvan.build_command(supvan.OP_INQUIRY_STATUS)]
    assert status["usb_connected"] and status["pages_printed"] == 9
    assert "USB connected" in supvan.format_status(status)


@pytest.mark.parametrize("captured,expected", [
    ("08 00 00 10 00 00", {"usb_connected"}),
    ("08 00 00 18 00 00", {"usb_connected", "cover_open"}),
])
def test_supvan_decodes_the_real_captures(captured, expected):
    """The two readings that caught the offset, kept verbatim.

    Decoded from byte 0 these said "media not recognised" on a healthy
    idle printer, and claimed USB was disconnected on a device that was
    answering over USB. Opening the media cover moved byte 3, not byte 2,
    which is what the leading byte predicts and the naive reading does
    not. If STATUS_PREFIX_LEN is ever "simplified" away, this fails."""
    report = bytes.fromhex(captured.replace(" ", ""))
    report += bytes(supvan.REPORT_SIZE - len(report))
    status = supvan.decode_status(report)

    lit = {n for n, _o, _m in supvan.STATUS_FLAGS if status[n]}
    assert lit == expected
    assert status["pages_printed"] == 0
    assert status["prefix"] == 0x08
    assert "media_not_recognised" not in lit, "the phantom error is back"


@pytest.mark.parametrize("name,captured,length", [
    ("status",   "08 00 00 10 00 00 00 00", 8),
    ("check",    "08 00 04 10 00 00 00 00", 8),
    ("revision", "04 32 2e 34 00",          4),
    ("firmware", "08 00 00 10 00 00 00 01 00", 8),
    ("media",    "3b 1d 4a 96 41 0c 10 80 4a bf 83 71 a2 63 36 f6", 59),
])
def test_supvan_every_reply_is_length_prefixed(name, captured, length):
    """Real replies from the device. The leading byte is a length, not a
    marker - these three differ (8, 4, 59), which is what settled it.

    Decoding from offset 0 instead reported "media not recognised" on a
    healthy printer, so this is pinned rather than left to memory."""
    report = bytes.fromhex(captured.replace(" ", ""))
    report += bytes(supvan.REPORT_SIZE - len(report))
    assert len(supvan.reply_payload(report)) == length


@pytest.mark.parametrize("first,second", [
    # 0x11 status, then 0x12 check device, each from two probe runs. The
    # second run's tails are literally the previous run's media reply.
    ("08 00 00 10 00 00 00 00 00 00 00 00 00 00 00 00",
     "08 00 00 10 00 00 00 01 00 bf 83 71 a2 63 36 f6"),
    ("08 00 04 10 00 00 00 00 00 00 00 00 00 00 00 00",
     "08 00 04 10 00 00 00 01 00 bf 83 71 a2 63 36 f6"),
])
def test_supvan_stale_tail_bytes_do_not_change_the_decode(first, second):
    """The device does not clear its report buffer between replies.

    Two real runs: the second carries `bf 83 71 a2 63 36 f6` on the end of
    every reply, which is byte-for-byte the tail of the *previous* run's
    media-info reply, and a `01` at payload byte 6 left by a firmware
    revision command. Only the bytes a command actually refreshes mean
    anything - which is why the status decode reads six and stops, even
    though the length byte says eight."""
    def decode(hexs):
        report = bytes.fromhex(hexs.replace(" ", ""))
        report += bytes(supvan.REPORT_SIZE - len(report))
        status = supvan.decode_status(report)
        return {k: v for k, v in status.items() if k != "raw"}

    assert decode(first) == decode(second)


def test_supvan_check_device_reports_busy():
    """Captured while the device rescanned itself. Byte 1 bit 0x04 is
    'busy', and it lighting exactly there - and nowhere else - is
    independent confirmation that the flag offsets are right."""
    report = bytes.fromhex("080004100000000000")
    report += bytes(supvan.REPORT_SIZE - len(report))
    status = supvan.decode_status(report)
    lit = {n for n, _o, _m in supvan.STATUS_FLAGS if status[n]}
    assert lit == {"busy", "usb_connected"}
    assert status["errors"] == []


def test_supvan_revision_decodes_to_text():
    report = bytes.fromhex("04322e3400") + bytes(supvan.REPORT_SIZE - 5)
    assert supvan.decode_revision(report) == "2.4"


def test_supvan_a_reply_shorter_than_its_length_byte_raises():
    """A length byte promising more than arrived is a truncated read."""
    with pytest.raises(supvan.SupvanError, match="arrived"):
        supvan.reply_payload(b"\x3b\x01\x02")


def test_supvan_test_pattern_is_asymmetric():
    """A symmetric pattern looks correct under a mirrored row order or a
    flipped axis, which is exactly what this is meant to detect."""
    raw, stride, rows = supvan.render_test_pattern(384, 120)
    assert stride == 48 and rows == 120 and len(raw) == 48 * 120

    row = lambda n: raw[n * stride:(n + 1) * stride]
    assert row(0) == b"\xFF" * stride, "no solid bar across the top"
    assert row(0) != row(100), "top and bottom are indistinguishable"
    middle = row(40)
    assert middle[0] == 0xFF, "left square missing"
    assert middle[stride - 1] == 0x01, "right edge rule missing"
    assert middle[stride // 2] == 0x00, "the middle should be blank"


def test_supvan_invert_flips_every_bit():
    plain, _s, _r = supvan.render_test_pattern(384, 16)
    flipped, _s, _r = supvan.render_test_pattern(384, 16, invert=True)
    assert all(a ^ 0xFF == b for a, b in zip(plain, flipped))


@pytest.mark.parametrize("opcode,value,value2,captured", [
    (supvan.OP_CHECK_DEVICE,       0,   None, "c0 40 00 00 12 00 08 00"),
    (supvan.OP_INQUIRY_STATUS,     0,   None, "c0 40 00 00 11 00 08 00"),
    (supvan.OP_START_PRINT,        1,   None, "c0 40 00 01 13 00 08 00"),
    (supvan.OP_NEXT_FRAME_IS_BULK, 123, None, "c0 40 00 7b 5c 00 08 00"),
    (supvan.OP_RETURN_MEDIA_INFO,  0,   None, "c0 40 00 00 30 00 08 00"),
    (supvan.OP_BUFFER_FULL,        123, 60,   "c0 40 00 7b 10 00 08 00 00 3c"),
])
def test_supvan_frames_match_a_captured_usb_print(opcode, value, value2,
                                                  captured):
    """Byte-for-byte against a USBPcap capture of the vendor app printing.

    This is the strongest evidence in the module: real frames off the wire
    rather than a reading of a document. Note the buffer-full second value
    is 0x3c - 60 - where this code sent 1 for a long time."""
    assert supvan.build_command(opcode, value, value2) == \
        bytes.fromhex(captured.replace(" ", ""))


def test_supvan_bulk_goes_bare_over_usb():
    """No wrapper. The capture sends the LZMA stream straight into 64-byte
    reports right after the 0x5c announce - the `0xbb` framing seen over
    Bluetooth has no USB equivalent, and the announce carries the
    compressed length, which is what those two reports hold."""
    payload = bytes(123)
    reports = supvan.split_reports(payload)
    assert len(reports) == 2 and all(len(r) == 64 for r in reports)
    assert supvan.build_command(supvan.OP_NEXT_FRAME_IS_BULK,
                                len(payload))[3] == 123


# --- the in-repo LZMA1 encoder ------------------------------------------
#
# This is the one corner of the T50M Pro work that can be settled on this
# machine instead of by spending a label: liblzma is the reference, and it
# has to accept what we produce.

@pytest.mark.parametrize("name,payload", [
    ("test pattern", None),                      # rendered below
    ("all zeros", bytes(4096)),
    ("all ones", b"\xff" * 4096),
    ("one byte", b"\x5a"),
    ("incompressible", bytes((i * 37 + 11) % 256 for i in range(2048))),
])
def test_lzma1_round_trips_through_liblzma(name, payload):
    """liblzma must decode it, with the declared size, to the original.

    Every other fact about this printer cost a label to learn. This one
    does not have to: if the reference decoder disagrees with us, the
    firmware's certainly will."""
    import lzma
    from mplabel import lzma1

    if payload is None:
        payload, _s, _r = supvan.render_test_pattern(384, 32)
    stream = lzma1.compress(payload)
    assert lzma.decompress(stream, format=lzma.FORMAT_ALONE) == payload


def test_lzma1_emits_no_end_of_stream_marker():
    """The whole reason this module exists.

    The device takes a declared size with no marker. Python's encoder
    always writes one and cannot be told not to; the marker is
    entropy-coded, so it cannot be trimmed off afterwards either. Both
    halves were proved against the captured print - it will not decode as
    unknown-size, ours would - and the printer refused ours either way.

    Blanking the declared size forces liblzma to look for a marker, so a
    stream that decodes here has one and this encoder has regressed."""
    import lzma
    from mplabel import lzma1

    raw, _s, _r = supvan.render_test_pattern(384, 32)
    stream = lzma1.compress(raw)
    unknown_size = stream[:5] + b"\xff" * 8 + stream[13:]
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(unknown_size, format=lzma.FORMAT_ALONE)


def test_lzma1_header_is_byte_identical_to_the_captured_print():
    """12288 bytes at 8KB, which is exactly what the vendor app sent."""
    from mplabel import lzma1

    raw, _s, _r = supvan.render_test_pattern(384, 256)
    assert len(raw) == 12288
    assert lzma1.compress(raw)[:13].hex(" ") == \
        "5d 00 20 00 00 00 30 00 00 00 00 00 00"


def test_lzma1_output_fits_the_announce_field():
    """0x5c carries the length in 16 bits, so a full label has to fit.

    Literals-only compresses worse than liblzma, and the worst case is
    slightly *larger* than the input - which for a 12288-byte label is
    still well inside 65535, but is worth pinning before someone raises
    the label height."""
    from mplabel import lzma1

    raw, _s, _r = supvan.render_test_pattern(384, 256)
    assert len(lzma1.compress(raw)) < 0xFFFF


def test_lzma1_refuses_an_empty_payload():
    """There is no such thing as a zero-row label, and an empty
    range-coded body would be a puzzling thing to hand a printer."""
    from mplabel import lzma1

    with pytest.raises(ValueError):
        lzma1.compress(b"")


def test_supvan_defaults_to_the_device_encoder():
    """compress_bitmap's default has to be the shape that prints.

    'alone' is liblzma's, and liblzma writes a marker - the failure this
    spent several labels finding. Leaving it as the default would put the
    known-bad stream back in the default path."""
    import lzma

    raw, _s, _r = supvan.render_test_pattern(384, 32)
    default = supvan.compress_bitmap(raw)
    assert default == supvan.compress_bitmap(raw, "device")
    assert lzma.decompress(default, format=lzma.FORMAT_ALONE) == raw


def test_supvan_cli_defaults_to_the_device_encoder(monkeypatch, capsys):
    """Through main(), because an argparse default has silently overridden
    the module default once already."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--max-buffer", "0"])
    cli.main()
    out = capsys.readouterr().out
    assert "device container" in out
    assert "size declared" in out
    assert "head 5d 00 20 00 00" in out

    # And the split path, which has no such flag to get wrong: every band
    # carries the device header with its own length declared.
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run"])
    cli.main()
    split = capsys.readouterr().out
    heads = [l for l in split.splitlines() if "head 5d 00 20 00 00" in l]
    assert len(heads) > 1
    assert "ff ff ff ff" not in split

def test_supvan_sparse_pattern_is_smaller_as_well_as_lighter():
    """Both, or the experiment it exists for proves nothing.

    The sparse pattern is there to test whether the device refuses a job
    for having too much ink. The first attempt ruled both edges down the
    full height, which cut the ink but made the *stream* bigger than the
    blocks it replaced - and stream size is the other open suspect, so it
    would have varied two things at once for a second time.

    With no match coder a row holding one dot costs nearly what a row of
    many costs, so keeping rows completely blank is what keeps the stream
    small. The captured print that worked leaves 242 of 256 rows empty."""
    from mplabel import lzma1

    blocks, stride, rows = supvan.render_test_pattern(384, 256)
    sparse, _s, _r = supvan.render_test_pattern(384, 256, style="sparse")

    def ink(buf):
        return sum(bin(b).count("1") for b in buf)

    def blank_rows(buf):
        return sum(1 for y in range(rows)
                   if not any(buf[y * stride:(y + 1) * stride]))

    assert ink(sparse) < ink(blocks) / 5
    assert blank_rows(sparse) > blank_rows(blocks)
    assert len(lzma1.compress(sparse)) < len(lzma1.compress(blocks))


def test_supvan_sparse_pattern_is_still_asymmetric():
    """A symmetric pattern reads as correct under a mirrored row order or
    a flipped axis, which is most of what it is for."""
    raw, stride, rows = supvan.render_test_pattern(384, 256, style="sparse")
    top = raw[:stride * (rows // 2)]
    bottom = raw[stride * (rows // 2):]
    assert top != bottom[::-1]
    assert sum(bin(b).count("1") for b in top) != \
        sum(bin(b).count("1") for b in bottom)


def test_supvan_reencode_holds_the_image_still(tmp_path, monkeypatch, capsys):
    """--reencode varies the encoder and nothing else.

    --replay sends the vendor's exact bytes and a generated pattern
    changes both the encoder and the picture, so neither can say which of
    the two a refusal belongs to. This decodes a captured stream and
    re-encodes the identical image."""
    import lzma
    from mplabel import cli, lzma1

    image, _s, _r = supvan.render_test_pattern(384, 256)
    captured = tmp_path / "captured.lzma"
    captured.write_bytes(lzma.compress(image, format=lzma.FORMAT_ALONE))

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--reencode", str(captured)])
    cli.main()
    out = capsys.readouterr().out

    assert f"{len(image)} bytes" in out
    assert str(len(lzma1.compress(image))) in out, \
        "the stream sent must be ours, not the captured one"
    assert "head 5d 00 20 00 00" in out
    assert "nothing sent" in out

def test_supvan_scatter_holds_ink_low_and_pushes_the_stream_high():
    """The diagnostic only works if it varies exactly one thing.

    On hardware the device printed 0.13% ink in 7 reports and refused
    7.54% in 12. Both moved together, so either could be the cause.
    `scatter` has to sit at the *working* end for ink and the *failing*
    end for size, or it answers nothing - so both halves are asserted,
    against the real measurements rather than against each other."""
    from mplabel import lzma1

    raw, stride, rows = supvan.render_test_pattern(384, 256, style="scatter")
    ink = sum(bin(b).count("1") for b in raw) / (len(raw) * 8)
    stream = len(lzma1.compress(raw))

    # ink near the print that worked (0.13%), far from the one refused
    assert ink < 0.005, f"{ink:.4%} is not low enough to clear ink"
    # stream near the one refused (724 bytes / 12 reports), well past the
    # one that printed (419 / 7)
    assert stream > 600, f"{stream} bytes will not exercise size"
    assert -(-stream // 64) >= 10


def test_supvan_scatter_leaves_no_row_blank():
    """Which is how it defeats compression at almost no ink: with no match
    coder a row holding one dot costs nearly what a full row costs."""
    raw, stride, rows = supvan.render_test_pattern(384, 256, style="scatter")
    assert all(any(raw[y * stride:(y + 1) * stride]) for y in range(rows))


def test_supvan_every_style_declares_the_same_image_size():
    """The three patterns differ in ink and in stream length on purpose,
    and must not differ in anything else - a different row count would be
    a fourth variable in an experiment that already has too many."""
    sizes = {style: len(supvan.render_test_pattern(384, 256, style=style)[0])
             for style in ("blocks", "sparse", "scatter")}
    assert set(sizes.values()) == {12288}, sizes

# --- splitting the image into buffers -----------------------------------
#
# Measured on the hardware, and the reason this exists at all:
#
#     123 B,  2 reports, 0.13% ink   printed
#     419 B,  7 reports, 0.13% ink   printed
#     695 B, 11 reports, 0.26% ink   REFUSED
#     724 B, 12 reports, 7.54% ink   REFUSED
#
# Ink spans both outcomes and size does not, so the device has a per-buffer
# limit. `scatter` was built to force exactly that comparison.

def test_supvan_split_keeps_every_buffer_under_the_limit():
    """The whole point. A buffer over the limit is one the device refuses,
    and it refuses the job, not the buffer."""
    for style in ("blocks", "sparse", "scatter"):
        raw, stride, _rows = supvan.render_test_pattern(384, 256, style=style)
        bands = supvan.split_bitmap(raw, stride)
        assert bands, style
        for compressed, _band_rows, _raw_len in bands:
            assert len(compressed) <= supvan.MAX_BUFFER_BYTES, style


def test_supvan_split_covers_the_image_exactly_once():
    """Bands are strips of the label. Dropping one loses a band of the
    picture silently; overlapping one prints it twice."""
    raw, stride, rows = supvan.render_test_pattern(384, 256)
    bands = supvan.split_bitmap(raw, stride)

    assert sum(band_rows for _c, band_rows, _n in bands) == rows
    assert sum(raw_len for _c, _r, raw_len in bands) == len(raw)


def test_supvan_each_buffer_is_a_complete_lzma_stream():
    """Not slices of one long stream - each carries its own 13-byte header
    declaring *that band's* length, and decodes standing alone.

    A slice would be undecodable by itself, which is the obvious way to
    write this and would fail on the device rather than here."""
    import lzma

    raw, stride, _rows = supvan.render_test_pattern(384, 256)
    bands = supvan.split_bitmap(raw, stride)
    assert len(bands) > 1, "the test pattern must actually need splitting"

    rebuilt = b""
    for compressed, band_rows, raw_len in bands:
        assert compressed[0] == 0x5D
        assert int.from_bytes(compressed[5:13], "little") == raw_len
        chunk = lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
        assert len(chunk) == raw_len == band_rows * stride
        rebuilt += chunk
    assert rebuilt == raw


def test_supvan_split_refuses_a_limit_it_cannot_meet():
    """Better than returning buffers that are over it anyway, which would
    look like it worked and fail on the device."""
    raw, stride, _rows = supvan.render_test_pattern(384, 256)
    with pytest.raises(ValueError, match="too low"):
        supvan.split_bitmap(raw, stride, max_bytes=8)


def test_supvan_split_rejects_a_partial_row():
    """A bitmap that is not a whole number of rows means the stride is
    wrong, and silently truncating it prints a sheared label."""
    with pytest.raises(ValueError, match="whole number of rows"):
        supvan.split_bitmap(b"\x00" * 100, 48)


def test_supvan_multi_buffer_print_repeats_the_cycle_per_buffer(monkeypatch):
    """One 0x13 for the job, then 0x5c / data / 0x10 for each buffer.

    The alternative reading - a fresh job per band - would print each
    strip on its own label."""
    dev = _FakeDevice(_status_report((0, 0)))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    raw, stride, _rows = supvan.render_test_pattern(384, 256)
    bands = supvan.split_bitmap(raw, stride)
    supvan.experimental_print(
        {"buffers": [(c, n) for c, _r, n in bands]}, settle=0)

    def opcodes(op):
        return [s for s in dev.sent
                if len(s) >= 5 and s[0] == 0xC0 and s[4] == op]

    assert len(opcodes(supvan.OP_START_PRINT)) == 1
    assert len(opcodes(supvan.OP_NEXT_FRAME_IS_BULK)) == len(bands)
    assert len(opcodes(supvan.OP_BUFFER_FULL)) == len(bands)

    for (compressed, _r, _n), frame in zip(
            bands, opcodes(supvan.OP_NEXT_FRAME_IS_BULK)):
        assert int.from_bytes(frame[2:4], "big") == len(compressed)


def test_supvan_cli_splits_by_default(monkeypatch, capsys):
    """A whole-label stream is 724 bytes and the device refuses it, so
    sending one buffer must not be what happens when nobody asks."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert "1 buffer(s)" not in out
    assert "split at 448 bytes" in out

    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--max-buffer", "0"])
    cli.main()
    assert "724 bytes in 1 buffer(s)" in capsys.readouterr().out

def test_supvan_lzma_header_matches_a_captured_print():
    """Taken from a Bluetooth capture of the vendor app printing a label:

        5d 00 20 00 00 00 30 00 00 00 00 00 00
        |  |________|  |____________________|
        |   8KB dict    12288 bytes declared
        properties

    Both numbers were guessed wrong before this capture - 64MB then 64KB
    for the dictionary, and "unknown" for the size - and each wrong guess
    produced the same symptom: a job the printer accepted, positioned for,
    and never completed."""
    raw, _s, _r = supvan.render_test_pattern(384, 64)
    head = supvan.compress_bitmap(raw, "alone")[:13]

    assert head[0] == 0x5D, "lc=3 lp=0 pb=2"
    assert int.from_bytes(head[1:5], "little") == 8192
    assert int.from_bytes(head[5:13], "little") == len(raw), "size must be declared"


@pytest.mark.parametrize("argv,expected", [
    ([], "size declared"),
    (["--no-declare-size"], "size unknown"),
])
def test_supvan_cli_declares_the_size_by_default(monkeypatch, capsys,
                                                 argv, expected):
    """Through the real CLI, not the function default.

    A store_true flag defaulting to False once passed straight over the
    module default, so a run that printed "size unknown" looked like a
    fair test of the fix and was not. Only the end-to-end path catches
    that, which is why this goes through main()."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    # --max-buffer 0 because these two flags belong to the single-buffer
    # path. Splitting always uses lzma1 and always declares the size, and
    # the header printed per band is the proof of it.
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--max-buffer", "0"] + argv)
    cli.main()
    out = capsys.readouterr().out
    assert expected in out
    assert "dict 8192" in out, "the dictionary must match the captured print"


def test_supvan_the_alone_container_is_the_one_that_cannot_print():
    """Why `device` exists, kept as a test rather than as a comment.

    Python always appends an end-of-stream marker, and liblzma then
    refuses its own output when a size is also declared. The captured
    print has no marker and reads back fine. This is the stream the
    printer accepted, positioned for, and never completed - three times -
    and `--lzma alone` is the way back to reproducing that."""
    import lzma
    raw, _s, _r = supvan.render_test_pattern(384, 64)
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(supvan.compress_bitmap(raw, "alone"),
                        format=lzma.FORMAT_ALONE)


@pytest.mark.parametrize("fmt,magic", [
    ("alone", b"\x5d\x00\x20\x00\x00"),   # properties, then the 8KB dict
    ("xz", b"\xfd7zXZ"),
])
def test_supvan_lzma_containers_differ(fmt, magic):
    """Which container the firmware wants is unknown, so it is a flag -
    and the containers have to actually differ for the flag to mean
    anything."""
    raw, _s, _r = supvan.render_test_pattern(384, 32)
    assert supvan.compress_bitmap(raw, fmt).startswith(magic)


def test_supvan_experimental_print_stops_on_an_error_flag(monkeypatch):
    """A device that has already refused will not be persuaded by more
    data, and leaving it mid-job is how it needs a power cycle."""
    # out of media, reported the moment we ask
    dev = _FakeDevice(_status_report((0, 0x04)))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    with pytest.raises(supvan.SupvanError, match="out_of_media"):
        supvan.experimental_print({"compressed": b"\x00" * 8, "raw_len": 64},
                                  path="fake")

    # Polling to find out it refused is fine; anything that commits the
    # device to a job is not.
    opcodes = {frame[4] for frame in dev.sent if len(frame) > 4}
    assert opcodes <= {supvan.OP_INQUIRY_STATUS}, \
        f"sent {[hex(o) for o in opcodes]} after the device refused"


def test_supvan_safe_probe_list_excludes_everything_that_prints():
    """SAFE_PROBE_OPCODES is a safety boundary, not a convenience list.

    Adding an opcode to it asserts the device will not print, feed, or be
    written to. Start-print in particular would leave the device waiting
    for bitmap data a probe never sends. This is the guard against someone
    adding one because the probe looked incomplete."""
    safe = {op for op, _name in supvan.SAFE_PROBE_OPCODES}
    for dangerous in (supvan.OP_START_PRINT, supvan.OP_BUFFER_FULL,
                      supvan.OP_STOP_PRINT, supvan.OP_NEXT_FRAME_IS_BULK,
                      supvan.OP_SET_RFID_DATA,
                      supvan.OP_NEXT_FRAME_IS_FIRMWARE):
        assert dangerous not in safe, f"0x{dangerous:02x} moves paper or writes"
    assert supvan.OP_INQUIRY_STATUS in safe


def test_supvan_deep_probe_asks_each_question_and_keeps_going(monkeypatch):
    """A device that will not answer one command may answer the next, and
    which ones fail is the finding. Stopping at the first would hide it."""
    dev = _FakeDevice(b"\x08" + b"\x00" * (supvan.REPORT_SIZE - 1))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    reads = {"n": 0}
    real_read = dev.read_report

    def flaky(*a, **k):
        reads["n"] += 1
        if reads["n"] == 2:
            raise supvan.SupvanError("timed out")
        return real_read(*a, **k)

    monkeypatch.setattr(dev, "read_report", flaky)
    results = supvan.probe_deep("fake")

    assert [op for _n, op, _r, _e in results] == \
        [op for op, _n in supvan.SAFE_PROBE_OPCODES]
    assert results[1][3] == "timed out" and results[1][2] is None
    assert results[2][2] is not None, "it stopped at the first failure"


def test_supvan_missing_device_says_which_node_and_which_printer(tmp_path):
    """There is no /dev/hidraw0 on a dev machine and there does not need
    to be. The message has to name the node and keep the two printers
    apart: /dev/usb/lp0 is the G4, not this."""
    missing = tmp_path / "no-such-hidraw"
    with pytest.raises(supvan.SupvanError) as exc:
        supvan.poll_status(missing)
    assert "no-such-hidraw" in str(exc.value)
    assert "/dev/usb/lp0" in str(exc.value)


def test_supvan_reading_before_opening_is_an_error():
    with pytest.raises(supvan.SupvanError, match="not open"):
        supvan.SupvanDevice("fake").command(supvan.OP_INQUIRY_STATUS)


def test_supvan_read_report_returns_what_the_node_held(tmp_path):
    """The read side takes no report-id byte: hidraw prepends one only for
    devices with numbered reports, and this one has none."""
    node = _fake_node(tmp_path)
    node.write_bytes(bytes(range(64)))
    with supvan.SupvanDevice(node, timeout=0) as dev:
        assert dev.read_report() == bytes(range(64))


def test_supvan_print_path_refuses_and_names_what_is_unknown():
    """No guessing at anything that pulls paper through a hot head. The
    row format, the bit polarity, the dot width and the RFID exchange are
    all undetermined, and the message has to say so."""
    with pytest.raises(NotImplementedError) as exc:
        supvan.print_bitmap(b"")
    said = str(exc.value)
    for unknown in ("row format", "polarity", "dot width", "0x5d"):
        assert unknown in said


def test_supvan_probe_is_wired_above_the_database(tmp_path, capsys):
    """`supvan-probe` is a hardware test, and a hardware test must not
    need the database - same reason probe, selftest and file sit above
    connect_db. It must also fail with one plain line, not a traceback,
    when the device is absent."""
    import argparse

    from mplabel import cli

    args = argparse.Namespace(device=str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_supvan_probe(dict(cli.DEFAULTS), args)
    assert "absent" in str(exc.value)
    assert cli.DEFAULTS["supvan_device"] == supvan.DEFAULT_DEVICE

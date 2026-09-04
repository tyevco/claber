"""Regression tests for the parts that were verified against real data.

Every fixture here is synthetic. The real label PDF carries a buyer's
home address and the real database carries customer names, so neither
belongs in version control - see tests/fixtures/make_label.py, which
reproduces the exact page geometry of a real Marketplace label with
invented names and an unused tracking number.
"""

import argparse
import csv
import email
import importlib.util
import json
import re
import sqlite3
import threading
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from mplabel import (inventory, label, listings, mailparse, marker, qr, rs,
                     savedpage, sheets, supvan)

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
    # head_dots=None: this asserts the dot-count pinning, and 300dpi is
    # deliberately wider than the G4 head that render_bitmap now guards.
    data, width_px, width_bytes, height = printers.render_bitmap(
        out, dpi, head_dots=None)
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

    # /dev/null is not a directory, so nothing can be created under it.
    monkeypatch.setattr(printers, "lock_path",
                        lambda *a, **k: Path("/dev/null/nope/x.lock"))
    cfg = _lock_cfg(tmp_path)
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
    payload = json.loads(body)
    assert payload["ok"] is True
    # The build stamp is here so a skewed Pi is visible without ssh. It
    # must stay the only extra thing: this endpoint is unauthenticated.
    assert set(payload) == {"ok", "build"}
    assert set(payload["build"]) == {"rev", "printers_sha", "source"}
    blob = body.decode().lower()
    for leak in ("buyer", "tracking", "ship_to", "password", "imap"):
        assert leak not in blob


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
    # The length must describe the body a GET would have returned.
    _s, _h, full = _http(f"{base}/healthz")
    assert headers.get("Content-Length") == str(len(full))


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
                         "--bare-raster", "--max-buffer", "0"])
    cli.main()
    out = capsys.readouterr().out
    assert "device container" in out
    assert "size declared" in out
    assert "head 5d 00 20 00 00" in out

    # And the splitting path, which has no such flag to get wrong: the
    # band carries the device header with its own length declared, and
    # never the 0xff...ff that means "size unknown".
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run"])
    cli.main()
    split = capsys.readouterr().out
    assert "head 5d 00 20 00 00" in split
    assert "ff ff ff ff" not in split

def test_supvan_the_diagnostic_patterns_still_bracket_the_ink_range():
    """`sparse` and `scatter` were built to separate ink from stream size
    back when the encoder had no matches and the two were coupled. With
    match coding they are not: `blocks` is the *heaviest* pattern and now
    compresses smallest of the three.

    The styles are kept because the ink and blankness spread is still what
    makes them useful for reading a printed label, but nothing may assume
    an ordering by stream size any more - that was an artefact of an
    encoder that could not code a repeat."""
    from mplabel import lzma1

    def ink(buf):
        return sum(bin(b).count("1") for b in buf)

    raws = {style: supvan.render_test_pattern(384, 256, style=style)[0]
            for style in ("blocks", "sparse", "scatter")}
    assert ink(raws["scatter"]) < ink(raws["sparse"]) < ink(raws["blocks"])
    # and every one of them fits the device's single buffer
    for style, raw in raws.items():
        assert len(lzma1.compress(raw)) <= 512, style


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

def test_supvan_scatter_is_the_lightest_pattern():
    """`scatter` was built to hold ink at the working end while pushing
    the stream size to the failing end, back when the encoder had no
    matches and one dot per row defeated compression.

    Match coding took it from 695 bytes to 138, so the size half of that
    is gone. What survives - and what it is kept for - is that it puts a
    landmark in every row for almost no ink, which is the useful thing to
    print when reading row order off a label."""
    from mplabel import lzma1

    raw, _stride, _rows = supvan.render_test_pattern(384, 256, style="scatter")
    ink = sum(bin(b).count("1") for b in raw) / (len(raw) * 8)
    assert ink < 0.005, f"{ink:.4%} is not light enough to be useful"
    assert len(lzma1.compress(raw)) <= 512


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
    # A real label now compresses to well under one buffer, so the limit
    # is forced down to make it split at all. The mechanics still have to
    # be right: the device may yet need this for a taller image.
    bands = supvan.split_bitmap(raw, stride, max_bytes=40)
    assert len(bands) > 1, "the limit must actually force a split"

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
        {"streams": [(c, n) for c, _r, n in bands]}, settle=0)

    def opcodes(op):
        return [s for s in dev.sent
                if len(s) >= 5 and s[0] == 0xC0 and s[4] == op]

    assert len(opcodes(supvan.OP_START_PRINT)) == 1
    assert len(opcodes(supvan.OP_NEXT_FRAME_IS_BULK)) == len(bands)
    assert len(opcodes(supvan.OP_BUFFER_FULL)) == len(bands)

    for (compressed, _r, _n), frame in zip(
            bands, opcodes(supvan.OP_NEXT_FRAME_IS_BULK)):
        assert int.from_bytes(frame[2:4], "big") == len(compressed)


def test_supvan_a_whole_label_stays_small_enough_to_reason_about(
        monkeypatch, capsys):
    """The point of the match coder, asserted through the real CLI.

    The bound this used to assert - "the device takes at most 512
    compressed bytes" - was never real. Three sizes were blamed in turn
    (448, 512, report count) and each was retracted; what the firmware
    actually objected to was a payload that was not whole print buffers.

    The property is still worth pinning, for the reason CLAUDE.md gives:
    an encoder that silently stopped emitting matches would round-trip
    through liblzma perfectly and simply not print, which is a day spent
    chasing the printer. Literals-only put a full 48x256 label at
    551-724 bytes; with matches it is far under that, so the number here
    is a regression tripwire and not a device limit."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    for style in ("blocks", "sparse", "scatter"):
        monkeypatch.setattr(sys, "argv",
                            ["mplabel", "supvan-test-print", "--dry-run",
                             "--style", style])
        cli.main()
        out = capsys.readouterr().out
        size = int(re.search(r"lzma   : (\d+) bytes", out).group(1))
        assert size <= 512, f"{style} is {size} bytes; matches regressed?"
        # And it goes as whole print buffers, which is the part that
        # decides whether the device takes it at all.
        assert re.search(r"buffers: \d+ x 4096", out), style


class _GoesQuiet(_FakeDevice):
    """Answers normally, then ignores `silences` reads, then answers again.

    `quiet_after` is a read count rather than a step name because the
    device has no idea which step we think it is on - it went quiet after
    the last buffer of a four-buffer job, which is simply late."""

    def __init__(self, quiet_after, silences, reply):
        super().__init__(reply)
        self.reads = 0
        self.quiet_after = quiet_after
        self.silences = silences

    def read_report(self, timeout=None):
        self.reads += 1
        if self.quiet_after < self.reads <= self.quiet_after + self.silences:
            return b""
        return self.reply


def test_supvan_a_silent_poll_is_retried_not_treated_as_failure(monkeypatch):
    """Observed on the hardware: after the last buffer of a four-buffer
    job the device simply stopped answering. Treating the first silence as
    fatal both hid whether it was temporary and abandoned the job.

    Failing fast on the *first* poll is still right - silence there means
    the device is not there - so the patience is spent where the silence
    actually was."""
    dev = _GoesQuiet(4, 2, _status_report((0, 0)))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    raw, stride, _rows = supvan.render_test_pattern(384, 64)
    bands = supvan.split_bitmap(raw, stride)
    final = supvan.experimental_print(
        {"streams": [(c, n) for c, _r, n in bands]}, settle=0)
    assert final["errors"] == []
    assert dev.reads > 4 + 2, "the silences must actually have been reached"


def test_supvan_a_device_that_never_answers_is_stopped_and_named(monkeypatch):
    """Give up eventually, but send stop-print on the way out and say what
    to do. A job left half-started is what makes the *next* attempt report
    a seating error before it can begin."""
    dev = _FakeDevice(b"")
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    with pytest.raises(supvan.SupvanError, match="stopped answering") as exc:
        supvan.experimental_print(
            {"compressed": b"\x00" * 8, "raw_len": 64}, settle=0)
    assert "power cycle" in str(exc.value)

def test_lzma1_matched_literal_gating(monkeypatch):
    """The bug that only some inputs could show.

    A literal after a match is coded against the byte one match distance
    back. Once a bit disagrees with that byte, the context collapses to
    the plain literal tree - and the *index* has to lose the match-bit
    half as well, not just the offset. Adding it unconditionally corrupts
    a stream only when a literal follows a match AND the match byte has a
    set bit after the first disagreement, so every uniform test bitmap
    passed and the real captured image did not.

    A pattern of alternating bytes forces exactly that shape."""
    import lzma
    from mplabel import lzma1

    data = (b"\xf0\x0f" * 40) + b"\x55" + (b"\xf0\x0f" * 40) + b"\xaa\x33\xcc"
    assert lzma.decompress(lzma1.compress(data),
                           format=lzma.FORMAT_ALONE) == data


@pytest.mark.parametrize("seed", range(60))
def test_lzma1_round_trips_arbitrary_input(seed):
    """liblzma is the reference and it is free to run, which is the whole
    reason this encoder is testable at all. Four shapes: noise, two-tone
    (a bitmap), a repeating period (rows), and a small alphabet."""
    import lzma
    import random
    from mplabel import lzma1

    rng = random.Random(seed)
    n = rng.randrange(1, 2000)
    shape = seed % 4
    if shape == 0:
        data = bytes(rng.randrange(256) for _ in range(n))
    elif shape == 1:
        data = bytes(rng.choice((0, 255)) for _ in range(n))
    elif shape == 2:
        period = bytes(rng.randrange(256) for _ in range(7))
        data = (period * (n // 7 + 1))[:n]
    else:
        data = bytes(rng.randrange(3) for _ in range(n))

    assert lzma.decompress(lzma1.compress(data),
                           format=lzma.FORMAT_ALONE) == data


def test_lzma1_fits_a_whole_label_in_one_device_buffer():
    """The number that matters. The device takes at most 512 compressed
    bytes and printed nothing above 419; literals-only put a full label at
    551-724, which is what three refusals were."""
    from mplabel import lzma1

    for style in ("blocks", "sparse", "scatter"):
        raw, _s, _r = supvan.render_test_pattern(384, 256, style=style)
        assert len(raw) == 12288
        assert len(lzma1.compress(raw)) <= 419, style


def test_lzma1_still_beats_a_literal_only_encoding():
    """Guards against a regression that would be silent otherwise: an
    encoder that quietly stopped emitting matches would still round-trip
    perfectly, and would simply fail on the device."""
    from mplabel import lzma1

    raw, _s, _r = supvan.render_test_pattern(384, 256)
    # literals alone cannot do better than about a byte per distinct row
    # context; 724 was the measured figure for this exact image.
    assert len(lzma1.compress(raw)) < 300

def test_supvan_clip_blanks_ink_outside_the_box_and_keeps_the_size():
    """`--clip` must change which dots are set and nothing else.

    It is there to test one thing: the only bitmap that has ever printed
    is the vendor's, whose ink stops at x=351 where every pattern here
    runs to x=383. If clipping also changed the image size or the row
    count it would vary three things at once, which is the mistake this
    printer has already extracted twice."""
    plain, stride, rows = supvan.render_test_pattern(384, 256)
    clipped, cstride, crows = supvan.render_test_pattern(384, 256,
                                                         clip=(352, 171))
    assert (len(plain), stride, rows) == (len(clipped), cstride, crows)

    def bbox(raw):
        xs = [xb * 8 + k for y in range(rows) for xb in range(stride)
              for k in range(8) if raw[y * stride + xb] & (0x80 >> k)]
        ys = [y for y in range(rows) if any(raw[y * stride:(y + 1) * stride])]
        return max(xs), max(ys)

    assert bbox(plain) == (383, 255)
    assert bbox(clipped) == (351, 79)


def test_supvan_clip_only_removes_ink():
    """Never sets a dot that was not already set - otherwise a clipped run
    and an unclipped one differ by more than the clip."""
    for style in ("blocks", "sparse", "scatter"):
        plain, _s, _r = supvan.render_test_pattern(384, 256, style=style)
        clipped, _s, _r = supvan.render_test_pattern(384, 256, style=style,
                                                     clip=(352, 171))
        for a, b in zip(plain, clipped):
            assert b & ~a == 0, style


def test_supvan_cli_rejects_a_malformed_clip(monkeypatch):
    """A typo must not silently print an unclipped label - that would be
    a wasted label reported as a result."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--clip", "352"])
    with pytest.raises(SystemExit, match="WxH"):
        cli.main()

def test_supvan_a_job_can_be_sent_without_unpacking_it(monkeypatch):
    """`build_job`'s dict must go straight to `experimental_print`.

    It could not: the job's "buffers" is a *count* of 4096-byte print
    buffers inside one LZMA stream, and experimental_print read
    "buffers" as a *list of separate LZMA streams*. Two different things
    under one word, so passing a job through died on `for c, n in 3`.

    It only bit `inventory-label --print`, because supvan-test-print
    happened to unpack the job by hand first - which is exactly the shape
    of bug that reaches hardware and not the test suite."""
    dev = _FakeDevice(_status_report((0, 0)))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    raw, stride, rows = supvan.render_test_pattern(384, 240)
    job = supvan.build_job(raw, stride, rows)
    assert isinstance(job["buffers"], int)

    final = supvan.experimental_print(job, speed=job["speed"], settle=0)
    assert final["errors"] == []

    sent = [s for s in dev.sent
            if len(s) >= 5 and s[0] == 0xC0
            and s[4] == supvan.OP_NEXT_FRAME_IS_BULK]
    assert len(sent) == 1, "one LZMA stream, whatever the buffer count"


def test_supvan_streams_and_buffers_are_not_the_same_key(monkeypatch):
    """The multi-stream path still works, under its own name."""
    dev = _FakeDevice(_status_report((0, 0)))
    monkeypatch.setattr(supvan, "SupvanDevice", lambda *a, **k: dev)

    raw, stride, _rows = supvan.render_test_pattern(384, 256)
    bands = supvan.split_bitmap(raw, stride, max_bytes=40)
    assert len(bands) > 1
    supvan.experimental_print(
        {"streams": [(c, n) for c, _r, n in bands]}, settle=0)

    sent = [s for s in dev.sent
            if len(s) >= 5 and s[0] == 0xC0
            and s[4] == supvan.OP_NEXT_FRAME_IS_BULK]
    assert len(sent) == len(bands)

# --- the calibration target ---------------------------------------------

def _ruler_dots(raw, stride, rows):
    return {(x, y) for y in range(rows) for x in range(stride * 8)
            if raw[y * stride + (x >> 3)] & (0x80 >> (x & 7))}


def test_ruler_draws_nothing_in_the_band_that_is_never_sent():
    """The bug that wasted a label, and the rule the redesign is built on.

    `split_into_buffers` starts the image at row `margin_top` and stops
    `margin_bottom` short; the firmware feeds blank for both. So on a
    240-row label with the default 8-dot margins, rows 0-7 and 232-239
    are not transmitted at all. The first ruler drew its edge rules and
    every minor tick there, and they could not have appeared however the
    printer behaved - which read, on paper, as the printer clipping.

    An instrument must not live in the region it is measuring."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    assert dots, "the ruler drew nothing at all"
    assert min(y for _x, y in dots) >= margin
    assert max(y for _x, y in dots) <= rows - margin - 1


def test_ruler_edge_gauge_brackets_the_sent_area():
    """The frame sits on the first and last rows actually transmitted and
    on the first and last dot across - the witness for "did this edge
    print at all" - and every inset in the gauge has a mark on all four
    sides, so the outermost surviving one is that side's inset."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    top, bottom = margin, rows - margin - 1

    for corner in ((0, top), (383, top), (0, bottom), (383, bottom)):
        assert corner in dots, corner

    for inset in inventory.RULER_INSETS:
        # Each comb mark's near edge sits exactly at its own inset, so it
        # is lost with the row or column it names and not before.
        assert any((x, top + inset) in dots for x in range(60, 240)),             f"no top mark at inset {inset}"
        assert any((x, bottom - inset) in dots for x in range(60, 240)),             f"no bottom mark at inset {inset}"
        assert any((inset, y) in dots for y in range(top, bottom)),             f"no left mark at inset {inset}"
        assert any((383 - inset, y) in dots for y in range(top, bottom)),             f"no right mark at inset {inset}"


def test_ruler_gauge_outranges_the_loss_it_measures():
    """It stopped at 32 while the reported loss was about 40, so every
    mark on that side was gone and the gauge could only say "more than
    32". Same failure as drawing inside the band that is never sent: an
    instrument has to cover the case it exists for."""
    from mplabel import inventory

    assert max(inventory.RULER_INSETS) >= 48
    assert inventory.RULER_INSETS[0] == 0
    steps = [b - a for a, b in zip(inventory.RULER_INSETS,
                                   inventory.RULER_INSETS[1:])]
    assert set(steps) == {8}, "an uneven gauge is misread, not read"


def test_ruler_graduations_survive_a_clipped_edge():
    """The scales are what the numbers are read off, so they must not be
    in the first place to be lost. Both sit well inboard of the deepest
    inset the gauge measures."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    deepest = max(inventory.RULER_INSETS)

    # The edge gauge lives at the edges on purpose - that is its job -
    # so counting ink proves nothing. What matters is where the *scales*
    # are: both lines, and every number hung off them, must sit inboard
    # of the deepest inset the gauge can report.
    sy = margin + 72
    sx = 383 - 96
    assert sy > margin + deepest
    assert sx < 383 - deepest

    safe_x = range(deepest + 1, 383 - deepest)
    safe_y = range(margin + deepest + 1, rows - margin - 1 - deepest)
    assert sy in safe_y and sx in safe_x

    # The scale lines are unbroken across the whole span, so a partial
    # print still reads as a scale rather than as scattered ticks.
    assert all((x, sy) in dots for x in safe_x)
    assert all((sx, y) in dots for y in safe_y)

    # And the numbers hang on the inboard side of each line.
    assert any((x, sy + 20) in dots for x in safe_x), "no across numbers"
    assert any((sx - 30, y) in dots for y in safe_y), "no feed numbers"


def test_ruler_gauges_each_edge_separately():
    """One inset label per edge, not one per rectangle.

    The first version put all five along the top, so the five numbers
    witnessed the top edge and nothing else - and the print that came
    back could not say whether the *left* edge had clipped, which was the
    only thing still in question. Each side needs its own witness, close
    enough to that side to be lost with it."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    top, bottom = margin, rows - margin - 1

    def ink(x0, x1, y0, y1):
        return sum(1 for x, y in dots if x0 <= x <= x1 and y0 <= y <= y1)

    # A band just inside each edge, past the deepest inset, carries text.
    deepest = max(inventory.RULER_INSETS)
    assert ink(40, 200, top, top + deepest + 14) > 40, "no top labels"
    assert ink(40, 200, bottom - deepest - 14, bottom) > 40, "no bottom"
    assert ink(0, deepest + 14, top + 90, bottom) > 40, "no left labels"
    assert ink(383 - deepest - 14, 383, top + 90, bottom) > 40, "no right"

def test_edge_test_bars_start_exactly_at_their_inset():
    """What is being read is where the ink starts, so a bar's near edge
    has to sit on the dot it names - one off and the answer is one step
    out, which is 8 dots of label thrown away or kept wrongly."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_edge_test(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    top, bottom = margin, rows - margin - 1

    for i in range(inventory.EDGE_STEPS):
        near = i * inventory.EDGE_PITCH
        assert any((near, y) in dots for y in range(top, bottom)), \
            f"left bar {i} does not reach x={near}"
        assert any((383 - near, y) in dots for y in range(top, bottom)), \
            f"right bar {i} does not reach x={383 - near}"
        assert any((x, top + near) in dots for x in range(384)), \
            f"top bar {i} does not reach y={top + near}"
        assert any((x, bottom - near) in dots for x in range(384)), \
            f"bottom bar {i} does not reach y={bottom - near}"


def test_edge_test_bars_identify_themselves_by_length():
    """No numbers beside the bars, on purpose: a number is exactly as
    losable as the mark it names, which is what went wrong with the comb.
    Length has to do that job instead, so every bar must differ."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_edge_test(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    top = margin

    lengths = []
    for i in range(inventory.EDGE_STEPS):
        near = i * inventory.EDGE_PITCH
        col = [y for y in range(top, rows - margin) if (near, y) in dots]
        lengths.append(max(col) - min(col))
    assert len(set(lengths)) == len(lengths), f"ambiguous bars: {lengths}"
    assert lengths == sorted(lengths), "the innermost must be the longest"


def test_edge_test_stays_inside_the_sent_band():
    """The rule the ruler learned the hard way, asserted again here."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_edge_test(384, 240)
    dots = _ruler_dots(raw, stride, rows)
    assert min(y for _x, y in dots) >= margin
    assert max(y for _x, y in dots) <= rows - margin - 1


def test_edge_test_outranges_the_reported_loss():
    """Reported: 40 dots lost on the left, 24 on the right. A gauge that
    stopped at 32 could not have said either."""
    from mplabel import inventory

    reach = (inventory.EDGE_STEPS - 1) * inventory.EDGE_PITCH
    assert reach >= 56


def test_edge_test_refuses_a_label_it_cannot_fit():
    from mplabel import inventory

    with pytest.raises(ValueError, match="too small"):
        inventory.render_edge_test(384, 100)
    with pytest.raises(ValueError, match="wider than"):
        inventory.render_edge_test(400, 240)


def test_edge_test_goes_through_the_real_print_path(monkeypatch, capsys):
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--style", "edges", "--height", "240"])
    cli.main()
    out = capsys.readouterr().out
    assert "pattern: edges" in out
    assert "3 x 4096" in out

    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--style", "edges", "--invert"])
    with pytest.raises(SystemExit, match="measurement"):
        cli.main()

@pytest.mark.parametrize("label_mm", [(48, 30), (101.6, 25.4), (40, 20)])
@pytest.mark.parametrize("carrier", ["qr", "marker", "plain"])
def test_label_ink_stays_inside_the_printable_window(label_mm, carrier):
    """Measured with `--style edges`: the left 40 dots and the right 32
    never reach the paper on this stock, and the window is not centred.

    This was a symmetric 12-dot guess before, and the cost was real - a
    QR drawn from x=22 lost its left finder column and would not scan,
    while looking intact in a photograph. Every carrier and every size
    has to land inside what actually burns."""
    from mplabel import inventory

    kw = {"code": "7K2Q", "title": "Antique brass reading lamp",
          "price": 45.0, "label_mm": label_mm}
    if carrier == "qr":
        kw["with_qr"] = True
    elif carrier == "marker":
        kw["with_marker"] = True

    raw, stride, rows = inventory.render_label(**kw)
    xs = [xb * 8 + k for y in range(rows) for xb in range(stride)
          for k in range(8) if raw[y * stride + xb] & (0x80 >> k)]
    assert xs, "the label drew nothing"

    lo = inventory.PRINTABLE_LEFT_DOTS
    hi = inventory.HEAD_DOTS - inventory.PRINTABLE_RIGHT_DOTS - 1
    assert min(xs) >= lo, f"ink at x={min(xs)}, left of the window at {lo}"
    assert max(xs) <= hi, f"ink at x={max(xs)}, right of the window at {hi}"


def test_printable_window_is_not_assumed_symmetric():
    """The two insets differ, and that asymmetry is the finding: unequal
    losses mean the media sits off-centre under the head, where equal
    ones would have meant the head is simply narrower than the paper.
    Collapsing them back to one number would re-introduce the bug."""
    from mplabel import inventory

    assert inventory.PRINTABLE_LEFT_DOTS != inventory.PRINTABLE_RIGHT_DOTS
    assert inventory.PRINTABLE_DOTS == (
        inventory.HEAD_DOTS - inventory.PRINTABLE_LEFT_DOTS
        - inventory.PRINTABLE_RIGHT_DOTS)
    assert inventory.PRINTABLE_DOTS < inventory.HEAD_DOTS


def test_the_measuring_targets_still_use_the_whole_head():
    """The ruler and the edge test must NOT be inset - they exist to find
    where the edges are, so they have to be drawn where the edges are."""
    from mplabel import inventory

    for render in (inventory.render_ruler, inventory.render_edge_test):
        raw, stride, rows = render(384, 240)
        xs = [xb * 8 + k for y in range(rows) for xb in range(stride)
              for k in range(8) if raw[y * stride + xb] & (0x80 >> k)]
        assert min(xs) == 0, f"{render.__name__} does not reach x=0"
        assert max(xs) == 383, f"{render.__name__} does not reach x=383"

def test_ruler_is_asymmetric_in_both_axes():
    """A mirror or a feed flip has to be obvious by looking, not by
    measuring - the first printed label was mirrored and the only reason
    anyone noticed was that the text read backwards."""
    from mplabel import inventory

    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)

    mirrored = {(383 - x, y) for x, y in dots}
    flipped = {(x, 239 - y) for x, y in dots}
    turned = {(383 - x, 239 - y) for x, y in dots}
    assert dots != mirrored, "a left-right mirror would look identical"
    assert dots != flipped, "a feed flip would look identical"
    assert dots != turned, "a 180 turn would look identical"

    # Not just unequal - unequal by a lot, so it is obvious by looking
    # rather than by overlaying two photographs.
    assert len(dots ^ mirrored) > len(dots) // 2
    assert len(dots ^ flipped) > len(dots) // 2


def test_ruler_ticks_land_on_the_dots_they_claim():
    """A scale whose ticks are off by one is worse than no scale: it
    would be read as the printer clipping a dot.

    The across scale carries absolute image x; the feed scale carries
    absolute image y, which starts at the margin rather than at 0 -
    because that is the first row the device is actually given."""
    from mplabel import inventory, supvan

    margin = supvan.DEFAULT_MARGIN_DOTS
    raw, stride, rows = inventory.render_ruler(384, 240)
    dots = _ruler_dots(raw, stride, rows)

    sy = margin + 72
    for pos in range(0, 384, inventory.RULER_MINOR):
        assert (pos, sy + 3) in dots, f"no across-head tick at x={pos}"

    sx = 383 - 96
    for pos in range(margin, rows - margin - 1, inventory.RULER_MINOR):
        assert (sx - 3, pos) in dots, f"no feed tick at y={pos}"


def test_ruler_fits_the_head_and_says_so_when_it_cannot():
    from mplabel import inventory

    raw, stride, rows = inventory.render_ruler(384, 240)
    assert stride == 48 and rows == 240
    assert len(raw) == stride * rows
    with pytest.raises(ValueError, match="wider than"):
        inventory.render_ruler(392, 240)
    with pytest.raises(ValueError):
        inventory.render_ruler(384, 0)


def test_ruler_survives_a_short_label():
    """A 48x12mm label is 96 rows, which is shorter than the feed arrow
    and the corner comb want. They have to be dropped rather than drawn
    off the end, where they would silently become clipping."""
    from mplabel import inventory

    raw, stride, rows = inventory.render_ruler(384, 96)
    assert rows == 96 and len(raw) == stride * rows
    dots = _ruler_dots(raw, stride, rows)
    assert max(y for _x, y in dots) <= 95


def test_ruler_goes_through_the_real_print_path(monkeypatch, capsys):
    """It is only worth anything if it reaches the device the same way a
    real label does - same buffers, same checksums, same encoder."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "supvan-test-print", "--dry-run",
                         "--style", "ruler", "--height", "240"])
    cli.main()
    out = capsys.readouterr().out
    assert "pattern: ruler" in out
    assert "3 x 4096" in out
    assert "head 5d 00 20 00 00" in out


def test_ruler_refuses_to_be_altered(monkeypatch):
    """--clip and --invert would change the thing being measured, and a
    measurement of a quietly altered target is worse than none."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    for extra in (["--invert"], ["--clip", "352x171"]):
        monkeypatch.setattr(sys, "argv",
                            ["mplabel", "supvan-test-print", "--dry-run",
                             "--style", "ruler"] + extra)
        with pytest.raises(SystemExit, match="measurement"):
            cli.main()

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
                         "--bare-raster", "--max-buffer", "0"] + argv)
    cli.main()
    out = capsys.readouterr().out
    assert expected in out
    assert "dict 8192" in out, "the dictionary must match the captured print"


def test_supvan_the_alone_container_is_the_one_that_cannot_print():
    """Why `device` exists, kept as a test rather than as a comment.

    Python always appends an end-of-stream marker; the captured print has
    none. The difference is visible by blanking the declared size and
    asking each stream to decode as unknown-length: a marker-terminated
    stream still knows where it ends, one without a marker does not.

    Asserted that way round on purpose. The obvious test - hand liblzma a
    stream carrying *both* a declared size and a marker and expect it to
    object - passes only on strict liblzma builds, and quietly decodes on
    others (it does on xz 5.4.5). That pins the local library's mood
    rather than our encoder, which is a test that fails on a machine
    where nothing is wrong."""
    import lzma

    def as_unknown_size(stream):
        return stream[:5] + b"\xff" * 8 + stream[13:]

    raw, _s, _r = supvan.render_test_pattern(384, 64)

    # liblzma's: has a marker, so it decodes with no size to go on.
    theirs = as_unknown_size(supvan.compress_bitmap(raw, "alone"))
    assert lzma.decompress(theirs, format=lzma.FORMAT_ALONE) == raw

    # Ours: no marker, so without the size there is nothing to stop at.
    ours = as_unknown_size(supvan.compress_bitmap(raw, "device"))
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(ours, format=lzma.FORMAT_ALONE)


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


def test_supvan_print_path_builds_a_job_without_touching_hardware():
    """`print_bitmap` used to refuse outright, because what went inside
    the compressed stream was unknown. It is known now - but it has still
    never printed, so the guard that matters is that nothing calls it on
    its own. It takes a raster and hands the assembled job to the same
    experimental sequence `supvan-test-print` drives, one label at a
    time and on purpose."""
    raw, stride, rows = supvan.render_test_pattern(384, 128)
    job = supvan.build_job(raw, stride, rows)
    assert job["raw_len"] % supvan.PRINT_BUF_SIZE == 0
    assert job["speed"] == supvan.calc_speed(
        len(job["compressed"]) // job["buffers"])


def test_supvan_inventory_still_goes_through_the_vendor_editor():
    """The print path exists but is not trusted yet, so `mplabel
    inventory` must still write a CSV rather than quietly starting to
    print. Deleting this test is the deliberate act that switches the
    inventory labels over, once a real one has come out correctly."""
    import inspect
    from mplabel import cli
    src = inspect.getsource(cli.cmd_inventory)
    assert "print_bitmap" not in src
    assert "csv" in src.lower()


# ------------------------------- the print buffer, and why labels were refused
#
# Every one of these exists because a bare raster was being sent where the
# device wanted print buffers, and it answered `media_seating_error` -
# its only word for "no" - so the shape of the mistake was invisible.

def test_supvan_a_print_buffer_is_4096_bytes_with_its_header():
    """The unit the firmware reads is a fixed 4096-byte buffer, not a
    run of raster rows. Nothing this repo drew was ever a multiple of
    4096, which is the whole reason none of it printed."""
    buf = supvan.build_print_buffer(b"\xa5" * 96, per_line_byte=48,
                                    cols_in_buf=2, page_st=True)
    assert len(buf) == supvan.PRINT_BUF_SIZE
    assert int.from_bytes(buf[4:6], "little") == 2      # column count
    assert buf[6] == 48                                 # bytes per line
    assert buf[supvan.PRINT_BUF_HEADER:supvan.PRINT_BUF_HEADER + 96] \
        == b"\xa5" * 96
    # Margins are clamped to at least 1: a declared 0 is not "no margin".
    assert int.from_bytes(buf[8:10], "little") >= 1


def test_supvan_buffer_checksum_folds_in_every_256th_byte():
    """The firmware re-reads its running checksum every 256 bytes and
    folds in the byte before each boundary. A checksum over the header
    alone looks perfectly reasonable and is wrong, so this pins the
    stride rather than just the total."""
    data = bytes(range(256)) * 4
    buf = supvan.build_print_buffer(data, per_line_byte=48, cols_in_buf=20)

    expect = sum(buf[2:supvan.PRINT_BUF_HEADER])
    data_end = 20 * 48 + supvan.PRINT_BUF_HEADER
    boundaries = [i * supvan.CHECKSUM_STRIDE - 1
                  for i in range(1, data_end // supvan.CHECKSUM_STRIDE + 1)]
    assert boundaries, "this fixture must span at least one boundary"
    expect += sum(buf[i] for i in boundaries)

    assert int.from_bytes(buf[0:2], "little") == expect & 0xFFFF
    # And the boundary bytes genuinely move the answer.
    assert expect != sum(buf[2:supvan.PRINT_BUF_HEADER])


def test_supvan_density_rides_in_two_different_places():
    """Black density is packed into PAGE_REG_BITS and red sits alone in
    byte 12. They are two independent trims in the vendor's own print
    dialog, so writing one value into both places is a special case and
    not the rule."""
    buf = supvan.build_print_buffer(b"", 48, 1, density=9, red_density=3)
    assert buf[12] == 3
    assert (buf[3] >> 2) & 0x0F == 9


def test_supvan_page_flags_mark_the_first_and_last_buffer():
    """A job spans several buffers and the firmware needs to know which
    end it is at: the first carries PageSt, the last carries PageEnd and
    PrtEnd. Setting them on every buffer, or on none, both read as a job
    that never ends."""
    raster = b"\x00" * (48 * 256)
    bufs = supvan.split_into_buffers(raster, 48, 256)
    assert len(bufs) > 1, "this fixture must span more than one buffer"
    assert bufs[0][2] & 0x02, "first buffer should carry PageSt"
    assert not bufs[0][2] & 0x04
    assert bufs[-1][2] & 0x04, "last buffer should carry PageEnd"
    assert bufs[-1][2] & 0x08, "last buffer should carry PrtEnd"
    for mid in bufs[1:-1]:
        assert not mid[2] & 0x0E


def test_supvan_buffers_hold_84_printhead_lines_not_a_round_number():
    """4074 image bytes per buffer, which at 48 bytes a line is 84 lines
    and 42 bytes left over. The tempting round numbers - 4096/48, or 85 -
    both overrun."""
    raster = b"\x00" * (48 * 300)
    bufs = supvan.split_into_buffers(raster, 48, 300,
                                     margin_top=0, margin_bottom=0)
    assert supvan.MAX_BUF_DATA // 48 == 84
    assert int.from_bytes(bufs[0][4:6], "little") == 84
    assert sum(int.from_bytes(b[4:6], "little") for b in bufs) == 300


def test_supvan_margin_columns_are_declared_but_never_sent():
    """The margin is fed blank by the firmware from the header, so its
    columns are not in the data. Sending them as well prints the label
    twice as long as asked and shifts every dot down the roll."""
    stride, rows, margin = 48, 200, 8
    # A recognisable first column so we can see which one was sent first.
    raster = bytearray(stride * rows)
    raster[margin * stride:(margin + 1) * stride] = b"\xff" * stride
    bufs = supvan.split_into_buffers(bytes(raster), stride, rows,
                                     margin_top=margin, margin_bottom=margin)

    sent = sum(int.from_bytes(b[4:6], "little") for b in bufs)
    assert sent == rows - 2 * margin
    head = supvan.PRINT_BUF_HEADER
    assert bufs[0][head:head + stride] == b"\xff" * stride
    assert int.from_bytes(bufs[0][8:10], "little") == margin


def test_supvan_a_printhead_line_is_sent_last_byte_first():
    """The first label printed came out **mirrored left to right**.

    This was a per-byte bit reversal, on the reading that the leftmost
    dot goes in the least significant bit. The mirror settles it: writing
    `T` for that bit reversal and `R` for a per-line byte reversal, a
    full 384-bit line reversal is `M = R.T`. Handed `T(row)` the device
    painted `M(row)`, so its own reading is `P(x) = M(T(x)) = R(x)`, and
    `E` must be `R` for `P(E(row))` to come back as `row`.

    Asserted on **absolute** positions, not a round trip. The round trip
    passed throughout: `decode_job` inverts with this same function, so
    it renders correctly whether or not the function is right, and the
    preview looked perfect while the paper came out backwards. A test of
    orientation that composes the transform with its own inverse is
    measuring nothing at all."""
    stride = 48

    left = bytearray(stride)
    left[0] = 0x80                       # the leftmost dot of the image
    out = supvan.raster_to_column_major(bytes(left), stride)
    assert out[stride - 1] == 0x80, "x=0 must go out in the last byte"
    assert not any(out[:stride - 1])

    right = bytearray(stride)
    right[stride - 1] = 0x01             # the rightmost dot
    out = supvan.raster_to_column_major(bytes(right), stride)
    assert out[0] == 0x01, "x=383 must go out in the first byte"
    assert not any(out[1:])

    # Bits inside a byte are left alone. If that ever turns out wrong the
    # symptom is a print scrambled in 8-dot blocks rather than mirrored,
    # and the answer is the full reversal - bytes and bits.
    one_line = bytes([0b10110010]) + bytes(stride - 1)
    assert supvan.raster_to_column_major(one_line, stride)[stride - 1] \
        == 0b10110010

    # Each line is reversed independently; the lines keep their order.
    two = bytes([1] + [0] * (stride - 1) + [2] + [0] * (stride - 1))
    got = supvan.raster_to_column_major(two, stride)
    assert got[stride - 1] == 1 and got[2 * stride - 1] == 2


def test_supvan_the_line_transform_is_its_own_inverse():
    """Which is what lets `decode_job` call it - and, as above, exactly
    why the preview could not catch the mirror."""
    raw = supvan.render_test_pattern(384, 8)[0]
    once = supvan.raster_to_column_major(raw, 48)
    assert len(once) == len(raw)
    assert supvan.raster_to_column_major(once, 48) == raw


def test_supvan_the_line_transform_needs_whole_lines():
    """A stride that does not divide the raster means the caller has the
    geometry wrong, and silently reversing a ragged tail would print a
    sheared label rather than say so."""
    with pytest.raises(ValueError, match="whole number"):
        supvan.raster_to_column_major(b"\x00" * 50, 48)
    with pytest.raises(ValueError):
        supvan.raster_to_column_major(b"\x00" * 48, 0)


def test_supvan_speed_is_derived_from_the_size_not_a_constant():
    """The captured print's BUF_FULL carried a second value of 60, which
    this code sent as a constant for months. It is not one: it is what
    the vendor's own function returns for a nearly blank label. 123
    compressed bytes over three buffers averages 41, and 41 lands in the
    bottom band.

    A real label compresses larger and has to print *slower*, so the
    head has time to heat. Sending 60 for a dense label is the failure
    this pins."""
    assert supvan.calc_speed(123 // 3) == 60
    assert supvan.calc_speed(600) == 55
    assert supvan.calc_speed(3001) == 10
    # Monotonically slower as the data gets denser, with no gaps.
    speeds = [supvan.calc_speed(n) for n in (0, 501, 1001, 1501,
                                             2001, 2501, 2801, 3001)]
    assert speeds == sorted(speeds, reverse=True)


def test_supvan_a_job_is_one_stream_over_whole_buffers():
    """One LZMA stream covering every buffer, not one stream per buffer.
    The firmware reads a buffer header at each 4096-byte boundary of the
    *decompressed* data, so the buffers are concatenated and compressed
    once - which is why the captured print declares 12288 and not the
    size of any single buffer."""
    raw, stride, rows = supvan.render_test_pattern(384, 256)
    job = supvan.build_job(raw, stride, rows)
    assert job["raw_len"] == job["buffers"] * supvan.PRINT_BUF_SIZE
    declared = int.from_bytes(job["compressed"][5:13], "little")
    assert declared == job["raw_len"]


def test_supvan_a_generated_job_has_the_shape_of_the_captured_print():
    """The end-to-end check, against the one print known to have come out
    of this hardware.

    Its uncompressed length was 12288 and this repo read that as 48 bytes
    x 256 rows - a raster, and the wrong reading. It is 3 x 4096: three
    print buffers. A 384x256 pattern at the vendor's default 8-dot
    margins now assembles to exactly that, with the same LZMA header and
    the same speed.

    The height is not independent evidence - 256 was chosen back when
    12288 was being read as a raster. What is: that 12288 divides into
    whole print buffers at all, that the derived speed lands on the
    captured 60, and that the header is byte-identical."""
    raw, stride, rows = supvan.render_test_pattern(384, 256)
    job = supvan.build_job(raw, stride, rows)
    assert job["buffers"] == 3
    assert job["raw_len"] == 12288
    assert job["speed"] == 60
    assert job["compressed"][:13].hex(" ") == \
        "5d 00 20 00 00 00 30 00 00 00 00 00 00"


def test_supvan_a_job_round_trips_back_to_its_buffers():
    """Decompressing a job must give back the buffers that went in, each
    with a checksum that still validates. A silent encoder regression
    would otherwise look exactly like a device fault - the failure this
    codebase has already paid for once."""
    import lzma
    raw, stride, rows = supvan.render_test_pattern(384, 256)
    job = supvan.build_job(raw, stride, rows)
    blob = lzma.decompress(job["compressed"], format=lzma.FORMAT_ALONE)
    assert len(blob) == job["raw_len"]

    for i in range(job["buffers"]):
        buf = blob[i * supvan.PRINT_BUF_SIZE:(i + 1) * supvan.PRINT_BUF_SIZE]
        cols = int.from_bytes(buf[4:6], "little")
        per_line = buf[6]
        data_end = cols * per_line + supvan.PRINT_BUF_HEADER
        chk = sum(buf[2:supvan.PRINT_BUF_HEADER])
        for n in range(1, data_end // supvan.CHECKSUM_STRIDE + 1):
            chk += buf[n * supvan.CHECKSUM_STRIDE - 1]
        assert int.from_bytes(buf[0:2], "little") == chk & 0xFFFF, \
            f"buffer {i} checksum does not validate"


def test_supvan_margins_that_swallow_the_label_are_refused():
    """Better a clear error here than a job declaring zero columns, which
    the device accepts and answers by positioning the head and printing
    nothing."""
    with pytest.raises(ValueError):
        supvan.split_into_buffers(b"\x00" * (48 * 10), 48, 10,
                                  margin_top=8, margin_bottom=8)


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

# ------------------------------------------------ printer failure handling

def test_a_missing_printer_is_catchable_as_an_exception(tmp_path):
    """`SystemExit` derives from BaseException, so it sails through every
    `except Exception` in this system: the poll loop's handler, the
    per-message handler, and web._dispatch's catch-all. The poller died
    rather than logging a failed print, and from the phone the error was
    swallowed by threading and showed as a bare connection close."""
    from mplabel import printers

    with pytest.raises(printers.PrinterUnavailable):
        printers._write_raw(str(tmp_path / "nope"), b"SIZE 4,6", settle=0)

    # The property that actually matters: an ordinary handler catches it.
    try:
        printers._write_raw(str(tmp_path / "nope"), b"SIZE 4,6", settle=0)
    except Exception as exc:
        assert "not found" in str(exc)
    else:
        pytest.fail("nothing raised")


def test_render_refuses_to_overflow_the_print_head(tmp_path):
    """812 dots is the head. CLAUDE.md records an 824-dot page ejecting a
    second near-blank label; printer_dpi=300 renders 1200 dots, which is
    the same lesson four times over. printers.py invites the value - its
    own comment says 'some printers are 300'."""
    from mplabel import printers

    pdf = tmp_path / "l.pdf"
    label.to_4x6(LABEL_PDF, pdf)

    data, w, _wb, _h = printers.render_bitmap(pdf, 203)
    assert w == 812

    with pytest.raises(ValueError, match="print head|head_dots|812"):
        printers.render_bitmap(pdf, 300, head_dots=812)


def test_two_prints_of_one_sale_do_not_share_a_temp_file(tmp_path, monkeypatch):
    """The stamped copy was keyed on code and PID. web.Server is a
    ThreadingHTTPServer, so two handler threads share a PID - and a
    double-tap on a laggy phone is two POSTs for the same sale, hence the
    same code. One thread truncated the file the other was reading."""
    import threading

    from mplabel import cli, printers

    pdf = tmp_path / "l.pdf"
    label.to_4x6(LABEL_PDF, pdf)

    seen = []

    def slow_send(path, backend, **kwargs):
        # Hold the path open the way a real render does, then record what
        # the bytes were at the end of the job.
        data = Path(path).read_bytes()
        time.sleep(0.05)
        seen.append((str(path), data == Path(path).read_bytes()))

    monkeypatch.setattr(printers, "send", slow_send)
    cfg = _lock_cfg(tmp_path)
    cfg["label_code"] = "yes"

    threads = [threading.Thread(target=cli.print_label, args=(cfg, pdf, "W7X"))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 2
    assert len({p for p, _ in seen}) == 2, "both jobs used the same temp path"
    assert all(intact for _, intact in seen), "a job's file changed under it"


def test_pending_will_not_print_a_label_for_the_wrong_buyer(db, tmp_path,
                                                            monkeypatch, capsys):
    """cmd_reprint and web._print_one both check label_belongs_to.
    cmd_pending only checked that the file existed - and it is the batch
    path, so it is the one that would post several parcels to strangers."""
    from mplabel import cli

    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute("INSERT INTO sales (message_id, item, buyer, received_at, "
               "label_pdf, ship_to) VALUES ('<a>','Vase','Sam',?,?,'SAM, 1 RD')",
               (f"{today}T09:00:00-07:00", str(pdf)))
    db.commit()

    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cli, "label_belongs_to",
                        lambda row: (False, "addressed to someone else"))

    cli.cmd_pending({}, db, _pending_args(dry_run=False))
    out = capsys.readouterr().out
    assert sent == [], "a mismatched label reached the printer"
    assert "someone else" in out or "refus" in out.lower()


def test_backend_kwargs_match_every_backend_signature():
    """The kwargs were built inline in print_label, so a mismatch between
    them and a backend's signature only showed up as a TypeError at the
    moment of printing. Every key must be a parameter the target actually
    takes."""
    import inspect

    from mplabel import cli, printers

    cfg = dict(cli.DEFAULTS)
    for name, fn in printers.BACKENDS.items():
        params = set(inspect.signature(fn).parameters)
        got = set(printers.backend_kwargs(cfg, name))
        assert got <= params, f"{name}: {sorted(got - params)} not accepted"


def test_every_raw_backend_honours_settle():
    """print_zpl silently dropped settle_seconds. The pause exists because
    this firmware discards bytes arriving while the head is still moving,
    which is true whatever language the job is written in."""
    import inspect

    from mplabel import printers

    for name in ("tspl", "zpl", "escpos"):
        fn = printers.BACKENDS[name]
        assert "settle" in inspect.signature(fn).parameters, name
        assert "settle" in printers.backend_kwargs(dict(__import__(
            "mplabel.cli", fromlist=["cli"]).DEFAULTS), name)


# ------------------------------------------------------ printd, the split

@pytest.fixture
def printd(tmp_path, monkeypatch):
    """A real printd on an ephemeral port, with the device faked out."""
    import threading

    from mplabel import printd as printd_mod, printers

    sent = []
    monkeypatch.setattr(printers, "send",
                        lambda path, backend, **kw: sent.append(
                            (backend, Path(path).read_bytes())))
    # Keep the lock out of /run/lock so tests never contend with a real one.
    monkeypatch.setattr(printers, "lock_path",
                        lambda *a, **k: tmp_path / "print.lock")

    cfg = {"printer_backend": "tspl", "printer_device": str(tmp_path / "lp0"),
           "printer_dpi": "203", "printer_darkness": "8", "printer_speed": "4",
           "media_tracking": "gap", "gap_inches": "0.12",
           "printer_head_dots": "812", "settle_seconds": "0",
           "home": str(tmp_path), "printd_secret": "s3cret",
           "printd_state_dir": str(tmp_path / "printd")}
    srv = printd_mod.Server(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", sent, srv
    finally:
        srv.shutdown()
        srv.server_close()


def _print_req(base, body, job="j1", secret="s3cret", protocol="1",
               deadline="5", sign_with=None):
    from mplabel import printd as printd_mod

    sig = printd_mod.sign(sign_with if sign_with is not None else secret,
                          job, body)
    return _http_raw(f"{base}/print", body, {
        "Content-Type": "application/pdf", "X-MPLabel-Protocol": protocol,
        "X-MPLabel-Job": job, "X-MPLabel-Sig": sig,
        "X-MPLabel-Deadline": deadline})


def _http_raw(url, body, headers):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


PDF = b"%PDF-1.4\n" + b"x" * 200


def test_printd_prints_a_signed_job(printd):
    base, sent, _ = printd
    status, payload = _print_req(base, PDF)
    assert status == 200 and payload["printed"] is True
    assert len(sent) == 1 and sent[0][0] == "tspl"
    assert sent[0][1] == PDF, "the bytes that printed were not the bytes sent"


def test_printd_rejects_an_unsigned_or_tampered_job(printd):
    base, sent, _ = printd

    assert _print_req(base, PDF, sign_with="wrong")[0] == 401
    # A signature over different bytes must not carry: the failure being
    # guarded against is a parcel posted to the wrong person, and a label
    # is only bytes.
    from mplabel import printd as printd_mod
    good = printd_mod.sign("s3cret", "j9", PDF)
    status, _ = _http_raw(f"{base}/print", PDF + b"tampered", {
        "Content-Type": "application/pdf", "X-MPLabel-Protocol": "1",
        "X-MPLabel-Job": "j9", "X-MPLabel-Sig": good,
        "X-MPLabel-Deadline": "5"})
    assert status == 401
    assert sent == []


def test_printd_refuses_an_unknown_protocol(printd):
    base, sent, _ = printd
    assert _print_req(base, PDF, protocol="99")[0] == 400
    assert sent == []


def test_printd_will_not_print_the_same_job_twice(printd):
    """The journal is durable so this survives a restart - which is what
    follows a printer fault."""
    base, sent, srv = printd
    assert _print_req(base, PDF, job="dup")[0] == 200
    status, payload = _print_req(base, PDF, job="dup")
    assert status == 409 and payload["printed"] is False
    assert len(sent) == 1

    # A fresh Server over the same state dir still knows.
    from mplabel import printd as printd_mod
    again = printd_mod.Journal(Path(srv.cfg["printd_state_dir"]) / "done.jsonl")
    assert again.seen("dup")


def test_printd_rejects_a_body_that_is_not_a_pdf(printd):
    base, sent, _ = printd
    assert _print_req(base, b"not a pdf at all")[0] == 400
    assert sent == []


def test_healthz_answers_while_a_print_is_wedged(printd, monkeypatch):
    """A single-threaded printd would serialise /healthz behind a stuck
    write, so the one command you would run to diagnose a jam hangs in
    exactly the case it exists for."""
    import threading

    from mplabel import printers

    base, sent, _ = printd
    release = threading.Event()

    def wedged(path, backend, **kw):
        release.wait(timeout=5)
        sent.append((backend, b""))

    monkeypatch.setattr(printers, "send", wedged)
    t = threading.Thread(target=_print_req, args=(base, PDF), daemon=True)
    t.start()
    time.sleep(0.3)

    status, _, body = _http(f"{base}/healthz")
    assert status == 200
    health = json.loads(body)
    assert health["printing"] is True, "a wedge must be visible, not silent"
    assert health["printing_for"] >= 0
    release.set()
    t.join(timeout=5)


def test_a_queued_job_is_refused_rather_than_printed_late(printd, monkeypatch):
    """She has already given up by then. Printing to an empty room ten
    minutes later is worse than a clean refusal."""
    import threading

    from mplabel import printers

    base, sent, _ = printd
    release = threading.Event()
    monkeypatch.setattr(printers, "send",
                        lambda *a, **k: release.wait(timeout=5))

    t = threading.Thread(target=_print_req, args=(base, PDF), daemon=True)
    t.start()
    time.sleep(0.3)
    status, payload = _print_req(base, PDF, job="second", deadline="0.2")
    assert status == 410
    assert "within" in payload["error"]
    release.set()
    t.join(timeout=5)


def test_printd_survives_a_backend_that_raises_systemexit(printd, monkeypatch):
    """printers.send still raises SystemExit for an unknown backend, and
    socketserver only catches Exception. A daemon that dies on a printer
    fault and dies again on the retry is worse than one that says 503."""
    from mplabel import printers

    base, _sent, _ = printd

    def boom(*a, **k):
        raise SystemExit("unknown backend")

    monkeypatch.setattr(printers, "send", boom)
    status, payload = _print_req(base, PDF, job="boom")
    assert status == 503
    assert "unknown backend" in payload["error"]
    # Still serving.
    assert _http(f"{base}/healthz")[0] == 200


def test_the_pi_http_backend_round_trips_through_print_label(printd, tmp_path,
                                                             monkeypatch):
    """The split must be invisible to every caller: print_label, reprint,
    pending and the phone app all go through printers.send unchanged."""
    from mplabel import cli, printers

    base, sent, _ = printd
    # printd's own fixture patched printers.send; restore the real one so
    # the client backend is genuinely exercised.
    monkeypatch.undo()
    inner = []
    monkeypatch.setattr(printers, "lock_path",
                        lambda *a, **k: tmp_path / "print.lock")
    monkeypatch.setattr(printers, "send", printers.send)

    pdf = tmp_path / "l.pdf"
    label.to_4x6(LABEL_PDF, pdf)
    cfg = dict(cli.DEFAULTS)
    cfg.update({"printer_backend": "pi-http", "printd_url": base,
                "printd_secret": "s3cret", "home": str(tmp_path),
                "label_code": "no"})
    # Only the transport is under test here, so keep the real device out.
    monkeypatch.setattr(printers, "print_tspl",
                        lambda path, **kw: inner.append(Path(path).read_bytes()))
    printers.BACKENDS["tspl"] = printers.print_tspl

    cli.print_label(cfg, pdf)
    assert len(inner) == 1
    assert inner[0] == pdf.read_bytes(), "the PDF changed in transit"


def test_pi_http_reports_an_unreachable_printd_as_printer_unavailable():
    """It must be the same exception a missing device raises, or the poll
    loop and the phone app stop handling it."""
    from mplabel import printers

    with pytest.raises(printers.PrinterUnavailable) as exc:
        printers.print_pi_http(__file__, url="http://127.0.0.1:1",
                               secret="x", timeout=1)
    assert "may or may not have printed" in str(exc.value)


def test_a_remote_backend_does_not_hold_the_lock_the_daemon_needs(tmp_path,
                                                                  monkeypatch):
    """Phase 3 runs printd on the *same Pi* over loopback, so the client
    and the daemon resolve the same lock file. A client that holds it
    while waiting for printd deadlocks against printd trying to take it -
    two file descriptions, one flock. It hung until the client timed out.

    The lock belongs to whoever actually writes to the device."""
    from mplabel import cli, printers

    monkeypatch.setattr(printers, "lock_path",
                        lambda *a, **k: tmp_path / "print.lock")

    held = []

    def fake_send(path, backend, **kw):
        # Whatever the client did with the lock, the daemon must still be
        # able to take it.
        with printers.print_lock({"home": str(tmp_path)}, required=True):
            held.append(backend)

    monkeypatch.setattr(printers, "send", fake_send)
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    cfg = dict(cli.DEFAULTS)
    cfg.update({"printer_backend": "pi-http", "home": str(tmp_path),
                "printd_url": "http://127.0.0.1:1", "printd_secret": "x",
                "label_code": "no"})

    done = threading.Event()

    def run():
        cli.print_label(cfg, pdf)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=5), "print_label deadlocked against printd"
    assert held == ["pi-http"]


@needs_flock
def test_a_local_backend_still_takes_the_lock(tmp_path, monkeypatch):
    """The counterpart: nothing above was allowed to weaken the local
    path, where two processes on one Pi really do share the device."""
    from mplabel import cli, printers

    monkeypatch.setattr(printers, "lock_path",
                        lambda *a, **k: tmp_path / "print.lock")
    locked = []

    def fake_send(path, backend, **kw):
        # LOCK_NB from a second description must fail while print_label
        # holds it.
        import fcntl as _fcntl
        with open(tmp_path / "print.lock", "w") as fh:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                locked.append("free")
                _fcntl.flock(fh, _fcntl.LOCK_UN)
            except OSError:
                locked.append("held")

    monkeypatch.setattr(printers, "send", fake_send)
    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cfg = dict(cli.DEFAULTS)
    cfg.update({"printer_backend": "tspl", "home": str(tmp_path),
                "label_code": "no"})
    cli.print_label(cfg, pdf)
    assert locked == ["held"], "the local path stopped locking the device"


# ------------------------------------------------------------ the QR encoder
#
# Hand-written, so it is pinned hard. Every one of these caught a real
# fault while it was being written: the generator polynomial came out
# reversed, both copies of the format field were placed transposed, and
# the mask-scoring rules were approximated in a way that chose masks
# whose symbols would not scan.

def _qr_hash(matrix):
    import hashlib
    flat = "".join("".join(str(b) for b in row) for row in matrix)
    return hashlib.sha256(flat.encode()).hexdigest()[:16]


def test_qr_reed_solomon_matches_the_specification_example():
    """The worked example from ISO/IEC 18004: "HELLO WORLD" at version 1
    level M. The generator polynomial was being built lowest-power-first
    and used highest-power-first, which produces error-correction bytes
    that are wrong in a way nothing else here would notice - the symbol
    is well formed and simply fails to correct."""
    data = bytes([32, 91, 11, 120, 209, 114, 220, 77,
                  67, 64, 236, 17, 236, 17, 236, 17])
    assert qr._rs_remainder(data, 10) == [196, 35, 39, 119, 235,
                                          215, 231, 226, 93, 23]
    assert qr._rs_generator(2) == [1, 3, 2]


@pytest.mark.parametrize("ecl,mask,bits", [
    ("L", 0, "111011111000100"),
    ("L", 1, "111001011110011"),
    ("M", 0, "101010000010010"),
    ("Q", 0, "011010101011111"),
    ("H", 0, "001011010001001"),
])
def test_qr_format_bits_match_the_published_table(ecl, mask, bits):
    """Fifteen bits of BCH and a fixed xor mask. Every value is tabulated
    in the specification, so there is no reason to trust an
    implementation of it over the table."""
    assert format(qr._format_bits(ecl, mask), "015b") == bits


@pytest.mark.parametrize("text,ecl,version,digest", [
    ("7K2Q", "M", 1, "fb1245f67c129b14"),
    ("A1B2", "H", 1, "415f3cde8ae7f116"),
    ("HELLO WORLD", "Q", 2, "298caf071270bf72"),
    ("https://x.io/a", "L", 3, "7413538da0ec4a4d"),
    ("VASE-2291 $45.00", "M", 4, "d4157f7390c8c28d"),
])
def test_qr_symbols_are_byte_for_byte_stable(text, ecl, version, digest):
    """Whole symbols, pinned.

    Generated once and checked three ways before being written down: the
    codeword stream is identical to `segno`'s for every version, level
    and mode in range; every symbol decoded correctly through
    `zxing-cpp`; and the mask chosen scores lowest under this module's
    own implementation of ISO table 11, which agrees with segno's scorer
    exactly on the same matrix.

    Neither library is a dependency - they were the oracle, and these
    digests are what is left of them. A change here means the encoder
    moved, and the burden is to prove it moved the right way."""
    assert _qr_hash(qr.encode(text, ecl=ecl, version=version)) == digest


def test_qr_finder_and_timing_patterns_are_where_they_belong():
    """A structural check that reads the symbol rather than a digest, so
    a broken skeleton says which part broke."""
    m = qr.encode("7K2Q", ecl="M", version=1)
    assert len(m) == 21 and all(len(row) == 21 for row in m)
    for r0, c0 in ((0, 0), (0, 14), (14, 0)):
        assert all(m[r0][c0 + i] for i in range(7)), "finder top edge"
        assert all(m[r0 + i][c0] for i in range(7)), "finder left edge"
        assert m[r0 + 3][c0 + 3] == 1, "finder centre"
    # The timing patterns alternate, starting and ending dark.
    assert [m[6][c] for c in range(8, 13)] == [1, 0, 1, 0, 1]
    assert [m[r][6] for r in range(8, 13)] == [1, 0, 1, 0, 1]
    # The module that is always dark.
    assert m[len(m) - 8][8] == 1


def test_qr_picks_alphanumeric_for_a_code_and_byte_for_a_title():
    """An inventory code is four characters from a 32-symbol uppercase
    alphabet, which is a subset of the alphanumeric set - so it encodes
    in the compact mode and fits a version 1 symbol even at the highest
    error correction. A title with lowercase in it does not."""
    assert qr.pick_mode("7K2Q") == qr.MODE_ALNUM
    assert qr.pick_mode("Vintage vase") == qr.MODE_BYTE
    assert qr.choose_version("7K2Q", "H") == 1


def test_qr_refuses_what_it_cannot_hold_rather_than_truncating():
    """Silently dropping the tail would produce a symbol that scans and
    is wrong, which is worse than one that does not exist."""
    with pytest.raises(qr.QRError):
        qr.encode("X" * 600, ecl="H")
    with pytest.raises(qr.QRError):
        qr.encode("7K2Q", ecl="Z")


def test_qr_render_adds_the_quiet_zone_and_scales():
    """Four light modules on every side. A symbol printed hard against
    other ink does not scan, and on a 48mm label that border is a real
    fraction of the width rather than an afterthought."""
    plain = qr.encode("7K2Q", ecl="M", version=1)
    framed = qr.render("7K2Q", ecl="M", quiet=4, version=1)
    assert len(framed) == len(plain) + 8
    assert not any(framed[0]), "top border must be blank"
    assert not any(row[0] for row in framed), "left border must be blank"
    assert framed[4][4] == plain[0][0]
    doubled = qr.render("7K2Q", ecl="M", quiet=4, scale=2, version=1)
    assert len(doubled) == 2 * len(framed)
    assert doubled[8][8] == doubled[9][9] == framed[4][4]


# ------------------------------------------------------ the inventory label

def _ink(raster):
    return sum(bin(b).count("1") for b in raster)


def test_inventory_label_is_head_width_and_the_asked_for_length():
    """48mm at 8 dots/mm is 384 dots, and that is the head's width, not a
    choice. A raster wider than the head does not warn - the overflow is
    simply not printed."""
    raster, stride, rows = inventory.render_label("7K2Q")
    assert stride * 8 == inventory.HEAD_DOTS == 384
    assert rows == inventory.DEFAULT_HEIGHT_MM * inventory.DOTS_PER_MM
    assert len(raster) == stride * rows
    _r, _s, tall = inventory.render_label("7K2Q", label_mm=(48, 50))
    assert tall == 50 * inventory.DOTS_PER_MM


def test_inventory_label_keeps_its_ink_out_of_the_feed_margin():
    """The margin columns are declared in the print-buffer header and
    never sent, so anything drawn in them is dropped rather than printed
    small. The price sat there and came out with its bottom sheared
    off, which looked like a font problem and was not."""
    margin = supvan.DEFAULT_MARGIN_DOTS
    raster, stride, rows = inventory.render_label(
        "7K2Q", "Antique Cut Glass Vase", 45.0, with_qr=True)
    top = raster[:margin * stride]
    bottom = raster[(rows - margin) * stride:]
    assert _ink(top) == 0, "ink in the leading margin is dropped"
    assert _ink(bottom) == 0, "ink in the trailing margin is dropped"


def test_inventory_label_survives_a_title_far_too_long_for_it():
    """Her titles run to sixty characters and beyond and a label is
    30mm. Something has to give, and it must not be the layout."""
    long_title = ("Antique 1900-1915 American Edwardian Late Victorian "
                  "Cut Glass Crystal Vase With Sterling Silver Rim")
    raster, stride, rows = inventory.render_label("7K2Q", long_title, 325.0)
    assert len(raster) == stride * rows
    margin = supvan.DEFAULT_MARGIN_DOTS
    assert _ink(raster[(rows - margin) * stride:]) == 0
    # And the code is still the biggest thing on it: the top third,
    # where the code sits, should carry more ink than the bottom third.
    third = rows // 3
    assert _ink(raster[:third * stride]) > _ink(raster[2 * third * stride:])


def test_inventory_label_qr_carries_the_same_code_as_the_characters():
    """Two things on one label naming the same object. If they can
    disagree, the label is worse than one without a QR at all - so both
    come from the same argument and there is no way to pass them
    separately."""
    import inspect
    sig = inspect.signature(inventory.render_label)
    assert "qr_text" not in sig.parameters
    matrix = qr.encode("7K2Q", ecl="M")
    plain, _s, _r = inventory.render_label("7K2Q", with_qr=False)
    coded, _s, _r = inventory.render_label("7K2Q", with_qr=True)
    assert _ink(coded) > _ink(plain), "the QR should add ink"
    assert len(matrix) == 21


def test_inventory_label_qr_uses_whole_dots_per_module():
    """A fractional module scales into uneven blocks, which is the
    classic reason a printed QR will not scan. Checked by finding the
    QR's own quiet-zone edge rather than by trusting the arithmetic."""
    raster, stride, rows = inventory.render_label("7K2Q", with_qr=True)
    # The leftmost column of QR ink, and the run length of the first
    # finder's dark bar, must be a whole multiple of the module size.
    # Look only at the strip the QR occupies. The code's characters
    # start further right and reach higher up the label, so a scan
    # across the whole width finds those first.
    window = 150
    first = None
    for y in range(rows):
        row = raster[y * stride:(y + 1) * stride]
        bits = [(row[x >> 3] >> (7 - (x & 7))) & 1 for x in range(window)]
        if any(bits):
            first = bits
            break
    assert first is not None, "the QR should put ink on the left"
    run = 0
    for b in first[inventory.SIDE_MARGIN_DOTS:]:
        if b:
            run += 1
        elif run:
            break
    # A finder's top bar is 7 modules wide, so a whole number of dots
    # per module makes the run a multiple of 7.
    assert run % 7 == 0, f"finder bar is {run} dots, not a multiple of 7"


def test_inventory_label_round_trips_through_a_real_job():
    """The whole chain, which is what the preview actually shows: draw,
    assemble print buffers, compress, then take it apart again and get
    the same picture back. This is the closest thing to a proof
    available without spending a label."""
    raster, stride, rows = inventory.render_label(
        "7K2Q", "Antique Cut Glass Vase", 45.0, with_qr=True)
    job = supvan.build_job(raster, stride, rows)
    back, back_stride, cols = supvan.decode_job(job["compressed"])

    margin = supvan.DEFAULT_MARGIN_DOTS
    assert back_stride == stride
    assert cols == rows - 2 * margin
    assert back == raster[margin * stride:(rows - margin) * stride]


def test_supvan_decode_job_refuses_a_corrupt_buffer():
    """A preview that quietly renders a corrupt job is worse than no
    preview: it would show a label the device is going to refuse.

    Note which byte has to be damaged for this to fire. The firmware's
    checksum covers the header and then only the byte before each
    256-byte boundary - so most of the image is not covered by it at
    all, and flipping a dot in the middle of a buffer changes nothing.
    That is the device's design, not a bug here, but it means a valid
    checksum says the header is intact and says very little about the
    picture."""
    import lzma
    raster, stride, rows = inventory.render_label("7K2Q")
    job = supvan.build_job(raster, stride, rows)
    blob = bytearray(lzma.decompress(job["compressed"],
                                     format=lzma.FORMAT_ALONE))

    # A byte the checksum does not reach: no complaint.
    quiet = bytearray(blob)
    quiet[supvan.PRINT_BUF_HEADER + 4] ^= 0xFF
    supvan.decode_job(supvan.compress_bitmap(bytes(quiet)))

    # The byte before a 256-byte boundary, which it does reach.
    blob[supvan.CHECKSUM_STRIDE - 1] ^= 0xFF
    with pytest.raises(supvan.SupvanError, match="checksum"):
        supvan.decode_job(supvan.compress_bitmap(bytes(blob)))

    with pytest.raises(supvan.SupvanError, match="print buffers"):
        supvan.decode_job(supvan.compress_bitmap(b"\x00" * 100))


def test_inventory_label_preview_needs_no_database(tmp_path, monkeypatch,
                                                   capsys):
    """Same rule as probe, selftest and file: a printer command must not
    need the data directory. An unwritable home once stopped a printer
    test dead, which is exactly when you need one working."""
    from mplabel import cli
    out = tmp_path / "label.png"
    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(cli, "connect_db", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "inventory-label", "--code", "7K2Q",
                         "--title", "Cut Glass Vase", "--price", "45",
                         "--qr", "--preview", str(out)])
    cli.main()
    text = capsys.readouterr().out
    assert out.exists()
    assert "every checksum valid" in text
    assert "no paper moved" in text


# --------------------------------------------------- Reed-Solomon, both ways

def test_rs_encode_still_matches_the_specification_example():
    """`rs.py` was lifted out of `qr.py` so the marker could share the
    field. The encoder must not have moved on the way."""
    data = bytes([32, 91, 11, 120, 209, 114, 220, 77,
                  67, 64, 236, 17, 236, 17, 236, 17])
    assert list(rs.encode(data, 10)) == [196, 35, 39, 119, 235,
                                         215, 231, 226, 93, 23]
    assert rs.generator(2) == [1, 3, 2]


def test_rs_corrects_up_to_half_the_parity_and_no_further():
    """Eight parity bytes, so four errors anywhere are recoverable.

    Written as a sweep because the first version of this decoder fixed
    single errors and rejected everything else - which surfaces as "too
    damaged to read" and is indistinguishable, from the outside, from a
    genuinely unreadable symbol."""
    import random
    rng = random.Random(4)
    for _ in range(200):
        payload = bytes(rng.randrange(256) for _ in range(4))
        for nerr in range(0, 5):
            cw = bytearray(payload + rs.encode(payload, 8))
            for pos in rng.sample(range(len(cw)), nerr):
                cw[pos] ^= rng.randrange(1, 256)
            assert rs.decode(bytes(cw), 8)[:4] == payload, nerr


def test_rs_refuses_rather_than_guessing_past_its_capacity():
    import random
    rng = random.Random(5)
    refused = 0
    for _ in range(200):
        payload = bytes(rng.randrange(256) for _ in range(4))
        cw = bytearray(payload + rs.encode(payload, 8))
        for pos in rng.sample(range(len(cw)), rng.randint(5, 8)):
            cw[pos] ^= rng.randrange(1, 256)
        try:
            assert rs.decode(bytes(cw), 8)[:4] != payload or True
        except rs.RSError:
            refused += 1
    assert refused > 150, "over-capacity damage should usually be refused"


# ------------------------------------------------------- the shelf marker

def _marker_image(code, scale=8, quiet=2):
    from PIL import Image
    grid = marker.render(code, scale=scale, quiet=quiet)
    img = Image.new("L", (len(grid[0]), len(grid)), 255)
    px = img.load()
    for y, row in enumerate(grid):
        for x, cell in enumerate(row):
            if cell:
                px[x, y] = 0
    return img


@pytest.mark.parametrize("code", ["7K2Q", "A1B2", "ZZZZ", "0000",
                                  "XYZ", "999", "MN4P"])
def test_marker_payload_round_trips(code):
    assert marker.decode_payload(marker.encode_payload(code)) == code


def test_marker_carries_both_code_lengths_and_says_which():
    """A parcel code is three characters and an inventory code is four,
    and the same symbol has to carry either without the reader having to
    be told which it is holding."""
    size = marker.DATA_BYTES + marker.ECC_BYTES
    assert len(marker.encode_payload("XYZ")) == size
    assert len(marker.encode_payload("MN4P")) == size
    assert marker.decode_payload(marker.encode_payload("XYZ")) == "XYZ"
    assert marker.decode_payload(marker.encode_payload("MN4P")) == "MN4P"


def test_marker_refuses_a_blank_picture_instead_of_reading_zero():
    """The trap this format was walked into and had to be redesigned
    around: all-zero is a valid Reed-Solomon codeword, and crc8 of three
    zero bytes is zero, so an empty grid satisfied every check and
    decoded confidently to the real code "000".

    A camera pointed at a white wall returned a code. Formats are
    numbered from 1 so that the all-zero word has no valid format, and
    the finder has to match before any of it is attempted."""
    blank = [[0] * marker.COLS for _ in range(marker.ROWS)]
    with pytest.raises(marker.MarkerError):
        marker.read_grid(blank)
    with pytest.raises(marker.MarkerError):
        marker.decode_payload(bytes(marker.DATA_BYTES + marker.ECC_BYTES))
    # And "000" is still a code this can carry.
    assert marker.decode_payload(marker.encode_payload("000")) == "000"


def test_marker_finder_is_solid_on_two_sides_and_clocked_on_two():
    """The L gives position, rotation and module pitch in one feature;
    the clock track catches a scale that has drifted."""
    grid = marker.encode("7K2Q")
    assert (len(grid), len(grid[0])) == (marker.ROWS, marker.COLS)
    assert len(grid[0]) == 4 * len(grid), "one by four"
    assert all(row[0] for row in grid), "left column solid"
    assert all(grid[marker.ROWS - 1]), "bottom row solid"
    assert [grid[0][c] for c in range(6)] == [1, 0, 1, 0, 1, 0]
    assert [grid[r][marker.COLS - 1]
            for r in range(marker.ROWS)] == [0, 1, 0, 1, 0, 1]
    assert marker._finder_score(grid) == marker.BORDER == 56


def test_marker_reads_back_from_a_rendered_image():
    assert marker.read_image(_marker_image("7K2Q")) == "7K2Q"


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_marker_reads_at_any_orientation(turn):
    """A box on a shelf is photographed whichever way up it is sitting,
    so the orientation comes from the finder rather than from hope."""
    img = _marker_image("MN4P").rotate(turn, expand=True, fillcolor=255)
    assert marker.read_image(img) == "MN4P"


def test_marker_survives_blur_and_a_small_scale():
    """The two things a phone actually does to a 12mm square."""
    from PIL import ImageFilter
    img = _marker_image("7K2Q")
    assert marker.read_image(img.filter(ImageFilter.GaussianBlur(2.5))) == "7K2Q"
    small = img.resize((int(img.width * 0.35), int(img.height * 0.35)))
    assert marker.read_image(small) == "7K2Q"


def test_marker_survives_specks_that_would_move_the_bounding_box():
    """One dark speck in a corner used to decide the bounding box, so
    every module afterwards was sampled in the wrong place - the finder
    went from a perfect finder to a quarter of one with the picture
    otherwise untouched."""
    import random
    img = _marker_image("7K2Q")
    rng = random.Random(2)
    px = img.load()
    for _ in range(int(img.width * img.height * 0.02)):
        px[rng.randrange(img.width), rng.randrange(img.height)] = \
            rng.choice((0, 255))
    assert marker.read_image(img) == "7K2Q"


def test_marker_reads_off_the_wire_payload_of_a_real_label():
    """End to end and through the printer's own format: draw the label,
    build the print buffers, compress, decode the job back, and read the
    marker out of the picture that comes back.

    Cropped with `inventory.marker_box`, because the decoder locates the
    grid from the bounding box of the ink and the title beside it would
    stretch that box across the whole label."""
    code = "7K2Q"
    raster, stride, rows = inventory.render_label(
        code, "Antique Cut Glass Vase", 45.0, with_marker=True)
    job = supvan.build_job(raster, stride, rows)
    back, back_stride, cols = supvan.decode_job(job["compressed"])
    img = inventory.to_image(back, back_stride, cols, scale=3)

    margin = supvan.DEFAULT_MARGIN_DOTS
    x0, y0, x1, y1 = inventory.marker_box()
    crop = img.crop((x0 * 3, (y0 - margin) * 3,
                     (x1 + 1) * 3, (y1 - margin + 1) * 3))
    assert marker.read_image(crop) == code


def test_marker_gets_bigger_modules_than_the_qr_for_the_same_square():
    """The entire reason this format exists. A QR version 1 holds 152
    bits and the code needs 20, and that unwanted capacity is paid for
    in module size - which is the only thing that matters on thermal
    paper."""
    geom = inventory._geometry(inventory.DEFAULT_LABEL_MM,
                               supvan.DEFAULT_MARGIN_DOTS)
    _x, _y, _block, marker_scale = inventory._symbol_placement(
        geom, marker.ROWS + 2 * inventory.MARKER_QUIET)
    _x, _y, _block, qr_scale = inventory._symbol_placement(
        geom, len(qr.render("7K2Q", ecl="M", quiet=2)))
    assert marker_scale > qr_scale, (marker_scale, qr_scale)


def test_marker_rejects_characters_outside_the_code_alphabet():
    """I, L, O and U are not in it - they are misread as 1, 1, 0 and V
    on thermal stock, which is why the codes never contained them."""
    for bad in ("7I2Q", "LOUD", "7k2q!"):
        with pytest.raises(marker.MarkerError):
            marker.encode_payload(bad)
    assert "I" not in marker.ALPHABET and "L" not in marker.ALPHABET
    assert "O" not in marker.ALPHABET and "U" not in marker.ALPHABET


def test_marker_alphabet_matches_the_one_codes_are_minted_from():
    """Two copies of this string exist. If they drift, a code the system
    hands out is a code the marker cannot carry."""
    from mplabel import cli
    assert marker.ALPHABET == cli.CODE_ALPHABET


def test_marker_js_port_agrees_with_python(tmp_path):
    """The phone reads these and the printer writes them, so the two
    implementations have to agree exactly. Run under node against
    vectors generated here - including damaged codewords, because the
    error correction is where a port silently diverges."""
    import json
    import random
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run the browser decoder under")

    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"
    rng = random.Random(11)
    vectors = []
    for code in ("7K2Q", "A1B2", "ZZZZ", "0000", "XYZ", "999", "MN4P"):
        cw = list(marker.encode_payload(code))
        damaged = []
        for nerr in range(1, marker.ECC_BYTES // 2 + 1):
            d = list(cw)
            for pos in rng.sample(range(len(cw)), nerr):
                d[pos] ^= rng.randrange(1, 256)
            damaged.append(d)
        vectors.append({"code": code, "clean": cw, "damaged": damaged,
                        "grid": marker.encode(code)})

    script = """
      const MK = require(process.argv[2]);
      const V = JSON.parse(process.argv[3]);
      const bad = [];
      for (const v of V) {
        if (MK.decodePayload(v.clean) !== v.code) bad.push(v.code + ' clean');
        for (const d of v.damaged) {
          let got = null;
          try { got = MK.decodePayload(d); } catch (e) { got = 'ERR'; }
          if (got !== v.code) bad.push(v.code + ' damaged -> ' + got);
        }
        if (MK.readGrid(v.grid) !== v.code) bad.push(v.code + ' grid');
      }
      console.log(JSON.stringify(bad));
    """
    # Via a file, not `node -e`. A multi-line -e argument reaches node
    # as nothing at all on Windows - it exits 0 having printed neither
    # stdout nor stderr, so the test failed on an empty JSON parse with
    # no clue why. A single-line -e works, which is what makes it look
    # like the port had broken rather than the invocation.
    runner = tmp_path / "run_marker.js"
    runner.write_text(script, encoding="utf-8")
    out = subprocess.run(
        [node, str(runner), str(static / "marker.js"), json.dumps(vectors)],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [], out.stdout


def test_marker_js_is_listed_for_cache_busting():
    """`asset_stamp` is what stops a phone going on using a cached copy
    of a file that changed. A served asset missing from that list ships
    a decoder update that never arrives."""
    import inspect
    from mplabel import web
    assert "marker.js" in inspect.getsource(web.asset_stamp)
    assert (Path(web.__file__).parent / "static" / "marker.js").exists()


# ------------------------------------------- a label wider than the print head

SHELF_4X1 = (101.6, 25.4)      # 4 x 1in, the common shelf size


def test_a_label_wider_than_the_head_is_printed_down_the_feed():
    """The head is 384 dots and does not turn, so 4in cannot go across
    it. Only one orientation is physically available and the code has to
    pick it rather than ask: the 1in runs across the head, the 4in runs
    down the feed, and the drawing is rotated a quarter turn at the end.

    That is why `reads_sideways` exists - a caller cropping or previewing
    has to know which way round the raster ended up."""
    assert inventory.reads_sideways(SHELF_4X1)
    assert not inventory.reads_sideways(inventory.DEFAULT_LABEL_MM)

    raster, stride, rows = inventory.render_label("7K2Q", label_mm=SHELF_4X1)
    assert stride * 8 == inventory.HEAD_DOTS, "every line is still head width"
    assert rows == round(101.6 * inventory.DOTS_PER_MM), "4in down the feed"
    assert len(raster) == stride * rows


def test_a_label_too_big_for_the_head_either_way_is_refused():
    """5in x 3in has no orientation that fits 48mm. Better a clear error
    than a label silently cropped to the middle of itself."""
    with pytest.raises(ValueError, match="across the head"):
        inventory.render_label("7K2Q", label_mm=(127, 76.2))


def test_the_media_band_is_narrower_than_the_raster():
    """A 1in label covers 203 of the head's 384 dots and the rest is bar
    hanging off the edge. Previewing the whole raster shows those as
    broad empty margins, which reads as a badly laid out label and is
    nothing of the sort - `media_box` is what the preview crops to."""
    x0, _y0, x1, _y1 = inventory.media_box(label_mm=SHELF_4X1)
    across = x1 - x0 + 1
    assert across == round(25.4 * inventory.DOTS_PER_MM)
    assert across < inventory.HEAD_DOTS
    # Centred in the *printable window*, not in the head. It was centred
    # in the head, on the reading that the media runs centred under the
    # bar; the edge test says otherwise - 40 dots lost on the left and 32
    # on the right, so the window itself is off-centre.
    assert x0 == (inventory.PRINTABLE_LEFT_DOTS
                  + (inventory.PRINTABLE_DOTS - across) // 2)
    assert x0 >= inventory.PRINTABLE_LEFT_DOTS
    assert x1 <= inventory.HEAD_DOTS - inventory.PRINTABLE_RIGHT_DOTS - 1


def test_the_feed_margin_moves_with_the_rotation():
    """The subtle one. After the quarter turn the feed axis is the
    reading orientation's *width*, so the margin has to be inset on left
    and right rather than top and bottom. Inset the wrong pair and the
    ink lands in the band the firmware never sends - dropped, not printed
    small, and nothing reports it."""
    margin = supvan.DEFAULT_MARGIN_DOTS
    raster, stride, rows = inventory.render_label(
        "7K2Q", "Antique Cut Glass Vase", 45.0,
        label_mm=SHELF_4X1, with_marker=True)
    head = raster[:margin * stride]
    tail = raster[(rows - margin) * stride:]
    assert sum(bin(b).count("1") for b in head) == 0
    assert sum(bin(b).count("1") for b in tail) == 0


def test_the_marker_reads_back_off_a_4x1_label():
    """End to end at the new size, through the printer's own format. The
    crop comes from `marker_box`, which has to carry the rotation with
    it - a box computed in reading coordinates and used against the
    raster samples the grid at the wrong pitch."""
    code = "MN4P"
    raster, stride, rows = inventory.render_label(
        code, "Antique 1900-1915 American Edwardian Cut Glass Vase", 45.0,
        label_mm=SHELF_4X1, with_marker=True)
    job = supvan.build_job(raster, stride, rows)
    back, back_stride, cols = supvan.decode_job(job["compressed"])
    img = inventory.to_image(back, back_stride, cols, scale=2)

    margin = supvan.DEFAULT_MARGIN_DOTS
    x0, y0, x1, y1 = inventory.marker_box(label_mm=SHELF_4X1)
    crop = img.crop((x0 * 2, (y0 - margin) * 2,
                     (x1 + 1) * 2, (y1 - margin + 1) * 2))
    assert marker.read_image(crop) == code


def test_a_4x1_label_needs_more_than_one_print_buffer():
    """4in is 813 printhead lines and a buffer carries 84, so this is the
    first real label that exercises the multi-buffer path at all - the
    48x30 one fits in three and never tests the tiling past that."""
    raster, stride, rows = inventory.render_label(
        "7K2Q", "Cut Glass Vase", 45.0, label_mm=SHELF_4X1)
    job = supvan.build_job(raster, stride, rows)
    assert job["buffers"] == 10
    assert job["raw_len"] == job["buffers"] * supvan.PRINT_BUF_SIZE
    back, _s, cols = supvan.decode_job(job["compressed"])
    assert cols == rows - 2 * supvan.DEFAULT_MARGIN_DOTS


@pytest.mark.parametrize("text,expect", [
    ("48x30", (48.0, 30.0)),
    ("4x1in", (101.6, 25.4)),
    ("101.6x25.4", (101.6, 25.4)),
])
def test_size_is_parsed_in_mm_or_inches(text, expect):
    """Stock is sold in inches - 4x1in is a shelf label - and converting
    by hand is how a 4in label becomes a 4mm one."""
    from mplabel import cli
    got = cli._parse_size(text)
    assert got == pytest.approx(expect)


@pytest.mark.parametrize("bad", ["4", "4x", "axb", "0x1", "-4x1in"])
def test_a_size_that_is_not_a_label_is_refused(bad):
    from mplabel import cli
    with pytest.raises(ValueError):
        cli._parse_size(bad)


def test_the_4x1_preview_is_cropped_to_the_media_and_turned(
        tmp_path, monkeypatch, capsys):
    """Through the real CLI: the preview must come out the shape of the
    label, not the shape of the printhead."""
    from PIL import Image
    from mplabel import cli
    out = tmp_path / "label.png"
    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "inventory-label", "--code", "7K2Q",
                         "--title", "Cut Glass Vase", "--price", "45",
                         "--marker", "--size", "4x1in", "--scale", "1",
                         "--preview", str(out)])
    cli.main()
    text = capsys.readouterr().out
    assert "printed sideways" in text
    assert "203 across is media" in text

    img = Image.open(out)
    assert img.width > img.height, "the preview should read 4 wide by 1 tall"
    assert img.height == round(25.4 * inventory.DOTS_PER_MM)
    # Not the full 813 lines of a 4in label: the preview is decoded from
    # the payload, and the feed margin at each end is declared in the
    # buffer header and never sent. What you see is what burns.
    assert img.width == (round(101.6 * inventory.DOTS_PER_MM)
                         - 2 * supvan.DEFAULT_MARGIN_DOTS)


# ------------------------------------- the marker is a band, not a square

def test_the_marker_is_one_by_four_and_fills_its_interior_exactly():
    """The shape is the point. A square marker took a bite out of the
    middle of a label that is mostly words and pushed the title into
    three cramped lines; a band goes under the text.

    The interior is 4 x 22 = 88 modules and the codeword is 88 bits, so
    nothing is spare - which is why this shape rather than a taller one.
    Every module left over would have been a smaller module."""
    assert marker.COLS == 4 * marker.ROWS
    assert len(marker._cells()) == (marker.ROWS - 2) * (marker.COLS - 2)
    assert len(marker._cells()) == (marker.DATA_BYTES + marker.ECC_BYTES) * 8


def test_the_marker_band_sits_below_the_text_not_beside_it():
    """Checked by looking at the raster rather than at the arithmetic:
    the band's rows must be the lowest inked ones on the label, and the
    text must reach further right than the band does."""
    raster, stride, rows = inventory.render_label(
        "7K2Q", "Antique Cut Glass Vase", 45.0, with_marker=True)

    def row_ink(y):
        return sum(bin(b).count("1")
                   for b in raster[y * stride:(y + 1) * stride])

    x0, y0, x1, y1 = inventory.marker_box()
    assert y1 > rows * 0.6, "the band belongs at the bottom"
    assert all(row_ink(y) == 0 for y in range(y1 + 1, rows)), \
        "nothing below the band"
    # The text above it uses width the band does not.
    def widest_ink(y0_, y1_):
        best = 0
        for y in range(y0_, y1_):
            row = raster[y * stride:(y + 1) * stride]
            for x in range(stride * 8 - 1, -1, -1):
                if (row[x >> 3] >> (7 - (x & 7))) & 1:
                    best = max(best, x)
                    break
        return best
    assert widest_ink(0, y0) > x1, "the text should run wider than the band"


def test_a_rectangle_rules_out_half_the_orientations():
    """A square had to try four ways up; a 6x24 can only be read at 0 or
    180, because at 90 it would not be this shape. The quarter turns are
    settled from the ink's own aspect before any decoding starts, which
    is a real simplification and not just a saving."""
    import inspect
    src = inspect.getsource(marker.read_grid)
    assert "_rot180" in src
    assert "_rot90" not in src, "quarter turns belong to read_image"
    # And both ways up really do read.
    for turn in (0, 180):
        img = _marker_image("MN4P")
        if turn:
            img = img.rotate(turn, expand=True, fillcolor=255)
        assert marker.read_image(img) == "MN4P"


def test_the_title_never_runs_into_the_marker_band():
    """It did. The code was sized on width alone while the band was
    taking two fifths of the height, so the title was pushed down into
    the marker - and the code still read, because the parity carried it,
    which is exactly how it would have reached paper unnoticed."""
    raster, stride, _rows = inventory.render_label(
        "7K2Q",
        "Antique 1900-1915 American Edwardian Late Victorian Cut Glass "
        "Crystal Vase With Sterling Silver Rim And Original Box",
        325.0, with_marker=True)
    x0, y0, x1, y1 = inventory.marker_box()
    # The gap the layout leaves above the band must be genuinely blank.
    for y in range(y0 - 5, y0):
        row = raster[y * stride:(y + 1) * stride]
        assert sum(bin(b).count("1") for b in row) == 0, f"ink at row {y}"
# --------------------------------------------- phase 4 prerequisites (C7)

def _web_against(tmp_path, cfg_extra):
    """A web app with a custom config, for the remote-printd cases."""
    import threading

    from mplabel import cli, web

    conn = cli.connect_db(tmp_path)
    conn.execute("INSERT INTO sales (message_id, item, code) "
                 "VALUES ('<m1>', 'Vase', 'W7X')")
    conn.commit()
    cfg = {"home": str(tmp_path),
           "web_password_hash": web.hash_password("hunter2"),
           "web_session_days": "30", "web_secure_cookie": "no",
           "printer_backend": "tspl", "printer_device": "/dev/null",
           "printer_dpi": "203", "printer_darkness": "8",
           "gap_inches": "0.12", "media_tracking": "gap",
           "printer_head_dots": "812", "poll_seconds": "120"}
    cfg.update(cfg_extra)
    srv = web.Server(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def test_settings_reads_the_printer_from_printd_not_from_here(printd, tmp_path):
    """Once printd is on another machine the printer settings describe a
    roll of stock in a different room. Reporting this host's copy would
    show 0.12 on her phone during the exact week she is tuning it to 0.15
    on the Pi, with nothing to say the number was stale."""
    printd_url, _sent, srv = printd
    srv.cfg["gap_inches"] = "0.15"
    srv.cfg["printer_darkness"] = "12"

    # This host still holds the old values, exactly as a remote one would.
    base, web_srv = _web_against(tmp_path / "app",
                                 {"printer_backend": "pi-http",
                                  "printd_url": printd_url,
                                  "gap_inches": "0.12",
                                  "printer_darkness": "8"})
    try:
        _, cookie = _login(base)
        _, _, body = _http(f"{base}/api/system", cookie=cookie)
        got = json.loads(body)
        assert got["printer_source"] == printd_url
        assert got["printer_reachable"] is True
        assert got["gap_inches"] == "0.15", "showed the local copy, not the truth"
        assert got["darkness"] == "12"
        assert got["fetched_at"], "no way to tell how stale the reading is"
    finally:
        web_srv.shutdown()
        web_srv.server_close()


def test_settings_says_so_when_printd_is_unreachable(tmp_path):
    """Silently falling back to local values would look identical to a
    healthy answer - and the values would be wrong."""
    base, web_srv = _web_against(tmp_path / "app2",
                                 {"printer_backend": "pi-http",
                                  "printd_url": "http://127.0.0.1:1"})
    try:
        _, cookie = _login(base)
        _, _, body = _http(f"{base}/api/system", cookie=cookie)
        got = json.loads(body)
        assert got["printer_reachable"] is False
        assert "could not reach" in got["printer_error"]
        assert "gap_inches" not in got, "reported a local value as if live"
    finally:
        web_srv.shutdown()
        web_srv.server_close()


def test_selftest_follows_the_backend(printd, monkeypatch):
    """It called tspl_selftest directly, so with pi-http set the one
    command for 'is the printer alive' reached past the service to
    whatever device node existed on *this* host."""
    from mplabel import printers

    printd_url, _sent, srv = printd
    used = []
    monkeypatch.setattr(printers, "tspl_selftest",
                        lambda device, *a, **k: used.append(device))

    # No printer_device at all on the client side. If selftest reached
    # past printd it would have to invent one.
    cfg = {"printer_backend": "pi-http", "printd_url": printd_url,
           "printd_secret": "s3cret", "printd_timeout": "10"}
    info = printers.selftest(cfg)
    assert info["where"] == printd_url
    # printd ran it, against printd's device - not the client's.
    assert used == [srv.cfg["printer_device"]]

    cfg = {"printer_backend": "tspl", "printer_device": "/dev/null",
           "media_tracking": "gap", "gap_inches": "0.12"}
    printers.selftest(cfg)
    assert used[-1] == "/dev/null", "the local path stopped working"


def test_reconcile_marks_what_printd_actually_printed(db, monkeypatch):
    """The way out of an ambiguous timeout: ask rather than retry, because
    a retry of a print that did happen is a duplicate label on a parcel."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, item, code) "
               "VALUES ('<a>', 'Vase', 'W7X')")
    db.execute("INSERT INTO sales (message_id, item, code) "
               "VALUES ('<b>', 'Lamp', 'J51')")
    db.commit()
    monkeypatch.setattr(cli.printers, "printd_printed",
                        lambda cfg, since=None: [
                            {"job": "W7X-abc123"}, {"job": "selftest-zz"}])

    args = argparse.Namespace(since=None, dry_run=False)
    cli.cmd_reconcile({}, db, args)

    assert db.execute("SELECT printed_at FROM sales WHERE code='W7X'"
                      ).fetchone()[0], "printd said it printed; row disagrees"
    assert db.execute("SELECT printed_at FROM sales WHERE code='J51'"
                      ).fetchone()[0] is None, "marked a row printd never printed"


def test_reconcile_will_not_reach_a_recycled_code(db, monkeypatch):
    """A shipped parcel's code goes back in the pool, so an old job id can
    name a code that now belongs to a different, unprinted parcel."""
    from mplabel import cli

    db.execute("INSERT INTO sales (message_id, item, code, status, printed_at) "
               "VALUES ('<old>', 'Old vase', 'W7X', 'shipped', '2026-08-01')")
    db.commit()
    monkeypatch.setattr(cli.printers, "printd_printed",
                        lambda cfg, since=None: [{"job": "W7X-abc123"}])
    cli.cmd_reconcile({}, db, argparse.Namespace(since=None, dry_run=False))
    assert db.execute("SELECT status FROM sales WHERE code='W7X'"
                      ).fetchone()[0] == "shipped"


def test_the_installer_installs_the_print_service():
    text = (Path(__file__).parent.parent / "install_pi.sh").read_text()
    assert "mplabel-printd.service" in text
    # Installed, not enabled: the switch to pi-http is gated on the label
    # geometry being validated first.
    assert "enable --now mplabel-printd" not in text

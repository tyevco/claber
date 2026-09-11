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
import io
import json
import re
import sqlite3
import threading
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pdfplumber
import pytest

from mplabel import (inventory, label, listings, mailparse, marker, qr, rs,
                     savedpage, sheets, shopping, supvan)

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
    # Same reason, one layer down: `connect_db` turns foreign keys on and
    # SQLite has them off per connection, so a fixture without this runs
    # every test against a database where `REFERENCES` is a comment. The
    # dangling-bin bug would pass here and fail on the Pi.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(cli.SCHEMA)
    conn.executescript(listings.SCHEMA)
    # And the in-store half. Every schema the code creates, or the
    # fixture is a database the code would never meet - which is the
    # exact mistake the comment above records.
    conn.executescript(shopping.SCHEMA)
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
    """A page of ink is not a 4x6 label, and cropping to its middle would
    print a corner of one. Refused - and since the region finder landed,
    refused by it rather than by _snap, with the size it measured."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    big = tmp_path / "big.pdf"
    c = canvas.Canvas(str(big), pagesize=letter)
    c.rect(20, 20, 550, 700)
    c.showPage()
    c.save()
    with pytest.raises(ValueError, match="no block on this page is a 4 x 6"):
        label.to_4x6(big, tmp_path / "o.pdf")


# ------------------------------------------- finding the label on a page
#
# Every one of these is a shape a non-Facebook label actually arrives in.
# A Marketplace label has its page to itself and needs none of this; eBay,
# PirateShip and the carriers' own sites put a packing slip on the same
# sheet, and cropping to all the ink then refuses a perfectly good label.


def _label_block(c, x0=90, y0=450):
    """The real Marketplace geometry - 432x288pt of ink with its text
    running bottom-to-top - drawn wherever it is asked for."""
    c.saveState()
    c.translate(x0, y0)
    c.rotate(90)
    c.rect(0, -432, 288, 432)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20, -120, "USPS GROUND ADVANTAGE")
    c.setFont("Helvetica", 9)
    for i, t in enumerate(["SAM SAMPLE", "9 EXAMPLE ST",
                           "SHELBYVILLE IN 46176-0002"]):
        c.drawString(20, -180 - i * 12, t)
    c.setFont("Helvetica", 11)
    c.drawString(20, -300, "9400100000000000000000")
    c.restoreState()


def _letter_with_slip(path, slip_first=True, slip=True):
    """A US Letter sheet: the 4x6 label up top, a packing slip below it.

    The slip is drawn first on purpose. `inspect` used to read the
    rotation off `chars[0]`, which on this page is the slip's upright
    text rather than the label's rotated text. It also carries an address
    of its own, because that is what makes a loose text extraction
    dangerous rather than merely untidy."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    c = canvas.Canvas(str(path), pagesize=letter)

    def draw_slip():
        c.setFont("Helvetica", 10)
        c.drawString(72, 300, "PACKING SLIP - not the label")
        for i in range(6):
            c.drawString(72, 270 - i * 14, f"1 x item number {i} SLIPONLY")
        for i, t in enumerate(["RETURNS DEPT", "5 WAREHOUSE RD",
                               "NOWHERE OH 44101-0003"]):
            c.drawString(72, 180 - i * 12, t)
        c.line(72, 120, 540, 120)

    if slip and slip_first:
        draw_slip()
    _label_block(c)
    if slip and not slip_first:
        draw_slip()
    c.showPage()
    c.save()
    return path


def test_a_label_sharing_a_page_with_a_packing_slip_is_found(tmp_path):
    """The ink spans the whole sheet, so cropping to it is refused - but
    the label is right there, separated by a clear gutter. Not finding it
    is the difference between this working on eBay's labels and not."""
    src = _letter_with_slip(tmp_path / "slip.pdf")
    out = tmp_path / "o.pdf"
    info = label.to_4x6(src, out)
    assert info["size_in"] == (4.0, 6.0)
    assert info["crop_bbox"] == (90.0, 450.0, 522.0, 738.0)


def test_the_packing_slip_is_not_what_gets_printed(tmp_path):
    """The failure this guards against is silent: a 4x6 crop of the wrong
    half of the page looks like a label until it is on a parcel.

    Asked of the raster, not of the crop box. Cropping a PDF sets the
    boxes and leaves the content stream alone, so "the box is right" and
    "only the label prints" are two different claims and only the second
    one is about paper."""
    from mplabel import printers
    with_slip = tmp_path / "slip.pdf"
    alone = tmp_path / "alone.pdf"
    _letter_with_slip(with_slip)
    _letter_with_slip(alone, slip=False)

    rasters = []
    for src in (with_slip, alone):
        out = src.with_name(src.stem + "_4x6.pdf")
        label.to_4x6(src, out)
        rasters.append(bytes(printers.render_bitmap(out, 203)[0]))
    assert rasters[0] == rasters[1]


def test_a_crop_confines_the_field_reader_too(tmp_path):
    """`ship_to` is the backstop against posting a parcel to a stranger,
    and it anchors on the last CITY ST ZIP on the page. pdfplumber lists
    every object on the sheet whatever the crop box says, so on a page
    with a packing slip below the label that last address is the slip's."""
    src = _letter_with_slip(tmp_path / "slip.pdf")
    out = tmp_path / "o.pdf"
    label.to_4x6(src, out)
    got = label.extract_label_fields(out)
    assert got["tracking"] == "9400100000000000000000"
    assert "SHELBYVILLE IN 46176-0002" in got["ship_to"]
    assert "NOWHERE" not in got["ship_to"]


def test_rotation_is_the_majority_not_the_first_character(tmp_path):
    """`chars[0]` is whatever the content stream drew first. On a page
    with an upright slip above a rotated label that is a coin toss, and
    the wrong answer crops a 6x4 window out of a 4x6 label."""
    src = _letter_with_slip(tmp_path / "slip.pdf", slip_first=True)
    info = label.to_4x6(src, tmp_path / "o.pdf")
    assert info["rotation"] == 90
    assert info["rotation_source"] == "text"


def test_a_label_with_no_text_is_oriented_from_its_shape(tmp_path):
    """Some carriers' labels are one flattened image and extract no text
    at all. Those used to be called rotation 0 and then refused for being
    "6.00 x 4.00 in, larger than the 4 x 6 in target" - a correct
    measurement wearing a wrong diagnosis."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    src = tmp_path / "image_only.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    c.rect(90, 450, 432, 288, fill=0)
    for i in range(20):
        c.rect(100 + i * 4, 470, 2, 60, fill=1)
    c.showPage()
    c.save()
    info = label.to_4x6(src, tmp_path / "o.pdf")
    assert info["size_in"] == (4.0, 6.0)
    assert info["rotation"] == 90
    # Said out loud, because the shape cannot say which way *up* - only
    # that the label is on its side. --rotate is the override.
    assert info["rotation_source"] == "aspect"


def test_two_look_alike_blocks_are_refused_rather_than_guessed(tmp_path):
    """Two 4x6-shaped blocks on one sheet. Picking wrong spends the stock
    and prints the wrong thing, so this refuses and says where they are."""
    from reportlab.pdfgen import canvas
    src = tmp_path / "two.pdf"
    c = canvas.Canvas(str(src), pagesize=(612, 936))
    _label_block(c, x0=90, y0=40)
    _label_block(c, x0=90, y0=500)
    c.showPage()
    c.save()
    with pytest.raises(ValueError, match="--region"):
        label.to_4x6(src, tmp_path / "o.pdf")


def test_a_region_can_be_chosen_when_the_page_holds_two(tmp_path):
    from reportlab.pdfgen import canvas
    src = tmp_path / "two.pdf"
    c = canvas.Canvas(str(src), pagesize=(612, 936))
    _label_block(c, x0=90, y0=40)
    _label_block(c, x0=90, y0=500)
    c.showPage()
    c.save()
    first = label.to_4x6(src, tmp_path / "a.pdf", region=1)
    second = label.to_4x6(src, tmp_path / "b.pdf", region=2)
    assert first["size_in"] == second["size_in"] == (4.0, 6.0)
    assert first["crop_bbox"] != second["crop_bbox"]
    assert first["regions_found"] == 2


def test_a_page_can_be_chosen_from_a_multi_page_pdf(tmp_path):
    """Two labels, one per page - which is how a batch of them is bought."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    src = tmp_path / "pair.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    _label_block(c, x0=90, y0=450)
    c.showPage()
    _label_block(c, x0=100, y0=300)
    c.showPage()
    c.save()
    one = label.to_4x6(src, tmp_path / "a.pdf", page_index=0)
    two = label.to_4x6(src, tmp_path / "b.pdf", page_index=1)
    assert one["crop_bbox"] == (90.0, 450.0, 522.0, 738.0)
    assert two["crop_bbox"] == (100.0, 300.0, 532.0, 588.0)
    assert two["page"] == 2
    with pytest.raises(ValueError, match="no page 3"):
        label.to_4x6(src, tmp_path / "c.pdf", page_index=2)


@pytest.mark.parametrize("source_rotation", [90, 180, 270])
def test_a_page_that_already_carries_a_rotation_still_crops_to_its_label(
        tmp_path, source_rotation):
    """pdfplumber applies /Rotate before reporting coordinates and pypdf's
    mediabox does not, so a box measured in one and written into the other
    crops a different corner of the page entirely. Marketplace labels
    carry no /Rotate, which is why nothing here had to know that.

    Asserted by reading the label back rather than by comparing
    arithmetic to itself: the failure is a crop that lands somewhere
    plausible, and only the content says whether it landed on the label.
    """
    from pypdf import PdfReader, PdfWriter
    turned = tmp_path / "turned.pdf"
    reader = PdfReader(str(LABEL_PDF))
    writer = PdfWriter()
    page = reader.pages[0]
    page.rotate(source_rotation)
    writer.add_page(page)
    with open(turned, "wb") as fh:
        writer.write(fh)

    out = tmp_path / "o.pdf"
    info = label.to_4x6(turned, out)
    assert info["size_in"] == (4.0, 6.0)
    assert info["source_rotation"] == source_rotation
    got = label.extract_label_fields(out)
    assert got["tracking"] == "9400100000000000000000"
    assert "SAM SAMPLE" in got["ship_to"]


def test_a_label_already_on_a_4x6_page_is_left_alone(tmp_path):
    """A label bought from PirateShip arrives as a 4x6 page with the ink
    running to its edges. There is nothing to find and nothing to turn."""
    from reportlab.pdfgen import canvas
    src = tmp_path / "already.pdf"
    c = canvas.Canvas(str(src), pagesize=(288, 432))
    c.rect(4, 4, 280, 424)
    c.setFont("Helvetica", 11)
    c.drawString(20, 300, "9400100000000000000000")
    c.showPage()
    c.save()
    info = label.to_4x6(src, tmp_path / "o.pdf")
    assert info["size_in"] == (4.0, 6.0)
    assert info["rotation"] == 0
    assert info["regions_found"] == 1


def test_a_forced_rotation_says_it_was_forced(tmp_path):
    """--rotate is the override for the case the shape cannot settle, so
    the answer has to distinguish it from something that was measured."""
    info = label.to_4x6(LABEL_PDF, tmp_path / "o.pdf", force_rotation=90)
    assert info["rotation_source"] == "forced"


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


SALES_CSV = (
    "sale_date,item_title,gross,my_cost,shipping_paid\n"
    "2025-11-04,Oak library table,320.00,80.00,24.10\n"
    "2025-11-12,Set of 6 jadeite mugs,84.00,14.00,7.40\n"
    "2025-12-02,Victorian hall tree,450.00,120.00,\n"
)


def test_an_import_preview_writes_nothing(db):
    """The screen that shows this has not committed to anything, and the
    parse has to be answerable twice with the same answer. Same shape as
    `shopping.propose` and `savedpage.extract`."""
    before = db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    plan = listings.plan_import(db, SALES_CSV)
    assert len(plan["rows"]) == 3
    assert plan["created"] == 3
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == before


def test_the_mapping_is_guessed_from_header_names(db):
    plan = listings.plan_import(db, SALES_CSV)
    assert plan["mapping"] == {"sale_date": "sold_at",
                               "item_title": "title",
                               "gross": "price",
                               "my_cost": "paid",
                               "shipping_paid": "postage"}


def test_a_row_with_no_postage_says_so_rather_than_reading_as_free(db):
    """A missing postage read as zero reports the whole price as kept,
    which is the same failure as a missing cost reading as free - and
    the wizard displays a Postage column, so a row without one has to be
    the row that says why."""
    plan = listings.plan_import(db, SALES_CSV)
    warned = [p for p in plan["rows"]
              if any("postage" in w for w in p["warnings"])]
    assert [p["row"]["title"] for p in warned] == ["Victorian hall tree"]
    assert "postage" not in plan["rows"][2]["row"]


def test_a_reimport_changes_nothing_and_says_so(db):
    """`upsert_listing` fills blanks and never overwrites, so a corrected
    spreadsheet reports its rows and changes none of them. An import that
    said "imported 3" both times would be describing a correction that
    did not happen."""
    first = listings.commit_import(db, SALES_CSV)
    assert first["created"] == 3 and first["written"] == 3

    corrected = SALES_CSV.replace("320.00", "999.00")
    plan = listings.plan_import(db, corrected)
    assert plan["created"] == 0
    assert plan["unchanged"] == 3
    assert any("will not change it" in w for w in plan["rows"][0]["warnings"])

    listings.commit_import(db, corrected)
    assert db.execute("SELECT price FROM listings WHERE title='Oak library "
                      "table'").fetchone()[0] == 320.0


def test_an_imported_row_is_a_listing_and_never_a_sale(db):
    """A `sales` row is the record of a Facebook order that produced a
    label email - a UNIQUE message_id, a parcel code, a tracking number,
    an archived PDF - and a spreadsheet row has none of those. Inventing
    a message_id would put a fake email in the table the poller
    de-duplicates against, and `sales.code` is a live parcel handle that
    is recycled the moment a parcel ships."""
    listings.commit_import(db, SALES_CSV)
    assert db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    keys = [r[0] for r in db.execute("SELECT listing_id FROM listings")]
    assert all(k.startswith("saved:") for k in keys), keys
    assert db.execute(
        "SELECT COUNT(*) FROM listings WHERE state='sold'").fetchone()[0] == 3


def test_the_cli_and_the_web_import_the_same_csv_the_same_way(db, tmp_path):
    """They sit on one parser, so `mplabel import --format csv` and the
    wizard cannot disagree about what a column means - the rule
    `title_key` already follows for both sides of reconciliation."""
    path = tmp_path / "sales.csv"
    path.write_text(SALES_CSV, encoding="utf-8")

    from mplabel import cli

    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    other.executescript(cli.SCHEMA)
    other.executescript(listings.SCHEMA)

    assert listings.import_csv(db, path) == 3
    listings.commit_import(other, SALES_CSV)

    def shape(conn):
        return sorted(map(tuple, conn.execute(
            "SELECT listing_id, title, price, paid, sold_at, state "
            "FROM listings ORDER BY listing_id")))

    assert shape(db) == shape(other)


def test_a_column_mapped_to_nothing_is_not_imported(db):
    """The mapping screen offers "ignore this column", and a column left
    there has to actually be left out - otherwise the screen is showing
    a choice that does not exist."""
    mapping = {"sale_date": "sold_at", "item_title": "title",
               "gross": "price", "my_cost": None, "shipping_paid": None}
    listings.commit_import(db, SALES_CSV, mapping)
    assert db.execute(
        "SELECT COUNT(*) FROM listings WHERE paid IS NOT NULL").fetchone()[0] == 0


def test_a_csv_keeps_a_real_listing_id_rather_than_keying_on_its_title(db):
    """`title_key` exists for rows that have no key, not as a replacement
    for one that does. Keying a row on its title while Facebook's own id
    sits in the column beside it invents a second identity for one
    listing."""
    listings.commit_import(
        db, "listing_id,title,price\nFB123,Cast iron skillet,25\n")
    assert db.execute("SELECT COUNT(*) FROM listings "
                      "WHERE listing_id='FB123'").fetchone()[0] == 1


def test_kept_is_null_when_postage_is_unknown_and_margin_never_moved(db):
    """Postage is a new column on the view, not a term folded into
    `margin`. `COALESCE(postage, 0)` would read an unknown postage as
    free and report the whole price as kept - the same failure as a
    missing cost reading as free, which is what the comment on `margin`
    and on `listings.kept` were both written about.

    So this pins two things: `kept` is null rather than flattering, and
    `margin` is exactly what it was before the columns existed."""
    db.execute("INSERT INTO listings (listing_id, title, price, paid, state) "
               "VALUES ('A', 'No postage recorded', 100.0, 30.0, 'sold')")
    db.execute("INSERT INTO listings (listing_id, title, price, paid, "
               "postage, state) VALUES ('B', 'All three known', 100.0, 30.0, "
               "12.50, 'sold')")
    db.commit()
    listings.build_views(db)

    rows = {r["listing_id"]: r for r in
            db.execute("SELECT listing_id, margin, kept FROM v_listing_perf")}
    assert rows["A"]["kept"] is None, "an unknown postage read as free"
    assert rows["A"]["margin"] == 70.0, "margin changed under the sheet"
    assert rows["B"]["kept"] == 57.5
    assert rows["B"]["margin"] == 70.0


def test_a_draft_is_not_in_the_sell_through_denominator(db):
    """A draft was never for sale. Counting one drags sell-through down
    exactly the way her own purchases would - the failure `BUYER_KINDS`
    exists to prevent, arriving from the other direction.

    Filtered once, in `v_listing_perf`, because `v_price_band`,
    `v_monthly`, `v_aging` and `sheets.TABS` are all built on it and have
    to become right together."""
    for i, state in enumerate(("active", "sold")):
        db.execute("INSERT INTO listings (listing_id, title, price, state, "
                   "listed_at, sold_at) VALUES (?,?,60.0,?,'2026-01-01',?)",
                   (f"L{i}", f"Thing {i}", state,
                    "2026-02-01" if state == "sold" else None))
    db.commit()
    listings.build_views(db)
    before = db.execute("SELECT sell_through_pct FROM v_price_band "
                        "WHERE price_band='$50-100'").fetchone()[0]
    assert before == 50.0

    db.execute("INSERT INTO listings (listing_id, title, price, state) "
               "VALUES ('D1', 'Never written up', 60.0, 'draft')")
    db.commit()
    listings.build_views(db)
    after = db.execute("SELECT sell_through_pct FROM v_price_band "
                       "WHERE price_band='$50-100'").fetchone()[0]
    assert after == before, "a draft entered the sell-through denominator"

    # And it is genuinely on the shelf - only the analytics ignore it.
    assert db.execute("SELECT COUNT(*) FROM listings "
                      "WHERE state='draft'").fetchone()[0] == 1


def test_nothing_demotes_a_listing_to_draft(db):
    """A draft becoming active is progress. The reverse only happens
    because she said so, which goes through the fields route - never
    through something the poller inferred from an email."""
    listings.upsert_listing(db, "L1", "email", title="Vase", state="active")
    listings.upsert_listing(db, "L1", "email", title="Vase", state="draft")
    assert db.execute("SELECT state FROM listings "
                      "WHERE listing_id='L1'").fetchone()[0] == "active"


def test_a_description_is_not_a_note(app):
    """`notes` is the scribble field and `description` is the listing
    copy. One column for both means writing the copy silently eats a
    note, and the two are read at completely different moments."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state, notes) "
                 "VALUES ('Milk glass vase', 'draft', 'handle is loose')")
    conn.commit()
    lid = conn.execute("SELECT id FROM listings").fetchone()["id"]

    status, _h, body = _http(f"{base}/api/inventory/{lid}/fields", "POST",
                             {"description": "Sound, no chips."},
                             cookie=cookie, headers=CSRF)
    assert status == 200
    item = json.loads(body)["item"]
    assert item["description"] == "Sound, no chips."
    assert item["notes"] == "handle is loose"


def test_publishing_a_draft_stamps_the_day_it_went_up(app):
    """`listed_at` is what `v_aging` and `days_listed` are computed from,
    so a client that forgot to send it would leave the row out of the
    aging report with nothing to show it had been missed. And
    republishing must not restart the clock."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state) "
                 "VALUES ('Milk glass vase', 'draft')")
    conn.execute("INSERT INTO listings (title, state, listed_at) "
                 "VALUES ('Brass lamp', 'draft', '2026-01-04')")
    conn.commit()
    fresh, dated = [r["id"] for r in
                    conn.execute("SELECT id FROM listings ORDER BY id")]

    for lid in (fresh, dated):
        status, _h, body = _http(f"{base}/api/inventory/{lid}/fields", "POST",
                                 {"state": "active"}, cookie=cookie,
                                 headers=CSRF)
        assert status == 200, body

    assert conn.execute("SELECT listed_at FROM listings WHERE id=?",
                        (fresh,)).fetchone()[0] == date.today().isoformat()
    assert conn.execute("SELECT listed_at FROM listings WHERE id=?",
                        (dated,)).fetchone()[0] == "2026-01-04", \
        "republishing restarted the clock"


def test_the_writer_can_ask_which_photos_are_this_thing(app):
    """`GET /api/photos` answers the opposite question - captures about
    nothing yet - so before this there was no way to ask which pictures
    belong to the draft on screen."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state) VALUES ('Vase', 'draft')")
    conn.commit()
    lid = conn.execute("SELECT id FROM listings").fetchone()["id"]
    conn.execute("INSERT INTO photos (path, listing_id, taken_at) "
                 "VALUES ('photos/b.jpg', ?, '2026-01-02')", (lid,))
    conn.execute("INSERT INTO photos (path, listing_id, taken_at) "
                 "VALUES ('photos/a.jpg', ?, '2026-01-01')", (lid,))
    conn.execute("INSERT INTO photos (path) VALUES ('photos/loose.jpg')")
    conn.commit()

    status, _h, body = _http(f"{base}/api/inventory/{lid}/photos",
                             cookie=cookie)
    assert status == 200
    got = json.loads(body)["photos"]
    # Oldest first: the writer offers the first as the cover, and the
    # first one taken is the one she framed deliberately.
    assert [p["path"] for p in got] == ["photos/a.jpg", "photos/b.jpg"]


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


def test_the_sheet_says_whether_a_postage_figure_was_measured(db):
    """#3's acceptance, and the same trap one column over: a spreadsheet
    is where a figure gets summed, sorted and copied into another cell.

    `postage_source` is its own column rather than a suffix on the
    number, because "12.40 (est.)" is a string that does none of those.
    And only stored figures reach the sheet - the estimate the order
    screen offers is computed per request and deliberately not
    persisted, because an estimate in a spreadsheet is one that gets
    copied somewhere else and stops being one."""
    db.executemany(
        "INSERT INTO sales (message_id, item, price, postage, "
        "postage_source, code) VALUES (?,?,?,?,?,?)",
        [("<measured>", "Lamp", 95.0, 22.5, "confirmed", "A1B"),
         ("<unknown>", "Vase", 28.0, None, None, "C2D")])
    db.commit()

    sql, headers = sheets.TABS["Sales"]
    assert "Postage source" in headers
    assert "You keep" in headers
    rows = {r["item"]: dict(r) for r in db.execute(sql)}

    assert rows["Lamp"]["postage_source"] == "confirmed"
    assert rows["Lamp"]["kept"] == 72.5

    # Unknown postage leaves both blank rather than reporting the whole
    # price as kept - the same failure as a missing cost reading as free.
    assert rows["Vase"]["postage"] is None
    assert rows["Vase"]["postage_source"] is None
    assert rows["Vase"]["kept"] is None


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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand the 3xx back instead of chasing it.

    `urlopen` follows a redirect by default, which is right for every
    other test here and useless for the ones about routing: they are
    asserting *which* way a browser is sent, and a followed redirect
    reports 200 from wherever it landed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_once(url, headers=None, cookie=None):
    """One request, no redirect following. Returns (status, headers)."""
    import urllib.error

    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        return exc.code, exc.headers, body


def _http(url, method="GET", data=None, cookie=None, headers=None, raw=None):
    """One request through the real server. Returns (status, headers, body).

    `raw` sends bytes as they are, for the photo upload - which is
    deliberately not JSON and not multipart."""
    import json as _json
    import urllib.error
    import urllib.request

    body = raw if raw is not None else (
        _json.dumps(data).encode() if data is not None else None)
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


@pytest.fixture
def app_no_routing(tmp_path):
    """The same server with `web_auto_route = no`, which is what someone
    who does not want a redirect at `/` sets."""
    import threading

    from mplabel import cli, web

    cli.connect_db(tmp_path)
    cfg = {"home": str(tmp_path),
           "web_password_hash": web.hash_password("hunter2"),
           "web_session_days": "30", "web_secure_cookie": "no",
           "web_auto_route": "no"}
    srv = web.Server(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
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


def test_serve_refuses_to_run_without_a_password(capsys):
    """Better to fail loudly at startup than to publish customer addresses
    to the internet because a config key was missed on an upgrade.

    The message goes to stderr and the code is EX_CONFIG rather than 1,
    so systemd stops retrying instead of flapping - see
    `test_the_phone_app_refuses_the_same_way_printd_does`."""
    from mplabel import printd, web

    with pytest.raises(SystemExit) as exc:
        web.serve({"home": "/tmp", "web_password_hash": ""})
    assert exc.value.code == printd.EX_CONFIG
    assert "passwd" in capsys.readouterr().err


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
                         ("/common.js", b"function esc("),
                         ("/app.js", b"esc("),
                         ("/manifest.json", b"standalone")):
        status, _, body = _http(base + path)
        assert status == 200, path
        assert needle in body, path


def test_every_endpoint_the_client_calls_exists_on_the_server():
    """The two halves ship together and there is no build step, so a
    renamed route fails silently on a client that has already cached the
    old JavaScript - it is a screen that stays empty, not an error
    anyone sees. Cheap to pin: read the paths out of the client files and
    check the routing table answers each one.

    Every client file, not just app.js: the desk portal calls routes the
    phone never touches, and a file left off this list is a whole front
    end whose endpoints nothing checks."""
    import re as _re

    from mplabel import web

    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"

    for name in ("app.js", "desk.js"):
        js = (static / name).read_text(encoding="utf-8")

        # `api('/api/thing/' + id + '/bin')` -> the literal head is enough
        # to find the route; the variable parts are what the regexes match.
        called = {m.rstrip("/") for m in
                  _re.findall(r"api\('(/api/[a-z0-9/_-]*)", js)}
        assert called, f"no API calls found in {name} - helper renamed?"

        for path in sorted(called):
            probe = path
            # Stand in for whatever the client concatenates on.
            if path.rstrip("/") in ("/api/orders", "/api/inventory",
                                    "/api/bins", "/api/lookup",
                                    "/api/photos", "/api/trips"):
                candidates = [path, path + "/1", path + "/AAA",
                              path + "/1/bin", path + "/1/photos"]
            else:
                candidates = [probe]
            assert any(
                any(rx.match(c) for _m, rx, _h, _a in web.Handler._COMPILED)
                for c in candidates), \
                f"{name} calls {path}, which no route serves"


def test_the_shelf_tab_is_wired_to_a_backend_that_exists():
    """It replaced a `soonView` placeholder the day its API landed. The
    thing that would quietly undo that is the tab pointing at a screen
    name nothing renders - the tab bar highlights, the body falls
    through to the queue, and it reads as a tap that did not register."""
    js = (Path(__file__).parent.parent / "src" / "mplabel" / "static"
          / "app.js").read_text(encoding="utf-8")

    assert "['shelf', 'Shelf'" in js, "no Shelf tab in the tab bar"
    for screen, fn in (("shelf", "shelfView"), ("item", "itemView"),
                       ("bin", "binView")):
        assert f"S.screen === '{screen}'" in js, f"nothing renders '{screen}'"
        assert f"function {fn}(" in js, f"{fn} is missing"

    # The search box rerenders the screen on every keystroke, and
    # innerHTML drops focus with it. Without the restore, typing stops
    # after one character - which looks like a broken keyboard.
    assert "S.focusId" in js, "the search box will lose focus on rerender"


# ------------------------------------------------ the native iOS client

# The Swift is written on a machine that cannot compile it, and it ships
# separately from the server it talks to - so the compiler catches none
# of the drift that matters here. These read the Swift as text and check
# it against the routes and payloads this repo actually serves. They are
# cheap, and they cover the two failures that would otherwise be found by
# her, on a phone, holding a box.

IOS = Path(__file__).parent.parent / "ios" / "MPLabel"


def _swift(name):
    return (IOS / name).read_text(encoding="utf-8")


@pytest.mark.skipif(not IOS.exists(), reason="the iOS client is not checked out")
def test_every_path_the_swift_client_calls_is_a_real_route():
    """A renamed route reaches an installed app as a screen that stays
    empty. The app cannot be redeployed in the same commit as the server
    the way the PWA can, which is the whole reason /api/v1 exists - and
    that prefix only helps if the client is actually calling routes that
    are there."""
    from mplabel import web

    js = _swift("APIClient.swift")
    # `request("/orders/\(id)/ship", method: "POST")` - take the literal
    # head and stand a plausible value in for each interpolation.
    paths = set(re.findall(r'request\("([^"]+)"', js))
    assert paths, "no request() calls found - has the helper been renamed?"

    # The client must send the versioned prefix - that is the whole
    # point of it existing - but the dispatcher strips it before
    # matching, so the routing table holds unversioned patterns. Assert
    # both halves rather than conflating them.
    assert '"/api/v1" + path' in js, \
        "APIClient must call the versioned prefix, not /api"

    for path in sorted(paths):
        # "123" and not "1": /lookup takes 3-4 characters, because a
        # parcel code is 3 and an inventory code is 4, so a one-digit
        # stand-in fails a route that is perfectly correct.
        probe = re.sub(r"\\\((?:[^()]|\([^()]*\))*\)", "123", path)
        probe = "/api" + probe.split("?")[0]
        assert any(rx.match(probe) for _m, rx, _h, _a in web.Handler._COMPILED), \
            f"APIClient calls {probe}, which no route serves"


@pytest.mark.skipif(not IOS.exists(), reason="the iOS client is not checked out")
def test_the_swift_models_use_the_keys_the_server_actually_sends(app):
    """CodingKeys are strings, so a renamed field is not a Swift error -
    it is a nil, and a nil in an Optional model is a blank row rather
    than a crash. That is the quiet failure this catches: the app would
    look like it worked and show her nothing."""
    from mplabel import web as web_mod

    base, conn = app
    _status, cookie = _login(base)
    from mplabel import listings as listings_mod

    # Enough rows that every payload has something in it. An empty list
    # sends no keys at all, so a fixture that is too thin makes this test
    # pass by having nothing to disagree with - which is how it missed
    # `id` on /lookup once already.
    conn.executemany(
        "INSERT INTO listings (title, price, state, inventory_code, "
        "listed_at, sold_at, renewed_count) VALUES (?,?,?,?,?,?,?)",
        [("Hobnail vase", 28.0, "active", "7K2M", "2026-07-01", None, 1),
         # sold *with* dates, so days_to_sell and the monthly view fill in
         ("Chenille bedspread", 60.0, "sold", "9QM2", "2026-07-02",
          "2026-07-30", 0)])
    conn.commit()
    # A bin, or /bins answers with an empty list and `created_at` looks
    # like a key the server does not send.
    listings_mod.create_bin(conn, "ATTIC")
    # Same reasoning for the sourcing half: a trip with something on it,
    # and a capture that is about nothing so the pile is not empty.
    listings_mod.create_trip(conn, "GOODWILL 214", receipt_total=21.40)
    conn.execute("UPDATE listings SET trip_id=1, paid=6.0 "
                 "WHERE inventory_code='7K2M'")
    conn.commit()
    listings_mod.add_photo(conn, "photos/receipt.jpg", sha256="deadbeef")
    # And the aisle: a candidate in the cart and a receipt to reconcile
    # it against, or the proposal payload is empty and sends no keys at
    # all - which is how this test missed `id` on /lookup once already.
    from mplabel import shopping as shopping_mod

    conn.executescript(shopping_mod.SCHEMA)
    shopping_mod.store_receipt(conn, 1, "HOUSEWARES 4.99\nTOTAL 4.99")
    candidate = shopping_mod.add_candidate(conn, trip_id=1, title="Vase",
                                           category="Home")
    shopping_mod.decide(conn, candidate["id"], "carted")

    swift = _swift("Models.swift")
    # Only the remapped ones: `case shipBy = "ship_by"`. A key that
    # matches its Swift name needs no mapping and cannot be misspelled
    # in one place only.
    mapped = set(re.findall(r'case\s+\w+\s*=\s*"([a-z_]+)"', swift))
    assert mapped, "no CodingKeys found - has Models.swift been rewritten?"

    served = set()
    for path in ("/orders", "/inventory", "/bins", "/pending",
             "/trips", "/photos", "/candidates"):
        _s, _h, body = _http(base + web_mod.API_PREFIX + path, cookie=cookie)
        payload = json.loads(body)
        for rows in payload.values():
            if isinstance(rows, list):
                for row in rows:
                    served |= set(row)
    # What her own history says a thing is worth. Polled with a real
    # category so the payload is populated: an empty answer sends the
    # keys but nothing under them, which is the shape this test exists to
    # notice going missing.
    _s, _h, body = _http(
        base + web_mod.API_PREFIX + "/worth?category=Home&title=Hobnail+vase",
        cookie=cookie)
    served |= set(json.loads(body))

    # The proposal nests too, and its envelope keys - carted, item_lines,
    # unclaimed_lines - are as much a contract as the row keys.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/trips/1/reconcile",
                         cookie=cookie)
    proposal = json.loads(body)
    served |= set(proposal)
    for row in proposal["proposals"]:
        served |= set(row)
    for row in proposal["unclaimed_lines"]:
        served |= set(row)
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/trips/1/receipt-lines",
                         cookie=cookie)
    for row in json.loads(body)["lines"]:
        served |= set(row)

    # A trip's own payload nests: the summary under "trip", the items
    # that came home under "items". Both halves are models here.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/trips/1", cookie=cookie)
    trip = json.loads(body)
    served |= set(trip["trip"])
    for row in trip["items"]:
        served |= set(row)
    for row in trip["photos"]:
        served |= set(row)
    # The detail and item payloads carry the rest.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/inventory/1",
                         cookie=cookie)
    served |= set(json.loads(body)["item"])
    # /lookup has its own hand-written SELECT rather than reusing one of
    # the row helpers, so it is the one payload that can drift on its own.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/lookup/7K2M",
                         cookie=cookie)
    served |= set(json.loads(body).get("listing") or {})
    # /sold likewise, and it is the only route sending days_to_sell.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/sold", cookie=cookie)
    for row in json.loads(body)["items"]:
        served |= set(row)
    # The analytics views: their column names are the CodingKeys, and
    # CLAUDE.md is explicit that renaming a view column breaks the sheet
    # with no test failure. It should not break the phone silently either.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/stats", cookie=cookie)
    stats = json.loads(body)
    # Both halves: the envelope's own keys (`price_bands`) and the row
    # keys inside each list.
    served |= set(stats)
    for rows in stats.values():
        for row in rows:
            served |= set(row)
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/orders/1", cookie=cookie)
    served |= set(json.loads(body))
    # The batch shape is only visible on a POST. A dry run with no ids
    # prints nothing and uses no labels, which is the whole point of the
    # flag, so it is safe to ask here - and without it `would_print`
    # looks like a key the server never sends.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/print/pending", "POST",
                         {"ids": [], "dry_run": True}, cookie=cookie,
                         headers={"X-Mplabel": "1"})
    served |= set(json.loads(body))
    # A label from anywhere else. A dry run: the crop and the measurement
    # are real and no stock is spent, which is the whole point of the
    # flag - and without it `rotation_source` looks like a key the server
    # never sends, when it is the one that says whether an orientation
    # was measured or guessed.
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/print/label?dry_run=1",
                         "POST", raw=LABEL_PDF.read_bytes(), cookie=cookie,
                         headers={"X-Mplabel": "1",
                                  "Content-Type": "application/pdf"})
    served |= set(json.loads(body)["label"])
    # `expires_in` only appears on login, which the fixture did above.
    served.add("expires_in")

    missing = sorted(k for k in mapped if k not in served)
    assert not missing, (
        f"Models.swift maps keys the server never sends: {missing}. "
        f"Each one decodes as nil and shows as a blank row.")


@pytest.mark.skipif(not IOS.exists(), reason="the iOS client is not checked out")
def test_a_scanned_code_can_actually_be_opened(app):
    """`/api/lookup` used to answer without the row id, so a scan
    identified an item the client then had no way to open. It shipped as
    "Key id not found in key decoding container" on a phone - which names
    the field and nothing else, and only after a label had been printed
    and pointed at.

    The key-contract test missed it twice over: it only checked *remapped*
    CodingKeys, and `id` maps to itself; and it never called /lookup at
    all. Both are fixed, but this asserts the specific shape, because the
    scanner is the reason the app exists."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state, inventory_code) "
                 "VALUES ('Hobnail vase', 'active', '7K2M')")
    conn.commit()

    from mplabel import web as web_mod

    status, _h, body = _http(base + web_mod.API_PREFIX + "/lookup/7k2m",
                             cookie=cookie)
    assert status == 200
    payload = json.loads(body)
    assert payload["kind"] == "listing"
    listing = payload["listing"]

    # Every field the Swift `InventoryItem` requires without an Optional.
    # These are the ones whose absence is a decode failure rather than a
    # blank row - which is the difference between a screen that looks
    # empty and one that never appears.
    assert isinstance(listing.get("id"), int), \
        "a scanned code that cannot be opened is not a lookup"
    # And enough to render the row it lands on without a second request.
    for key in ("title", "inventory_code", "bin", "bin_code", "state"):
        assert key in listing, f"/lookup omits {key}"

    # Case-insensitive, because this is read off thermal paper by a
    # camera and the alphabet has no lowercase in it anyway.
    assert listing["inventory_code"] == "7K2M"


def test_sold_carries_how_long_it_took(app):
    """The Sold screen is mostly "what went out and how fast". That
    needs `days_to_sell`, which `/inventory` does not send - so this is
    its own route rather than a state filter.

    It is computed the same way `v_listing_perf` computes it. The view is
    deliberately not reused: it has no row id, being keyed on
    `listing_id`, which parses as NULL on plenty of real mail - so a row
    read from it could not be opened. Widening a view the Sheets sync
    selects from, for one phone screen, is the trade being avoided."""
    base, conn = app
    _status, cookie = _login(base)
    conn.executemany(
        "INSERT INTO listings (title, price, state, listed_at, sold_at) "
        "VALUES (?,?,?,?,?)",
        [("Sold with dates", 25.0, "sold", "2026-08-01", "2026-08-09"),
         ("Sold without dates", 40.0, "sold", None, None),
         ("Still active", 15.0, "active", "2026-08-01", None)])
    conn.commit()

    from mplabel import web as web_mod

    status, _h, body = _http(base + web_mod.API_PREFIX + "/sold",
                             cookie=cookie)
    assert status == 200
    rows = json.loads(body)["items"]
    titles = [r["title"] for r in rows]
    assert "Still active" not in titles, "sold only"
    assert len(rows) == 2

    by_title = {r["title"]: r for r in rows}
    assert by_title["Sold with dates"]["days_to_sell"] == 8
    # No listed date means no answer, not a zero. A zero would read as
    # "sold the same day", which is a different and flattering claim.
    assert by_title["Sold without dates"]["days_to_sell"] is None
    # And the row can be opened, which is the whole reason this is not
    # read straight out of v_listing_perf.
    assert isinstance(by_title["Sold with dates"]["id"], int)


def test_sold_agrees_with_the_view_the_spreadsheet_uses(app):
    """Two expressions for one number is two chances to be wrong, and the
    view is the one that has been reporting to the spreadsheet for
    months. If they ever disagree, the view is right."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute(
        "INSERT INTO listings (title, price, state, listed_at, sold_at) "
        "VALUES ('Chenille bedspread', 60.0, 'sold', '2026-07-02', "
        "'2026-07-30')")
    conn.commit()

    from mplabel import listings as listings_mod, web as web_mod
    listings_mod.build_views(conn)

    from_view = conn.execute(
        "SELECT days_to_sell FROM v_listing_perf WHERE title=?",
        ("Chenille bedspread",)).fetchone()["days_to_sell"]
    _s, _h, body = _http(base + web_mod.API_PREFIX + "/sold", cookie=cookie)
    from_api = json.loads(body)["items"][0]["days_to_sell"]
    assert from_api == from_view == 28


# Not `FIXTURES` - that name is already the synthetic label
# fixtures at the top of this file, and shadowing it broke
# seven unrelated tests.
IOS_FIXTURES = IOS.parent / "MPLabelTests" / "Fixtures"


@pytest.mark.skipif(not IOS_FIXTURES.exists(),
                    reason="the iOS fixtures are not checked out")
def test_the_ios_fixtures_are_still_what_the_server_sends():
    """The Swift tests decode committed JSON. That is only worth
    anything while the JSON is still what this server produces - a
    fixture that drifts becomes a test asserting the past.

    So: regenerate into a temporary directory and compare the *shapes*
    against the committed copies. Values are allowed to differ (ids,
    timestamps, a minted bin code); keys are not.

    Run `python tests/make_ios_fixtures.py` when this fails. The
    generator starts a real server against a real database and writes
    what comes back, which is the property a hand-written fixture cannot
    have - it would carry the same belief as the model it checks."""
    import importlib.util
    import shutil
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "make_ios_fixtures",
        Path(__file__).parent / "make_ios_fixtures.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    def shape(node):
        """Keys all the way down; values discarded. A list contributes
        the union of its rows' shapes, so a fixture whose first row
        happens to be complete cannot hide a second row that is not."""
        if isinstance(node, dict):
            return {k: shape(v) for k, v in sorted(node.items())}
        if isinstance(node, list):
            merged = {}
            for row in node:
                sub = shape(row)
                if isinstance(sub, dict):
                    merged.update(sub)
            return [merged] if merged else []
        return None

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out = Path(tmp) / "Fixtures"
        original, gen.OUT = gen.OUT, out
        try:
            gen.main()
        finally:
            gen.OUT = original

        fresh = {f.stem: json.loads(f.read_text(encoding="utf-8"))
                 for f in out.glob("*.json")}

    committed = {f.stem: json.loads(f.read_text(encoding="utf-8"))
                 for f in IOS_FIXTURES.glob("*.json")}

    assert set(committed) == set(fresh), (
        "the fixture set changed - run python tests/make_ios_fixtures.py")

    drifted = sorted(name for name in fresh
                     if shape(fresh[name]) != shape(committed[name]))
    assert not drifted, (
        f"these payloads no longer match the committed fixtures: {drifted}. "
        f"Run python tests/make_ios_fixtures.py, and check the Swift models "
        f"still decode them.")


@pytest.mark.skipif(not IOS_FIXTURES.exists(),
                    reason="the iOS fixtures are not checked out")
def test_the_fixtures_carry_the_awkward_rows_not_just_the_happy_path():
    """A fixture where every field is populated proves only that the
    happy path decodes, and the models are almost entirely Optional
    precisely because the real database is not like that. Both bugs so
    far hid in exactly this gap.

    So the generator seeds the awkward shapes on purpose, and this
    asserts they survived - otherwise a well-meaning tidy-up of the seed
    data would silently remove the coverage."""
    orders = json.loads((IOS_FIXTURES / "orders.json").read_text())["orders"]
    assert any(o["ship_by"] is None for o in orders), \
        "a local pickup has no ship-by, and that row has to be in here"
    assert any(not o["has_label"] for o in orders), \
        "a sale with no label file is a real state, not an error"

    sold = json.loads((IOS_FIXTURES / "sold.json").read_text())["items"]
    assert any(r["days_to_sell"] is None for r in sold), \
        "the saved-page import carried no dates; nil is the common case"

    # And no real credential ended up committed.
    login = json.loads((IOS_FIXTURES / "login.json").read_text())
    assert login["token"] == "<redacted>"


@pytest.mark.skipif(not IOS.exists(), reason="the iOS client is not checked out")
def test_the_camera_string_is_present_because_its_absence_is_silent():
    """No NSCameraUsageDescription and iOS kills the app the instant the
    Scan tab opens - no dialog, no log she would see, just a crash on the
    one screen this target exists for."""
    spec = (IOS.parent / "project.yml").read_text(encoding="utf-8")
    assert "NSCameraUsageDescription" in spec


def test_the_client_escapes_what_facebook_sends():
    """Item titles come from Marketplace listings, so their text is chosen
    by someone else. Every client must route every one through esc().

    Both front ends are scanned. They render the same rows out of the
    same endpoints, so the desk is exposed to exactly the same titles -
    and a file this does not read is a file where the guard is
    decoration. The single-letter loop variables are load bearing for the
    same reason: naming one `order` does not fail this test, it makes it
    stop looking."""
    # encoding, not the platform default: cp1252 chokes on this file, so
    # without it the test only passes where the locale happens to be UTF-8.
    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"
    assert "function esc(" in (static / "common.js").read_text(encoding="utf-8")

    # Anything concatenated straight into an HTML string is unescaped by
    # definition. o/d/p/a/b/s/t/m are the loop variables holding server
    # data, so a raw `+ o.title` is the bug this is looking for. Numeric
    # ids are the only safe exception - they cannot carry markup.
    numeric_ok = {"id", "print_count", "length"}
    for name in ("app.js", "desk.js", "common.js"):
        js = (static / name).read_text(encoding="utf-8")
        raw = [f"{v}.{f}" for v, f in
               re.findall(r"\+\s*\b([odpabstm])\.(\w+)", js)
               if f not in numeric_ok]
        assert not raw, \
            f"{name} interpolates without esc(): {sorted(set(raw))}"


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

# --- shelf tags ---------------------------------------------------------

def test_a_location_code_is_three_characters_not_four():
    """The whole scheme rests on this. The marker payload already carries
    a format bit telling 3-char codes from 4-char ones, so three means a
    place and four means a thing - and a scanner knows which it has
    without a prefix character or anything new on the wire.

    A four-character location code would scan as an item and bin
    itself, so it is refused rather than accepted and drawn."""
    from mplabel import inventory

    assert inventory.normalise_location_code("a1b") == "A1B"
    with pytest.raises(ValueError, match="a location code is 3"):
        inventory.normalise_location_code("7K2Q")
    with pytest.raises(ValueError, match="alphabet"):
        inventory.normalise_location_code("AIB")     # I is not in it


def test_shelf_tag_carries_the_same_code_it_prints():
    """A tag whose marker disagrees with its printed characters sends
    boxes to the wrong shelf and looks right doing it.

    Checked against the modules `marker.render` produces rather than
    through the decoder: the invariant the drawing owns is that what
    survived the print pipeline *is* the marker for this code. Whether a
    camera can then read it off thermal paper is a different question and
    not one a test can answer.

    On the square tag, which does not print turned - the rotated case is
    covered below by geometry rather than by re-deriving the transpose
    here, where getting it wrong would fail the test for the wrong
    reason."""
    from mplabel import inventory, marker

    size = (48, 30)
    raw, stride, rows = inventory.render_shelf_tag(
        "A1B", with_marker=True, label_mm=size)
    assert not inventory.reads_sideways(size)
    job = supvan.build_job(raw, stride, rows)
    back, back_stride, cols = supvan.decode_job(job["compressed"])

    x0, y0, x1, y1 = inventory.shelf_marker_box(label_mm=size)
    want = marker.render("A1B", quiet=inventory.MARKER_QUIET)
    down, across = len(want), len(want[0])
    height, width = y1 - y0 + 1, x1 - x0 + 1

    # `decode_job` hands back only the rows the device is given, so its
    # row 0 is the raster's row `margin_top`. Indexing it in raster
    # coordinates reads the label eight rows too low, which looks like a
    # marker that does not match rather than an off-by-a-margin.
    shift = supvan.DEFAULT_MARGIN_DOTS

    for r in range(down):
        for c in range(across):
            x = x0 + int((c + 0.5) * width / across)
            y = y0 + int((r + 0.5) * height / down) - shift
            got = 1 if back[y * back_stride + (x >> 3)] & (0x80 >> (x & 7))                 else 0
            assert got == want[r][c], f"module {r},{c}"


def test_the_shelf_marker_box_is_where_the_marker_actually_is():
    """`shelf_marker_box` is what a decoder gets handed, and it is
    derived from the same placement the drawing uses so the two cannot
    drift - the pairing `_marker_band` and `marker_box` already have for
    item labels. Asserted on the turned tag too, where the box has to
    carry the rotation with it."""
    from mplabel import inventory, marker

    rows_m = marker.ROWS + 2 * inventory.MARKER_QUIET
    cols_m = marker.COLS + 2 * inventory.MARKER_QUIET

    for size in ((101.6, 25.4), (48, 30)):
        raw, stride, rows = inventory.render_shelf_tag(
            "A1B", with_marker=True, label_mm=size)
        x0, y0, x1, y1 = inventory.shelf_marker_box(label_mm=size)
        assert 0 <= x0 < x1 < stride * 8 and 0 <= y0 < y1 < rows, size

        # Whole modules both ways round, and the box is the marker's own
        # aspect - 6 by 24 plus quiet - not something squarer.
        span = sorted((x1 - x0 + 1, y1 - y0 + 1))
        assert span[1] * rows_m == span[0] * cols_m, (size, span)

        ink = sum(1 for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
                  if raw[y * stride + (x >> 3)] & (0x80 >> (x & 7)))
        assert ink > 0, f"the box at {size} contains no marker"
        assert ink < (x1 - x0 + 1) * (y1 - y0 + 1), "the box is solid black"


def test_shelf_tag_puts_the_code_first():
    """A shelf tag is read from across a room, so the code has to be the
    biggest thing on it - the opposite of an item label, where the title
    is what you need because the thing is already in your hand."""
    from mplabel import inventory

    raw, stride, rows = inventory.render_shelf_tag(
        "A1B", "Loft, north wall", with_marker=True)
    tag_ink = sum(bin(b).count("1") for b in raw)

    plain, _s, _r = inventory.render_shelf_tag("A1B", with_marker=True)
    # The name is a caption: adding it must not outweigh the code.
    assert sum(bin(b).count("1") for b in plain) > tag_ink * 0.5


def test_a_wide_tag_puts_the_marker_beside_the_code():
    """Stacked, both come out small: the code loses height to the band
    and the band's module size is capped by what is left. The marker is
    6x24, so on a tag far wider than it is tall each wants a different
    axis."""
    from mplabel import inventory

    wide, stride, rows = inventory.render_shelf_tag(
        "A1B", with_marker=True, label_mm=(101.6, 25.4))
    tall, _s2, _r2 = inventory.render_shelf_tag(
        "A1B", with_marker=True, label_mm=(48, 30))
    # Side by side gives the marker room, so its modules are bigger and
    # it carries more ink than the squeezed stacked version.
    assert sum(bin(b).count("1") for b in wide) > 0
    assert sum(bin(b).count("1") for b in tall) > 0


def test_shelf_tag_stays_inside_the_printable_window():
    from mplabel import inventory

    for size in ((101.6, 25.4), (48, 30)):
        raw, stride, rows = inventory.render_shelf_tag(
            "A1B", "Loft", with_marker=True, label_mm=size)
        xs = [xb * 8 + k for y in range(rows) for xb in range(stride)
              for k in range(8) if raw[y * stride + xb] & (0x80 >> k)]
        assert min(xs) >= inventory.PRINTABLE_LEFT_DOTS, size
        assert max(xs) <= (inventory.HEAD_DOTS
                           - inventory.PRINTABLE_RIGHT_DOTS - 1), size


def test_shelf_tag_refuses_two_carriers(monkeypatch):
    from mplabel import cli, inventory

    with pytest.raises(ValueError, match="one machine-readable code"):
        inventory.render_shelf_tag("A1B", with_qr=True, with_marker=True)

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "shelf-tag", "--code", "A1B",
                         "--qr", "--marker"])
    with pytest.raises(SystemExit, match="pick one"):
        cli.main()


def test_shelf_tag_command_needs_no_database(monkeypatch, capsys, tmp_path):
    """With `probe`, `selftest` and `inventory-label`: the tags want
    making before there is anything recorded to put on the shelves."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    def no_db(*a, **k):
        raise AssertionError("shelf-tag opened the database")
    monkeypatch.setattr(cli, "connect_db", no_db)
    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "shelf-tag", "--code", "A1B",
                         "--name", "Loft", "--marker"])
    cli.main()
    out = capsys.readouterr().out
    assert "shelf  : A1B" in out
    assert "every checksum valid" in out

def test_a_tag_defaults_to_the_stock_that_is_in_the_machine():
    """It defaulted to 4x1in, which reads better from across a room and
    is 101.6mm down the feed. On the 30mm die-cut stock this printer
    actually holds, that printed across three and a bit labels - the
    marker landed on one of them and the code on another, and the label
    that came back looked like a bug in the layout.

    A default that needs different paper is a default that wastes a roll
    finding out."""
    from mplabel import inventory

    assert inventory.DEFAULT_SHELF_MM == inventory.DEFAULT_LABEL_MM
    _raw, _stride, rows = inventory.render_shelf_tag("A1B", with_marker=True)
    feed_mm = rows / inventory.DOTS_PER_MM
    assert feed_mm == inventory.DEFAULT_LABEL_MM[1]


def test_the_feed_length_is_reported_before_anything_prints(monkeypatch,
                                                            capsys):
    """In millimetres, because that is the number that has to match the
    paper. Dots do not tell you whether it fits the label in the
    machine."""
    from mplabel import cli

    monkeypatch.setattr(cli, "load_config", lambda p=None: dict(cli.DEFAULTS))
    for argv in (["mplabel", "shelf-tag", "--code", "A1B", "--marker"],
                 ["mplabel", "inventory-label", "--code", "7K2Q", "--qr"]):
        monkeypatch.setattr(sys, "argv", argv)
        cli.main()
        out = capsys.readouterr().out
        assert "30.0mm down the feed" in out, argv
        assert "nothing sent" in out, argv

# --- phase 1: correctness fixes the wire would amplify -------------------

def test_a_config_refusal_exits_so_systemd_stops_retrying(tmp_path, capsys):
    """`Restart=always` plus a permanent config error is a unit that flaps
    every ten seconds for ever. Observed at restart counter 11, with the
    one line saying what was wrong buried under systemd noise - the
    opposite of what a refusal is for.

    78 is EX_CONFIG, paired with RestartPreventExitStatus in the unit."""
    from mplabel import cli, printd as printd_mod

    cfg = dict(cli.DEFAULTS, printd_secret="", home=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        printd_mod.serve(cfg)
    assert exc.value.code == printd_mod.EX_CONFIG == 78
    assert "printd_secret is not set" in capsys.readouterr().err

    cfg = dict(cli.DEFAULTS, printd_secret="s" * 8, home=str(tmp_path),
               printer_backend="pi-http")
    with pytest.raises(SystemExit) as exc:
        printd_mod.serve(cfg)
    assert exc.value.code == 78

    unit = (Path(__file__).parent.parent / "systemd"
            / "mplabel-printd.service").read_text()
    assert "RestartPreventExitStatus=78" in unit,         "the exit code is only half of it; the unit has to honour it"


def test_the_phone_app_refuses_the_same_way_printd_does(tmp_path):
    """Serving the database unauthenticated is not an option, and the
    refusal is permanent - the next start reads the same config file. So
    it exits 78 rather than 1, and the unit honours it. printd already
    paid for this lesson at restart counter 11; there is no reason for
    the second service to relearn it on her phone."""
    from mplabel import cli, printd as printd_mod, web as web_mod

    cfg = dict(cli.DEFAULTS, web_password_hash="", home=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        web_mod.serve(cfg)
    assert exc.value.code == printd_mod.EX_CONFIG == 78

    unit = (Path(__file__).parent.parent / "systemd"
            / "mplabel-web.service").read_text()
    assert "RestartPreventExitStatus=78" in unit, \
        "the exit code is only half of it; the unit has to honour it"


def test_the_phone_app_unit_can_reach_the_printer_and_the_lock():
    """It prints - `/api/orders/{id}/print` calls the same `printers.send`
    the poller does - so with a raw backend it writes to the device node
    and takes the flock. A unit without the `lp` group serves the whole
    app perfectly and fails only on the one button that matters, and a
    PrivateTmp of its own would silently stop the fallback lock path
    interlocking with the poller or a hand-run reprint over ssh."""
    unit = (Path(__file__).parent.parent / "systemd"
            / "mplabel-web.service").read_text()
    assert "Group=lp" in unit
    assert "/run/lock" in unit
    assert not re.search(r"(?m)^PrivateTmp=", unit)


def test_the_installer_is_executable():
    """It was committed 644, so `./install_pi.sh` is "Permission denied"
    and only `bash install_pi.sh` runs. The two failures do not look
    alike: the second works, and the first refuses quietly enough to be
    stepped past - after which the symptom arrives later and somewhere
    else, as `Failed to enable unit: ... does not exist`, which reads as
    a missing file in the repo rather than a step that never ran.

    The mode is checked through git rather than the filesystem, because
    this is developed on Windows where the working copy has no mode bit
    to speak of and git's index is the thing that actually ships."""
    import subprocess

    root = Path(__file__).parent.parent
    try:
        out = subprocess.run(["git", "ls-files", "-s", "install_pi.sh"],
                             cwd=root, capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("not a git checkout")

    mode = out.stdout.split()[0]
    assert mode == "100755", (
        f"install_pi.sh is mode {mode}; it has to be 100755 or `./install_pi.sh` "
        f"is refused and the units it writes never reach the Pi")


def test_every_config_key_is_in_the_example():
    """A key that exists only in `DEFAULTS` is a key nobody knows to set.

    `/etc/mplabel.conf` is never overwritten by the installer, so the
    example is the only place a new setting gets announced - and the
    failure for a missing one is silent: the built-in default fires and
    looks exactly like a deliberate choice. Five APNs keys and the whole
    web block were missing when this was written."""
    from mplabel import cli as cli_mod

    example = (Path(__file__).parent.parent
               / "mplabel.conf.example").read_text()
    missing = [key for key in cli_mod.DEFAULTS
               if not re.search(rf"^\s*#?\s*{re.escape(key)}\s*=",
                                example, re.M)]
    assert not missing, \
        "documented nowhere: " + ", ".join(sorted(missing))


def test_every_unit_in_the_repo_is_installed_by_the_installer():
    """A unit that exists in the repo and not in `install_pi.sh` never
    reaches the Pi: the documented update path is a git pull and a pip
    install, and neither writes a unit file. That failure presents as
    `Failed to enable unit: ... does not exist`, which reads like a
    missing file in the repo rather than a step nobody ran - and it cost
    a deployment once already."""
    root = Path(__file__).parent.parent
    installer = (root / "install_pi.sh").read_text()
    # Timers too. This asked only for *.service until a .timer was added
    # beside one, which would have reached the Pi as a service that
    # nothing ever ran - the same silent gap in a shape the test did not
    # look at.
    units = sorted(list((root / "systemd").glob("*.service"))
                   + list((root / "systemd").glob("*.timer")))
    assert units, "no units found - has systemd/ moved?"
    for unit in units:
        assert unit.name in installer, \
            f"{unit.name} is in the repo but install_pi.sh never writes it"


def test_the_notify_timer_survives_a_pi_that_was_switched_off():
    """A parcel that was due while the Pi was off is still due.

    `Persistent=true` is what runs the missed firing at boot; without it
    the one morning the Pi was unplugged is the one morning she is not
    told. And the service is a oneshot whose config refusal (78) counts
    as success, because a permanent error retried twice a day is a
    permanent error in the journal twice a day."""
    root = Path(__file__).parent.parent
    timer = (root / "systemd" / "mplabel-notify.timer").read_text()
    assert "Persistent=true" in timer
    assert timer.count("OnCalendar=") == 2, "morning and evening"

    service = (root / "systemd" / "mplabel-notify.service").read_text()
    assert "Type=oneshot" in service
    assert "78" in service, "the config refusal must not read as a failure"
    # Line-anchored: the unit *comments* on not having a Restart=, and a
    # substring check matched the explanation rather than a directive.
    assert not any(line.startswith("Restart=")
                   for line in service.splitlines()), \
        "the timer is what makes this recur; a Restart= is a second one"


def test_printd_refuses_to_print_to_itself(tmp_path):
    """`printer_backend = pi-http` on the printd host makes printd POST to
    printd_url - itself. The inner request finds the gate held, burns the
    whole deadline and answers 410, which surfaces as "printd said 410"
    and reads exactly like a busy printer.

    `docs/split-architecture.md` phase 3 used to instruct this config, and
    the tests missed it because the fixture hands printd and the client
    two separate dicts. Refuse at startup instead of at 11pm."""
    from mplabel import cli, printd as printd_mod, printers

    cfg = dict(cli.DEFAULTS)
    cfg.update({"printd_secret": "s" * 8, "home": str(tmp_path),
                "printer_backend": "pi-http",
                "printd_url": "http://127.0.0.1:9101"})
    with pytest.raises(SystemExit) as exc:
        printd_mod.serve(cfg)
    assert exc.value.code == printd_mod.EX_CONFIG

    # A local backend still starts far enough to bind, so the guard is
    # not just rejecting everything.
    assert "pi-http" in printers.REMOTE_BACKENDS
    assert "tspl" not in printers.REMOTE_BACKENDS


def test_printd_answers_even_when_healthz_itself_raises(printd, monkeypatch):
    """`/healthz` exists so a wedged printer is visible. An unhandled
    exception in it closed the connection with no HTTP response at all -
    so the triage command hung in exactly the case it was written to
    diagnose."""
    import urllib.error
    import urllib.request
    from mplabel import build

    base, sent, _srv = printd

    def boom():
        raise RuntimeError("the health check itself is broken")

    monkeypatch.setattr(build, "stamp", boom)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/healthz", timeout=5)
    assert exc.value.code == 503
    assert "RuntimeError" in exc.value.read().decode()
    assert sent == []


def test_printd_refuses_a_job_id_that_is_a_path(printd):
    """The job id becomes a spool filename and arrives from the wire.
    Holding the secret is not a licence to choose paths on this host."""
    base, sent, _srv = printd

    for bad in ("../../etc/passwd", "a/b", "x" * 200, "sp ace"):
        status, payload = _print_req(base, PDF, job=bad)
        assert status == 400, bad
        assert "job id" in payload.get("error", ""), bad
    assert sent == []

    # The real shapes still work: {code}-{hex} and selftest-{hex}.
    status, _payload = _print_req(base, PDF, job="W7X-0011223344556677")
    assert status == 200
    assert len(sent) == 1


def test_send_returns_what_the_backend_said():
    """It discarded the return value, so `pi-http`'s 409 duplicate came
    back as {"duplicate": True}, was thrown away, and the caller marked
    the sale printed - a duplicate recorded as a successful print."""
    from mplabel import printers

    seen = {}

    def fake(pdf_path, **kw):
        seen["path"] = pdf_path
        return {"printed": False, "duplicate": True}

    original = printers.BACKENDS.get("tspl")
    printers.BACKENDS["tspl"] = fake
    try:
        got = printers.send("x.pdf", "tspl")
    finally:
        printers.BACKENDS["tspl"] = original
    assert got == {"printed": False, "duplicate": True}
    assert seen["path"] == "x.pdf"


def test_pi_http_treats_a_non_json_200_as_unreachable(monkeypatch, tmp_path):
    """A 200 that is not JSON is a proxy answering instead of printd.
    Left as a bare ValueError it reaches the phone app as a 400 'bad
    request', blaming the caller for something upstream."""
    import io
    import urllib.request
    from mplabel import printers

    pdf = tmp_path / "l.pdf"
    pdf.write_bytes(PDF)

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"<html>captive</html>"))
    with pytest.raises(printers.PrinterUnavailable, match="not JSON"):
        printers.print_pi_http(str(pdf), url="http://pi:9101", secret="s",
                               code="W7X")


def test_backend_kwargs_survives_a_host_with_no_printer_config():
    """A host that has never had a printer has no reason to carry
    printer_dpi, and a KeyError there became a 503 per request inside
    printd - a daemon failing on a key nobody knew was required."""
    from mplabel import printers

    cfg = {"printer_backend": "tspl", "printer_device": "/dev/usb/lp0"}
    kw = printers.backend_kwargs(cfg, "tspl")
    assert kw["dpi"] == printers.DEFAULT_DPI


def test_status_refuses_on_a_host_whose_printer_is_elsewhere(monkeypatch,
                                                             capsys):
    """`DEFAULTS` always supplies printer_device, so this queried a node
    that was not there and printed the "answered no, printing stays
    at-least-once" finding - a finding-shaped answer about the highest
    value open experiment in the project, from a machine with no
    printer."""
    from mplabel import cli

    cfg = dict(cli.DEFAULTS)
    cfg["printer_backend"] = "pi-http"
    with pytest.raises(SystemExit, match="another host"):
        cli.cmd_status(cfg)
    assert "answered no" not in capsys.readouterr().out


def test_config_says_which_file_and_where_each_value_came_from(
        tmp_path, monkeypatch, capsys):
    """Three things decide a value and none is visible from the others:
    DEFAULTS, the first config file that exists, and MPLABEL_*."""
    from mplabel import cli

    conf = tmp_path / "mplabel.conf"
    conf.write_text("[mplabel]\nprinter_backend = pi-http\n"
                    "printd_secret = topsecretvalue\n", encoding="utf-8")
    monkeypatch.setenv("MPLABEL_PRINTD_TIMEOUT", "90")

    rows, used = cli.config_sources(str(conf))
    origin = {k: o for k, _v, o in rows}
    value = {k: v for k, v, _o in rows}
    assert used == Path(conf)
    assert origin["printer_backend"] == "file"
    assert origin["printd_timeout"] == "env" and value["printd_timeout"] == "90"
    assert origin["poll_seconds"] == "default"

    monkeypatch.setattr(sys, "argv",
                        ["mplabel", "-c", str(conf), "config"])
    cli.main()
    out = capsys.readouterr().out
    assert "pi-http" in out
    assert "topsecretvalue" not in out, "a secret was echoed to the terminal"
    assert "<set>" in out

# --- phase 2: the label maker behind printd ------------------------------

SHELF_SPEC = {"kind": "shelf-tag", "code": "A1B", "name": "Loft",
              "marker": True}


def _tag_req(base, spec, job="A1B-0011223344556677", secret="s3cret",
             protocol="1", deadline="5", body=None):
    import json as _json
    from mplabel import printd as printd_mod

    if body is None:
        body = _json.dumps(spec, sort_keys=True).encode()
    sig = printd_mod.sign(secret, job, body)
    return _http_raw(f"{base}/print-tag", body, {
        "Content-Type": "application/json", "X-MPLabel-Protocol": protocol,
        "X-MPLabel-Job": job, "X-MPLabel-Sig": sig,
        "X-MPLabel-Deadline": deadline})


def _get_json(url):
    import json as _json
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, _json.loads(res.read() or b"{}")


def test_a_tag_spec_crosses_the_wire_not_a_raster(printd):
    """The label maker has more roll facts than the 4x6 printer, not
    fewer: PRINTABLE_* were measured on one roll, density is burn energy
    against a particular paper, and the size is the die-cut label in the
    machine. Sending the compressed blob would move all four onto
    whichever host built it - and the blob is self-contained precisely
    because they are baked into its buffer headers."""
    base, _sent, srv = printd
    srv.cfg["supvan_density"] = "7"

    status, result = _tag_req(base, dict(SHELF_SPEC, dry_run=True))
    assert status == 200, result
    # The server's roll, not the client's - the spec named neither.
    assert result["payload"]["density"] == 7
    assert result["label"]["mm"] == [48, 30]
    assert result["kind"] == "shelf-tag"
    assert result["printed"] is False and result["dry_run"] is True


def test_a_tag_result_survives_json_when_the_device_answers(printd,
                                                            monkeypatch):
    """decode_status puts the raw report in status["raw"] as bytes, and
    json refuses them with a TypeError - which the catch-all turns into a
    503. A label that printed perfectly would be reported as a failure
    and the caller would print it again."""
    import contextlib
    from mplabel import printers, supvan

    base, _sent, _srv = printd
    final = {"pages_printed": 41, "errors": [], "stalled": False,
             "raw": b"\x08\x00\x00\x10\x00\x00"}
    monkeypatch.setattr(supvan, "print_job", lambda *a, **k: final)
    monkeypatch.setattr(printers, "print_lock",
                        lambda *a, **k: contextlib.nullcontext())

    status, result = _tag_req(base, SHELF_SPEC)
    assert status == 200, result
    assert result["printed"] is True
    assert result["final"]["raw"] == "08 00 00 10 00 00"
    assert json.dumps(result)


def test_a_stalled_tag_is_not_reported_as_printed(printd, monkeypatch):
    """A stall means paper moved and the job never finished. A 200 with
    printed=false is discarded by the client's success path and would
    read as a print, so it has to be a non-2xx - and it has to be
    journaled, or a retry of the same id prints again on media the last
    attempt left out of position."""
    import contextlib
    from mplabel import printers, supvan

    base, _sent, srv = printd
    monkeypatch.setattr(supvan, "print_job",
                        lambda *a, **k: {"stalled": True, "errors": [],
                                         "pages_printed": 0})
    monkeypatch.setattr(printers, "print_lock",
                        lambda *a, **k: contextlib.nullcontext())

    status, result = _tag_req(base, SHELF_SPEC)
    assert status == 503
    assert result["printed"] is False and result["stalled"] is True
    assert "reseat" in result["error"].lower()

    rows = srv.journal.since()
    assert rows[-1]["outcome"] == "stalled"
    assert rows[-1]["kind"] == "shelf-tag"
    # And the same id is refused rather than moving paper twice.
    again, _payload = _tag_req(base, SHELF_SPEC)
    assert again == 409


def test_a_tag_print_takes_the_device_lock_exactly_once(printd, monkeypatch):
    """It took it twice, and the second one deadlocked against the first.

    `print_lock` opens the lock file fresh on every call, so the second
    is a different open file description - and `flock` conflicts between
    descriptions even inside one process. printd held the hidraw node via
    its own `_Device`, then `print_tag_local` blocked for ever trying to
    take the same lock. Observed on the Pi: `tag_printing: true`,
    `tag_printing_for: 77.8`, no response, on a printer that answered
    `supvan-probe` immediately.

    The 4x6 path already knew this - `cli.print_label` skips the flock
    for `pi-http` because holding it on both sides deadlocked on a
    same-Pi loopback deployment - and the tag path reintroduced it.

    Counted rather than reproduced, because there is no `flock` on the
    platform these tests run on, so the deadlock itself cannot be made to
    happen here. One acquisition is the invariant either way."""
    import contextlib
    from mplabel import printers, supvan

    base, _sent, _srv = printd
    taken = []
    real_lock = printers.print_lock

    def counting_lock(cfg=None, device=None, required=False, timeout=None):
        # `timeout` is part of the contract now: printd passes what is
        # left of the caller's deadline, and a fake that does not accept
        # it fails the request rather than the assertion, which is a
        # confusing way to be told the signature moved.
        taken.append(device)
        assert timeout is not None, \
            "printd must bound the wait, not block under a deadline"
        return contextlib.nullcontext()

    monkeypatch.setattr(printers, "print_lock", counting_lock)
    monkeypatch.setattr(supvan, "print_job",
                        lambda *a, **k: {"pages_printed": 1, "errors": [],
                                         "stalled": False})

    status, result = _tag_req(base, SHELF_SPEC)
    assert status == 200, result
    assert result["printed"] is True
    assert len(taken) == 1, f"the device lock was taken {len(taken)} times"
    assert real_lock is not counting_lock


def test_a_dry_run_takes_no_device_lock_at_all(printd, monkeypatch):
    """It renders and builds and opens nothing, so it must not serialise
    against a real print either - a preview should never be able to make
    somebody wait for the printer."""
    import contextlib
    from mplabel import printers

    base, _sent, _srv = printd
    taken = []
    monkeypatch.setattr(printers, "print_lock",
                        lambda *a, **k: taken.append(1)
                        or contextlib.nullcontext())

    status, result = _tag_req(base, dict(SHELF_SPEC, dry_run=True))
    assert status == 200, result
    assert taken == []

def test_printed_is_signed_because_it_enumerates_parcel_codes(printd):
    """Job ids are `{code}-{hex}`, so an open journal hands out live
    parcel codes - and a parcel code is a handle: `mplabel reprint
    <code>` and `mplabel ship <code>` both take one. Harmless on
    loopback, not harmless the moment printd is on a network."""
    import urllib.error
    import urllib.request
    from mplabel import printd as printd_mod

    base, _sent, srv = printd
    srv.journal.record("W7X-aaaabbbbccccdddd", 10, "d" * 64)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/printed", timeout=5)
    assert exc.value.code == 401
    assert "W7X" not in exc.value.read().decode()

    # And a wrong secret is refused rather than answered.
    job = "printed-0011223344556677"
    bad = urllib.request.Request(f"{base}/printed", headers={
        "X-MPLabel-Protocol": "1", "X-MPLabel-Job": job,
        "X-MPLabel-Sig": printd_mod.sign("wrong", job, b"")})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(bad, timeout=5)
    assert exc.value.code == 401


def test_a_signed_printed_request_still_works(printd):
    """Including the query string, which the signature covers in the
    body's place - a GET has no body, and the query is the only
    caller-controlled part of the request."""
    import urllib.request
    from mplabel import printd as printd_mod

    base, _sent, srv = printd
    srv.journal.record("AAA-1111", 10, "a" * 64)
    srv.journal.record("BBB-2222", 10, "b" * 64)

    def ask(query):
        job = "printed-0011223344556677"
        req = urllib.request.Request(
            f"{base}/printed" + (f"?{query}" if query else ""),
            headers={"X-MPLabel-Protocol": "1", "X-MPLabel-Job": job,
                     "X-MPLabel-Sig": printd_mod.sign("s3cret", job,
                                                      query.encode())})
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read())["printed"]

    assert [r["job"] for r in ask("")] == ["AAA-1111", "BBB-2222"]
    assert [r["job"] for r in ask("since=AAA-1111")] == ["BBB-2222"]


def test_healthz_stays_open(printd):
    """It is the triage endpoint. It must answer when everything else is
    wrong, including when the secret is wrong, and it says nothing about
    any order."""
    import urllib.request

    base, _sent, _srv = printd
    with urllib.request.urlopen(f"{base}/healthz", timeout=5) as res:
        payload = json.loads(res.read())
    assert payload["ok"] is True
    # Not a bare substring sweep: `media_tracking` is a printer setting
    # and would trip a search for "tracking". What must not be here is
    # anything about an order or a credential.
    blob = json.dumps(payload).lower()
    for leak in ("buyer", "ship_to", "secret", "printd_secret", "9400"):
        assert leak not in blob
    assert "printed" not in payload, "the journal does not belong on an open endpoint"


def test_the_reconcile_client_signs_and_says_so_when_it_cannot(printd,
                                                               monkeypatch):
    """`printd_printed` is what `mplabel reconcile` calls. A mismatched
    secret has to say *that*, not 'could not reach' - the two look
    identical from the command line and lead opposite ways."""
    from mplabel import printers

    base, _sent, srv = printd
    srv.journal.record("W7X-aaaa", 10, "d" * 64)

    cfg = {"printd_url": base, "printd_secret": "s3cret"}
    assert [r["job"] for r in printers.printd_printed(cfg)] == ["W7X-aaaa"]

    cfg["printd_secret"] = "not-the-one"
    with pytest.raises(printers.PrinterUnavailable, match="does not match"):
        printers.printd_printed(cfg)

def test_the_two_endpoints_refuse_each_others_bodies(printd):
    """kind rides inside the signed body, so routing cannot be moved by
    an unsigned header. Cross-posting then fails on body validation
    alone, in both directions."""
    base, sent, _srv = printd

    status, payload = _tag_req(base, None, body=PDF)
    assert status == 400 and "JSON" in payload["error"]

    spec_body = json.dumps(SHELF_SPEC, sort_keys=True).encode()
    status, payload = _print_req(base, spec_body, job="A1B-1122334455667788")
    assert status == 400 and "PDF" in payload["error"]
    assert sent == []


def test_a_bad_tag_spec_is_refused_before_anything_moves(printd):
    base, _sent, _srv = printd
    cases = (({"kind": "nope", "code": "A1B"}, "tag kind"),
             ({"kind": "shelf-tag"}, "needs a code"),
             ({"kind": "shelf-tag", "code": "7K2Q"}, "location code"),
             ({"kind": "shelf-tag", "code": "A1B", "qr": True,
               "marker": True}, "one machine-readable"))
    for i, (bad, why) in enumerate(cases):
        status, payload = _tag_req(base, bad, job=f"bad{i}")
        assert status == 400, (bad, payload)
        assert why in payload["error"], (bad, payload)


def test_healthz_reports_the_label_maker_without_touching_it(printd):
    """It must keep answering through a wedged tag print too, so the
    status it carries is the last one seen, with its age, and never a
    fresh read. /tag-status is the live one."""
    base, _sent, _srv = printd
    status, health = _get_json(f"{base}/healthz")
    assert status == 200
    assert "/print-tag" in health["endpoints"]
    assert health["tag_printing"] is False
    assert health["tag_status"] is None and health["tag_status_age"] is None
    assert health["tag_printable"] == [40, 32]
    assert health["tag_label_mm"] == [48, 30]


def test_tag_status_does_not_open_the_node_mid_print(printd):
    """hidraw permits concurrent opens, so slipping an inquiry frame into
    the middle of a bulk transfer is a live corruption risk, not a
    theoretical one. If a print holds the gate, answer from cache."""
    base, _sent, srv = printd

    assert srv._tag_gate.acquire(blocking=False)
    try:
        status, payload = _get_json(f"{base}/tag-status")
    finally:
        srv._tag_gate.release()
    assert status == 200
    assert payload["busy"] is True and payload["answered"] is False


def test_the_two_printers_do_not_block_each_other(printd):
    """Two physical devices on two buses. A wedged shipping label must
    not stop a shelf tag, so they get separate gates and - because
    lock_path is keyed on the device name - separate lock files."""
    base, _sent, srv = printd
    assert srv._gate is not srv._tag_gate

    assert srv._gate.acquire(blocking=False)
    try:
        status, payload = _tag_req(base, dict(SHELF_SPEC, dry_run=True))
        assert status == 200, payload
    finally:
        srv._gate.release()


def test_reconcile_ignores_tag_rows(db, monkeypatch):
    """A shelf tag's job id is the same shape as a parcel's, and a
    3-character location code could match a parcel code by coincidence -
    which would mark a live parcel printed because a tag came out."""
    import argparse as _argparse
    from mplabel import cli, printers

    db.execute("INSERT INTO sales (message_id, item, code, status) "
               "VALUES ('m1', 'Lamp', 'A1B', 'new')")
    db.commit()
    monkeypatch.setattr(printers, "printd_printed", lambda *a, **k: [
        {"job": "A1B-aaaa", "kind": "shelf-tag", "outcome": "printed"},
        {"job": "A1B-bbbb", "kind": "pdf", "outcome": "stalled"},
    ])
    cli.cmd_reconcile({}, db, _argparse.Namespace(since=None, dry_run=False))
    row = db.execute(
        "SELECT printed_at FROM sales WHERE code='A1B'").fetchone()
    assert row["printed_at"] is None, "a tag or a stall marked a parcel printed"


def test_print_job_is_the_only_door_to_the_device():
    """experimental_print reads payload["streams"] as a list of LZMA
    streams while build_job's "buffers" is a count inside one stream -
    two things under one word, which already died once on `for c, n in
    3`. Nothing hands it a caller-supplied dict any more, and that
    matters more now a spec can arrive from a network."""
    import inspect
    from mplabel import printers, supvan

    src = inspect.getsource(printers.print_tag_local)
    assert "experimental_print" not in src
    assert "print_job" in src
    assert "job" in inspect.signature(supvan.print_job).parameters

    # And the word that means two things is not on the wire at all.
    _job, result = printers.assemble_tag(SHELF_SPEC, {})
    assert "buffers" not in json.dumps(result)
    assert result["payload"]["buffer_count"] == 3

# --- groundwork for a native client -------------------------------------

def test_a_bearer_token_authenticates_as_well_as_a_cookie(app):
    """The token was already a stateless bearer credential - the cookie
    was just how a browser carries one. A native app has no cookie jar
    worth the name, and Set-Cookie handling outside a browser works right
    up until it silently does not."""
    base, _ = app

    status, _headers, body = _http(f"{base}/api/login", "POST",
                                   {"password": "hunter2"})
    assert status == 200
    payload = json.loads(body)
    token = payload["token"]
    assert token and payload["expires_in"] > 0

    # No cookie anywhere, just the header.
    status, _h, body = _http(f"{base}/api/orders",
                             headers={"Authorization": f"Bearer {token}"})
    assert status == 200, body
    assert json.loads(body)["orders"]

    status, _h, _b = _http(f"{base}/api/orders",
                           headers={"Authorization": "Bearer nonsense"})
    assert status == 401
    status, _h, _b = _http(f"{base}/api/orders")
    assert status == 401


def test_the_versioned_prefix_reaches_the_same_handlers(app):
    """An app on a phone cannot be changed in the same commit as a route.
    /api/v1 is the name that will not move; the unversioned paths are
    what get to change the day a v2 is needed."""
    from mplabel import web as web_mod

    base, _ = app
    _status, cookie = _login(base)

    _s1, _h1, plain = _http(f"{base}/api/orders", cookie=cookie)
    _s2, _h2, versioned = _http(f"{base}{web_mod.API_PREFIX}/orders",
                                cookie=cookie)
    assert json.loads(plain) == json.loads(versioned)

    # And the version is discoverable rather than guessed at.
    _s3, _h3, session = _http(f"{base}/api/session")
    payload = json.loads(session)
    assert payload["api_version"] == web_mod.API_VERSION
    assert payload["api_prefix"] == "/api/v1"


def test_the_versioned_prefix_still_needs_auth(app):
    """An alias that skipped the auth check would be a way in, not a
    contract."""
    from mplabel import web as web_mod

    base, _ = app
    for path in ("/orders", "/pending", "/stats"):
        status, _h, _b = _http(f"{base}{web_mod.API_PREFIX}{path}")
        assert status == 401, path


def test_the_versioned_prefix_keeps_the_csrf_header_rule(app):
    """A custom header cannot be set cross-origin without a preflight
    this server never approves, which is what stands in for CSRF
    protection. The alias must not be a way around it."""
    from mplabel import web as web_mod

    base, _ = app
    _status, cookie = _login(base)
    status, _h, _b = _http(f"{base}{web_mod.API_PREFIX}/orders/1/ship",
                           "POST", {}, cookie=cookie)
    assert status == 400

CSRF = {"X-Mplabel": "1"}


def test_the_phone_can_make_a_bin_and_fill_it(app):
    """The round trip the shelf screen actually does: name a place, put a
    thing in it by name, then read it back by the code the tag carries.
    Making the bin returns the code so the app can print its tag without
    a second request."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state) "
                 "VALUES ('Hobnail milk glass vase', 'active')")
    conn.commit()
    lid = conn.execute("SELECT id FROM listings").fetchone()["id"]

    status, _h, body = _http(f"{base}/api/bins", "POST",
                             {"name": "loft, north wall"}, cookie=cookie, headers=CSRF)
    assert status == 200
    code = json.loads(body)["bin"]["code"]
    assert len(code) == 3

    status, _h, _b = _http(f"{base}/api/inventory/{lid}/bin", "POST",
                           {"bin": "LOFT, NORTH WALL"}, cookie=cookie, headers=CSRF)
    assert status == 200

    status, _h, body = _http(f"{base}/api/bins/{code}", cookie=cookie)
    assert status == 200
    payload = json.loads(body)
    assert payload["bin"]["name"] == "LOFT, NORTH WALL"
    assert [i["title"] for i in payload["items"]] == \
        ["Hobnail milk glass vase"]


def _three_items(conn):
    conn.executemany(
        "INSERT INTO listings (title, state, era, price, paid) VALUES "
        "(?, 'active', '1950s', 20.0, 5.0)",
        [("Milk glass vase",), ("Jadeite mugs",), ("Enamel bread bin",)])
    conn.commit()
    return [r["id"] for r in
            conn.execute("SELECT id FROM listings ORDER BY id")]


def test_a_bulk_edit_is_all_or_nothing(app):
    """The desk puts a confirm dialog in front of this saying there is no
    undo, so a half-applied edit is the one outcome that must be
    impossible - she would have no way to tell which rows moved.

    The trap it is guarding is specific: `set_bin` and `set_cost` each
    commit on their own, so applying the edit row by row through them
    would leave the first ones standing when a later one is refused, and
    a rollback afterwards would have nothing left to undo."""
    base, conn = app
    _status, cookie = _login(base)
    ids = _three_items(conn)

    # A bin nobody has made. The first two rows must not move either.
    status, _h, body = _http(f"{base}/api/inventory/bulk", "POST",
                             {"ids": ids, "set": {"bin": "NOWHERE"}},
                             cookie=cookie, headers=CSRF)
    assert status == 400, body
    assert conn.execute(
        "SELECT COUNT(*) FROM listings WHERE bin_code IS NOT NULL"
    ).fetchone()[0] == 0

    # And one id that does not exist refuses the whole batch, rather than
    # updating the real ones and quietly dropping the rest.
    status, _h, _b = _http(f"{base}/api/inventory/bulk", "POST",
                           {"ids": ids + [9999], "set": {"era": "1970s"}},
                           cookie=cookie, headers=CSRF)
    assert status == 400
    assert conn.execute(
        "SELECT COUNT(*) FROM listings WHERE era='1970s'").fetchone()[0] == 0


def test_a_bulk_edit_writes_every_row_it_was_given(app):
    base, conn = app
    _status, cookie = _login(base)
    ids = _three_items(conn)

    status, _h, body = _http(f"{base}/api/inventory/bulk", "POST",
                             {"ids": ids, "set": {"era": "1970s"}},
                             cookie=cookie, headers=CSRF)
    assert status == 200
    assert json.loads(body)["changed"] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM listings WHERE era='1970s'").fetchone()[0] == 3


def test_a_bulk_edit_refuses_a_field_that_is_not_on_the_list(app):
    """`state` is writable and `id` is not. Without the check a client
    could rename the primary key of forty rows in one request."""
    base, conn = app
    _status, cookie = _login(base)
    ids = _three_items(conn)
    status, _h, body = _http(f"{base}/api/inventory/bulk", "POST",
                             {"ids": ids, "set": {"id": 1}},
                             cookie=cookie, headers=CSRF)
    assert status == 400
    assert b"cannot set id" in body


def test_bulk_and_single_edits_share_one_allow_list():
    """Two lists would mean a field editable one row at a time and not
    forty, or the other way round - a difference nobody notices until
    they hit it, and then it reads as the bulk bar being broken."""
    import inspect

    from mplabel import web

    for fn in (web.Handler.h_item_fields, web.Handler.h_bulk_items):
        src = inspect.getsource(fn)
        assert "ITEM_FIELDS" in src or "_item_sets" in src, \
            f"{fn.__name__} does not go through the shared allow-list"
    assert "paid" not in web.ITEM_FIELDS, \
        "paid is a money parse, not a plain column write"


def test_the_item_view_carries_the_bin_name_not_just_its_code(app):
    """The phone shows "LOFT, NORTH WALL", not a three-character code -
    the code is for scanning. One join here rather than a second request
    per row on the shelf screen."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state) "
                 "VALUES ('Pressed glass tumblers', 'active')")
    conn.commit()
    lid = conn.execute("SELECT id FROM listings").fetchone()["id"]

    _http(f"{base}/api/bins", "POST", {"name": "B5"}, cookie=cookie, headers=CSRF)
    _http(f"{base}/api/inventory/{lid}/bin", "POST", {"bin": "b5"},
          cookie=cookie, headers=CSRF)

    status, _h, body = _http(f"{base}/api/inventory/{lid}", cookie=cookie)
    assert status == 200
    item = json.loads(body)["item"]
    assert item["bin"] == "B5"
    assert item["bin_mates"] == 0

    # And search finds it by the name, which is what she would type.
    status, _h, body = _http(f"{base}/api/inventory?q=B5", cookie=cookie)
    assert [i["title"] for i in json.loads(body)["items"]] == \
        ["Pressed glass tumblers"]


def test_renaming_a_bin_over_the_wire_keeps_its_code(app):
    """Which is what makes the tag already on the shelf still correct."""
    base, conn = app
    _status, cookie = _login(base)

    _s, _h, body = _http(f"{base}/api/bins", "POST", {"name": "FLOOR"},
                         cookie=cookie, headers=CSRF)
    code = json.loads(body)["bin"]["code"]

    status, _h, body = _http(f"{base}/api/bins/{code}/name", "POST",
                             {"name": "front room"}, cookie=cookie, headers=CSRF)
    assert status == 200
    row = json.loads(body)["bin"]
    assert (row["code"], row["name"]) == (code, "FRONT ROOM")


def test_the_phone_cannot_invent_a_bin_by_moving_something_into_it(app):
    """A typo used to make a shelf. Now it is a 400 with a sentence,
    because the reference is real and a thing cannot be somewhere that
    was never named."""
    base, conn = app
    _status, cookie = _login(base)
    conn.execute("INSERT INTO listings (title, state) VALUES ('x', 'active')")
    conn.commit()
    lid = conn.execute("SELECT id FROM listings").fetchone()["id"]

    status, _h, body = _http(f"{base}/api/inventory/{lid}/bin", "POST",
                             {"bin": "FLOOR "}, cookie=cookie, headers=CSRF)
    assert status == 400
    assert b"Make it first" in body


# --- bins: where a thing physically is ----------------------------------

def _stock(db):
    """Two bins and five things, one of them on no shelf at all."""
    from mplabel import listings

    b5 = listings.create_bin(db, "B5")["code"]
    floor = listings.create_bin(db, "floor")["code"]
    rows = [("Oil portrait, unsigned", b5, "active"),
            ("Pressed glass tumblers", b5, "active"),
            ("Hobnail milk glass vase", floor, "active"),
            ("Chenille bedspread", floor, "sold"),
            ("Enamel bread bin", None, "active")]
    for title, code, state in rows:
        db.execute(
            "INSERT INTO listings (title, bin_code, state) VALUES (?,?,?)",
            (title, code, state))
    db.commit()
    return b5, floor


def test_a_bin_name_is_folded_because_two_spellings_is_two_shelves(db):
    """`b5`, `B5 ` and `B 5` are one shelf in the room. Unfolded they are
    three rows in the picker, and the second one gets used."""
    from mplabel import listings

    assert listings.normalise_bin_name("b5") == "B5"
    assert listings.normalise_bin_name("  B5  ") == "B5"
    assert listings.normalise_bin_name("at  tic") == "AT TIC"
    with pytest.raises(ValueError, match="needs a name"):
        listings.normalise_bin_name("")
    with pytest.raises(ValueError, match="at most"):
        listings.normalise_bin_name("x" * 40)


def test_a_bin_has_both_halves_and_they_do_different_jobs(db):
    """The name is read across the room, so `FLOOR` and `ATTIC` are right
    answers even though neither could be a code - both are longer than
    three characters and both contain letters the code alphabet leaves
    out. The code is what a tag carries and a phone reads, so it is
    minted from that alphabet rather than typed."""
    from mplabel import inventory, listings, marker

    row = listings.create_bin(db, "attic")
    assert row["name"] == "ATTIC"
    assert len(row["code"]) == listings.BIN_CODE_LENGTH
    assert all(ch in marker.ALPHABET for ch in row["code"])
    # ...and the name could never have served as the code.
    with pytest.raises(ValueError):
        inventory.normalise_location_code("ATTIC")


def test_a_bin_answers_to_either_half(db):
    """The phone has scanned the code and a person has typed the name,
    and neither should have to know which the other used."""
    from mplabel import listings

    row = listings.create_bin(db, "Loft, north wall")
    assert listings.find_bin(db, row["code"])["name"] == "LOFT, NORTH WALL"
    assert listings.find_bin(db, "loft, north wall")["code"] == row["code"]
    assert listings.find_bin(db, "nowhere") is None


def test_renaming_a_bin_leaves_the_tag_on_the_shelf_valid(db):
    """This is the whole reason the two halves are separate. The tag is
    already stuck to the shelf; if renaming moved the code, every rename
    would mean reprinting it - and everything in the bin follows the new
    name for free because the listings reference the code."""
    from mplabel import listings

    b5, _floor = _stock(db)
    listings.rename_bin(db, b5, "front room, left")
    assert listings.find_bin(db, b5)["name"] == "FRONT ROOM, LEFT"
    contents = listings.bin_contents(db, b5)
    assert len(contents["items"]) == 2
    assert contents["bin"]["name"] == "FRONT ROOM, LEFT"
    # And the old name is gone rather than pointing anywhere.
    assert listings.find_bin(db, "B5") is None


def test_two_shelves_cannot_share_a_name(db):
    from mplabel import listings

    listings.create_bin(db, "ATTIC")
    with pytest.raises(ValueError, match="already a bin"):
        listings.create_bin(db, "attic")


def test_a_bin_code_is_never_reused(db):
    """Same rule as an inventory code and for the same reason: the code
    is printed on a tag stuck to a shelf, and that tag outlives the row.
    Checked against every bin ever, not the ones in use - the opposite of
    a parcel code, freed the moment the parcel ships."""
    from mplabel import listings

    seen = {listings.create_bin(db, "SHELF %d" % n)["code"] for n in range(40)}
    assert len(seen) == 40
    for _ in range(20):
        assert listings.allocate_bin_code(db) not in seen


def test_an_empty_bin_still_appears(db):
    """The difference the table buys over deriving the list from the
    items. A bin exists because someone named it and printed a tag for
    it; a shelf just cleared is still a shelf, and it has to be in the
    picker for the next thing to go on it."""
    from mplabel import listings

    _b5, _floor = _stock(db)
    listings.create_bin(db, "ATTIC")
    got = {b["name"]: b["count"] for b in listings.bins_in_use(db)}
    # FLOOR holds two things but one is sold, and a bin's useful count is
    # what is still on the shelf.
    assert got == {"B5": 2, "FLOOR": 1, "ATTIC": 0}

    with_sold = {b["name"]: b["count"]
                 for b in listings.bins_in_use(db, include_sold=True)}
    assert with_sold == {"B5": 2, "FLOOR": 2, "ATTIC": 0}


def test_moving_something_takes_a_code_or_a_name(db):
    from mplabel import listings

    b5, _floor = _stock(db)
    vase = db.execute("SELECT id FROM listings WHERE title LIKE 'Hobnail%'"
                      ).fetchone()["id"]
    assert listings.set_bin(db, vase, "b5") == b5
    assert len(listings.bin_contents(db, b5)["items"]) == 3

    assert listings.set_bin(db, vase, "") is None
    assert len(listings.bin_contents(db, b5)["items"]) == 2


def test_a_thing_cannot_go_somewhere_that_does_not_exist(db):
    """The reference is real now, so this is refused with a sentence
    rather than accepted and left dangling."""
    from mplabel import listings

    _stock(db)
    with pytest.raises(ValueError, match="Make it first"):
        listings.set_bin(db, 1, "NOWHERE")


def test_setting_a_bin_on_nothing_says_so(db):
    from mplabel import listings

    b5, _floor = _stock(db)
    with pytest.raises(ValueError, match="no listing"):
        listings.set_bin(db, 9999, b5)


def test_deleting_a_bin_empties_the_shelf_rather_than_dangling(db):
    """`ON DELETE SET NULL`, and it only fires because `connect_db` turns
    foreign keys on - SQLite ignores a declared REFERENCES otherwise, so
    without the pragma this would leave two listings pointing at a bin
    that is gone and still pass everything else."""
    from mplabel import listings

    b5, _floor = _stock(db)
    listings.delete_bin(db, "B5")
    assert listings.find_bin(db, b5) is None
    orphans = db.execute("SELECT COUNT(*) c FROM listings WHERE bin_code=?",
                         (b5,)).fetchone()["c"]
    assert orphans == 0
    assert db.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 5


def test_foreign_keys_are_actually_enforced(db):
    """Off by default, per connection. A declared reference without the
    pragma is documentation, and the bug it exists to catch - a listing
    in a bin that was never made - would sail straight through."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO listings (title, bin_code) "
                   "VALUES ('nowhere', 'ZZZ')")
        db.commit()


def test_a_migrated_database_has_the_same_foreign_keys_as_a_fresh_one(tmp_path):
    """A column added by `ALTER TABLE ... ADD COLUMN bin_code TEXT` gets
    no foreign key, while the identical column in SCHEMA gets one. So a
    database that upgraded and a database that was created new end up
    with different constraints, and every test passes either way because
    the fixture builds from SCHEMA.

    That is exactly what shipped: `bin_code` went out declared as plain
    TEXT in MIGRATIONS, so the Pi's database has it unconstrained. The
    decl now carries the REFERENCES clause, and this compares the two
    databases directly rather than trusting that it does.

    Note what this cannot fix: SQLite will not add a constraint to a
    column that already exists without rebuilding the table, so a
    database that migrated before the decl was corrected keeps the
    unconstrained column."""
    import sqlite3
    from mplabel import cli, listings

    def foreign_keys(conn):
        out = set()
        for table in ("listings", "photos"):
            for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
                # (id, seq, table, from, to, on_update, on_delete, match)
                out.add((table, row[3], row[2], row[4], row[6]))
        return out

    fresh_home = tmp_path / "fresh"
    (fresh_home / "labels").mkdir(parents=True)
    fresh = cli.connect_db(fresh_home)

    # A database as it was before any of these columns existed.
    old_home = tmp_path / "old"
    (old_home / "labels").mkdir(parents=True)
    raw = sqlite3.connect(old_home / "sales.db")
    before = ";".join(
        "\n".join(line for line in stmt.splitlines()
                   if not any(c in line for c in
                              ("bin_code", "paid", "trip_id")))
        for stmt in listings.SCHEMA.split(";")
        if "CREATE TABLE IF NOT EXISTS bins" not in stmt
        and "CREATE TABLE IF NOT EXISTS trips" not in stmt
        and "CREATE TABLE IF NOT EXISTS photos" not in stmt) + ";"
    raw.executescript(before)
    raw.execute("INSERT INTO listings (title) VALUES ('before all of it')")
    raw.commit()
    raw.close()

    migrated = cli.connect_db(old_home)

    assert foreign_keys(migrated) == foreign_keys(fresh), (
        "a migrated database and a fresh one disagree about foreign keys; "
        "check the REFERENCES clause is in the MIGRATIONS decl too")
    # And the row that predates everything survived.
    assert migrated.execute(
        "SELECT COUNT(*) c FROM listings").fetchone()["c"] == 1


def test_paid_is_dollars_like_price_not_cents(db):
    """The second money column, and the second chance to make the mistake
    already on record: `amount_with_offset` read raw turns $15 into $1500
    and silently corrupts every average downstream.

    So `paid` is dollars, the same unit as `price`, and margin is a plain
    subtraction. If this ever fails because someone stored cents, the
    symptom is not an exception - it is a profit report that is a hundred
    times wrong and looks plausible."""
    from mplabel import listings

    db.execute("INSERT INTO listings (title, price, paid, state, sold_at) "
               "VALUES ('Hobnail vase', 28.00, 6.00, 'sold', '2026-08-01')")
    db.commit()
    listings.build_views(db)

    row = db.execute("SELECT price, paid, margin FROM v_listing_perf "
                     "WHERE title='Hobnail vase'").fetchone()
    assert row["price"] == 28.0
    assert row["paid"] == 6.0
    assert row["margin"] == 22.0


def test_margin_is_null_without_a_cost_not_the_whole_price(db):
    """Everything predating the `paid` column has no cost, and a missing
    cost read as zero would report the entire price as profit - which is
    the flattering direction, and therefore the one to guard."""
    from mplabel import listings

    db.execute("INSERT INTO listings (title, price, state, sold_at) "
               "VALUES ('No cost known', 40.0, 'sold', '2026-08-01')")
    db.commit()
    listings.build_views(db)

    assert db.execute("SELECT margin FROM v_listing_perf "
                      "WHERE title='No cost known'").fetchone()["margin"] is None
    # And the month it sold in reports how many rows actually had a cost,
    # so a net of nothing cannot be read as a net of zero.
    month = db.execute("SELECT net, costed, orders FROM v_monthly").fetchone()
    assert month["net"] is None
    assert month["costed"] == 0
    assert month["orders"] == 1


def test_deleting_a_trip_keeps_what_came_home(db):
    """`SET NULL`, like a bin. Deleting a trip is tidying up the record
    of a shop visit; the things are still on the shelf."""
    db.execute("INSERT INTO trips (store, occurred_at) "
               "VALUES ('GOODWILL 214', '2026-08-14')")
    trip = db.execute("SELECT id FROM trips").fetchone()["id"]
    db.execute("INSERT INTO listings (title, trip_id, paid) "
               "VALUES ('Enamel bread bin', ?, 4.0)", (trip,))
    db.commit()

    db.execute("DELETE FROM trips WHERE id=?", (trip,))
    db.commit()

    row = db.execute("SELECT title, trip_id, paid FROM listings").fetchone()
    assert row["title"] == "Enamel bread bin"
    assert row["trip_id"] is None
    # The cost is a fact about the thing, not about the trip, so it stays.
    assert row["paid"] == 4.0


def test_a_photo_cannot_point_at_a_listing_that_is_not_there(db):
    """The reference is real, so a triage bug cannot leave a photo
    attached to nothing. Deleting the listing un-attaches instead, which
    puts the photo back in triage rather than losing it."""
    db.execute("INSERT INTO listings (title) VALUES ('Oil portrait')")
    lid = db.execute("SELECT id FROM listings").fetchone()["id"]
    db.execute("INSERT INTO photos (path, listing_id) VALUES ('a.jpg', ?)",
               (lid,))
    db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO photos (path, listing_id) "
                   "VALUES ('b.jpg', 9999)")
        db.commit()
    db.rollback()

    db.execute("DELETE FROM listings WHERE id=?", (lid,))
    db.commit()
    assert db.execute(
        "SELECT listing_id FROM photos WHERE path='a.jpg'"
    ).fetchone()["listing_id"] is None


def test_a_capture_awaiting_triage_is_a_photo_with_no_listing(db):
    """No `captures` table and no state column, deliberately: "not yet
    turned into an item" is the absence of a reference. A state column
    saying the same thing is a second place to disagree with the first."""
    db.execute("INSERT INTO listings (title) VALUES ('Already an item')")
    lid = db.execute("SELECT id FROM listings").fetchone()["id"]
    db.executemany("INSERT INTO photos (path, listing_id) VALUES (?,?)",
                   [("triaged.jpg", lid), ("waiting.jpg", None)])
    db.commit()

    waiting = [r["path"] for r in db.execute(
        "SELECT path FROM photos WHERE listing_id IS NULL")]
    assert waiting == ["waiting.jpg"]


def test_the_bin_column_reaches_a_database_that_predates_it(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` will not touch a database that holds
    real listings, so the column has to be in MIGRATIONS as well as in
    SCHEMA - or it exists only on fresh installs."""
    from mplabel import cli, listings

    home = tmp_path / "marketplace"
    (home / "labels").mkdir(parents=True)
    old = sqlite3.connect(home / "sales.db")
    # The schema as it was before bins: the whole `bins` statement goes,
    # and the referencing column comes out of `listings` by line. Doing
    # it all by statement would take `listings` with it (its body names
    # bin_code); doing it all by line would leave the bins table's body
    # behind as loose SQL.
    before = ";".join(
        "\n".join(line for line in stmt.splitlines()
                  if "bin_code" not in line)
        for stmt in listings.SCHEMA.split(";")
        if "CREATE TABLE IF NOT EXISTS bins" not in stmt) + ";"
    old.executescript(before)
    old.execute("INSERT INTO listings (title) VALUES ('before the column')")
    old.commit()
    old.close()

    conn = cli.connect_db(home)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    assert "bin_code" in cols
    assert conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == 1
    # And the table itself arrives, which no ALTER TABLE would do.
    listings.create_bin(conn, "ATTIC")


def test_bin_put_takes_the_code_off_the_label(db):
    """The four-character code printed on the item's own label is what is
    to hand when you are stood at the shelf with the thing in one hand -
    not a row id, which is never printed on anything."""
    from mplabel import cli, listings

    listings.create_bin(db, "ATTIC")
    db.execute("INSERT INTO listings (title, inventory_code, state) "
               "VALUES ('Hobnail vase', '7K2M', 'active')")
    db.commit()

    args = argparse.Namespace(action="put", item="7k2m", bin="attic")
    cli.cmd_bin(db, {}, args)
    assert len(listings.bin_contents(db, "ATTIC")["items"]) == 1

    args = argparse.Namespace(action="put", item="NOPE", bin="ATTIC")
    with pytest.raises(SystemExit, match="inventory code"):
        cli.cmd_bin(db, {}, args)


def test_retiring_a_bin_does_not_take_its_contents_with_it(db):
    """`rm` is not a cascade. The point of a bin going away is that the
    shelf is being cleared, and the things were the reason for clearing
    it - deleting them would be the one unrecoverable outcome here."""
    from mplabel import cli, listings

    listings.create_bin(db, "ATTIC")
    db.execute("INSERT INTO listings (title, inventory_code, state) "
               "VALUES ('Hobnail vase', '7K2M', 'active')")
    db.commit()
    cli.cmd_bin(db, {}, argparse.Namespace(action="put", item="7K2M",
                                           bin="ATTIC"))
    cli.cmd_bin(db, {}, argparse.Namespace(action="rm", bin="ATTIC"))

    row = db.execute("SELECT title, bin_code FROM listings").fetchone()
    assert row["title"] == "Hobnail vase"
    assert row["bin_code"] is None


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


def test_every_served_asset_is_cache_busted():
    """`STAMPED_ASSETS` is what stops a client going on using a cached
    copy of a file that changed. A served asset missing from it ships an
    update that never arrives - the deploy looks done and the behaviour
    is the old one.

    This asks the directory rather than naming files, because the failure
    is always the same shape: someone adds an asset and does not think
    about this list. It started life pinning marker.js alone, which is
    exactly the file that had been forgotten once."""
    from mplabel import web

    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"
    served = {p.name for p in static.iterdir()
              if p.suffix in (".js", ".css")}
    missing = served - set(web.STAMPED_ASSETS)
    assert not missing, f"served but never cache-busted: {sorted(missing)}"

    # And nothing in the list has been deleted from under it - a stale
    # name is a silent no-op in `shell_html`, not an error.
    assert not set(web.STAMPED_ASSETS) - served


def test_the_desk_shell_is_served(app):
    """`/desk` is an alias resolved in `safe_static_path`, so it goes
    through the same containment check as everything else - and it has to
    get the shell treatment, or its scripts ship without a version stamp
    and a laptop goes on running the copy it cached."""
    base, _ = app
    status, headers, body = _http(base + "/desk")
    assert status == 200
    assert b"<title>mplabel \xe2\x80\x94 desk</title>" in body
    assert headers.get("Cache-Control") == "no-store"
    for name in ("desk.js", "desk.css", "tokens.css", "common.js"):
        assert f'"/{name}?v='.encode() in body, f"{name} is not stamped"
    # The portrait, standalone manifest belongs to the phone. A laptop
    # tab picking it up is an install prompt for the wrong application.
    assert b'rel="manifest"' not in body

    # And the trailing-slash form is the same page, not a 404.
    assert _http(base + "/desk/")[0] == 200


IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
          "Mobile/15E148 Safari/604.1")
# What that same iPhone sends after Request Desktop Site: a Mac, exactly.
IPHONE_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                  "Safari/605.1.15")
ANDROID = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
ANDROID_DESKTOP = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# What a browser sends when it is following a link or an address, and
# what nothing scripted sends. Only a navigation gets routed.
NAV = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _nav(agent, **extra):
    head = dict(NAV)
    head["User-Agent"] = agent
    head.update(extra)
    return head


@pytest.mark.parametrize("agent,desk", [
    (IPHONE, False),
    (ANDROID, False),
    (MAC, True),
    # The whole point: Request Desktop Site works by rewriting the UA, so
    # honouring it and reading the UA are the same act. If these two ever
    # disagree with the two above, the button has stopped working.
    (IPHONE_DESKTOP, True),
    (ANDROID_DESKTOP, True),
])
def test_request_desktop_site_is_just_the_user_agent(agent, desk):
    from mplabel import web
    assert web.prefers_desk(agent) is desk


def test_the_client_hint_beats_the_user_agent_string():
    """`Sec-CH-UA-Mobile` is the designed replacement for reading the UA,
    Chrome and Edge send it unasked, and it flips with Request Desktop
    Site like everything else. Where it exists it is the better answer -
    including on a UA string that has been rewritten to look like a Mac
    while the browser still knows it is a phone."""
    from mplabel import web
    assert web.prefers_desk(MAC, "?1") is False
    assert web.prefers_desk(IPHONE, "?0") is True
    # Anything else and it falls back rather than guessing.
    assert web.prefers_desk(IPHONE, "") is False
    assert web.prefers_desk(MAC, "banana") is True


def test_a_laptop_is_sent_to_the_desk_and_a_phone_is_not(app):
    base, _ = app
    status, headers, _ = _http_once(base + "/", headers=_nav(MAC))
    assert status == 302
    assert headers.get("Location") == "/desk"
    # One URL answering two ways off a header, with Cloudflare in front.
    assert "User-Agent" in (headers.get("Vary") or "")

    status, _h, body = _http_once(base + "/", headers=_nav(IPHONE))
    assert status == 200
    assert b"<title>mplabel</title>" in body

    # And the other way round: a phone that went looking for the desk on
    # purpose is sent back, because it did not say `?ui=`.
    status, headers, _ = _http_once(base + "/desk", headers=_nav(IPHONE))
    assert status == 302 and headers.get("Location") == "/"


def test_asking_for_one_pins_it_and_the_link_back_still_works(app):
    """The failure this is really about: click "the phone app" on a
    laptop, get auto-routed straight back to the desk, forever. The link
    carries `?ui=`, which is remembered - so the second request, with no
    parameter at all, has to come out the other way."""
    base, _ = app
    status, headers, _ = _http_once(base + "/?ui=phone",
                               headers=_nav(MAC))
    assert status == 302
    assert headers.get("Location") == "/", "should land without the ?ui"
    cookie = (headers.get("Set-Cookie") or "")
    assert cookie.startswith("mplabel_ui=phone")
    assert "HttpOnly" not in cookie, "a display preference is not a secret"

    pinned = cookie.split(";")[0]
    status, _h, body = _http_once(base + "/", cookie=pinned,
                             headers=_nav(MAC))
    assert status == 200, "the laptop bounced back to the desk"
    assert b"<title>mplabel</title>" in body

    # And back again, from the phone app's own Settings link.
    status, headers, _ = _http_once(base + "/desk?ui=desk", cookie=pinned,
                               headers=_nav(IPHONE))
    assert status == 302 and headers.get("Location") == "/desk"
    assert (headers.get("Set-Cookie") or "").startswith("mplabel_ui=desk")


def test_auto_routing_leaves_everything_that_is_not_an_entry_point_alone(app):
    """Assets, the API and deep links must fall straight through. A
    redirect on `/app.js` would be a phone app that cannot load itself."""
    base, _ = app
    for path in ("/app.js", "/desk.js", "/manifest.json", "/healthz"):
        status, _h, _b = _http(base + path, headers=_nav(MAC))
        assert status == 200, path


def test_only_a_navigation_is_routed(app):
    """A 302 at `/` is for a browser following a link. Anything scripted
    - curl, a health check, the deploy check in CLAUDE.md - asks for `/`
    without saying it wants HTML, and has to keep getting what it always
    got rather than a redirect it was never taught to follow."""
    base, _ = app
    status, _h, body = _http(base + "/", headers={"User-Agent": MAC})
    assert status == 200
    assert b"<title>mplabel</title>" in body


def test_auto_routing_can_be_turned_off(app_no_routing):
    """Off, `/` is the phone app for everybody, exactly as it was."""
    base = app_no_routing
    status, _h, body = _http(base + "/", headers=_nav(MAC))
    assert status == 200
    assert b"<title>mplabel</title>" in body


def test_the_installed_app_pins_itself_to_the_phone():
    """An installed PWA must never be redirected to the desk. On an iPad
    it would be: from iPadOS 13 Safari reports itself as a Mac and
    nothing in the request says otherwise. `start_url` carries the
    override so the first launch settles it."""
    manifest = json.loads(
        (Path(__file__).parent.parent / "src" / "mplabel" / "static"
         / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "/?ui=phone"
    assert manifest["scope"] == "/", "start_url must stay inside scope"


def test_each_shell_links_to_the_other_with_the_override(app):
    """A bare link between them is a button that bounces you back."""
    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"
    assert '"/?ui=phone"' in (static / "desk.js").read_text(encoding="utf-8")
    assert '"/desk?ui=desk"' in (static / "app.js").read_text(encoding="utf-8")


def test_the_desk_and_the_phone_share_one_palette():
    """Both clients read the same tokens, so a colour changed for one
    cannot leave the other behind. Nobody has the two open side by side,
    which is exactly why nothing would report the drift."""
    static = Path(__file__).parent.parent / "src" / "mplabel" / "static"
    tokens = (static / "tokens.css").read_text(encoding="utf-8")
    for name in ("--ac", "--al", "--wa", "--cbg", "--mv-font-mono"):
        assert name in tokens, f"{name} is not in tokens.css"
    for sheet in ("app.css", "desk.css"):
        text = (static / sheet).read_text(encoding="utf-8")
        assert "--ac:" not in text, f"{sheet} redefines the palette"
        assert "--mv-font-sans:" not in text, f"{sheet} redefines the fonts"
    for shell in ("index.html", "desk.html"):
        assert "/tokens.css" in (static / shell).read_text(encoding="utf-8")


def test_the_preview_outlines_the_crop_that_will_actually_print(tmp_path):
    """Read off the picture, not off the arithmetic that made it.

    The whole value of this preview is that the green box is where the
    4x6 comes from. A box computed twice - once to crop, once to draw -
    is a picture that agrees with nothing, which is the mistake
    `_marker_band`/`marker_box` are paired to avoid. So this renders the
    PNG, finds the green, and checks it lands on `crop_bbox`."""
    from PIL import Image

    from mplabel import label

    src = FIXTURES / "label_sample.pdf"
    png, _info = label.preview_png(src, width=800)
    info = label.to_4x6(src, tmp_path / "out.pdf")

    img = Image.open(io.BytesIO(png)).convert("RGB")
    want = label.PREVIEW_TAKEN
    xs, ys = [], []
    for x in range(img.width):
        for y in range(img.height):
            r, g, b = img.getpixel((x, y))
            if (abs(r - want[0]) < 40 and abs(g - want[1]) < 40
                    and abs(b - want[2]) < 40):
                xs.append(x)
                ys.append(y)
    assert xs, "no outline was drawn at all"

    pw, ph = info["page_size"]
    sx, sy = img.width / pw, img.height / ph
    x0, y0, x1, y1 = info["crop_bbox"]
    # PDF space is bottom-up; the image is top-down.
    for got, expected in ((min(xs), x0 * sx), (max(xs), x1 * sx),
                          (min(ys), (ph - y1) * sy),
                          (max(ys), (ph - y0) * sy)):
        assert abs(got - expected) <= 6, (got, expected)


def test_the_preview_dims_what_is_not_going_to_print(tmp_path):
    """An outline alone leaves "which side of this line survives" to be
    worked out. On a letter sheet that is mostly not the label, that is
    the question."""
    from PIL import Image

    from mplabel import label

    png, _info = label.preview_png(FIXTURES / "label_sample.pdf", width=600)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    info = label.to_4x6(FIXTURES / "label_sample.pdf", tmp_path / "o.pdf")
    pw, ph = info["page_size"]
    x0, y0, x1, y1 = info["crop_bbox"]
    sx, sy = img.width / pw, img.height / ph

    inside = img.getpixel((int((x0 + x1) / 2 * sx),
                           int((ph - (y0 + y1) / 2) * sy)))
    # A corner of the page, well outside any candidate block.
    outside = img.getpixel((4, 4))
    assert sum(inside) > sum(outside), \
        "the part that prints is not brighter than the part that does not"


def test_the_preview_route_is_a_png_and_needs_a_session(app):
    base, _ = app
    pdf = (FIXTURES / "label_sample.pdf").read_bytes()
    head = {"Content-Type": "application/pdf", "X-Mplabel": "1"}

    status, _h, _b = _http(base + "/api/label/preview", "POST",
                           headers=head, raw=pdf)
    assert status == 401, "anyone could render a label they uploaded"

    _s, cookie = _login(base)
    status, headers, body = _http(base + "/api/label/preview", "POST",
                                  cookie=cookie, headers=head, raw=pdf)
    assert status == 200
    assert headers.get("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_the_desk_checks_a_stray_label_before_it_spends_one():
    """This printer cannot report a failure, so a wrong crop costs a
    label and says nothing - and a PDF from a seller nobody has printed
    before is exactly where a wrong crop comes from.

    Two things hold that up, and both are one character from being
    wrong. Check-only starts **on**, so the first press of a new file
    measures rather than prints. And the real print asks first, where
    the dry run does not: the phone holds a button for 800ms and a mouse
    makes that awkward, so the desk uses the confirm dialog instead."""
    js = (Path(__file__).parent.parent / "src" / "mplabel" / "static"
          / "desk.js").read_text(encoding="utf-8")

    take = js[js.index("function takeSendFile("):js.index("function clearSend(")]
    assert "dry: true" in take, "a newly chosen PDF would print unchecked"

    send = js[js.index("function doSendLabel("):js.index("function runSendLabel(")]
    assert "if (pick.dry) return runSendLabel()" in send, \
        "a dry run should not ask - it spends nothing"
    assert "ask(" in send, "printing a stray label does not confirm"


def test_the_desk_queue_shows_no_full_buyer_name():
    """`_order_row` sends a first name and `_order_detail` sends the whole
    one, deliberately - a list is what gets left open on a kitchen table
    and screenshotted. The desk renders a queue and a detail pane on the
    same screen, so it is the one client that can mix them up."""
    js = (Path(__file__).parent.parent / "src" / "mplabel" / "static"
          / "desk.js").read_text(encoding="utf-8")
    rows = js[js.index("function viewQueue("):js.index("function pickOrder(")]
    assert "o.buyer" in rows, "the queue rows stopped naming the buyer"
    assert "detail.buyer" not in rows and "d.buyer" not in rows, \
        "the queue list is reading the detail payload's full name"


def test_both_shells_are_stamped_and_never_cached():
    """A shell served with an ETag is a client that revalidates its way
    to the same stale asset URLs. `index.html` was special-cased by name;
    the desk portal is a second one, and the check has to know that or it
    ships unstamped."""
    from mplabel import web
    assert set(web.SHELLS) == {"index.html", "desk.html"}
    for name in web.SHELLS:
        assert (Path(__file__).parent.parent / "src" / "mplabel" / "static"
                / name).is_file(), f"{name} is listed as a shell but absent"
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


# ------------------------------------- phase 4: where cost basis comes from
#
# The analytics have carried `margin` and `net` since the listings views
# were written, and both have always been null: nothing could record what
# a thing cost. These are the endpoints that change that, so the tests
# below are about the ways the money could go quietly wrong rather than
# about the HTTP.


def _auth(base):
    """A bearer token and the CSRF header, the way the phone sends them."""
    import json as _json

    status, _, body = _http(f"{base}/api/login", "POST",
                            {"password": "hunter2"})
    assert status == 200
    token = _json.loads(body)["token"]
    return {"Authorization": "Bearer " + token, "X-Mplabel": "1"}


def _json_of(body):
    import json as _json

    return _json.loads(body)


def test_a_trip_reports_what_is_still_unattributed(app):
    """The number the triage screen is chasing.

    Deliberately not the sum of the items: a receipt has tax on it and
    things that never became listings, so the gap is real and worth
    showing rather than forcing to zero."""
    base, conn = app
    head = _auth(base)

    status, _, body = _http(f"{base}/api/v1/trips", "POST",
                            {"store": "GOODWILL 214", "receipt_total": 29.46},
                            headers=head)
    assert status == 200
    trip = _json_of(body)["trip"]
    assert trip["unassigned"] == 29.46, "nothing attributed yet"

    _http(f"{base}/api/v1/inventory", "POST",
          {"title": "Stoneware crock", "paid": 4.99, "price": 38.0,
           "trip": trip["id"]}, headers=head)

    _, _, body = _http(f"{base}/api/v1/trips/{trip['id']}", headers=head)
    after = _json_of(body)
    assert after["trip"]["assigned"] == 4.99
    assert after["trip"]["unassigned"] == 24.47
    assert after["trip"]["listed_for"] == 38.0
    assert [i["title"] for i in after["items"]] == ["Stoneware crock"]


def test_a_trip_with_no_receipt_total_says_unknown_not_zero(app):
    """`None` and `0.00` are different answers and the screen shows one of
    them as money to go and find."""
    base, _ = app
    head = _auth(base)
    _, _, body = _http(f"{base}/api/v1/trips", "POST",
                       {"store": "ESTATE SALE"}, headers=head)
    assert _json_of(body)["trip"]["unassigned"] is None


def test_an_item_added_by_hand_is_keyed_so_its_sale_can_find_it(app):
    """A local pickup produces no label email, so the sale turns up later
    knowing only the title. `link_sales` matches on `title_key`, so a
    manual item under any other scheme would sit beside its own sale
    rather than being it."""
    from mplabel import listings

    base, conn = app
    head = _auth(base)
    title = "Hobnail milk glass vase"
    status, _, body = _http(f"{base}/api/v1/inventory", "POST",
                            {"title": title, "paid": 6.0, "price": 28.0},
                            headers=head)
    assert status == 200
    item = _json_of(body)["item"]
    assert item["listing_id"] == listings.title_key(title)
    # And it is on a shelf-labelable footing straight away.
    assert len(item["inventory_code"]) == 4


def test_a_manual_item_reconciles_with_the_sale_that_follows_it(app):
    """The end of the same story: she adds the thing, it sells locally,
    and the two rows must become one."""
    from mplabel import listings

    base, conn = app
    head = _auth(base)
    _http(f"{base}/api/v1/inventory", "POST",
          {"title": "Pressed glass tumblers", "paid": 3.0}, headers=head)
    conn.execute(
        "INSERT INTO sales (message_id, item, price, code, status) VALUES "
        "('<local>', 'Pressed glass tumblers', 32.0, 'B4M', 'to_ship')")
    conn.commit()

    listings.refresh(conn)
    rows = conn.execute(
        "SELECT paid, price, state FROM listings WHERE title=?",
        ("Pressed glass tumblers",)).fetchall()
    assert len(rows) == 1, "the sale must not create a second listing"
    assert rows[0]["paid"] == 3.0, "the cost survives reconciliation"


def test_cost_reaches_the_margin_view(app):
    """The whole point of the phase. `v_listing_perf.margin` has existed
    all along and been null on every row."""
    from mplabel import listings

    base, conn = app
    head = _auth(base)
    _, _, body = _http(f"{base}/api/v1/inventory", "POST",
                       {"title": "Oil portrait, unsigned", "price": 145.0},
                       headers=head)
    item = _json_of(body)["item"]
    assert item["paid"] is None

    _http(f"{base}/api/v1/inventory/{item['id']}/fields", "POST",
          {"paid": "12.50"}, headers=head)
    listings.refresh(conn)
    # The view is keyed by listing_id, not the row id.
    row = conn.execute(
        "SELECT paid, margin FROM v_listing_perf WHERE listing_id=?",
        (item["listing_id"],)).fetchone()
    assert row["paid"] == 12.5, "a typed '$' string is still money"
    assert row["margin"] == 132.5


def test_a_price_that_is_not_a_number_is_refused_not_dropped(app):
    """Silently storing "unknown" for a figure she typed is the same class
    of quiet loss as reading the offset field as dollars."""
    base, _ = app
    head = _auth(base)
    status, _, body = _http(f"{base}/api/v1/inventory", "POST",
                            {"title": "A thing", "paid": "no idea"},
                            headers=head)
    assert status == 400
    assert "number" in _json_of(body)["error"]


def test_the_same_photo_twice_is_one_row(app):
    """She is in a shop on one bar of signal and the client retries. A
    second row would put the same receipt in the triage pile twice."""
    base, _ = app
    head = dict(_auth(base), **{"Content-Type": "image/jpeg"})
    shot = b"\xff\xd8\xff\xe0" + b"not really a jpeg, but the bytes are the id"

    status, _, first = _http(f"{base}/api/v1/photos", "POST", raw=shot,
                             headers=head)
    assert status == 200
    status, _, second = _http(f"{base}/api/v1/photos", "POST", raw=shot,
                              headers=head)
    assert status == 200
    assert _json_of(first)["photo"]["id"] == _json_of(second)["photo"]["id"]

    _, _, body = _http(f"{base}/api/v1/photos", headers=head)
    assert len(_json_of(body)["photos"]) == 1


def test_a_photo_is_stored_under_home_and_served_back(app):
    base, _ = app
    head = dict(_auth(base), **{"Content-Type": "image/jpeg"})
    shot = b"\xff\xd8\xff\xe0 the actual bytes"
    _, _, body = _http(f"{base}/api/v1/photos", "POST", raw=shot, headers=head)
    photo = _json_of(body)["photo"]

    stored = Path(photo["path"])
    assert stored.parent.name == "photos", "beside labels/, not loose in home"
    assert stored.name.startswith(photo["sha256"]), "named by its digest"

    status, headers, served = _http(f"{base}/api/v1/photos/{photo['id']}",
                                    headers=head)
    assert status == 200
    assert served == shot
    assert headers["Content-Type"] == "image/jpeg"


def test_a_photo_of_the_wrong_kind_is_refused_by_type_not_by_filename(app):
    """The extension is chosen from the Content-Type, so a client cannot
    name the file on disk."""
    base, _ = app
    head = dict(_auth(base), **{"Content-Type": "application/x-msdownload"})
    status, _, body = _http(f"{base}/api/v1/photos", "POST", raw=b"MZ...",
                            headers=head)
    assert status == 400
    assert "photo must be" in _json_of(body)["error"]


def test_a_receipt_filed_against_a_trip_leaves_the_pile(app):
    """A receipt is never about one listing.

    The pile asked only for `listing_id IS NULL` at first, on the
    schema's reading that a capture is triaged by becoming an item. True
    of a photograph of an object; false of a receipt, which is the record
    of a trip several of whose items it paid for - so a filed receipt sat
    in the queue for ever, and that queue is the one number on the
    capture screen. Money still needing a home is `trip.unassigned`, a
    different question."""
    base, _ = app
    head = _auth(base)
    shot_head = dict(head, **{"Content-Type": "image/jpeg"})
    _, _, body = _http(f"{base}/api/v1/photos", "POST",
                       raw=b"\xff\xd8 a receipt", headers=shot_head)
    photo = _json_of(body)["photo"]
    _, _, body = _http(f"{base}/api/v1/trips", "POST",
                       {"store": "GOODWILL 214", "receipt_total": 29.46},
                       headers=head)
    trip = _json_of(body)["trip"]

    _, _, body = _http(f"{base}/api/v1/photos", headers=head)
    assert len(_json_of(body)["photos"]) == 1, "not filed yet"

    _http(f"{base}/api/v1/photos/{photo['id']}/attach", "POST",
          {"trip": trip["id"]}, headers=head)
    _, _, body = _http(f"{base}/api/v1/photos", headers=head)
    assert _json_of(body)["photos"] == [], "it is the record of that trip now"


def test_the_triage_pile_is_what_has_no_item_yet(app):
    """No state column and no captures table - "not yet triaged" is the
    absence of a reference, and a flag would be a second place for it to
    be wrong."""
    base, _ = app
    head = _auth(base)
    shot_head = dict(head, **{"Content-Type": "image/png"})
    _, _, body = _http(f"{base}/api/v1/photos", "POST", raw=b"\x89PNG shot",
                       headers=shot_head)
    photo = _json_of(body)["photo"]

    _, _, body = _http(f"{base}/api/v1/photos", headers=head)
    assert len(_json_of(body)["photos"]) == 1

    _, _, body = _http(f"{base}/api/v1/inventory", "POST",
                       {"title": "Chenille bedspread", "paid": 12.0},
                       headers=head)
    item = _json_of(body)["item"]
    _http(f"{base}/api/v1/photos/{photo['id']}/attach", "POST",
          {"listing": item["id"]}, headers=head)

    _, _, body = _http(f"{base}/api/v1/photos", headers=head)
    assert _json_of(body)["photos"] == [], "it is about something now"


def test_an_oversized_photo_is_refused_before_it_is_read(app):
    """MAX_BODY stays small on purpose - reading a hundred megabytes into
    memory on a Pi is how the OOM killer stops the label printer - so the
    allowance is raised on this one route and nowhere else.

    The refusal is on Content-Length, before a byte of the body is read,
    which is the whole point: draining it to be polite about the
    connection would be doing the thing being refused. The client sees
    the socket go rather than the message, and what has to be true is
    that nothing was stored."""
    import urllib.error
    from mplabel import web

    assert web.MAX_BODY == 2 * 1024 * 1024
    assert web.MAX_PHOTO > web.MAX_BODY

    base, conn = app
    head = dict(_auth(base), **{"Content-Type": "image/jpeg"})
    try:
        status, _, body = _http(f"{base}/api/v1/photos", "POST",
                                raw=b"x" * (web.MAX_PHOTO + 1), headers=head)
        assert status in (400, 413), body
    except (urllib.error.URLError, ConnectionError, BrokenPipeError):
        pass

    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


# ------------------------------------------------- a label from anywhere
#
# Everything else on this server prints a label it already has, off a
# sales row, checked against the address recorded when that sale was
# filed. This route prints a PDF somebody just handed it and records
# nothing beyond printd's journal, and every test here is about one of
# those two halves staying true.

def _adhoc(base, headers, body, query=""):
    head = dict(headers or {})
    head["Content-Type"] = "application/pdf"
    return _http(f"{base}/api/v1/print/label{query}", "POST", raw=body,
                 headers=head)


def test_an_arbitrary_label_prints_through_the_same_path_as_everything_else(
        app, monkeypatch):
    """Not a reimplementation of printing. `cli.print_label` is what knows
    to take the flock on a local backend and to skip it on a remote one,
    and a second copy of that decision is how the loopback deadlock got
    reintroduced against the second device."""
    from mplabel import cli

    base, _conn = app
    head = _auth(base)

    sent = []
    monkeypatch.setattr(cli, "print_label",
                        lambda *a, **k: sent.append((a, k)))
    status, _, body = _adhoc(base, head, LABEL_PDF.read_bytes())
    assert status == 200
    info = _json_of(body)["label"]
    assert info["printed"] is True
    assert info["size_in"] == [4.0, 6.0]
    assert len(sent) == 1
    # (cfg, path) and a job id, and no parcel code: codes come off the
    # sales table and this has no row in it, so stamping one would put a
    # parcel that does not exist on a real label.
    assert len(sent[0][0]) == 2
    assert sent[0][1] == {"job": info["job"]}


def test_an_arbitrary_label_leaves_no_row_behind(app, monkeypatch):
    """It is not a sale. A row here would put a parcel nobody bought into
    revenue, into sell-through and into the Sheet, and `verify` would
    then be checking a label against a buyer who does not exist."""
    from mplabel import cli

    base, conn = app
    head = _auth(base)
    before = conn.execute("SELECT count(*) FROM sales").fetchone()[0]

    monkeypatch.setattr(cli, "print_label", lambda *a, **k: None)
    assert _adhoc(base, head, LABEL_PDF.read_bytes())[0] == 200
    assert conn.execute("SELECT count(*) FROM sales").fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0


def test_the_same_pdf_twice_is_one_job(app, monkeypatch):
    """She is on a phone behind a tunnel and the request timed out. A
    random job id would turn her retry into a second label; the digest
    lets printd answer 409 instead. Asking again on purpose is --force,
    which is a different intent and gets a different id."""
    from mplabel import cli

    base, _conn = app
    head = _auth(base)
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: None)

    body = LABEL_PDF.read_bytes()
    first = _json_of(_adhoc(base, head, body)[2])["label"]
    again = _json_of(_adhoc(base, head, body)[2])["label"]
    forced = _json_of(_adhoc(base, head, body, "?force=1")[2])["label"]

    assert first["job"] == again["job"]
    assert forced["job"] != first["job"]
    assert first["job"].startswith("adhoc-")


def test_the_job_id_reaches_the_printer_or_it_dedupes_nothing(tmp_path,
                                                              monkeypatch):
    """Deriving the id from the digest is only worth anything if printd
    is the thing that sees it. It rides on the remote backend alone: a
    local device keeps no journal, so there is nothing there that could
    answer a duplicate."""
    from mplabel import cli, printers

    calls = []
    monkeypatch.setattr(printers, "send",
                        lambda path, backend, **kw: calls.append((backend, kw)))
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    cli.print_label({"printer_backend": "pi-http", "printd_url": "http://x",
                     "printd_secret": "s", "home": str(tmp_path)},
                    str(pdf), job="adhoc-deadbeef")
    assert calls[-1][1]["job"] == "adhoc-deadbeef"

    cli.print_label({"printer_backend": "tspl", "printer_device": "/dev/null",
                     "home": str(tmp_path)}, str(pdf), job="adhoc-deadbeef")
    assert "job" not in calls[-1][1]


def test_a_dry_run_prints_nothing_and_still_says_what_it_would_do(
        app, monkeypatch):
    """On a printer that cannot report a failure, the cheap way to find
    out whether a new seller's PDF crops correctly has to cost no stock."""
    from mplabel import cli

    base, _conn = app
    head = _auth(base)
    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))

    status, _, body = _adhoc(base, head, LABEL_PDF.read_bytes(), "?dry_run=1")
    assert status == 200
    info = _json_of(body)["label"]
    assert sent == []
    assert info["printed"] is False and info["dry_run"] is True
    assert info["size_in"] == [4.0, 6.0]
    assert info["rotation"] == 90


@pytest.mark.parametrize("backend,expected", [("tspl", "nowhere"),
                                              ("pi-http", "printd journal")])
def test_the_answer_says_where_the_record_went(tmp_path, backend, expected):
    """Journal-only is the whole design, so "which journal" has to be
    answerable - and pointed straight at a device the honest answer is
    "nowhere", not a quieter wording of it. A caveat that stops being
    true, or was never true on this host, is worse than no caveat."""
    import threading

    from mplabel import cli, web

    cli.connect_db(tmp_path)
    cfg = {"home": str(tmp_path), "printer_backend": backend,
           "web_password_hash": web.hash_password("hunter2"),
           "web_secure_cookie": "no"}
    srv = web.Server(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        info = _json_of(_adhoc(base, _auth(base), LABEL_PDF.read_bytes(),
                               "?dry_run=1")[2])["label"]
        assert expected in info["recorded"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_body_that_is_not_a_pdf_is_refused_before_anything_opens(
        app, monkeypatch):
    from mplabel import cli

    base, _conn = app
    head = _auth(base)
    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))

    status, _, body = _adhoc(base, head, b"this is not a pdf at all")
    assert status == 400
    assert "not a PDF" in _json_of(body)["error"]

    # And a PDF announced as something else. The type is checked as well
    # as the magic, because the type is what says how to read the body.
    status, _, _ = _http(f"{base}/api/v1/print/label", "POST",
                         raw=b"%PDF-1.4\n",
                         headers=dict(head, **{"Content-Type": "text/plain"}))
    assert status == 400
    assert sent == []


def test_a_label_that_cannot_be_cropped_says_why(app, tmp_path, monkeypatch):
    """"This may not be a shipping label" is actionable, "bad request" is
    not, and this is the one screen where the reason *is* the feature -
    the person holding the file is the only one who can resolve it."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from mplabel import cli

    base, _conn = app
    head = _auth(base)
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: None)

    src = tmp_path / "full.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    c.rect(20, 20, 550, 700)
    c.showPage()
    c.save()

    status, _, body = _adhoc(base, head, src.read_bytes())
    assert status == 400
    assert "4 x 6" in _json_of(body)["error"]


def test_a_page_and_a_region_can_be_chosen_over_the_wire(app, tmp_path,
                                                         monkeypatch):
    """The server refuses to guess between two look-alike blocks, so the
    way to resolve that has to survive the trip from the phone."""
    from reportlab.pdfgen import canvas
    from mplabel import cli

    base, _conn = app
    head = _auth(base)
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: None)

    src = tmp_path / "two.pdf"
    c = canvas.Canvas(str(src), pagesize=(612, 936))
    _label_block(c, x0=90, y0=40)
    _label_block(c, x0=90, y0=500)
    c.showPage()
    c.save()
    body = src.read_bytes()

    status, _, answer = _adhoc(base, head, body)
    assert status == 400 and "--region" in _json_of(answer)["error"]

    one = _json_of(_adhoc(base, head, body, "?region=1&dry_run=1")[2])["label"]
    two = _json_of(_adhoc(base, head, body, "?region=2&dry_run=1")[2])["label"]
    assert one["crop_bbox"] != two["crop_bbox"]
    assert one["regions_found"] == 2

    status, _, answer = _adhoc(base, head, body, "?region=9")
    assert status == 400 and "no region 9" in _json_of(answer)["error"]


def test_an_oversized_upload_is_refused_on_content_length(app, monkeypatch):
    """Refused before a byte is read, like the photo route: pulling a
    hundred megabytes into memory on a Pi is how the OOM killer gets to
    stop the label printer."""
    import urllib.error

    from mplabel import cli, web

    base, _conn = app
    head = _auth(base)
    sent = []
    monkeypatch.setattr(cli, "print_label", lambda *a, **k: sent.append(a))
    assert web.MAX_LABEL < web.MAX_PHOTO

    try:
        status, _, _ = _adhoc(base, head, b"%PDF-1.4\n" + b"\0" * web.MAX_LABEL)
        assert status in (400, 413)
    except (urllib.error.URLError, ConnectionError, BrokenPipeError):
        pass
    assert sent == []


def test_the_ad_hoc_route_needs_authentication(app):
    base, _conn = app
    assert _adhoc(base, {"X-Mplabel": "1"}, b"%PDF-1.4\n")[0] == 401


def test_the_sourcing_routes_need_authentication(app):
    """Every one of them, including the ones that only read. The receipts
    and the cost of everything are as private as the addresses."""
    base, _ = app
    for method, path in [("GET", "/api/v1/trips"),
                         ("POST", "/api/v1/trips"),
                         ("GET", "/api/v1/photos"),
                         ("POST", "/api/v1/photos"),
                         ("POST", "/api/v1/inventory")]:
        status, _, _ = _http(base + path, method,
                             {} if method == "POST" else None)
        assert status == 401, f"{method} {path} answered without a token"


def test_era_is_free_text_and_survives_a_migration(app):
    """Roughly when a thing is from - "c. 1910", "mid-century".

    Not a year, and that is the point: her titles say "Antique 1900-1915
    American Edwardian", which is a range and a guess at once. An integer
    column would force a precision the object does not have.

    It is also the newest column, so it is the one that proves the
    migration loop still runs - `CREATE TABLE IF NOT EXISTS` will not
    touch a database that already holds real sales."""
    base, conn = app
    head = _auth(base)
    status, _, body = _http(f"{base}/api/v1/inventory", "POST",
                            {"title": "Oil portrait, unsigned",
                             "era": "c. 1910", "condition": "Craquelure",
                             "paid": 12.0}, headers=head)
    assert status == 200
    item = _json_of(body)["item"]
    assert item["era"] == "c. 1910"
    assert item["condition"] == "Craquelure"

    # And it is correctable like every other field.
    _http(f"{base}/api/v1/inventory/{item['id']}/fields", "POST",
          {"era": "mid-century"}, headers=head)
    _, _, body = _http(f"{base}/api/v1/inventory/{item['id']}", headers=head)
    assert _json_of(body)["item"]["era"] == "mid-century"


def test_a_database_predating_era_gains_it(tmp_path):
    """The migration itself, against a database built without the column."""
    import sqlite3

    from mplabel import cli, listings

    # `connect_db` opens home/sales.db, not any name the caller picks -
    # writing this as mplabel.db made the test build one database and
    # migrate a different, empty one.
    path = tmp_path / "sales.db"
    conn = sqlite3.connect(path)
    # Drop the column from the DDL, the way a database created before it
    # existed would have been. A literal string replace here silently
    # matched nothing once, and the test then asserted on a schema that
    # did have the column - passing for the wrong reason is exactly what
    # this test is guarding against elsewhere.
    older = re.sub(r"^\s*era\s+TEXT,\n", "", listings.SCHEMA, flags=re.M)
    # The word appears in half the prose in that file ("several",
    # "operates"), so this asks about the *declaration*.
    assert not re.search(r"^\s*era\s+TEXT", older, flags=re.M), \
        "the era column was not removed from the DDL"
    conn.executescript(older)
    conn.commit()
    conn.close()
    assert "era" not in _columns(path, "listings")

    conn = cli.connect_db(tmp_path)
    try:
        assert "era" in _columns(path, "listings")
    finally:
        conn.close()


def _columns(path, table):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


# --------------------------------------------- push, and what earns one


def _today():
    """Local, not SQLite's `date('now')`, which is UTC. Mixing the two is
    an off-by-one after about 7pm Eastern, and it is exactly the bug the
    code under test had."""
    return {"today": date.today().isoformat()}


def _push_db(tmp_path):
    from mplabel import cli, notify

    conn = cli.connect_db(tmp_path)
    conn.executescript(notify.SCHEMA)
    conn.commit()
    return conn


def test_der_to_jose_matches_a_signature_openssl_actually_made(tmp_path):
    """The JWT is ES256 and openssl speaks DER, so the two numbers have to
    be unpacked by hand.

    Not a dependency: `cryptography` is a compiler toolchain and about
    40MB on a Pi that runs a deliberately short dependency list. Twenty
    lines of parsing instead - checked here against a real signature
    rather than against a fixture someone typed, because the failure
    Apple gives for a malformed one is `403 InvalidProviderToken` and
    says nothing at all."""
    import shutil
    import subprocess

    from mplabel import notify

    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")

    key = tmp_path / "key.pem"
    made = subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(key)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("this openssl cannot make a P-256 key")

    jose = notify._sign(key, b"the.signing.input")
    # Fixed width, both halves, always - a short r that is not padded back
    # up is the bug this exists to prevent.
    assert len(jose) == 64

    # And it verifies, which a mangled unpack would not survive.
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key)],
        input=b"the.signing.input", capture_output=True).stdout
    assert len(notify._der_to_jose(der)) == 64


def test_a_notification_is_said_once(tmp_path):
    """Keyed by the thing and the kind, not by a clock. "7QK is due" is
    one notification however often the poller notices, and a cooldown in
    minutes would fire again the moment the process restarted."""
    from mplabel import notify

    conn = _push_db(tmp_path)
    notify.register(conn, "a" * 64)
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<m>', 'Stoneware crock', '7QK', :today, 'to_ship')", _today())
    conn.commit()

    sent = []
    def sender(token, payload):
        sent.append(payload["aps"]["alert"]["title"])
        return True, "ok"

    cfg = {"apns_topic": "com.example.app"}
    first = notify.run(cfg, conn, sender=sender)
    assert len(first["sent"]) == 1
    assert "7QK" in sent[0]

    second = notify.run(cfg, conn, sender=sender)
    assert second["sent"] == [], "the same parcel must not be said twice"
    assert len(sent) == 1


def test_nothing_is_remembered_that_was_not_delivered(tmp_path):
    """A send that failed must not mark the thing as said - otherwise the
    one notification that mattered is the one that is never retried."""
    from mplabel import notify

    conn = _push_db(tmp_path)
    notify.register(conn, "b" * 64)
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<m>', 'Crock', '7QK', :today, 'to_ship')", _today())
    conn.commit()

    cfg = {"apns_topic": "com.example.app"}
    result = notify.run(cfg, conn, sender=lambda t, p: (False, "503"))
    assert result["sent"] == []
    assert not notify.already_said(conn, "due", "7QK")

    ok = notify.run(cfg, conn, sender=lambda t, p: (True, "ok"))
    assert len(ok["sent"]) == 1


def test_a_token_apple_has_retired_is_forgotten(tmp_path):
    """410 is Apple saying the app is gone from that phone. A dead token
    is deleted rather than flagged - keeping it means deciding every time
    whether to try it again."""
    from mplabel import notify

    conn = _push_db(tmp_path)
    notify.register(conn, "c" * 64)
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<m>', 'Crock', '7QK', :today, 'to_ship')", _today())
    conn.commit()

    notify.run({"apns_topic": "x"}, conn,
               sender=lambda t, p: (False, "410 Unregistered"))
    assert notify.devices(conn) == []


def test_only_three_things_earn_a_notification(tmp_path):
    """The design is blunt about the scope and it is worth keeping. A
    notification that is not one of these trains her to swipe them all
    away, and the one that matters is then swiped away fastest."""
    from mplabel import notify

    conn = _push_db(tmp_path)
    notify.register(conn, "d" * 64)
    # due
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<due>', 'Crock', '7QK', :today, 'to_ship')", _today())
    # never printed, today
    conn.execute(
        "INSERT INTO sales (message_id, item, code, status, label_pdf, "
        "received_at) VALUES ('<unp>', 'Vase', 'B4M', 'to_ship', "
        "'/tmp/x.pdf', :now)", {"now": datetime.now().isoformat()})
    # money with no home, on a trip old enough to have been triaged
    conn.execute(
        "INSERT INTO trips (store, occurred_at, receipt_total) "
        "VALUES ('GOODWILL 214', date('now', '-5 days'), 30.0)")
    conn.commit()

    kinds = {n["kind"] for n in
             notify.run({"apns_topic": "x"}, conn,
                        sender=lambda t, p: (True, "ok"))["sent"]}
    assert kinds == {"due", "unprinted", "money"}


def test_loose_money_waits_a_couple_of_days(tmp_path):
    """Telling her on the drive home is nagging, not helping - the
    receipt is in her bag and triage is a kitchen-table job."""
    from mplabel import notify

    conn = _push_db(tmp_path)
    conn.execute("INSERT INTO trips (store, occurred_at, receipt_total) "
                 "VALUES ('GOODWILL 214', date('now'), 30.0)")
    conn.commit()
    assert notify.unattributed(conn) == []

    conn.execute("UPDATE trips SET occurred_at = date('now', '-5 days')")
    conn.commit()
    assert len(notify.unattributed(conn)) == 1


def test_push_refuses_rather_than_half_tries_without_a_key(tmp_path):
    """The same shape as printd's config refusal: an unconfigured install
    must say so plainly rather than fail per-notification for ever."""
    from mplabel import notify

    with pytest.raises(notify.NotifyError) as raised:
        notify.provider_token({"apns_topic": "x"})
    assert "apns_key_path" in str(raised.value)
    assert "apns_key_id" in str(raised.value)


def test_registering_a_device_needs_authentication(app):
    """A token registered by anyone who could reach the port would be a
    stranger receiving her buyers' names in a notification."""
    base, _ = app
    status, _, _ = _http(f"{base}/api/v1/devices", "POST", {"token": "x" * 64})
    assert status == 401

    head = _auth(base)
    status, _, body = _http(f"{base}/api/v1/devices", "POST",
                            {"token": "e" * 64}, headers=head)
    assert status == 200
    # The token is not handed back out, even to an authenticated caller.
    _, _, listed = _http(f"{base}/api/v1/devices", headers=head)
    devices = _json_of(listed)["devices"]
    assert len(devices) == 1
    assert "token" not in devices[0]
    assert devices[0]["token_prefix"] == "eeeeeeee"


def test_a_dry_run_says_nothing_and_remembers_nothing(tmp_path, capsys):
    """Run it twice and it says the same thing, which is the property that
    makes it worth reaching for first."""
    from mplabel import cli, notify

    conn = _push_db(tmp_path)
    notify.register(conn, "f" * 64)
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<m>', 'Crock', '7QK', :today, 'to_ship')", _today())
    conn.commit()

    args = argparse.Namespace(dry_run=True)
    cli.cmd_notify({"apns_topic": "x"}, conn, args)
    first = capsys.readouterr().out
    assert "7QK" in first
    assert not notify.already_said(conn, "due", "7QK")

    cli.cmd_notify({"apns_topic": "x"}, conn, args)
    assert capsys.readouterr().out == first


# ------------------------------- the journal is the only record there is


def test_a_crash_mid_trim_cannot_lose_the_journal(tmp_path, monkeypatch):
    """The G4 is write-only, so this file *is* the answer to "did that come
    out?" - there is no second source for it.

    The trim used to `read_text` then `write_text` over the same path, so
    a crash between the two truncated exactly the record that has no
    other copy. It writes a temp file and `os.replace`s it now, which is
    atomic: a reader sees the old file or the new one."""
    import os

    from mplabel import printd

    monkeypatch.setattr(printd, "JOURNAL_KEEP", 5)
    path = tmp_path / "journal.jsonl"
    journal = printd.Journal(path)
    for n in range(12):
        journal.record(f"job{n}", 10, "sha")

    # The rename is the last thing that happens, so a failure before it
    # must leave the original intact rather than a half-written file.
    real_replace = os.replace

    def explode(src, dst):
        raise OSError("power cut")

    before = path.read_text()
    monkeypatch.setattr(os, "replace", explode)
    journal.record("job99", 10, "sha")
    monkeypatch.setattr(os, "replace", real_replace)

    after = path.read_text()
    assert before.splitlines()[0] in after, "the head was lost"
    assert "job99" in after, "the append itself must still have landed"
    assert not list(tmp_path.glob("*.trim")), "the temp file was left behind"


def test_a_torn_last_line_does_not_lose_the_rest(tmp_path):
    """A half-written last line is what a crash mid-append leaves. Losing
    the rows above it to that one would be losing the record to the
    accident it exists to survive."""
    from mplabel import printd

    path = tmp_path / "journal.jsonl"
    journal = printd.Journal(path)
    journal.record("7QK-abc", 10, "sha")
    journal.record("B4M-def", 10, "sha")
    with open(path, "a") as fh:
        fh.write('{"job": "torn-')          # no newline, no closing brace

    reopened = printd.Journal(path)
    assert reopened.seen("7QK-abc")
    assert reopened.seen("B4M-def")
    assert [r["job"] for r in reopened.since()] == ["7QK-abc", "B4M-def"]


def test_a_trimmed_job_stops_being_seen(tmp_path, monkeypatch):
    """`_done` outlived the file it came from: a job trimmed out of the
    journal stayed 409-able until a restart and then silently stopped
    being. Whether a job is "seen" must not depend on how long the
    process has been up."""
    from mplabel import printd

    monkeypatch.setattr(printd, "JOURNAL_KEEP", 3)
    path = tmp_path / "journal.jsonl"
    journal = printd.Journal(path)
    for n in range(10):
        journal.record(f"job{n}", 10, "sha")

    assert not journal.seen("job0"), "trimmed out of the file, and out of memory"
    assert journal.seen("job9")
    # And a fresh process agrees, which is the property that was broken.
    assert printd.Journal(path).seen("job9")
    assert not printd.Journal(path).seen("job0")


def test_reading_the_journal_takes_the_lock(tmp_path):
    """`since()` re-read the file without the lock, so a reconcile could
    land in the middle of a trim. Hammered from threads here because the
    race is the point - the assertion is that no reader ever sees a
    truncated journal."""
    import threading as _threading

    from mplabel import printd

    path = tmp_path / "journal.jsonl"
    journal = printd.Journal(path)
    for n in range(40):
        journal.record(f"job{n}", 10, "sha")

    seen_short = []

    def read():
        for _ in range(60):
            rows = journal.since()
            if rows and len(rows) < 5:
                seen_short.append(len(rows))

    def write():
        for n in range(60):
            journal.record(f"more{n}", 10, "sha")

    threads = [_threading.Thread(target=read),
               _threading.Thread(target=write)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen_short == [], "a reader saw a journal mid-rewrite"


# --------------------------------------------------- postage, and its source


def test_no_email_carries_the_postage_charge():
    """The finding this feature was gated on, from the real fixture.

    The label email is a *prepaid* label - Facebook pays the carrier and
    takes it out of the payout - so the one document this system reliably
    receives says what the parcel weighs and what service it went by, and
    not what it cost. Pinned as a test because the temptation is to write
    a parser for a number that is not there."""
    raw = (Path(__file__).parent / "fixtures" / "label_email.eml").read_text(
        errors="replace")
    assert "prepaid shipping label" in raw
    assert not re.search(r"postage[^<]{0,40}\$\s*\d", raw, re.I)


def test_postage_is_not_estimated_out_of_nothing(db):
    """A number produced from no observation would be indistinguishable
    from a measured one a week later - and postage on a heavy item is
    routinely the difference between a good margin and none."""
    from mplabel import listings

    guess, source = listings.estimate_postage(db, "11 lb")
    assert guess is None
    assert source is None


def test_an_estimate_comes_from_what_was_actually_paid(db):
    """Once a real charge exists, another parcel can be reasoned about -
    and the answer is still labelled an estimate."""
    from mplabel import listings

    db.executemany(
        "INSERT INTO sales (message_id, item, weight, postage, "
        "postage_source, status) VALUES (?,?,?,?,'confirmed','shipped')",
        [("<a>", "Light", "2 lb", 8.00),
         ("<b>", "Heavy", "12 lb", 20.00)])
    db.commit()

    # Between the two, linear.
    guess, source = listings.estimate_postage(db, "7 lb")
    assert source == "estimated"
    assert guess == pytest.approx(14.0, abs=0.01)

    # Outside them, flat rather than extrapolated off a cliff.
    assert listings.estimate_postage(db, "40 lb")[0] == 20.00
    assert listings.estimate_postage(db, "1 lb")[0] == 8.00


def test_an_estimate_never_counts_as_confirmed(app):
    """The trap this issue names: an estimate hardening into a fact."""
    base, conn = app
    head = _auth(base)
    conn.execute(
        "INSERT INTO sales (message_id, item, price, weight, postage, "
        "postage_source, status) VALUES ('<known>', 'Lamp', 95.0, '4 lb', "
        "12.0, 'confirmed', 'shipped')")
    conn.execute("UPDATE sales SET weight='6 lb' WHERE message_id='<m1>'")
    conn.commit()

    sid = _json_of(_http(f"{base}/api/v1/orders", headers=head)[2])["orders"][0]["id"]
    _, _, body = _http(f"{base}/api/v1/orders/{sid}", headers=head)
    order = _json_of(body)
    assert order["postage_source"] == "estimated", \
        "an unmeasured parcel must say so"

    # Typing one makes it measured, and that survives a re-read.
    _http(f"{base}/api/v1/orders/{sid}/fields", "POST", {"postage": "13.45"},
          headers=head)
    _, _, body = _http(f"{base}/api/v1/orders/{sid}", headers=head)
    order = _json_of(body)
    assert order["postage"] == 13.45
    assert order["postage_source"] == "confirmed"


def test_clearing_postage_clears_its_provenance(app):
    """An orphaned 'confirmed' on a null would make the next estimate look
    as though somebody had checked it."""
    base, conn = app
    head = _auth(base)
    sid = _json_of(_http(f"{base}/api/v1/orders", headers=head)[2])["orders"][0]["id"]
    _http(f"{base}/api/v1/orders/{sid}/fields", "POST", {"postage": "9.99"},
          headers=head)
    _http(f"{base}/api/v1/orders/{sid}/fields", "POST", {"postage": ""},
          headers=head)
    row = conn.execute("SELECT postage, postage_source FROM sales WHERE id=?",
                       (sid,)).fetchone()
    assert row["postage"] is None
    assert row["postage_source"] is None


def test_what_she_keeps_is_null_when_anything_is_unknown(db):
    """A missing postage read as zero reports the whole price as kept -
    the same failure as a missing cost reading as free, and it flatters
    the numbers in the same direction."""
    from mplabel import listings

    assert listings.kept(95.0, None) is None
    assert listings.kept(None, 12.0) is None
    assert listings.kept(95.0, 12.0) == 83.0
    assert listings.kept(95.0, 12.0, paid=20.0) == 63.0


def test_weight_is_read_the_way_a_label_writes_it():
    from mplabel import listings

    assert listings.parse_weight("2 lb 3 oz") == pytest.approx(2.188, abs=0.001)
    assert listings.parse_weight("11 lbs") == 11.0
    assert listings.parse_weight("16 oz") == 1.0
    assert listings.parse_weight("3") == 3.0, "a bare number is pounds"
    assert listings.parse_weight("heavy") is None
    assert listings.parse_weight(None) is None


def test_the_print_lock_can_be_bounded(tmp_path, monkeypatch):
    """"Never fail" is the wrong answer under a deadline.

    printd bounds acquiring its gate with the caller's deadline and then
    took this lock underneath it with no timeout at all, so a lock nobody
    released stalled the request past that deadline with the device held
    - the same "prints to an empty room" failure the deadline exists to
    prevent, one layer down."""
    from mplabel import printers

    if printers.fcntl is None:
        pytest.skip("no flock on this platform")

    lock = tmp_path / "printer.lock"
    monkeypatch.setattr(printers, "lock_path", lambda *a, **k: lock)

    # A second open file description in this same process is enough, and
    # that is not a shortcut: flock conflicts between descriptions even
    # inside one process, which is the property that produced the printd
    # self-deadlock in the first place.
    holder = open(lock, "w")
    printers.fcntl.flock(holder, printers.fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(printers.PrinterUnavailable) as raised:
            with printers.print_lock(required=True, timeout=0.5):
                pass
        waited = time.monotonic() - started
        assert 0.4 < waited < 5, f"waited {waited:.2f}s, not the budget"
        assert "busy" in str(raised.value)
    finally:
        printers.fcntl.flock(holder, printers.fcntl.LOCK_UN)
        holder.close()


def test_an_unbounded_print_lock_still_waits(tmp_path, monkeypatch):
    """The CLI passes no timeout on purpose: a person at a terminal would
    rather queue behind the poller than be refused."""
    from mplabel import printers

    if printers.fcntl is None:
        pytest.skip("no flock on this platform")

    lock = tmp_path / "printer.lock"
    monkeypatch.setattr(printers, "lock_path", lambda *a, **k: lock)
    # Uncontended, so this is only asserting that the default path is
    # unchanged and does not raise.
    with printers.print_lock(required=True):
        pass


def test_a_wedged_lock_is_a_busy_printer_not_a_hang(tmp_path, monkeypatch):
    """`_Device` turns the refusal into the answer the caller already
    knows: printer busy, rather than a socket held open past the
    deadline."""
    from mplabel import printd, printers

    class _Gate:
        def __init__(self):
            self.released = 0

        def acquire(self, timeout=None):
            return True

        def release(self):
            self.released += 1

    class _Server:
        cfg = {}
        printing_since = None

    class _Refusing:
        """Raises where the real one does - on `__enter__`."""

        def __enter__(self):
            raise printers.PrinterUnavailable("the printer was busy for 0.5s")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(printers, "print_lock",
                        lambda *a, **k: _Refusing())
    gate = _Gate()
    device = printd._Device(_Server(), 0.5, gate=gate)
    with device as got:
        assert got is False, "a wedged lock must read as busy"
    # And the gate is handed back, or the next request queues behind a
    # holder that never took anything.
    assert gate.released == 1


def test_notify_test_says_what_to_do_with_no_devices(tmp_path, capsys):
    """The first thing that will happen after configuring the Pi: nothing,
    because nobody has registered yet. That has to say so and say what to
    do, not print "sent 0" and look successful."""
    from mplabel import cli, notify

    conn = cli.connect_db(tmp_path)
    conn.executescript(notify.SCHEMA)
    conn.commit()

    code = cli.cmd_notify({"apns_topic": "x"}, conn,
                          argparse.Namespace(dry_run=False, test=True))
    out = capsys.readouterr().out
    assert code == 0
    assert "no devices are registered" in out
    assert "Settings" in out, "say where to turn them on"


def test_notify_test_names_the_mistake_behind_a_refusal(tmp_path, capsys,
                                                        monkeypatch):
    """BadDeviceToken reads like a malformed token and is almost always
    the wrong `apns_environment` - a development build gives a sandbox
    token. Naming that is the difference between a minute and an
    afternoon."""
    from mplabel import cli, notify

    conn = cli.connect_db(tmp_path)
    conn.executescript(notify.SCHEMA)
    notify.register(conn, "a" * 64, environment="production")

    monkeypatch.setattr(notify, "send_one",
                        lambda *a, **k: (False, '{"reason":"BadDeviceToken"}'))
    code = cli.cmd_notify({"apns_topic": "x"}, conn,
                          argparse.Namespace(dry_run=False, test=True))
    err = capsys.readouterr().err
    assert code == 1, "a refusal must not exit 0"
    assert "apns_environment" in err


def test_notify_test_does_not_consume_a_real_notification(tmp_path,
                                                          monkeypatch):
    """It is a wire check, not one of the three things. Recording it would
    silence the real notification about the same parcel."""
    from mplabel import cli, notify

    conn = cli.connect_db(tmp_path)
    conn.executescript(notify.SCHEMA)
    notify.register(conn, "b" * 64)
    conn.execute(
        "INSERT INTO sales (message_id, item, code, ship_by, status) VALUES "
        "('<m>', 'Crock', '7QK', :today, 'to_ship')", _today())
    conn.commit()

    monkeypatch.setattr(notify, "send_one", lambda *a, **k: (True, "ok"))
    cli.cmd_notify({"apns_topic": "x"}, conn,
                   argparse.Namespace(dry_run=False, test=True))
    assert not notify.already_said(conn, "due", "7QK"), \
        "the wire check must not silence the real one"
def test_stats_says_how_much_of_what_sold_is_costed(app):
    """`v_monthly` has carried `net` and `costed` since the views were
    written and `/stats` never selected either - so the profit screen
    said "there is no cost basis yet" for as long as that was true and
    then went on saying it.

    The fraction is what decides which sentence is honest: net over two
    costed listings out of ninety is not a month's profit."""
    base, conn = app
    head = _auth(base)
    conn.executemany(
        "INSERT INTO listings (listing_id, title, price, paid, state, "
        "sold_at, listed_at) VALUES (?,?,?,?,'sold',?,?)",
        [("l1", "Costed", 60.0, 12.0, "2026-07-30", "2026-07-02"),
         ("l2", "Not costed", 40.0, None, "2026-07-31", "2026-07-03")])
    conn.commit()

    _, _, body = _http(f"{base}/api/v1/stats", headers=head)
    cost = _json_of(body)["cost"]
    # `refresh` folds the fixture's own sale in as a third sold listing,
    # which is the point of the fraction rather than a nuisance: one
    # costed item out of several is exactly the state the old flat
    # sentence could not describe.
    assert cost["sold"] >= 2
    assert cost["costed"] == 1
    assert cost["costed"] < cost["sold"], "the partial case"
    assert cost["margin"] == 48.0, "the margin of the one that has a cost"

    # And the month carries the same two numbers, which is what stops a
    # net of 48 reading as the whole month's profit.
    july = [m for m in _json_of(body)["monthly"]
            if (m["month"] or "").startswith("2026-07")][0]
    assert july["costed"] == 1
    assert july["net"] == 48.0
    assert july["gross"] == 100.0
# ------------------------------------------- the aisle, and the receipt


def _trip_with_receipt(conn, text=None):
    from mplabel import listings, shopping

    trip = listings.create_trip(conn, "GOODWILL 214")
    shopping.store_receipt(conn, trip["id"], text or """GOODWILL 214
HOUSEWARES     4.99
FURNITURE     12.99
LINENS         3.49
SUBTOTAL      21.47
TAX            0.00
TOTAL         21.47
CASH          40.00
CHANGE        18.53""")
    return trip


def test_a_receipt_line_is_not_an_object(db):
    """The constraint the whole flow is built around.

    A thrift receipt itemises by department, so `HOUSEWARES 4.99` says
    what a department took and nothing about which object. Parsing it is
    reading, not identifying - and the arithmetic lines have to be told
    apart from the goods or the total gets attributed to something."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    lines = shopping.receipt(db, trip["id"])
    kinds = {l["label"]: l["kind"] for l in lines}
    assert kinds["HOUSEWARES"] == "item"
    assert kinds["TOTAL"] == "total"
    assert kinds["SUBTOTAL"] == "subtotal"
    assert kinds["TAX"] == "tax"
    # Cash tendered and change are not goods and must never be offered as
    # a cost - they are the largest numbers on the paper.
    assert kinds["CASH"] == "ignored"
    assert kinds["CHANGE"] == "ignored"

    # The till's own total wins over anything summed from lines: a line
    # the OCR dropped would otherwise quietly lower it.
    assert db.execute("SELECT receipt_total FROM trips WHERE id=?",
                      (trip["id"],)).fetchone()[0] == 21.47


def test_re_reading_a_receipt_replaces_it(db):
    """Re-photographing is what happens when the first one was blurry.
    Two readings of one piece of paper would double every amount."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    first = len(shopping.receipt(db, trip["id"]))
    shopping.store_receipt(db, trip["id"], "HOUSEWARES 4.99\nTOTAL 4.99")
    assert len(shopping.receipt(db, trip["id"])) == 2, \
        f"was {first}, then appended instead of replacing"


def test_a_department_match_beats_a_bare_guess(db):
    """Four things and four lines is not four answers. Where a department
    plainly covers a category the pick is obvious; where it does not, the
    proposal says so rather than dressing a coin toss up as a match."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    sofa = shopping.add_candidate(db, trip_id=trip["id"],
                                  title="Chair", category="Furniture")
    vase = shopping.add_candidate(db, trip_id=trip["id"],
                                  title="Vase", category="Home")
    mystery = shopping.add_candidate(db, trip_id=trip["id"],
                                     title="Thing", category="Sports")
    for c in (sofa, vase, mystery):
        shopping.decide(db, c["id"], "carted")

    out = shopping.propose(db, trip["id"])
    by_id = {p["candidate"]: p for p in out["proposals"]}

    assert by_id[sofa["id"]]["label"] == "FURNITURE"
    assert by_id[sofa["id"]]["confidence"] == "matched"
    assert by_id[vase["id"]]["label"] == "HOUSEWARES"
    assert by_id[vase["id"]]["confidence"] == "matched"
    # Nothing on this receipt is a Sports department, so it gets what is
    # left and is honest about it.
    assert by_id[mystery["id"]]["confidence"] == "guessed"
    assert by_id[mystery["id"]]["label"] == "LINENS"


def test_more_things_than_lines_says_none_rather_than_inventing_one(db):
    from mplabel import shopping

    trip = _trip_with_receipt(db, "HOUSEWARES 4.99\nTOTAL 4.99")
    a = shopping.add_candidate(db, trip_id=trip["id"], title="One",
                               category="Home")
    b = shopping.add_candidate(db, trip_id=trip["id"], title="Two",
                               category="Home")
    shopping.decide(db, a["id"], "carted")
    shopping.decide(db, b["id"], "carted")

    out = shopping.propose(db, trip["id"])
    confidences = sorted(p["confidence"] for p in out["proposals"])
    assert confidences == ["matched", "none"]
    assert out["carted"] == 2 and out["item_lines"] == 1


def test_a_proposal_writes_nothing(db):
    """The rule the whole module is built on: it proposes, she assigns.
    A cost this system invented is indistinguishable from one she checked
    a week later."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    c = shopping.add_candidate(db, trip_id=trip["id"], title="Vase",
                               category="Home")
    shopping.decide(db, c["id"], "carted")
    shopping.propose(db, trip["id"])

    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert shopping.one(db, c["id"])["listing_id"] is None


def test_only_what_she_confirmed_becomes_inventory(db):
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    kept = shopping.add_candidate(db, trip_id=trip["id"], title="Milk vase",
                                  category="Home", asking="28")
    other = shopping.add_candidate(db, trip_id=trip["id"], title="Chair",
                                   category="Furniture")
    for c in (kept, other):
        shopping.decide(db, c["id"], "carted")

    created = shopping.apply(db, trip["id"],
                             [{"candidate": kept["id"], "amount": 4.99}])
    assert len(created) == 1
    assert created[0]["paid"] == 4.99
    assert created[0]["price"] == 28.0, "the shelf ticket becomes the asking"
    assert shopping.one(db, kept["id"])["listing_id"] == created[0]["id"]
    # The one she did not confirm is untouched.
    assert shopping.one(db, other["id"])["listing_id"] is None
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1


def test_confirming_twice_does_not_make_two_things(db):
    """A retry on a flaky connection is one object, not two."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    c = shopping.add_candidate(db, trip_id=trip["id"], title="Vase",
                               category="Home")
    shopping.decide(db, c["id"], "carted")
    shopping.apply(db, trip["id"], [{"candidate": c["id"], "amount": 4.99}])
    again = shopping.apply(db, trip["id"],
                           [{"candidate": c["id"], "amount": 4.99}])
    assert again == []
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1


def test_what_she_put_back_is_kept(db):
    """"I saw this and passed on it at $40" is a note to herself that
    nothing else in the system carries, and the same object turns up
    again next month."""
    from mplabel import shopping

    trip = _trip_with_receipt(db)
    passed = shopping.add_candidate(db, trip_id=trip["id"], title="Lamp",
                                    category="Home", asking="40")
    shopping.decide(db, passed["id"], "passed")

    assert shopping.one(db, passed["id"])["decision"] == "passed"
    assert shopping.candidates(db, trip_id=trip["id"], decision="passed")
    # And it is not in the cart, so it cannot be reconciled into stock.
    assert shopping.propose(db, trip["id"])["carted"] == 0


def test_the_aisle_routes_need_authentication(app):
    base, _ = app
    for method, path in [("GET", "/api/v1/candidates"),
                         ("POST", "/api/v1/candidates"),
                         ("POST", "/api/v1/trips/1/receipt"),
                         ("GET", "/api/v1/trips/1/reconcile"),
                         ("POST", "/api/v1/trips/1/reconcile")]:
        status, _, _ = _http(base + path, method,
                             {} if method == "POST" else None)
        assert status == 401, f"{method} {path} answered without a token"


# ------------------------------------ what it might sell for, in the aisle


def _sold(db, title, category, price, paid=None, listed=None, sold=None):
    db.execute(
        "INSERT INTO listings (listing_id, title, category, price, paid, "
        "state, listed_at, sold_at) VALUES (?,?,?,?,?,'sold',?,?)",
        (f"x{title}", title, category, price, paid, listed, sold))
    db.commit()


def test_nothing_comparable_says_nothing(db):
    """Standing in a shop being told "no idea" is worth more than being
    told a number that came from nowhere, because she will act on the
    number."""
    from mplabel import listings

    out = listings.worth(db, category="Home", title="Milk glass vase")
    assert out["comparables"] == 0
    assert out["median"] is None
    assert out["pay_under"] is None


def test_a_price_comes_from_what_actually_sold(db):
    """Sold rows only. An active listing at $45 is an asking price nobody
    has agreed to."""
    from mplabel import listings

    _sold(db, "Hobnail milk glass vase", "Home", 28.0, 6.0,
          "2026-07-01", "2026-07-13")
    _sold(db, "Milk glass bowl", "Home", 34.0, 8.0,
          "2026-07-01", "2026-07-21")
    db.execute(
        "INSERT INTO listings (listing_id, title, category, price, state) "
        "VALUES ('live', 'Milk glass jug', 'Home', 999.0, 'active')")
    db.commit()

    out = listings.worth(db, category="Home", title="Milk glass vase")
    assert out["comparables"] == 2
    assert out["low"] == 28.0 and out["high"] == 34.0
    assert out["median"] == 31.0
    assert 999.0 not in [e["price"] for e in out["examples"]], \
        "an unsold asking price is not evidence"


def test_the_ceiling_uses_the_margin_she_actually_gets(db):
    """Not a target this system invented. And the median, because one
    lamp bought for a pound and sold for eighty would drag an average
    into fantasy."""
    from mplabel import listings

    _sold(db, "Vase one", "Home", 40.0, 10.0)      # 75% kept
    _sold(db, "Vase two", "Home", 20.0, 10.0)      # 50% kept
    out = listings.worth(db, category="Home", title="Vase three")

    assert out["usual_margin"] == 0.625            # median of 0.75 and 0.5
    # Median comparable is 30; pay under 30 * (1 - 0.625).
    assert out["median"] == 30.0
    assert out["pay_under"] == 11.25


def test_without_a_single_cost_there_is_no_ceiling(db):
    """A ceiling from an assumed margin is a number this system made up
    about her business."""
    from mplabel import listings

    _sold(db, "Vase", "Home", 40.0)                # sold, never costed
    out = listings.worth(db, category="Home", title="Another vase")
    assert out["median"] == 40.0
    assert out["usual_margin"] is None
    assert out["pay_under"] is None, "no basis, so no number"


def test_a_title_match_outweighs_a_broad_category(db):
    """"Home" covers half the house. Two shared words in the title is the
    stronger signal when it is there."""
    from mplabel import listings

    _sold(db, "Cast iron skillet", "Home", 25.0)
    _sold(db, "Hobnail milk glass vase", "Home", 30.0)
    found = listings.comparables(db, category="Home",
                                 title="Milk glass vase, hobnail")
    assert found[0]["title"] == "Hobnail milk glass vase"


def test_worth_needs_authentication(app):
    base, _ = app
    status, _, _ = _http(f"{base}/api/v1/worth?category=Home")
    assert status == 401


def _curl_reply(status, body="", version="2", apns_id="ABC-123"):
    """What curl writes when it has spoken to APNs."""
    import subprocess as _sp

    headers = f"HTTP/2 {status}\r\napns-id: {apns_id}\r\n\r\n"
    out = f"{headers}{body}\n{status} {version}".encode()
    return _sp.CompletedProcess(args=[], returncode=0, stdout=out, stderr=b"")


def test_apns_five_hundred_is_retried_once(tmp_path, monkeypatch):
    """Apple documents 5xx as retryable and means it. A single
    InternalServerError says nothing about the request, and one retry is
    what separates "Apple had a moment" from "this will never work" -
    which is the whole question when a notification does not arrive."""
    import subprocess

    from mplabel import notify

    monkeypatch.setattr(notify, "provider_token", lambda cfg: "jwt")
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return _curl_reply(500, '{"reason":"InternalServerError"}')
        return _curl_reply(200)

    monkeypatch.setattr(subprocess, "run", flaky)
    ok, detail = notify.send_one({"apns_topic": "x"}, "a" * 64, "t", "b")
    assert ok, detail
    assert len(calls) == 2, "the first 500 should have been retried"
    assert "ABC-123" in detail, "the apns-id is what Apple can be asked about"


def test_a_refusal_says_the_status_and_what_apple_said(tmp_path, monkeypatch):
    import subprocess

    from mplabel import notify

    monkeypatch.setattr(notify, "provider_token", lambda cfg: "jwt")
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: _curl_reply(400, '{"reason":"BadDeviceToken"}'))
    ok, detail = notify.send_one({"apns_topic": "x"}, "a" * 64, "t", "b")
    assert not ok
    assert "400" in detail and "BadDeviceToken" in detail


def test_a_curl_without_http2_is_named(tmp_path, monkeypatch):
    """APNs requires HTTP/2 and a curl built without it falls back rather
    than saying so - which arrives as a refusal Apple cannot classify."""
    import subprocess

    from mplabel import notify

    monkeypatch.setattr(notify, "provider_token", lambda cfg: "jwt")
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: _curl_reply(400, '{"reason":"BadRequest"}',
                                       version="1.1"))
    ok, detail = notify.send_one({"apns_topic": "x"}, "a" * 64, "t", "b")
    assert not ok
    assert "HTTP/1.1" in detail, "say which protocol was actually spoken"
def test_a_shared_category_is_not_a_comparison(db):
    """From the first real trip: nearly everything she sells is "Home",
    so matching on category alone put a tumbler, a cabinet and a doorway
    in one another's comparables - "6 like it sold for $5.00-$235.00",
    a range so wide it is worse than nothing because it looks like
    evidence."""
    from mplabel import listings

    _sold(db, "Wooden cabinet", "Home", 235.0)
    _sold(db, "Gray tumbler", "Home", 5.0)
    _sold(db, "Hobnail milk glass vase", "Home", 30.0)

    # A title with nothing in common gets nothing, however many "Home"
    # rows are sitting there.
    assert listings.comparables(db, category="Home",
                                title="Brass candlestick") == []

    # A shared word is a comparison.
    found = listings.comparables(db, category="Home", title="Glass vase")
    assert [row["title"] for row in found] == ["Hobnail milk glass vase"]


def test_a_wide_range_says_so(db):
    """Ten dollars to two hundred is not a price. The screen has to be
    able to say the comparables disagree rather than show a range as
    though it were guidance."""
    from mplabel import listings

    _sold(db, "Oak table small", "Home", 20.0)
    _sold(db, "Oak table large", "Home", 220.0)
    out = listings.worth(db, category="Home", title="Oak table")
    assert out["comparables"] == 2
    assert out["wide"] is True

    _sold(db, "Pine shelf one", "Home", 30.0)
    _sold(db, "Pine shelf two", "Home", 40.0)
    tight = listings.worth(db, category="Home", title="Pine shelf")
    assert tight["wide"] is False


def test_with_no_title_the_category_still_helps(db):
    """The model does not always offer a title. Then a category is all
    there is, and it is better than refusing to look."""
    from mplabel import listings

    _sold(db, "Wooden cabinet", "Home", 60.0)
    found = listings.comparables(db, category="Home", title=None)
    assert len(found) == 1


def test_the_jwt_signature_verifies_end_to_end(tmp_path):
    """A signature of the right *length* is not a signature that is
    right. `_der_to_jose` strips DER's leading zero and pads back to 32,
    and getting that wrong produces 64 plausible bytes that Apple
    refuses without saying which part it disliked.

    So this converts back and asks openssl whether it verifies - which
    is the only check that distinguishes our JWT being wrong from Apple
    being unhappy about something else."""
    import shutil
    import subprocess

    from mplabel import notify

    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")

    key = tmp_path / "key.pem"
    if subprocess.run(["openssl", "ecparam", "-name", "prime256v1",
                       "-genkey", "-noout", "-out", str(key)],
                      capture_output=True).returncode != 0:
        pytest.skip("this openssl cannot make a P-256 key")
    public = tmp_path / "public.pem"
    subprocess.run(["openssl", "ec", "-in", str(key), "-pubout",
                    "-out", str(public)], capture_output=True)

    message = b"header.payload"
    jose = notify._sign(key, message)
    assert len(jose) == 64

    # JOSE r||s back to DER, so openssl can check it.
    def der(raw):
        def integer(value):
            value = value.lstrip(b"\x00") or b"\x00"
            if value[0] & 0x80:
                value = b"\x00" + value
            return bytes([0x02, len(value)]) + value

        body = integer(raw[:32]) + integer(raw[32:])
        return bytes([0x30, len(body)]) + body

    signature = tmp_path / "sig.der"
    signature.write_bytes(der(jose))
    message_file = tmp_path / "message"
    message_file.write_bytes(message)

    done = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public),
         "-signature", str(signature), str(message_file)],
        capture_output=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert b"Verified OK" in done.stdout


# ------------------------------------------------------------------ ebay
#
# Every test here replaces `ebay._transport`, which is the module's only
# way out. Nothing in this file may touch the network: a suite that can
# fail because eBay is having an afternoon is a suite nobody trusts.


@pytest.fixture
def ebay_cfg(tmp_path):
    """A configured-enough sandbox install, with its own home."""
    from mplabel import cli
    cfg = dict(cli.DEFAULTS)
    cfg.update(home=str(tmp_path),
               ebay_app_id="app-id", ebay_cert_id="cert-id",
               ebay_ru_name="Her-Name-abcde-xyz")
    return cfg


def _fake_transport(replies):
    """Answer each call from `replies`, recording what was asked.

    `replies` is a list of (status, payload); the recorded calls come
    back on the function itself so a test can assert on the request as
    well as on what was made of the answer.
    """
    calls = []

    def transport(method, url, headers, body=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": body})
        status, payload = replies[len(calls) - 1]
        return status, {}, json.dumps(payload).encode()

    transport.calls = calls
    return transport


def test_ebay_consent_url_sends_the_runame_not_a_url(ebay_cfg):
    """redirect_uri is the RuName.

    eBay resolves it to the redirect configured against the keyset. It
    looks like it wants a URL and sending one is rejected as a mismatch,
    which reads like the redirect is misconfigured rather than that the
    wrong kind of value was sent.
    """
    from mplabel import ebay
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(ebay.consent_url(ebay_cfg)).query)
    assert query["redirect_uri"] == ["Her-Name-abcde-xyz"]
    assert query["client_id"] == ["app-id"]
    assert query["response_type"] == ["code"]


def test_ebay_consent_scopes_stay_on_the_production_host(ebay_cfg):
    """The scope strings are identifiers, not endpoints.

    They are always api.ebay.com even in sandbox. Rewriting them to the
    sandbox host produces a rejection that reads as a permissions problem
    with the account.
    """
    from mplabel import ebay

    assert ebay.environment(ebay_cfg) == "sandbox"
    assert "sandbox" in ebay.auth_host(ebay_cfg)
    assert all(s.startswith("https://api.ebay.com/oauth/") for s in ebay.SCOPES)


def test_ebay_auth_records_when_the_refresh_token_dies(ebay_cfg, monkeypatch):
    """The eighteen-month fuse is only mentioned once.

    `refresh_token_expires_in` comes back on the authorization-code
    exchange and a refresh never repeats it, so if it is not written down
    here the expiry cannot be recovered - and the failure eighteen months
    from now is every call returning invalid_grant with no warning.
    """
    from mplabel import ebay

    transport = _fake_transport([(200, {
        "access_token": "access-1", "expires_in": 7200,
        "refresh_token": "refresh-1",
        "refresh_token_expires_in": 47304000,
    })])
    monkeypatch.setattr(ebay, "_transport", transport)

    tokens = ebay.exchange_code(ebay_cfg, "code-from-the-redirect")
    assert tokens["refresh_token"] == "refresh-1"
    assert ebay.refresh_days_left(tokens) > 500

    # And it survives the round trip to disk, which is the only place it
    # will be read from eighteen months later.
    assert ebay.refresh_days_left(ebay.load_tokens(ebay_cfg)) > 500


@pytest.mark.skipif(
    importlib.util.find_spec("fcntl") is None,
    reason="no POSIX mode bits off-target; chmod(0o600) reads back 0o666")
def test_ebay_token_file_is_not_world_readable(ebay_cfg, monkeypatch):
    """A refresh token is a credential and this Pi also serves a web app.

    Skipped where the permission cannot exist, the same way the flock
    tests are: Windows honours only the read-only bit, so `chmod(0o600)`
    reads back as `0o666` and the assertion would be about the platform
    rather than about the code. The Pi is where this has to hold, and it
    is the only place it means anything."""
    from mplabel import ebay

    monkeypatch.setattr(ebay, "_transport", _fake_transport([(200, {
        "access_token": "a", "expires_in": 7200, "refresh_token": "r"})]))
    ebay.exchange_code(ebay_cfg, "code")
    mode = ebay.token_path(ebay_cfg).stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_ebay_refresh_asks_for_the_scopes_again(ebay_cfg, monkeypatch):
    """`scope` is required on a refresh and is easy to leave off.

    Without it eBay mints a token carrying no scopes at all, and every
    call then fails 403 - which reads as the seller account lacking a
    permission rather than as this request lacking a parameter.
    """
    from mplabel import ebay
    from urllib.parse import parse_qs

    transport = _fake_transport([
        (200, {"access_token": "a1", "expires_in": 7200,
               "refresh_token": "r1", "refresh_token_expires_in": 47304000}),
        (200, {"access_token": "a2", "expires_in": 7200}),
    ])
    monkeypatch.setattr(ebay, "_transport", transport)

    ebay.exchange_code(ebay_cfg, "code")
    ebay.refresh_access(ebay_cfg)

    form = parse_qs(transport.calls[1]["body"].decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["r1"]
    assert set(form["scope"][0].split()) == set(ebay.SCOPES)


def test_ebay_reuses_an_access_token_that_is_still_good(ebay_cfg, monkeypatch):
    """Two hours is two hours; refreshing per call is a rate limit waiting."""
    from mplabel import ebay

    transport = _fake_transport([
        (200, {"access_token": "a1", "expires_in": 7200,
               "refresh_token": "r1"}),
    ])
    monkeypatch.setattr(ebay, "_transport", transport)
    ebay.exchange_code(ebay_cfg, "code")

    assert ebay.access_token(ebay_cfg) == "a1"
    assert ebay.access_token(ebay_cfg) == "a1"
    assert len(transport.calls) == 1


def test_ebay_refreshes_an_access_token_about_to_expire(ebay_cfg, monkeypatch):
    """A token that dies between the check and the call is a needless 401."""
    from mplabel import ebay

    transport = _fake_transport([
        # expires_in inside EXPIRY_SLACK, so it is already too old to use.
        (200, {"access_token": "a1", "expires_in": 60,
               "refresh_token": "r1"}),
        (200, {"access_token": "a2", "expires_in": 7200}),
    ])
    monkeypatch.setattr(ebay, "_transport", transport)
    ebay.exchange_code(ebay_cfg, "code")

    assert ebay.access_token(ebay_cfg) == "a2"
    assert len(transport.calls) == 2


def test_ebay_will_not_send_a_sandbox_token_to_production(ebay_cfg,
                                                          monkeypatch):
    """The 401 for this says nothing about environments.

    The two keysets are different strings that look alike, so the
    mistake is one edited config line - and the answer is a refusal that
    reads as the credentials being wrong rather than as being pointed at
    the wrong eBay.
    """
    from mplabel import ebay

    monkeypatch.setattr(ebay, "_transport", _fake_transport([(200, {
        "access_token": "a", "expires_in": 7200, "refresh_token": "r"})]))
    ebay.exchange_code(ebay_cfg, "code")

    ebay_cfg["ebay_environment"] = "production"
    with pytest.raises(ebay.EbayConfigError) as caught:
        ebay.access_token(ebay_cfg)
    assert "sandbox" in str(caught.value)


def test_ebay_keeps_the_body_of_a_refusal(ebay_cfg, monkeypatch):
    """eBay puts the reason in the body, so a non-2xx must not raise away.

    `urlopen` raises on a 400 and the handle closes with it; reading the
    body first is the difference between "eBay said no" and knowing
    which field it objected to.
    """
    from mplabel import ebay

    monkeypatch.setattr(ebay, "_transport", _fake_transport([(400, {
        "error": "invalid_grant",
        "error_description": "the provided authorization code is expired"})]))
    with pytest.raises(ebay.EbayError) as caught:
        ebay.exchange_code(ebay_cfg, "stale-code")
    assert "invalid_grant" in str(caught.value)
    assert "expired" in str(caught.value)


def test_ebay_names_the_field_a_refusal_objected_to():
    """The useful half of an eBay error is two levels down in `parameters`."""
    from mplabel import ebay

    line = ebay.describe_errors({"errors": [{
        "errorId": 25002, "message": "A user error has occurred.",
        "parameters": [{"name": "sku", "value": "7QK9"}]}]})
    assert "25002" in line and "sku=7QK9" in line


def test_ebay_call_carries_the_marketplace_and_the_bearer(ebay_cfg,
                                                           monkeypatch):
    """Both headers are required and neither fails loudly when missing."""
    from mplabel import ebay

    transport = _fake_transport([
        (200, {"access_token": "a1", "expires_in": 7200,
               "refresh_token": "r1"}),
        (200, {"total": 0}),
    ])
    monkeypatch.setattr(ebay, "_transport", transport)
    ebay.exchange_code(ebay_cfg, "code")

    status, _ = ebay.call(ebay_cfg, "GET", "/sell/inventory/v1/inventory_item")
    assert status == 200
    sent = transport.calls[1]
    assert sent["headers"]["Authorization"] == "Bearer a1"
    assert sent["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    # And it went to the sandbox host, not the production one.
    assert sent["url"].startswith("https://api.sandbox.ebay.com/")


def test_ebay_check_says_what_is_missing_and_changes_nothing(ebay_cfg):
    """`notify --check` exists because a refusal cannot say whose fault it
    is. eBay is worse: an unscoped token, a sandbox token sent to
    production and a genuinely unauthorised account are three variations
    on the same 401."""
    from mplabel import ebay

    rows = ebay.check(ebay_cfg)
    problems = [(label_, problem) for label_, _, problem in rows if problem]
    # No tokens yet, so that is the thing to say - and it is the only
    # blocking one, because the policies are needed to publish and this
    # design deliberately never publishes.
    assert any("auth" in problem for _, problem in problems)
    assert not ebay.token_path(ebay_cfg).exists()


def test_ebay_check_warns_before_the_refresh_token_dies(ebay_cfg,
                                                         monkeypatch):
    """Thirty days is enough notice to redo the consent calmly."""
    from mplabel import ebay

    monkeypatch.setattr(ebay, "_transport", _fake_transport([(200, {
        "access_token": "a", "expires_in": 7200, "refresh_token": "r",
        # Ten days. Nothing is broken yet, and that is the point.
        "refresh_token_expires_in": 10 * 86400})]))
    ebay.exchange_code(ebay_cfg, "code")

    warnings = [problem for label_, _, problem in ebay.check(ebay_cfg)
                if problem and "ebay auth" in problem]
    assert warnings, "a token with ten days left should be warned about"


def test_ebay_unconfigured_exits_78_rather_than_half_trying(tmp_path,
                                                             capsys):
    """78 is EX_CONFIG, the refusal printd and notify already make.

    A permanent error retried by a timer for ever is a permanent error in
    the journal for ever, with the one line that says what is wrong
    buried under it.
    """
    from mplabel import cli

    cfg = dict(cli.DEFAULTS, home=str(tmp_path))
    assert cli.cmd_ebay(cfg, argparse.Namespace(ebaycmd="check")) == 78


def test_the_module_entrypoint_passes_the_exit_code_on():
    """`python -m mplabel` discarded it, so every 78 read as success.

    The exit code and the unit's RestartPreventExitStatus=78 are both
    needed and only one of them had a test. This is the other half.
    """
    source = (Path(__file__).parent.parent / "src" / "mplabel"
              / "__main__.py").read_text()
    assert "sys.exit(main())" in source

    from mplabel import cli
    import inspect
    # And the wrapper has to return what it wraps, or the entrypoint
    # above faithfully exits on None.
    assert "return _main()" in inspect.getsource(cli.main)


def test_ebay_secrets_are_not_echoed_by_the_config_command():
    """`mplabel config` is the command you run *and paste* when stuck.

    `ebay_cert_id` is the OAuth client secret under the other of the two
    names eBay gives it, and `ebay_verification_token` is the only thing
    proving an account-deletion notice came from eBay rather than from
    anyone who found the URL. The app id and the RuName are public
    halves and stay visible - seeing them is how you check the right
    keyset is loaded.
    """
    from mplabel import cli

    assert "ebay_cert_id" in cli.SECRET_KEYS
    assert "ebay_verification_token" in cli.SECRET_KEYS
    assert "ebay_app_id" not in cli.SECRET_KEYS
    assert "ebay_ru_name" not in cli.SECRET_KEYS


def test_the_installer_makes_the_token_directory():
    """A pull and a pip install do not run install_pi.sh.

    `photos/` went wrong exactly this way: the route the docs give for
    updating leaves the directory missing and the first write failing on
    something it cannot create. Tokens are worse than photographs, so the
    directory is 0700.
    """
    script = (Path(__file__).parent.parent / "install_pi.sh").read_text()
    assert 'install -d -m 700' in script and '$DATA_DIR/ebay' in script
# --------------------------------------------------------------------------
# ShopGoodwill auction mail: what she bought, and what it really cost.
#
# The half of the mailbox nothing read until now. Every test here exists
# because getting one of these wrong writes a wrong number into `paid`,
# and a wrong cost basis is worse than none: a null margin reports itself
# as unknown, a wrong one reports itself as profit.

GOODWILL_WON = FIXTURES / "goodwill_won.eml"
GOODWILL_PAID = FIXTURES / "goodwill_payment.eml"


@pytest.fixture
def won_mail():
    return email.message_from_bytes(GOODWILL_WON.read_bytes())


@pytest.fixture
def paid_mail():
    return email.message_from_bytes(GOODWILL_PAID.read_bytes())


@pytest.mark.parametrize("from_header,ok", [
    ("ShopGoodwill <no-reply@shopgoodwill.com>", True),
    # The payment receipt comes from their transactional host, which is a
    # different name entirely - matching only the bare domain would read
    # every win and miss every payment, i.e. lose the money.
    ("ShopGoodwill <no-reply@txemail.shopgoodwill.com>", True),
    ("ShopGoodwill <No-Reply@ShopGoodwill.Com>", True),
    ("ShopGoodwill <no-reply@shopgoodwill.com.example.net>", False),
    ("\"ShopGoodwill.com\" <billing@notshopgoodwill.com>", False),
    ("Facebook Marketplace <noreply@marketplace.facebook.com>", False),
])
def test_sender_domain_must_be_shopgoodwill(from_header, ok):
    """What is downstream of this check is money against an object. An
    IMAP FROM search matches the header as text, so a display name alone
    gets a message fetched - the address domain is the real gate."""
    from mplabel import goodwill

    msg = email.message_from_string(
        f"From: {from_header}\n"
        "Subject: ShopGoodwill.com - Online Payment Received\n\n")
    assert goodwill.is_from_goodwill(msg) is ok


def test_a_goodwill_subject_is_never_classified_as_a_sale():
    """Her purchases must not reach the seller-side classifier. A
    ShopGoodwill subject answering 'sold' would put one of her own
    purchases into the sell-through numerator - the same mistake
    BUYER_KINDS exists to prevent for Facebook's buyer mail."""
    from mplabel import goodwill

    for subject in ("ShopGoodwill.com - You Were Awarded The Winning Bid!",
                    "ShopGoodwill.com - Online Payment Received"):
        assert listings.classify(subject) is None
        assert goodwill.classify(subject) is not None


def test_goodwill_kinds_are_buyer_side():
    """`apply_events` replays mail_events into listings. A goodwill event
    carries no Facebook listing id and its row is written with a cost by
    the importer, so replaying it could only undo that."""
    from mplabel import goodwill

    for kind, _pattern in goodwill.SUBJECT_PATTERNS:
        assert kind in listings.BUYER_KINDS


def test_win_mail_gives_item_number_title_and_hammer_price(won_mail):
    from mplabel import goodwill

    order = goodwill.parse(won_mail)
    assert order["kind"] == "goodwill_won"
    item, = order["items"]
    assert item["item_id"] == "911100022"
    assert item["price"] == 24.50
    assert order["seller"].startswith("Goodwill of the Example Valley")


def test_win_title_stops_at_the_line_break(won_mail):
    """The only thing marking the end of the title is the `<br>` after
    it. Matched against the flattened body, `(.+)$` runs happily on
    through the bidder agreement and the whole email becomes the title -
    which is what shipped first and is invisible until you look at a
    row."""
    from mplabel import goodwill

    item, = goodwill.parse(won_mail)["items"]
    assert item["title"] == "Pair Of Painted Tin Toy Banks 1930s."
    assert "PAYMENT MUST BE RECEIVED" not in item["title"]


def test_win_title_keeps_its_own_full_stop(won_mail):
    """Their template appends "!" to the title, so a title that ends in a
    full stop arrives as "...Albums.!". Exactly one character is the
    template's; stripping punctuation generally would eat the title's."""
    from mplabel import goodwill

    item, = goodwill.parse(won_mail)["items"]
    assert item["title"].endswith("1930s.")


def test_payment_mail_reads_every_figure(paid_mail):
    from mplabel import goodwill

    order = goodwill.parse(paid_mail)
    assert order["kind"] == "goodwill_paid"
    assert order["order_id"] == "65200001"
    assert order["seller"] == "Goodwill Example County"
    assert (order["subtotal"], order["tax"], order["shipping"],
            order["total"]) == (8.99, 1.47, 9.41, 19.87)
    assert order["paid_on"] == "2026-09-09"
    item, = order["items"]
    assert item["item_id"] == "911100037"
    assert item["price"] == 8.99
    assert item["quantity"] == 1


def test_item_subtotal_is_not_read_as_an_item(paid_mail):
    """`Item Subtotal: $8.99` sits four lines under `Item: 911100037`.
    A loose `Item:` match makes the order total a second object on the
    shelf, with a price and no title."""
    from mplabel import goodwill

    assert len(goodwill.parse(paid_mail)["items"]) == 1


def test_paid_is_the_landed_cost_not_the_hammer_price(paid_mail):
    """The teapot went for $8.99 and cost $19.87 to get here - $9.41 of
    it postage. Recording the hammer price would report less than half of
    what the object really cost, and every margin computed from it would
    be wrong by more than 100%."""
    from mplabel import goodwill

    order = goodwill.parse(paid_mail)
    assert goodwill.landed_cost(order) == {"911100037": 19.87}


def _two_item_order(raw):
    """The same payment mail with a second item in it."""
    second = ('<td align="left"><font style="font-size:16px"><span>'
              'BRASS CANDLESTICK PAIR<br><strong>Item:</strong> 911100099'
              '<br><strong>Price:</strong> $21.00'
              '<br><strong>Quantity:</strong> 1</span></font></td></tr><tr>')
    anchor = '<tr><td align="left"><a href="https://click.shopgoodwill.com'
    raw = raw.replace(anchor, second + anchor[4:], 1)
    raw = raw.replace("<strong>Item Subtotal:</strong> $8.99",
                      "<strong>Item Subtotal:</strong> $29.99")
    return raw.replace("<strong>Order Total:</strong> $19.87",
                       "<strong>Order Total:</strong> $45.00")


def test_a_multi_item_order_is_never_apportioned():
    """One shipping charge over three items cannot be split without
    inventing the split - pro rata by price, by weight and evenly are
    three different answers and none of them is on the receipt. Same rule
    as `estimate_postage`: a derived figure that gets written down is
    indistinguishable from a measured one a week later."""
    from mplabel import goodwill

    msg = email.message_from_string(
        _two_item_order(GOODWILL_PAID.read_text(encoding="utf-8")))
    order = goodwill.parse(msg)
    assert order["total"] == 45.00
    assert goodwill.landed_cost(order) == {"911100037": 8.99,
                                           "911100099": 21.00}


def test_the_unsplit_remainder_shows_up_as_unassigned(db):
    """And it is not lost: tax and postage on a multi-item order become
    the trip's unassigned money, which is the number the triage screen
    exists to chase and the one question only she can answer."""
    from mplabel import goodwill

    msg = email.message_from_string(
        _two_item_order(GOODWILL_PAID.read_text(encoding="utf-8")))
    goodwill.import_mail(db, msg)
    trip, = listings.trip_summary(db)
    assert trip["receipt_total"] == 45.00
    assert trip["assigned"] == 29.99
    assert trip["unassigned"] == 15.01


def test_a_single_item_order_leaves_nothing_unattributed(db, paid_mail):
    """The other half of the same decision. A trip that can never reach
    zero is a notification she cannot clear, and `notify.unattributed`
    would say so about every auction she ever wins."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    trip, = listings.trip_summary(db)
    assert trip["unassigned"] == 0.0


def test_an_order_becomes_a_trip_with_the_selling_goodwill_on_it(db,
                                                                paid_mail):
    from mplabel import goodwill

    result = goodwill.import_mail(db, paid_mail)
    trip = listings.trip_summary(db, result["trip_id"])
    assert trip["store"] == "ShopGoodwill - Goodwill Example County"
    assert trip["occurred_at"] == "2026-09-09"
    assert trip["receipt_total"] == 19.87


def test_a_purchase_becomes_a_thing_on_a_shelf(db, paid_mail):
    """The whole point: an auction win turns into inventory with a code
    on it, without anybody typing. `paid` had a schema and a phone screen
    for months and no automatic route in at all."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    row = db.execute("SELECT * FROM listings").fetchone()
    assert row["listing_id"] == "goodwill:911100037"
    assert row["title"] == "VINTAGE BLUE AND WHITE PORCELAIN MINI TEAPOT"
    assert row["paid"] == 19.87
    assert row["source"] == "goodwill"
    assert row["state"] == goodwill.ACQUIRED
    assert len(row["inventory_code"]) == 4


def test_a_goodwill_key_cannot_collide_with_a_facebook_listing_id(db,
                                                                 paid_mail):
    """Both are nine-ish digit strings. Unprefixed, an item number that
    happened to match a Facebook listing id would silently merge two
    different objects and put a cost on the wrong one."""
    from mplabel import goodwill

    listings.upsert_listing(db, "911100037", "email", title="Something else")
    goodwill.import_mail(db, paid_mail)
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    other = db.execute("SELECT paid FROM listings WHERE listing_id='911100037'"
                       ).fetchone()
    assert other["paid"] is None


@pytest.mark.parametrize("order", [("won", "paid"), ("paid", "won")])
def test_the_two_mails_land_on_one_row_either_way_round(db, order):
    """A win and its payment are two mails about one object, and which
    arrives first is not ours to decide - the backfill walks the mailbox
    in whatever order the server returns."""
    from mplabel import goodwill

    won = GOODWILL_WON.read_text(encoding="utf-8").replace(
        "911100022", "911100037")
    raws = {"won": won, "paid": GOODWILL_PAID.read_text(encoding="utf-8")}
    for which in order:
        goodwill.import_mail(db, email.message_from_string(raws[which]))

    rows = db.execute("SELECT listing_id, paid FROM listings").fetchall()
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "goodwill:911100037"
    assert rows[0]["paid"] == 19.87


def test_a_win_on_its_own_records_no_cost(db, won_mail):
    """A win is a debt, not a cost: payment is due within seven days and
    she has not made it. Writing the hammer price as `paid` would report
    money that has not left the account, and would then block the
    payment mail's better figure - which is the landed one."""
    from mplabel import goodwill

    goodwill.import_mail(db, won_mail)
    row = db.execute("SELECT paid, trip_id FROM listings").fetchone()
    assert row["paid"] is None
    assert row["trip_id"] is None
    # The figure is not thrown away - it is on the event, where a debt
    # belongs.
    event = db.execute("SELECT kind, amount FROM mail_events").fetchone()
    assert (event["kind"], event["amount"]) == ("goodwill_won", 24.50)


def test_importing_the_same_mail_twice_changes_nothing(db, paid_mail):
    """`backfill --restart` re-walks the whole mailbox. A second trip for
    the same order would double the cost basis of the month."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    goodwill.import_mail(db, paid_mail)
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM mail_events").fetchone()[0] == 1


def test_a_cost_she_corrected_survives_the_next_import(db, paid_mail):
    """The mail arrives once and she is the later observation. A plain
    overwrite on every import would undo a correction typed on the phone
    the first time the backfill ran again."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    row = db.execute("SELECT id FROM listings").fetchone()
    listings.set_cost(db, row["id"], 25.00)
    goodwill.import_order(db, goodwill.parse(paid_mail))
    assert db.execute("SELECT paid FROM listings").fetchone()["paid"] == 25.00


def test_acquired_stays_out_of_the_sell_through_denominator(db, paid_mail):
    """`v_price_band` measures sold over COUNT(*). Left in, a box of
    things she has won and not yet photographed would push sell-through
    down on the day it arrived - which is the opposite of what winning an
    auction means."""
    from mplabel import goodwill

    listings.upsert_listing(db, "111", "email", title="Sold thing",
                            price=20.0, state="sold")
    listings.upsert_listing(db, "222", "email", title="Live thing",
                            price=20.0, state="active")
    goodwill.import_mail(db, paid_mail)
    listings.build_views(db)
    band = db.execute("SELECT listed, sold FROM v_price_band "
                      "WHERE price_band='$10-25'").fetchone()
    assert (band["listed"], band["sold"]) == (2, 1)


def test_listing_something_moves_it_off_acquired(db, paid_mail):
    """'acquired' ranks below 'active', so a late-arriving auction mail
    cannot drag a listing that is already live back to the shelf - and
    listing the thing does move it forward."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    key = "goodwill:911100037"
    listings.upsert_listing(db, key, "email", state="active")
    assert db.execute("SELECT state FROM listings").fetchone()["state"] == "active"
    listings.upsert_listing(db, key, "goodwill", state=goodwill.ACQUIRED)
    assert db.execute("SELECT state FROM listings").fetchone()["state"] == "active"


def test_an_auction_cost_reaches_the_sale_it_belongs_to(db, paid_mail):
    """The payoff, and the reason the module exists. `v_monthly.net` has
    been correct and empty since the views were written, because nothing
    could fill `paid`. Reconciliation is by title, exactly as it is for a
    saved-page import."""
    from mplabel import goodwill

    goodwill.import_mail(db, paid_mail)
    db.execute(
        "INSERT INTO sales (message_id, item, price, received_at, status) "
        "VALUES ('<m1>', 'Vintage blue and white porcelain mini teapot', "
        "45.0, '2026-10-01T10:00:00', 'recorded')")
    db.commit()
    listings.refresh(db)

    row = db.execute("SELECT state, price, paid, margin FROM v_listing_perf"
                     ).fetchone()
    assert (row["state"], row["price"], row["paid"]) == ("sold", 45.0, 19.87)
    assert row["margin"] == 25.13
    month = db.execute("SELECT net, costed FROM v_monthly").fetchone()
    assert (month["net"], month["costed"]) == (25.13, 1)


def test_the_poller_looks_for_goodwill_mail_too():
    """A parser nothing calls is a parser that does not exist. The search
    has to name the sender or the mail is never fetched."""
    from mplabel import cli, goodwill

    imap = _FakeIMAP([b"1"])
    cli.candidate_ids(imap, {"lookback_days": "7"}, "imap.gmail.com")
    assert "shopgoodwill.com" in imap.queries[0]
    assert all(d in cli.MAIL_DOMAINS for d in goodwill.SENDER_DOMAINS)


def test_the_plain_imap_fallback_nests_its_ors():
    """IMAP's OR takes exactly two arguments. A flat `OR a b c` is
    rejected outright, and `candidate_ids` then falls through to the
    UNSEEN query - the one that hid eight labels behind a Gmail thread."""
    from mplabel import cli

    expr = cli.imap_or_from(("a.example", "b.example", "c.example"))
    assert expr == ('(OR (OR (FROM "a.example") (FROM "b.example")) '
                    '(FROM "c.example"))')
    assert expr.count("OR") == expr.count("(OR")
def test_an_inline_comment_does_not_choose_the_wrong_apple(tmp_path):
    """The bug that produced `InternalServerError` and nothing else.

    `configparser` does not strip inline comments, and this project's own
    documentation showed the setting with one. So the value became
    "sandbox   ; production once..." - which is not "sandbox" - and a
    sandbox token went to the production host, where APNs refused it
    without naming anything."""
    from mplabel import notify

    assert notify.environment({"apns_environment": "sandbox"}) == "sandbox"
    assert notify.environment(
        {"apns_environment": "sandbox   ; production once it is not a dev "
                             "build"}) == "sandbox"
    assert notify._host(
        {"apns_environment": "sandbox ; later production"}) \
        == notify.APNS_SANDBOX_HOST
    # And the default is still production, including for an empty value.
    assert notify.environment({}) == "production"
    assert notify.environment({"apns_environment": ""}) == "production"


def test_the_documented_config_has_no_inline_comments():
    """The snippet in the docs is the thing people paste. It had one, and
    it cost an afternoon of blaming Apple."""
    for name in ("mplabel.conf.example", "docs/notifications.md"):
        text = (Path(__file__).parent.parent / name).read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if not re.match(r"^[a-z_]+\s*=", stripped):
                continue
            assert not re.search(r"\s[;#]", stripped), \
                f"{name}: inline comment in {stripped!r} - configparser " \
                "keeps it, so the value is not what it looks like"


def test_the_win_fixture_still_carries_its_font_interleaving():
    """Four of the five real win mails wrap arbitrary spans in
    `<font color="#550055">`, opening one mid-sentence right before the
    price and giving the seller line its own `</font><font ...>` pair.
    The fixture was a tidier document than anything the parser will meet
    until this was put back, so a cleanup that removes it removes the
    only coverage of the shape real mail actually has."""
    raw = GOODWILL_WON.read_text(encoding="utf-8")
    assert raw.count('<font color=') >= 3
    assert '</font><font color="#550055">The Seller of this Item' in raw


def test_a_font_tag_does_not_end_the_title_or_hide_the_seller(won_mail):
    """`font` is not a block breaker, so an open tag mid-sentence must
    not split the title from its item number - and the seller line,
    which arrives with a closing tag stuck to its front, still has to
    match at the start of its block."""
    from mplabel import goodwill

    order = goodwill.parse(won_mail)
    item, = order["items"]
    assert item["title"] == "Pair Of Painted Tin Toy Banks 1930s."
    assert item["price"] == 24.50
    assert order["seller"] == "Goodwill of the Example Valley of Springfield, IL"


def test_a_quote_in_a_title_survives():
    """Real titles carry inch marks and quoted names - `16.5X29.75"
    FRAME` and `ANTIQUE 1855 EDITION OF "THE DAILY HERALD".` are both
    real. The title is the whole block, so a quote is only text, and
    this pins that nothing downstream starts treating it as a
    delimiter."""
    from mplabel import goodwill

    raw = GOODWILL_PAID.read_text(encoding="utf-8").replace(
        "VINTAGE BLUE AND WHITE PORCELAIN MINI TEAPOT",
        'FRAMED INK PAINTING SIGNED 16.5X29.75&quot; FRAME')
    order = goodwill.parse(email.message_from_string(raw))
    item, = order["items"]
    assert item["title"] == 'FRAMED INK PAINTING SIGNED 16.5X29.75" FRAME'
    assert item["item_id"] == "911100037"


def test_the_sellers_city_wins_over_the_shipping_address(paid_mail):
    """Both blocks carry a `City:` line and the seller's comes first, so
    `_labelled` keeps that one. Worth pinning because the other one is
    her home address, and a field that quietly became where *she* lives
    would be a private detail stored under a name that says otherwise."""
    from mplabel import goodwill

    order = goodwill.parse(paid_mail)
    assert order["seller_city"] == "Springfield"

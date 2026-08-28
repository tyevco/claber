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

"""Generate synthetic USPS-style label PDFs matching the real geometry.

Deliberately synthetic: the real labels carry a buyer's home address, and
that does not belong in a git repo. These reproduce the *shape* that
matters for the tests, with invented names and tracking numbers that are
not in use.

Two shapes, because there are two channels and they are not the same
page:

`build()` is the Marketplace label - 432x288pt of content at
(90,450)-(522,738), text rotated 90 degrees counter-clockwise. The ink is
exactly the nominal 6x4in, so `label._snap` has nothing to do on it.

`build_ebay()` is an eBay one, measured off a real label. It differs in
both of the ways that turned out to matter. Its text is rotated the
**other** way, so `to_4x6` answers 270 where Facebook's answers 90 - and
a pipeline that only ever met one of them cannot tell a convention from a
constant. And its ink is 408x273 rather than 432x288, sitting at
(96,454): it does *not* fill the label, so the crop window `_snap`
produces extends past the ink on all four sides, which on a Marketplace
label it never did.
"""
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128

X0, Y0, W, H = 90, 450, 432, 288   # 6x4in landscape, becomes 4x6 once rotated


def build(path="label_sample.pdf"):
    c = canvas.Canvas(path, pagesize=letter)
    c.saveState()
    # Rotate so text runs bottom-to-top, exactly like the real labels.
    c.translate(X0, Y0)
    c.rotate(90)
    # Now drawing in a 288-wide x 432-tall space (the label, upright).
    c.setLineWidth(1.5)
    c.rect(0, -W, H, W)

    def line(y):
        c.line(0, -y, H, -y)

    for y in (36, 96, 130, 250, 330):
        line(y)

    c.setFont("Helvetica-Bold", 15)
    c.drawString(120, -26, "USPS APIs")
    c.setFont("Helvetica-Bold", 34)
    c.drawString(14, -84, "G")
    c.setFont("Helvetica", 7)
    c.drawString(60, -52, "usps.com")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(60, -66, "US POSTAGE")
    c.setFont("Helvetica", 7)
    c.drawString(60, -88, "08/28/2026")
    c.drawString(60, -96, "1 lb 15 oz")
    c.drawString(168, -88, "Mailed from 00000")
    c.setFont("Helvetica-Bold", 8)
    c.drawString(168, -66, "U.S. POSTAGE PAID")

    c.setFont("Helvetica-Bold", 13)
    c.drawString(28, -120, "USPS GROUND ADVANTAGE\u2122")

    c.setFont("Helvetica", 8)
    c.drawString(200, -142, "Created 08/28/2026")
    c.setFont("Helvetica", 13)
    c.drawString(210, -160, "RDC 01")

    c.setFont("Helvetica", 8)
    for i, t in enumerate(["JANE TESTER", "1 EXAMPLE WAY",
                           "SPRINGFIELD IL 62701-0001"]):
        c.drawString(14, -145 - i * 10, t)

    c.setFont("Helvetica", 11)
    for i, t in enumerate(["SAM SAMPLE", "2 FICTION RD",
                           "SHELBYVILLE IN 46176-0002"]):
        c.drawString(52, -228 - i * 13, t)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(46, -268, "USPS TRACKING # USPS Ship")
    bc = code128.Code128("9400100000000000000000", barHeight=44, barWidth=0.92)
    bc.drawOn(c, 16, -322)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(48, -324, "9400 1000 0000 0000 0000 00")

    c.restoreState()
    c.showPage()
    c.save()
    return path


# Measured off a real eBay label: ink at (96.2,453.8)-(504.0,727.0), i.e.
# 408x273pt, and every character drawn with the matrix (0,-1,1,0).
EX0, EY0, EW, EH = 96, 454, 408, 273


def build_ebay(path="ebay_label_sample.pdf"):
    """An eBay shipping label: the same parcel, the other way up."""
    c = canvas.Canvas(path, pagesize=letter)
    c.saveState()
    # The opposite quarter turn from build(). eBay draws its text
    # top-to-bottom where Facebook draws it bottom-to-top, and the whole
    # point of this fixture is that `to_4x6` reads that off the page
    # rather than assuming the one convention it had ever seen.
    c.translate(EX0, EY0 + EH)
    c.rotate(-90)
    # Drawing now in an EH-wide x EW-tall space, mirrored relative to
    # build()'s frame - so the label reads upright once rotated 270.
    c.setLineWidth(1.2)
    c.rect(0, 0, EH, EW)

    for y in (60, 118, 300, 372):
        c.line(0, y, EH, y)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(96, EW - 28, "US POSTAGE")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(96, EW - 38, "PAID IMI")
    c.setFont("Helvetica", 7)
    c.drawString(96, EW - 52, "09/12/2026")
    c.drawString(96, EW - 62, "From 00000")
    c.drawString(96, EW - 72, "2 lbs 0 ozs")
    c.setFont("Helvetica-Bold", 34)
    c.drawString(18, EW - 60, "G")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(178, EW - 52, "Pitney Bowes")
    c.drawString(178, EW - 62, "CommPrice")
    c.setFont("Helvetica", 7)
    c.drawString(178, EW - 74, "NO SURCHARGE")

    c.setFont("Helvetica-Bold", 14)
    c.drawString(28, EW - 96, "USPS GROUND ADVANTAGE\u2122")

    # Sender first, recipient second - the order the parser relies on.
    c.setFont("Helvetica", 8)
    for i, t in enumerate(["Jane Tester", "1 Example Way",
                           "Springfield IL 62701-0001"]):
        c.drawString(14, EW - 128 - i * 10, t)

    c.setFont("Helvetica", 10)
    for i, t in enumerate(["SAM SAMPLE", "2 FICTION RD",
                           "SHELBYVILLE IN 46176-0002"]):
        c.drawString(56, EW - 210 - i * 12, t)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(84, EW - 292, "USPS TRACKING #")
    bc = code128.Code128("9434000000000000000000", barHeight=42, barWidth=0.86)
    bc.drawOn(c, 16, EW - 352)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(34, EW - 364, "9434 0000 0000 0000 0000 00")

    c.restoreState()
    c.showPage()
    c.save()
    return path


if __name__ == "__main__":
    print("wrote", build())
    print("wrote", build_ebay())

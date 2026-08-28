"""Generate a synthetic USPS-style label PDF matching the real geometry.

Deliberately synthetic: the real labels carry a buyer's home address, and
that does not belong in a git repo. This reproduces the *shape* that
matters for the tests - US Letter page, 432x288pt of content at
(90,450)-(522,738), text rotated 90 degrees counter-clockwise - with
invented names and a tracking number that is not in use.
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


if __name__ == "__main__":
    print("wrote", build())

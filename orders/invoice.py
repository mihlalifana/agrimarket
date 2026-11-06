"""PDF invoice per farmer sub-order (FR-09, NFR-06)."""
from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from marketplace.models import fmt_qty


def _r(v):
    return f"R {v:,.2f}".replace(",", " ")


def render_invoice(sub):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=sub.invoice_number, author="AgriMarket")
    st = getSampleStyleSheet()
    green = colors.HexColor("#2f5d3a")
    farm = sub.farmer.farm
    order = sub.order
    is_tax = bool(sub.farmer_vat_number)
    story = [
        Paragraph(f'<font color="#2f5d3a"><b>{"TAX INVOICE" if is_tax else "INVOICE"}</b></font>', st["Title"]),
        Spacer(1, 4),
    ]
    seller = (f"<b>{farm.farm_name}</b><br/>{sub.farmer.get_full_name()}<br/>{farm.town}, "
              f"{farm.get_province_display()}<br/>{sub.farmer.phone}"
              + (f"<br/>VAT no: {sub.farmer_vat_number}" if is_tax else "<br/>Not VAT registered"))
    buyer = order.buyer
    to = (f"<b>{buyer.get_full_name()}</b><br/>{buyer.email}<br/>{buyer.phone}<br/>"
          + (order.delivery_address.replace("\n", "<br/>") if order.delivery_address else "Farm collection"))
    meta = (f"<b>Invoice no:</b> {sub.invoice_number}<br/><b>Date:</b> "
            f"{timezone.localtime(sub.approved_at or sub.created_at):%d %B %Y}<br/>"
            f"<b>Order:</b> {order.number} ({order.order_date:%d %b %Y})<br/><b>Payment:</b> {order.get_payment_method_display()}"
            f"<br/>Sold via AgriMarket")
    head = Table([[Paragraph("From", st["Heading4"]), Paragraph("Bill to", st["Heading4"]), ""],
                  [Paragraph(seller, st["Normal"]), Paragraph(to, st["Normal"]), Paragraph(meta, st["Normal"])]],
                 colWidths=[58 * mm, 58 * mm, 58 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 10)]

    rows = [["Item", "Batch", "Qty", "Unit price", "Amount"]]
    for it in sub.items.all():
        rows.append([Paragraph(it.product_name + (f"<br/><font size=8>Grade {it.grade}</font>" if it.grade else ""),
                               st["Normal"]), it.batch_number,
                     f"{fmt_qty(it.quantity)} {it.unit}", _r(it.unit_price), _r(it.line_total)])
    rows.append(["", "", "", "Subtotal (excl. VAT)", _r(sub.goods_subtotal)])
    vat_label = f"VAT @ {sub.items.first().vat_rate * 100:.0f}%" if is_tax else "VAT"
    rows.append(["", "", "", vat_label, _r(sub.vat_amount)])
    rows.append(["", "", "", "Delivery", _r(sub.delivery_fee)])
    rows.append(["", "", "", "TOTAL", _r(sub.total)])
    t = Table(rows, colWidths=[62 * mm, 30 * mm, 22 * mm, 34 * mm, 26 * mm], repeatRows=1)
    n = len(rows)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), green),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, n - 5), 0.4, colors.HexColor("#cccccc")),
        ("FONTNAME", (3, n - 1), (-1, n - 1), "Helvetica-Bold"),
        ("LINEABOVE", (3, n - 1), (-1, n - 1), 1, green),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [t, Spacer(1, 14),
              Paragraph("Amounts in South African rand (ZAR). This invoice is issued by the farmer "
                        "named above through the AgriMarket platform.", st["Italic"])]
    doc.build(story)
    return buf.getvalue()

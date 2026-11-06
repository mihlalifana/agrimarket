import csv
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import StyledMixin
from accounts.models import Province
from core.permissions import admin_required, farmer_required
from marketplace.models import fmt_qty

from . import services


class RangeForm(StyledMixin, forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    group = forms.ChoiceField(choices=[("day", "Daily"), ("month", "Monthly")], required=False)

    def clean(self):
        d = super().clean()
        if d.get("start") and d.get("end") and d["start"] > d["end"]:
            raise forms.ValidationError("Start date must be on or before the end date.")
        return d


def _range(request):
    today = timezone.localdate()
    form = RangeForm(request.GET or None, initial={"start": today.replace(day=1), "end": today, "group": "day"})
    if form.is_valid():
        d = form.cleaned_data
        return form, d["start"], d["end"], d.get("group") or "day"
    return RangeForm(initial={"start": today.replace(day=1), "end": today, "group": "day"}), \
        today.replace(day=1), today, "day"


def _csv(filename, header, rows):
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    w.writerows(rows)
    return resp


@farmer_required
def reports(request):
    """The farm's own sales, best sellers and wastage (FR-15)."""
    form, start, end, group = _range(request)
    farmer = request.user
    report = request.GET.get("report", "sales")
    if report not in ("sales", "best", "wastage"):
        report = "sales"
    ctx = {"form": form, "start": start, "end": end, "report": report, "farmer": farmer}
    export = request.GET.get("export") == "csv"
    tag = f"{start:%Y%m%d}-{end:%Y%m%d}"

    if report == "sales":
        rows, totals = services.sales_report(start, end, group, farmer)
        fmt = "%Y-%m-%d" if group == "day" else "%Y-%m"
        if export:
            return _csv(f"sales-{tag}.csv", ["Period", "Orders", "Goods (R)", "VAT (R)", "Delivery (R)", "Total (R)"],
                        [[timezone.localtime(r["period"]).strftime(fmt), r["orders"], r["goods"], r["vat"],
                          r["delivery"], r["total"]] for r in rows]
                        + [["TOTAL", totals["orders"], totals["goods"], totals["vat"], totals["delivery"],
                            totals["total"]]])
        ctx.update(rows=rows, totals=totals, group=group)
    elif report == "best":
        by = request.GET.get("by", "revenue")
        rows = services.best_sellers(start, end, by=by, farmer=farmer)
        if export:
            return _csv(f"best-sellers-{tag}.csv", ["Rank", "Product", "Quantity", "Unit", "Revenue (R)", "Orders"],
                        [[i + 1, r["product_name"], fmt_qty(r["quantity"]), r["unit"], r["revenue"], r["orders"]]
                         for i, r in enumerate(rows)])
        ctx.update(rows=rows, by=by)
    elif report == "wastage":
        rows, total, by_reason = services.wastage_report(start, end, farmer)
        if export:
            return _csv(f"wastage-{tag}.csv", ["Date", "Farm", "Batch", "Product", "Quantity", "Unit", "Reason",
                                               "Value lost (R)"],
                        [[timezone.localtime(a.created_at).strftime("%Y-%m-%d"), a.listing.farmer.farm.farm_name,
                          a.listing.batch_number, a.listing.name, fmt_qty(-a.quantity_change), a.listing.unit,
                          a.get_reason_display(), a.value_lost] for a in rows])
        ctx.update(rows=rows, total=total, by_reason=by_reason)
    return render(request, "finance/reports.html", ctx)


# ---------------------------------------------------------------- money
@farmer_required
def earnings(request):
    """What the farm earned each month: card money Paystack paid into its bank, AgriMarket's commission,
    and refunds. Nothing to settle by hand."""
    from orders.services import farm_debt
    return render(request, "finance/earnings.html", {"months": services.farm_earnings(request.user),
                                                     "debt": farm_debt(request.user)})


@admin_required
def income(request):
    """AgriMarket's own income per month (totals only) and refunds Paystack could not send yet."""
    from orders.models import Refund
    return render(request, "finance/income.html", {
        "months": services.platform_income(),
        "stuck": Refund.objects.filter(status=Refund.Status.PENDING).exclude(problem="")
        .select_related("suborder__order", "suborder__farmer__farm")})


@admin_required
@require_POST
def retry_refund(request, pk):
    from orders.services import send_refund
    if send_refund(pk):
        messages.success(request, "Refund sent.")
    else:
        messages.error(request, "Paystack still refused the refund. See the reason in the list.")
    return redirect("finance:income")

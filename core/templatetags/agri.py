from decimal import Decimal

from django import template

from marketplace.models import fmt_qty

register = template.Library()


@register.filter
def rand(value):
    """1234.5 -> 'R 1 234.50' (SA style)."""
    if value in (None, ""):
        return "R 0.00"
    return "R " + f"{Decimal(value):,.2f}".replace(",", " ")


@register.filter
def absval(value):
    """Size of an amount without its sign (e.g. what a farm owes)."""
    return abs(Decimal(value or 0))


@register.filter
def qty(value):
    return fmt_qty(value) if value not in (None, "") else ""


@register.filter
def pct(value):
    return f"{Decimal(value) * 100:.0f}%"


@register.filter
def stars(value):
    if value is None:
        return ""
    full = int(round(float(value)))
    return "★" * full + "☆" * (5 - full)


@register.simple_tag(takes_context=True)
def qs(context, **kwargs):
    """Rebuild the current query string with some keys replaced."""
    params = context["request"].GET.copy()
    for k, v in kwargs.items():
        if v in (None, ""):
            params.pop(k, None)
        else:
            params[k] = v
    params.pop("page", None) if "page" not in kwargs else None
    return params.urlencode()


@register.simple_tag
def in_set(collection, *parts):
    """{% in_set reviewed sub_id listing_id as done %} -> True if the tuple is in the set."""
    return tuple(parts) in collection

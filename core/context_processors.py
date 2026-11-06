def global_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    ctx = {"unread_count": user.notifications.filter(is_read=False).count()}
    if user.role in ("farmer", "admin"):
        from core.models import SiteSettings
        from orders.services import uses_gateway
        rules = SiteSettings.load()
        ctx["gateway"] = uses_gateway()
        ctx["commission_pct"] = f"{(rules.commission_rate * 100).normalize():f}"
    if user.is_buyer:
        cart = getattr(user, "cart", None)
        ctx["cart_count"] = cart.items.count() if cart else 0
    return ctx

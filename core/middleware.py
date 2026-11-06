"""Stops the same form being saved twice (double click, browser refresh or Back + resubmit).

Every POST form carries a one-time token (_ftoken, added by static/js/app.js).
When a POST with a token succeeds (the view redirects), the token is remembered
in the session. If the same token is posted again, nothing is saved and the user
is sent back with a short message.
"""
from django.contrib import messages
from django.shortcuts import redirect

KEEP = 100


class DoubleSubmitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request.POST.get("_ftoken") if request.method == "POST" else None
        if token and hasattr(request, "session"):
            used = request.session.get("_used_ftokens", [])
            if token in used:
                messages.info(request, "That was already saved — it was not entered a second time.")
                back = request.META.get("HTTP_REFERER") or "/"
                return redirect(back)
        response = self.get_response(request)
        if token and hasattr(request, "session") and response.status_code in (301, 302, 303):
            used = request.session.get("_used_ftokens", [])
            used.append(token)
            request.session["_used_ftokens"] = used[-KEEP:]
        return response

"""Role checks enforced on the server for every view (NFR-02)."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def role_required(*roles, verified_farmer=False):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            allowed = user.role in roles or ("admin" in roles and user.is_superuser)
            if not allowed:
                raise PermissionDenied("You do not have access to this page.")
            if verified_farmer and not user.is_verified_farmer:
                raise PermissionDenied("Your farm must be verified by an administrator first.")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


admin_required = role_required("admin")
farmer_required = role_required("farmer")
verified_farmer_required = role_required("farmer", verified_farmer=True)
buyer_required = role_required("buyer")
distributor_required = role_required("distributor")
agronomist_required = role_required("agronomist")


def verified_agronomist_required(view):
    @role_required("agronomist")
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_verified_agronomist:
            raise PermissionDenied("Your registration must be verified by an administrator first.")
        return view(request, *args, **kwargs)
    return wrapper

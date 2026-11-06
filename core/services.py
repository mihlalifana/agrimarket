"""Shared helpers: notifications, simulated messages and audit logging."""
from .models import AuditLog, MessageLog, Notification


def notify(user, title, body="", link="", sms=False, email=False):
    """Create an in-app notification and optionally log a simulated SMS/email."""
    Notification.objects.create(user=user, title=title, body=body, link=link)
    if sms and user.phone:
        MessageLog.objects.create(channel=MessageLog.Channel.SMS, recipient=user.phone,
                                  text=f"AgriMarket: {title}. {body}".strip())
    if email and user.email:
        MessageLog.objects.create(channel=MessageLog.Channel.EMAIL, recipient=user.email,
                                  subject=title, text=body or title)


def audit(user, action, obj, field="", old="", new=""):
    """Write one append-only audit entry."""
    AuditLog.objects.create(
        user=user if (user is not None and user.is_authenticated) else None,
        action=action,
        object_type=obj.__class__.__name__,
        object_id=str(obj.pk),
        object_repr=str(obj)[:200],
        field=field,
        old_value="" if old is None else str(old),
        new_value="" if new is None else str(new),
    )


def audit_changes(user, obj, old_values, fields):
    """Audit each field whose value differs from old_values[field]."""
    for f in fields:
        old, new = old_values.get(f), getattr(obj, f)
        if old != new:
            audit(user, AuditLog.Action.UPDATE, obj, field=f, old=old, new=new)

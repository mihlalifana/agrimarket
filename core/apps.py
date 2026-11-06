from django.apps import AppConfig
from django.db.models.signals import pre_save


def _stamp_with_clock(sender, instance, raw=False, **kwargs):
    """New records saved inside clock.as_of() take that moment as created_at.

    A form builds its record (and fills created_at) before the view knows which
    day the record belongs to, so the moment is applied here, at save time.
    """
    from . import clock
    moment = clock.override()
    if moment is None or raw or not instance._state.adding:
        return
    field = next((f for f in sender._meta.concrete_fields
                  if f.name in ("created_at", "added_at") and f.default is clock.now), None)
    if field is not None:
        setattr(instance, field.attname, moment)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        pre_save.connect(_stamp_with_clock, dispatch_uid="core.stamp_with_clock")

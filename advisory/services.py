from accounts.models import User
from core.services import notify
from farm.models import Planting

from .models import PestAlert


def send_alert(alert):
    """Tell every farm growing the crop in the alert's province. Returns how many were told."""
    growers = (User.objects.filter(role=User.Role.FARMER, is_active=True, farm__province=alert.province,
                                   plantings__commodity=alert.commodity,
                                   plantings__status__in=[Planting.Status.GROWING, Planting.Status.HARVESTING])
               .distinct())
    for farmer in growers:
        notify(farmer, f"{alert.get_severity_display()}: {alert.title}", alert.message[:160],
               link="/advice/", sms=alert.severity != PestAlert.Severity.WATCH)
    alert.farms_told = growers.count()
    alert.save(update_fields=["farms_told"])
    return alert.farms_told

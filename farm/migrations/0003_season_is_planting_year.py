from django.db import migrations


def recompute(apps, schema_editor):
    Planting = apps.get_model("farm", "Planting")
    for p in Planting.objects.all().only("pk", "planted_on"):
        Planting.objects.filter(pk=p.pk).update(season=str(p.planted_on.year))


class Migration(migrations.Migration):
    dependencies = [("farm", "0002_initial")]
    operations = [migrations.RunPython(recompute, migrations.RunPython.noop)]

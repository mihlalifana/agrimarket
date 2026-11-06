from django.db import migrations, models


def switch_on(apps, schema_editor):
    S = apps.get_model("core", "SiteSettings")
    S.objects.filter(card_gateway="simulated").update(card_gateway="payfast_sandbox")


class Migration(migrations.Migration):
    dependencies = [("core", "0004_sitesettings_card_gateway_and_more")]
    operations = [
        migrations.AlterField(
            model_name="sitesettings", name="card_gateway",
            field=models.CharField("card payments", max_length=16, default="payfast_sandbox",
                                   choices=[("simulated", "Simulated test card (no internet needed)"),
                                            ("payfast_sandbox", "PayFast sandbox (real gateway, no real money)"),
                                            ("payfast_live", "PayFast live (real money)")])),
        migrations.RunPython(switch_on, migrations.RunPython.noop),
    ]

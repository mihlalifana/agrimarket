"""Harvest page: only due crops, start harvesting, record a harvest, list it, finish the crop."""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from farm.models import Field, Harvest, Planting
from marketplace.models import Category, Commodity
from marketplace.standards import sync_standards

from . import factories as f

TODAY = timezone.localdate()
START = date(TODAY.year, 3, 1) if date(TODAY.year, 3, 1) <= TODAY else date(TODAY.year - 1, 3, 1)


class HarvestPageTests(TestCase):
    def setUp(self):
        sync_standards(Category, Commodity)
        self.farm = f.farmer()
        field = Field.objects.create(farmer=self.farm, name="Tunnel", size_ha=2, irrigated=True)
        tomato = Commodity.objects.get(code="TOMATO")
        self.due = Planting.objects.create(farmer=self.farm, field=field, commodity=tomato, planted_on=START,
                                           area_ha=1, unit="crate", expected_yield=4000, expected_harvest=TODAY)
        self.later = Planting.objects.create(farmer=self.farm, field=field, commodity=tomato, cultivar="Late",
                                             planted_on=TODAY, area_ha=1, unit="crate", expected_yield=4000,
                                             expected_harvest=TODAY + timedelta(days=30))
        self.url = reverse("farm:harvests")
        self.client.force_login(self.farm)

    def _post(self, planting, action, **data):
        return self.client.post(self.url, {"planting": planting.pk, "action": action, **data})

    def test_only_due_crops_are_listed(self):
        page = self.client.get(self.url)
        self.assertContains(page, "value=\"start\"", count=1)
        self.assertNotContains(page, "Late")
        self.assertContains(page, "1 other crop still growing")

    def test_crop_page_has_no_harvest_form(self):
        page = self.client.get(reverse("farm:planting", args=[self.due.pk]))
        self.assertNotContains(page, "Save harvest")
        self.assertNotContains(page, "List for sale")
        self.assertContains(page, "Harvest page")

    def test_start_record_list_and_finish(self):
        self._post(self.due, "start")
        self.due.refresh_from_db()
        self.assertEqual(self.due.status, Planting.Status.HARVESTING)
        r = self._post(self.due, "record", **{"h-harvested_on": TODAY.isoformat(), "h-quantity": "300",
                                               "h-grade": "Class 1"})
        h = Harvest.objects.get(planting=self.due)
        self.assertRedirects(r, f"{self.url}?crop={self.due.pk}&new={h.pk}#harvested", fetch_redirect_response=False)
        page = self.client.get(self.url)
        self.assertContains(page, f"?harvest={h.pk}#add-produce")      # list for sale from here
        self._post(self.due, "finish")
        self.due.refresh_from_db()
        self.assertEqual(self.due.status, Planting.Status.COMPLETED)
        self.assertNotContains(self.client.get(self.url), "Finish harvesting this crop")

    def test_cannot_record_before_starting_or_for_crop_not_due(self):
        self._post(self.due, "record", **{"h-harvested_on": TODAY.isoformat(), "h-quantity": "5", "h-grade": "Class 1"})
        self._post(self.later, "start")
        self.later.refresh_from_db()
        self.assertEqual(self.later.status, Planting.Status.GROWING)
        self.assertFalse(Harvest.objects.exists())

    def test_cannot_finish_without_a_harvest(self):
        self._post(self.due, "start")
        self._post(self.due, "finish")
        self.due.refresh_from_db()
        self.assertEqual(self.due.status, Planting.Status.HARVESTING)

    def test_spray_interval_blocks_start(self):
        f.spray(self.due, TODAY - timedelta(days=1), phi_days=7)
        self._post(self.due, "start")
        self.due.refresh_from_db()
        self.assertEqual(self.due.status, Planting.Status.GROWING)
        self.assertContains(self.client.get(self.url), "Safe from")

    def test_other_farmer_cannot_use_crop(self):
        other = f.farmer()
        self.client.force_login(other)
        self._post(self.due, "start")
        self.due.refresh_from_db()
        self.assertEqual(self.due.status, Planting.Status.GROWING)

"""The core guarantee: stock can never be oversold or go negative."""
import threading
from decimal import Decimal

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase

from marketplace.models import Listing
from marketplace.services import OutOfStock, take_stock
from orders import services
from orders.models import Order, OrderItem, Payment, SubOrder

from . import factories as f

CARD = "4111111111111111"


class LastItemTests(TestCase):
    def setUp(self):
        self.farmer = f.farmer()
        self.bull = f.listing(self.farmer, qty="1", price="45000", kind="livestock", name="Breeding bull")
        self.a, self.b = f.user("buyer"), f.user("buyer")

    def test_only_one_buyer_gets_the_last_item(self):
        f.cart_with(self.a, (self.bull, 1))
        f.cart_with(self.b, (self.bull, 1))   # both carts hold the same last animal

        services.place_order(self.a, "collection", "card", card_number=CARD)
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.b, "collection", "card", card_number=CARD)

        self.bull.refresh_from_db()
        self.assertEqual(self.bull.quantity_available, 0)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().buyer, self.a)
        self.assertEqual(self.b.cart.items.count(), 1, "losing buyer's cart is left untouched")

    def test_stale_copy_cannot_take_stock_twice(self):
        copy1 = Listing.objects.get(pk=self.bull.pk)
        copy2 = Listing.objects.get(pk=self.bull.pk)  # both believe 1 is available
        take_stock(copy1, 1)
        with self.assertRaises(OutOfStock):
            take_stock(copy2, 1)
        self.bull.refresh_from_db()
        self.assertEqual(self.bull.quantity_available, 0)

    def test_database_refuses_negative_stock(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Listing.objects.filter(pk=self.bull.pk).update(quantity_available=Decimal("-1"))

    def test_failed_line_rolls_back_whole_order(self):
        maize = f.listing(self.farmer, qty="50")
        f.cart_with(self.a, (maize, 5), (self.bull, 1))
        f.cart_with(self.b, (self.bull, 1))
        services.place_order(self.b, "collection", "card", card_number=CARD)   # B takes the bull
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.a, "collection", "card", card_number=CARD)
        maize.refresh_from_db()
        self.assertEqual(maize.quantity_available, 50, "maize must not be taken when the bull line fails")
        self.assertEqual(Order.objects.filter(buyer=self.a).count(), 0)
        self.assertEqual(Payment.objects.count(), 1)

    def test_declined_card_changes_nothing(self):
        f.cart_with(self.a, (self.bull, 1))
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.a, "collection", "card", card_number=services.TEST_DECLINED_CARD)
        self.bull.refresh_from_db()
        self.assertEqual(self.bull.quantity_available, 1)
        self.assertFalse(Order.objects.exists())

    def test_cancel_returns_stock_exactly_once(self):
        f.cart_with(self.a, (self.bull, 1))
        order = services.place_order(self.a, "collection", "card", card_number=CARD)
        sub = order.suborders.get()
        stale = SubOrder.objects.get(pk=sub.pk)
        services.change_status(sub, SubOrder.Status.CANCELLED, self.a)
        with self.assertRaises(services.TransitionError):
            services.change_status(stale, SubOrder.Status.CANCELLED, self.a)
        self.bull.refresh_from_db()
        self.assertEqual(self.bull.quantity_available, 1)

    def test_half_an_animal_is_refused(self):
        goats = f.listing(self.farmer, qty="10", kind="livestock", name="Boer goats")
        f.cart_with(self.a, (goats, "1.5"))
        with self.assertRaises(services.CheckoutError):
            services.place_order(self.a, "collection", "card", card_number=CARD)


class ConcurrentCheckoutTests(TransactionTestCase):
    """Real threads racing for 5 remaining items: exactly 5 orders succeed."""

    def test_parallel_buyers(self):
        farmer = f.farmer()
        lambs = f.listing(farmer, qty="5", kind="livestock", name="Dorper lambs")
        buyers = [f.user("buyer") for _ in range(12)]
        for b in buyers:
            f.cart_with(b, (lambs, 1))

        results = []
        barrier = threading.Barrier(len(buyers))

        def buy(buyer):
            close_old_connections()
            try:
                barrier.wait()
                with transaction.atomic():
                    services.place_order(buyer, "collection", "card", card_number=CARD)
                results.append("ok")
            except services.CheckoutError:
                results.append("sold out")
            except Exception as e:  # database busy etc. counts as a failed purchase
                results.append(f"error {type(e).__name__}")
            finally:
                connection.close()

        threads = [threading.Thread(target=buy, args=(b,)) for b in buyers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lambs.refresh_from_db()
        sold = OrderItem.objects.filter(listing=lambs).count()
        self.assertEqual(results.count("ok"), 5, results)
        self.assertEqual(sold, 5)
        self.assertEqual(lambs.quantity_available, 0)

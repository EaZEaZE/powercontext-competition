```python
import unittest
from unittest.mock import patch

import inventory.service as inventory_service
from inventory.service import InventoryError, InventoryService


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ReservationTTLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        time_patch = patch.object(inventory_service, "time", self.clock)
        time_patch.start()
        self.addCleanup(time_patch.stop)

        self.service = InventoryService()

    def test_reservation_can_commit_within_ttl(self) -> None:
        self.service.add_stock("sku-1", 5)
        reservation = self.service.reserve("sku-1", 2, ttl_seconds=30)

        self.assertEqual(reservation.expires_at, 1_030.0)

        self.clock.advance(29)

        self.assertTrue(self.service.commit(reservation.reservation_id))
        self.assertEqual(self.service.get_stock("sku-1"), 3)
        self.assertEqual(
            self.service.get_reservation(reservation.reservation_id).status,
            "committed",
        )

    def test_expired_reservation_restores_available_stock(self) -> None:
        self.service.add_stock("sku-1", 5)
        reservation = self.service.reserve("sku-1", 3, ttl_seconds=10)

        self.assertEqual(self.service.get_available("sku-1"), 2)

        self.clock.advance(10)

        self.assertEqual(self.service.get_available("sku-1"), 5)
        self.assertEqual(
            self.service.get_reservation(reservation.reservation_id).status,
            "expired",
        )

    def test_release_after_expiration_returns_false(self) -> None:
        self.service.add_stock("sku-1", 5)
        reservation = self.service.reserve("sku-1", 2, ttl_seconds=10)

        self.clock.advance(10)

        self.assertFalse(self.service.release(reservation.reservation_id))
        self.assertEqual(self.service.get_stock("sku-1"), 5)
        self.assertEqual(
            self.service.get_reservation(reservation.reservation_id).status,
            "expired",
        )

    def test_commit_after_expiration_returns_false(self) -> None:
        self.service.add_stock("sku-1", 5)
        reservation = self.service.reserve("sku-1", 2, ttl_seconds=10)

        self.clock.advance(10)

        self.assertFalse(self.service.commit(reservation.reservation_id))
        self.assertEqual(self.service.get_stock("sku-1"), 5)
        self.assertEqual(
            self.service.get_reservation(reservation.reservation_id).status,
            "expired",
        )

    def test_non_positive_ttl_raises_inventory_error(self) -> None:
        self.service.add_stock("sku-1", 5)

        for ttl_seconds in (0, -1):
            with self.subTest(ttl_seconds=ttl_seconds):
                with self.assertRaises(InventoryError):
                    self.service.reserve("sku-1", 1, ttl_seconds=ttl_seconds)

        self.assertEqual(self.service.list_reservations(), [])

    def test_non_integer_ttl_raises_inventory_error(self) -> None:
        self.service.add_stock("sku-1", 5)

        for ttl_seconds in (1.5, "10", True):
            with self.subTest(ttl_seconds=ttl_seconds):
                with self.assertRaises(InventoryError):
                    self.service.reserve("sku-1", 1, ttl_seconds=ttl_seconds)

    def test_none_ttl_never_expires(self) -> None:
        self.service.add_stock("sku-1", 5)
        reservation = self.service.reserve("sku-1", 2)

        self.assertIsNone(reservation.expires_at)

        self.clock.advance(86_400)

        self.assertEqual(
            self.service.get_reservation(reservation.reservation_id).status,
            "held",
        )
        self.assertEqual(self.service.get_available("sku-1"), 3)
        self.assertTrue(self.service.commit(reservation.reservation_id))
        self.assertEqual(self.service.get_stock("sku-1"), 3)
```

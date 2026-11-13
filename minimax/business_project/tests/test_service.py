import pytest

from inventory import InventoryError, InventoryService


@pytest.fixture()
def svc():
    service = InventoryService()
    service.add_stock("SKU-A", 10)
    service.add_stock("SKU-B", 3)
    return service


def test_add_stock_returns_new_total(svc):
    assert svc.add_stock("SKU-A", 5) == 15
    assert svc.get_stock("SKU-A") == 15


def test_add_stock_rejects_non_positive(svc):
    with pytest.raises(InventoryError):
        svc.add_stock("SKU-A", 0)


def test_available_equals_stock_without_reservation(svc):
    assert svc.get_available("SKU-A") == 10


def test_reserve_reduces_available(svc):
    rsv = svc.reserve("SKU-A", 4)
    assert rsv.status == "held"
    assert rsv.sku == "SKU-A"
    assert rsv.quantity == 4
    assert svc.get_available("SKU-A") == 6
    assert svc.get_stock("SKU-A") == 10


def test_reserve_unknown_sku_raises(svc):
    with pytest.raises(InventoryError):
        svc.reserve("SKU-Z", 1)


def test_reserve_over_available_raises(svc):
    with pytest.raises(InventoryError):
        svc.reserve("SKU-B", 4)


def test_release_restores_available(svc):
    rsv = svc.reserve("SKU-A", 4)
    assert svc.release(rsv.reservation_id) is True
    assert svc.get_available("SKU-A") == 10
    assert svc.get_reservation(rsv.reservation_id).status == "released"


def test_release_twice_returns_false(svc):
    rsv = svc.reserve("SKU-A", 1)
    assert svc.release(rsv.reservation_id) is True
    assert svc.release(rsv.reservation_id) is False


def test_commit_deducts_stock(svc):
    rsv = svc.reserve("SKU-A", 4)
    assert svc.commit(rsv.reservation_id) is True
    assert svc.get_stock("SKU-A") == 6
    assert svc.get_available("SKU-A") == 6
    assert svc.get_reservation(rsv.reservation_id).status == "committed"


def test_commit_released_reservation_returns_false(svc):
    rsv = svc.reserve("SKU-A", 2)
    svc.release(rsv.reservation_id)
    assert svc.commit(rsv.reservation_id) is False
    assert svc.get_stock("SKU-A") == 10


def test_list_reservations_filters_by_status(svc):
    first = svc.reserve("SKU-A", 1)
    svc.reserve("SKU-B", 1)
    svc.release(first.reservation_id)
    assert len(svc.list_reservations()) == 2
    assert len(svc.list_reservations("held")) == 1
    assert len(svc.list_reservations("released")) == 1


def test_reservation_id_is_unique(svc):
    ids = {svc.reserve("SKU-A", 1).reservation_id for _ in range(5)}
    assert len(ids) == 5

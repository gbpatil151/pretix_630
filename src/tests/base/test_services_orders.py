import pytest
from datetime import timedelta
from decimal import Decimal

from django.utils.timezone import now
from django_scopes import scope

from pretix.base.models import Event, Item, ItemVariation, Order, OrderPosition, Organizer
from pretix.base.services.orders import approve_order, deny_order, OrderError

@pytest.fixture
def order_setup():
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    event = Event.objects.create(
        organizer=o, name='Dummy', slug='dummy',
        date_from=now(), live=True
    )
    o1 = Order.objects.create(
        code='FOOBAR', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING, require_approval=True,
        datetime=now(), expires=now() + timedelta(days=10),
        total=Decimal('13.37'),
        sales_channel=o.sales_channels.get(identifier="web"),
    )
    shirt = Item.objects.create(event=event, name='T-Shirt', default_price=12)
    shirt_red = ItemVariation.objects.create(item=shirt, default_price=14, value="Red")
    OrderPosition.objects.create(
        order=o1, item=shirt, variation=shirt_red,
        price=12, attendee_name_parts={}, secret='1234'
    )
    return event, o1


@pytest.mark.django_db
def test_approve_order_requires_approval(order_setup):
    event, order = order_setup
    with scope(organizer=event.organizer):
        order.require_approval = False
        order.save()
        with pytest.raises(OrderError, match="This order is not pending approval."):
            approve_order(order, send_mail=False)


@pytest.mark.django_db
def test_approve_order_success(order_setup):
    event, order = order_setup
    with scope(organizer=event.organizer):
        result = approve_order(order, send_mail=False)
        assert result == order.pk
        order.refresh_from_db()
        assert not order.require_approval
        assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_deny_order_requires_approval(order_setup):
    event, order = order_setup
    with scope(organizer=event.organizer):
        order.require_approval = False
        order.save()
        with pytest.raises(OrderError, match="This order is not pending approval."):
            deny_order(order, send_mail=False)


@pytest.mark.django_db
def test_deny_order_success(order_setup):
    event, order = order_setup
    with scope(organizer=event.organizer):
        result = deny_order(order, comment='Denied', send_mail=False)
        assert result == order.pk
        order.refresh_from_db()
        assert order.status == Order.STATUS_CANCELED

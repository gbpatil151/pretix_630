import pytest
from datetime import timedelta
from decimal import Decimal
from django.utils.timezone import now

from pretix.base.models import Event, Item, Order, OrderPosition, Organizer
from pretix.base.services.orders import OrderError, approve_order, deny_order
from django_scopes import scope

@pytest.fixture(scope='function')
def event():
    o = Organizer.objects.create(name='Dummy', slug='dummy', plugins='pretix.plugins.banktransfer')
    event = Event.objects.create(
        organizer=o, name='Dummy', slug='dummy',
        date_from=now(),
        plugins='pretix.plugins.banktransfer'
    )
    with scope(organizer=o):
        yield event

@pytest.mark.django_db
def test_approve_order_valid(event):
    o1 = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() - timedelta(days=10),
        total=10, require_approval=True, locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    ticket = Item.objects.create(event=event, name='Early-bird ticket',
                                 default_price=Decimal('23.00'), admission=True)
    OrderPosition.objects.create(
        order=o1, item=ticket, variation=None,
        price=Decimal("23.00"), attendee_name_parts={'full_name': "Peter"}, positionid=1
    )
    o1.create_transactions()
    
    approve_order(o1, send_mail=False)
    
    o1.refresh_from_db()
    assert o1.status == Order.STATUS_PENDING
    assert not o1.require_approval
    assert o1.expires > now()

@pytest.mark.django_db
def test_approve_order_invalid_not_pending_approval(event):
    o1 = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() - timedelta(days=10),
        total=10, require_approval=False, locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    
    with pytest.raises(OrderError, match="This order is not pending approval."):
        approve_order(o1, send_mail=False)

@pytest.mark.django_db
def test_deny_order_valid(event):
    o1 = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() - timedelta(days=10),
        total=10, require_approval=True, locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    ticket = Item.objects.create(event=event, name='Early-bird ticket',
                                 default_price=Decimal('23.00'), admission=True)
    OrderPosition.objects.create(
        order=o1, item=ticket, variation=None,
        price=Decimal("23.00"), attendee_name_parts={'full_name': "Peter"}, positionid=1
    )
    
    deny_order(o1, send_mail=False)
    
    o1.refresh_from_db()
    assert o1.status == Order.STATUS_CANCELED
    assert o1.require_approval  # Should still be True

@pytest.mark.django_db
def test_deny_order_invalid_not_pending_approval(event):
    o1 = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() - timedelta(days=10),
        total=10, require_approval=False, locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    
    with pytest.raises(OrderError, match="This order is not pending approval."):
        deny_order(o1, send_mail=False)

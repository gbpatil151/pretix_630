#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scope

from pretix.base.models import Event, Item, Order, OrderPosition, Organizer
from pretix.base.services.orders import OrderError, approve_order, deny_order


@pytest.fixture(scope='function')
def event():
    o = Organizer.objects.create(
        name='Dummy', slug='dummy', plugins='pretix.plugins.banktransfer'
    )
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
    ticket = Item.objects.create(
        event=event, name='Early-bird ticket',
        default_price=Decimal('23.00'), admission=True
    )
    OrderPosition.objects.create(
        order=o1, item=ticket, variation=None,
        price=Decimal("23.00"),
        attendee_name_parts={'full_name': "Peter"}, positionid=1
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

    with pytest.raises(
            OrderError, match="This order is not pending approval."):
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
    ticket = Item.objects.create(
        event=event, name='Early-bird ticket',
        default_price=Decimal('23.00'), admission=True
    )
    OrderPosition.objects.create(
        order=o1, item=ticket, variation=None,
        price=Decimal("23.00"),
        attendee_name_parts={'full_name': "Peter"}, positionid=1
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

    with pytest.raises(
            OrderError, match="This order is not pending approval."):
        deny_order(o1, send_mail=False)

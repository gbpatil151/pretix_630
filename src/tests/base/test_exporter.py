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
import datetime
import tracemalloc
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled

from pretix.base.exporters.orderlist import OrderListExporter
from pretix.base.models import (
    Event, Order, OrderPosition, Organizer, User,
)
from pretix.base.models.orders import OrderPayment


@pytest.fixture(scope='function')
def organizer():
    return Organizer.objects.create(name='Big Events', slug='bigevents')


@pytest.fixture(scope='function')
def event(organizer):
    event = Event.objects.create(
        organizer=organizer, name='Conference', slug='conf',
        date_from=datetime.datetime(2025, 6, 15, 10, 0, 0, tzinfo=datetime.timezone.utc),
        plugins='pretix.plugins.banktransfer',
        live=True,
    )
    organizer.settings.timezone = "UTC"
    with scope(organizer=organizer):
        yield event


@pytest.fixture
def team(event):
    return event.organizer.teams.create(all_events=True, can_view_orders=True)


@pytest.fixture
def user(team):
    user = User.objects.create_user('exporter@test.com', 'password')
    team.members.add(user)
    return user


@pytest.fixture
def item(event):
    return event.items.create(name='Standard Ticket', default_price=Decimal('50.00'))


@pytest.fixture
def quota(event, item):
    q = event.quotas.create(name='Standard Quota', size=10000)
    q.items.add(item)
    return q


def _bulk_create_orders(event, item, count):
    """Create `count` orders each with one position and one pending payment."""
    sales_channel = event.organizer.sales_channels.get(identifier="web")
    orders = []
    for i in range(count):
        orders.append(Order(
            code=f'T{i:05d}',
            event=event,
            email=f'buyer{i}@example.com',
            status=Order.STATUS_PAID,
            datetime=now(),
            expires=now() + datetime.timedelta(days=14),
            total=Decimal('50.00'),
            locale='en',
            sales_channel=sales_channel,
        ))
    Order.objects.bulk_create(orders)
    created_orders = list(Order.objects.filter(event=event).order_by('pk'))

    positions = []
    for idx, order in enumerate(created_orders):
        positions.append(OrderPosition(
            order=order,
            item=item,
            price=Decimal('50.00'),
            attendee_name_parts={"full_name": f"Attendee {idx}", "_scheme": "full"},
            secret=f'secret{idx:08d}',
            pseudonymization_id=f'PID{idx:06d}',
            positionid=1,
        ))
    OrderPosition.objects.bulk_create(positions)

    payments = []
    for order in created_orders:
        payments.append(OrderPayment(
            order=order,
            provider='banktransfer',
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            amount=Decimal('50.00'),
            payment_date=now(),
        ))
    OrderPayment.objects.bulk_create(payments)

    return created_orders


@pytest.mark.django_db(transaction=True)
def test_export_completes_for_large_dataset(event, item, quota, user):
    """Export of 500+ orders completes without error (scaled-down CI-safe variant)."""
    order_count = 500
    with scopes_disabled():
        _bulk_create_orders(event, item, order_count)

    exporter = OrderListExporter(event, event.organizer)
    form_data = {
        '_format': 'default',
        'paid_only': False,
        'include_payment_amounts': False,
        'group_multiple_choice': False,
    }

    rows = []
    for line in exporter.iterate_orders(form_data):
        if hasattr(line, 'total'):
            continue
        rows.append(line)

    data_rows = rows[1:]
    assert len(data_rows) == order_count, (
        f"Expected {order_count} data rows, got {len(data_rows)}"
    )


@pytest.mark.django_db(transaction=True)
def test_export_memory_bounded(event, item, quota, user):
    """Memory usage during export stays within a reasonable bound (< 256 MB)."""
    order_count = 500
    with scopes_disabled():
        _bulk_create_orders(event, item, order_count)

    exporter = OrderListExporter(event, event.organizer)
    form_data = {
        '_format': 'default',
        'paid_only': False,
        'include_payment_amounts': False,
        'group_multiple_choice': False,
    }

    tracemalloc.start()
    row_count = 0
    for line in exporter.iterate_orders(form_data):
        if hasattr(line, 'total'):
            continue
        row_count += 1

    _, peak_mb = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak_mb / (1024 * 1024)

    assert peak_mb < 256, (
        f"Peak memory {peak_mb:.1f} MB exceeds 256 MB threshold"
    )
    assert row_count > 0


@pytest.mark.django_db(transaction=True)
def test_export_positions_sheet_large_dataset(event, item, quota, user):
    """Positions sheet export handles large datasets via chunked iteration."""
    order_count = 500
    with scopes_disabled():
        _bulk_create_orders(event, item, order_count)

    exporter = OrderListExporter(event, event.organizer)
    form_data = {
        '_format': 'default',
        'paid_only': False,
        'include_payment_amounts': False,
        'group_multiple_choice': False,
    }

    rows = []
    for line in exporter.iterate_positions(form_data):
        if hasattr(line, 'total'):
            continue
        rows.append(line)

    data_rows = rows[1:]
    assert len(data_rows) == order_count, (
        f"Expected {order_count} position rows, got {len(data_rows)}"
    )


@pytest.mark.django_db(transaction=True)
def test_export_fees_sheet_empty_gracefully(event, item, quota, user):
    """Fees sheet export handles case where there are orders but no fees."""
    order_count = 50
    with scopes_disabled():
        _bulk_create_orders(event, item, order_count)

    exporter = OrderListExporter(event, event.organizer)
    form_data = {
        '_format': 'default',
        'paid_only': False,
        'include_payment_amounts': False,
        'group_multiple_choice': False,
    }

    rows = []
    for line in exporter.iterate_fees(form_data):
        if hasattr(line, 'total'):
            continue
        rows.append(line)

    data_rows = rows[1:]
    assert len(data_rows) == 0


@pytest.mark.django_db(transaction=True)
def test_export_render_csv_does_not_raise(event, item, quota, user):
    """Full CSV render pipeline completes without raising for a non-trivial dataset."""
    order_count = 100
    with scopes_disabled():
        _bulk_create_orders(event, item, order_count)

    exporter = OrderListExporter(event, event.organizer)
    form_data = {
        '_format': 'orders:default',
        'paid_only': False,
        'include_payment_amounts': False,
        'group_multiple_choice': False,
    }

    filename, content_type, data = exporter.render(form_data)
    assert filename.endswith('.csv')
    assert data is not None
    lines = data.decode('utf-8').strip().split('\n')
    assert len(lines) == order_count + 1  # header + data rows

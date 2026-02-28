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
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.utils.timezone import now, make_aware
from django_scopes import scope
from pypdf import PdfReader

from pretix.base.models import (
    Event, Item, ItemVariation, Order, OrderPosition, Organizer,
)
from pretix.plugins.ticketoutputpdf.ticketoutput import PdfTicketOutput


@pytest.fixture
def env0():
    static_time = make_aware(datetime(2023, 1, 1, 10, 0, 0))
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    event = Event.objects.create(
        organizer=o, name='Dummy', slug='dummy',
        date_from=static_time, live=True
    )
    o1 = Order.objects.create(
        code='FOOBAR', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING,
        datetime=static_time, expires=static_time + timedelta(days=10),
        total=Decimal('13.37'),
        sales_channel=o.sales_channels.get(identifier="web"),
    )
    shirt = Item.objects.create(event=event, name='T-Shirt', default_price=12)
    shirt_red = ItemVariation.objects.create(item=shirt, default_price=14, value="Red")
    OrderPosition.objects.create(
        order=o1, item=shirt, variation=shirt_red,
        price=12, attendee_name_parts={}, secret='1234'
    )
    OrderPosition.objects.create(
        order=o1, item=shirt, variation=shirt_red,
        price=12, attendee_name_parts={}, secret='5678'
    )
    return event, o1


@pytest.mark.django_db
def test_generate_pdf(env0, data_regression):
    event, order = env0
    with scope(organizer=event.organizer):
        event.settings.set('ticketoutput_pdf_code_x', 30)
        event.settings.set('ticketoutput_pdf_code_y', 50)
        event.settings.set('ticketoutput_pdf_code_s', 2)
        o = PdfTicketOutput(event)
        fname, ftype, buf = o.generate(order.positions.first())
        assert ftype == 'application/pdf'
        pdf = PdfReader(BytesIO(buf))
        assert len(pdf.pages) == 1
        
        # Extract text and basic data for regression testing
        page = pdf.pages[0]
        extracted_text = page.extract_text()
        fonts_used = [str(k) for k in page.get("/Resources", {}).get("/Font", {}).keys()]
        
        data_regression.check({
            "text": extracted_text,
            "fonts": fonts_used,
            "filename": fname,
        })

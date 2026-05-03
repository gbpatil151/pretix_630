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

"""
OrderBuilder — Builder pattern for API order creation
=====================================================

Decomposes the former 523-line monolithic ``OrderCreateSerializer.create()``
method into six clearly-named construction phases.
"""
import os
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.files import File
from django.db.models import F, Q
from django.utils.timezone import now
from django.utils.translation import gettext_lazy
from rest_framework.exceptions import ValidationError

# Re-use helper classes from the serializer module
from pretix.api.serializers.order import WrappedList, WrappedModel
from pretix.base.decimal import round_decimal
from pretix.base.models import (
    Device, InvoiceAddress, Order, OrderPosition, QuestionAnswer, Seat,
    Voucher,
)
from pretix.base.models.orders import CartPosition, OrderFee, OrderPayment
from pretix.base.models.tax import TaxRule
from pretix.base.services.cart import error_messages
from pretix.base.services.locking import LOCK_TRUST_WINDOW, lock_objects
from pretix.base.services.pricing import (
    apply_discounts, apply_rounding, get_line_price, get_listed_price,
    is_included_for_free,
)
from pretix.base.services.quotas import QuotaAvailability


class OrderBuilder:
    """Builder pattern: constructs an Order step-by-step from validated API data.

    Phases
    ------
    1. extract_data        – pop nested/extra fields from validated_data
    2. build_resource_diffs – compute quota/voucher/seat diff counters
    3. lock_and_validate    – acquire locks, check availability, vouchers, seats
    4. create_order_and_positions – create Order + OrderPosition objects
    5. attach_fees          – create OrderFee objects, apply rounding
    6. process_payments     – create payment objects, handle free orders

    Usage (inside OrderCreateSerializer.create)::

        builder = OrderBuilder(context, validated_data)
        builder.extract_data()
        builder.build_resource_diffs()
        builder.lock_and_validate()
        builder.create_order_and_positions()
        builder.attach_fees()
        order = builder.process_payments()
        return order
    """

    def __init__(self, context, validated_data):
        self.ctx = context
        self.event = context['event']
        self.validated_data = validated_data
        # Will be populated by phases
        self.fees_data = []
        self.positions_data = []
        self.payment_provider = None
        self.payment_info = '{}'
        self.payment_date = None
        self.force = False
        self.simulate = False
        self.consume_carts = []
        self.ia = None
        self._send_mail = False
        self.order = None
        self.pos_map = {}
        self.fees = []
        self.delete_cps = []
        self.quotas_by_item = {}
        self.quota_diff_for_locking = Counter()
        self.voucher_diff_for_locking = Counter()
        self.seat_diff_for_locking = Counter()
        self.quota_usage = Counter()
        self.voucher_usage = Counter()
        self.seat_usage = Counter()
        self.v_budget = {}
        self.now_dt = now()

    def extract_data(self):  # NOSONAR
        """Phase 1: Pop nested data from self.validated_data and set up invoice address."""
        self.fees_data = self.validated_data.pop('fees') if 'fees' in self.validated_data else []
        self.positions_data = self.validated_data.pop('positions') if 'positions' in self.validated_data else []
        self.payment_provider = self.validated_data.pop('payment_provider', None)
        self.payment_info = self.validated_data.pop('payment_info', '{}')
        self.payment_date = self.validated_data.pop('payment_date', now())
        self.force = self.validated_data.pop('force', False)
        self.simulate = self.validated_data.pop('simulate', False)

        if not self.validated_data.get("sales_channel"):
            self.validated_data["sales_channel"] = self.event.organizer.sales_channels.get(identifier="web")

        if self.validated_data.get("testmode") and not self.validated_data["sales_channel"].type_instance.testmode_supported:
            raise ValidationError({"testmode": ["This sales channel does not provide support for test mode."]})

        self._send_mail = self.validated_data.pop('send_email', False)
        if self._send_mail is None:
            self._send_mail = self.validated_data["sales_channel"].identifier in self.event.settings.mail_sales_channel_placed_paid

        if 'invoice_address' in self.validated_data:
            iadata = self.validated_data.pop('invoice_address')
            name = iadata.pop('name', '')
            if name and not iadata.get('name_parts'):
                iadata['name_parts'] = {
                    '_legacy': name
                }
            self.ia = InvoiceAddress(**iadata)
        else:
            self.ia = None

        self.quotas_by_item = {}
        self.quota_diff_for_locking = Counter()
        self.voucher_diff_for_locking = Counter()
        self.seat_diff_for_locking = Counter()
        self.quota_usage = Counter()
        self.voucher_usage = Counter()
        self.seat_usage = Counter()
        self.v_budget = {}
        self.now_dt = now()
        self.delete_cps = []
        self.consume_carts = self.validated_data.pop('consume_carts', [])

    def build_resource_diffs(self):
        """Phase 2: Build quota/voucher/seat diff counters from positions."""

        for pos_data in self.positions_data:
            if (pos_data.get('item'), pos_data.get('variation'), pos_data.get('subevent')) not in self.quotas_by_item:
                self.quotas_by_item[pos_data.get('item'), pos_data.get('variation'), pos_data.get('subevent')] = list(
                    pos_data.get('variation').quotas.filter(subevent=pos_data.get('subevent'))
                    if pos_data.get('variation')
                    else pos_data.get('item').quotas.filter(subevent=pos_data.get('subevent'))
                )
            for q in self.quotas_by_item[pos_data.get('item'), pos_data.get('variation'), pos_data.get('subevent')]:
                self.quota_diff_for_locking[q] += 1
            if pos_data.get('voucher'):
                self.voucher_diff_for_locking[pos_data['voucher']] += 1
            if pos_data.get('seat'):
                try:
                    seat = self.event.seats.get(seat_guid=pos_data['seat'], subevent=pos_data.get('subevent'))
                except Seat.DoesNotExist:
                    pos_data['seat'] = Seat.DoesNotExist
                else:
                    pos_data['seat'] = seat
                    self.seat_diff_for_locking[pos_data['seat']] += 1

        if self.consume_carts:
            offset = now() + timedelta(seconds=LOCK_TRUST_WINDOW)
            for cp in CartPosition.objects.filter(
                event=self.event, cart_id__in=self.consume_carts, expires__gt=self.now_dt
            ):
                quotas = (cp.variation.quotas.filter(subevent=cp.subevent)
                          if cp.variation else cp.item.quotas.filter(subevent=cp.subevent))
                for quota in quotas:
                    if cp.expires > offset:
                        self.quota_diff_for_locking[quota] -= 1
                    self.quota_usage[quota] -= 1
                if cp.voucher:
                    if cp.expires > offset:
                        self.voucher_diff_for_locking[cp.voucher] -= 1
                    self.voucher_usage[cp.voucher] -= 1
                if cp.seat:
                    if cp.expires > offset:
                        self.seat_diff_for_locking[cp.seat] -= 1
                    self.seat_usage[cp.seat] -= 1
                self.delete_cps.append(cp)

    def lock_and_validate(self):  # NOSONAR
        """Phase 3: Acquire locks, check quotas, vouchers, seats, validity."""
        if not self.simulate:
            full_lock_required = self.seat_diff_for_locking and self.event.settings.seating_minimal_distance > 0
            if full_lock_required:
                # We lock the entire event in this case since we don't want to deal with fine-granular locking
                # in the case of seating distance enforcement
                lock_objects([self.event])
            else:
                lock_objects(
                    [q for q, d in self.quota_diff_for_locking.items() if d > 0 and q.size is not None and not self.force] +
                    [v for v, d in self.voucher_diff_for_locking.items() if d > 0 and not self.force] +
                    [s for s, d in self.seat_diff_for_locking.items() if d > 0],
                    shared_lock_objects=[self.event]
                )

        qa = QuotaAvailability()
        qa.queue(*[q for q, d in self.quota_diff_for_locking.items() if d > 0])
        qa.compute()

        # These are not technically correct as diff use due to the time offset applied above, so let's prevent accidental
        # use further down
        del self.quota_diff_for_locking, self.voucher_diff_for_locking, self.seat_diff_for_locking

        errs = [{} for _ in self.positions_data]

        for i, pos_data in enumerate(self.positions_data):
            if pos_data.get('voucher'):
                v = pos_data['voucher']

                if pos_data.get('addon_to'):
                    errs[i]['voucher'] = ['Vouchers are currently not supported for add-on products.']
                    continue

                if not v.applies_to(pos_data['item'], pos_data.get('variation')):
                    errs[i]['voucher'] = [error_messages['voucher_invalid_item']]
                    continue

                if v.subevent_id and pos_data.get('subevent').pk != v.subevent_id:
                    errs[i]['voucher'] = [error_messages['voucher_invalid_subevent']]
                    continue

                if v.valid_until is not None and v.valid_until < self.now_dt:
                    errs[i]['voucher'] = [error_messages['voucher_expired']]
                    continue

                self.voucher_usage[v] += 1
                if self.voucher_usage[v] > 0:
                    redeemed_in_carts = CartPosition.objects.filter(
                        Q(voucher=pos_data['voucher']) & Q(event=self.event) & Q(expires__gte=self.now_dt)
                    ).exclude(pk__in=[cp.pk for cp in self.delete_cps])
                    v_avail = v.max_usages - v.redeemed - redeemed_in_carts.count()
                    if v_avail < self.voucher_usage[v]:
                        errs[i]['voucher'] = [
                            'The voucher has already been used the maximum number of times.'
                        ]

                if v.budget is not None:
                    price = pos_data.get('price')
                    listed_price = get_listed_price(pos_data.get('item'), pos_data.get('variation'), pos_data.get('subevent'))

                    if pos_data.get('voucher'):
                        price_after_voucher = pos_data.get('voucher').calculate_price(listed_price)
                    else:
                        price_after_voucher = listed_price
                    if price is None:
                        price = price_after_voucher

                    if v not in self.v_budget:
                        self.v_budget[v] = v.budget - v.budget_used()
                    disc = max(listed_price - price, 0)
                    if disc > self.v_budget[v]:
                        new_disc = self.v_budget[v]
                        self.v_budget[v] -= new_disc
                        if new_disc == Decimal('0.00') or pos_data.get('price') is not None:
                            errs[i]['voucher'] = [
                                'The voucher has a remaining budget of {}, therefore a discount of {} can not be '
                                'given.'.format(self.v_budget[v] + new_disc, disc)
                            ]
                            continue
                        pos_data['price'] = price + (disc - new_disc)
                    else:
                        self.v_budget[v] -= disc

            seated = pos_data.get('item').seat_category_mappings.filter(subevent=pos_data.get('subevent')).exists()
            if pos_data.get('seat'):
                if pos_data.get('addon_to'):
                    errs[i]['seat'] = ['Seats are currently not supported for add-on products.']
                    continue
                if not seated:
                    errs[i]['seat'] = ['The specified product does not allow to choose a seat.']
                seat = pos_data['seat']
                if seat is Seat.DoesNotExist:
                    errs[i]['seat'] = ['The specified seat does not exist.']
                else:
                    self.seat_usage[seat] += 1
                    sales_channel_id = self.validated_data['sales_channel'].identifier
                    if (self.seat_usage[seat] > 0 and not seat.is_available(sales_channel=sales_channel_id)) or self.seat_usage[seat] > 1:
                        errs[i]['seat'] = [gettext_lazy('The selected seat "{seat}" is not available.').format(seat=seat.name)]
            elif seated:
                errs[i]['seat'] = ['The specified product requires to choose a seat.']

            requested_valid_from = pos_data.pop('requested_valid_from', None)
            if 'valid_from' not in pos_data and 'valid_until' not in pos_data:
                valid_from, valid_until = pos_data['item'].compute_validity(
                    requested_start=(
                        requested_valid_from
                        if requested_valid_from and pos_data['item'].validity_dynamic_start_choice
                        else now()
                    ),
                    enforce_start_limit=True,
                    override_tz=self.event.timezone,
                )
                pos_data['valid_from'] = valid_from
                pos_data['valid_until'] = valid_until

        if not self.force:
            for i, pos_data in enumerate(self.positions_data):
                if pos_data.get('voucher'):
                    if pos_data['voucher'].allow_ignore_quota or pos_data['voucher'].block_quota:
                        continue

                if pos_data.get('subevent'):
                    if pos_data.get('item').pk in pos_data['subevent'].item_overrides and pos_data['subevent'].item_overrides[pos_data['item'].pk].disabled:
                        errs[i]['item'] = [gettext_lazy('The product "{}" is not available on this date.').format(
                            str(pos_data.get('item'))
                        )]
                    if (
                            pos_data.get('variation') and pos_data['variation'].pk in pos_data['subevent'].var_overrides and
                            pos_data['subevent'].var_overrides[pos_data['variation'].pk].disabled
                    ):
                        errs[i]['item'] = [gettext_lazy('The product "{}" is not available on this date.').format(
                            str(pos_data.get('item'))
                        )]

                new_quotas = self.quotas_by_item[pos_data.get('item'), pos_data.get('variation'), pos_data.get('subevent')]
                if len(new_quotas) == 0:
                    errs[i]['item'] = [gettext_lazy('The product "{}" is not assigned to a quota.').format(
                        str(pos_data.get('item'))
                    )]
                else:
                    for quota in new_quotas:
                        self.quota_usage[quota] += 1
                        if self.quota_usage[quota] > 0 and qa.results[quota][1] is not None:
                            if qa.results[quota][1] < self.quota_usage[quota]:
                                errs[i]['item'] = [
                                    gettext_lazy('There is not enough quota available on quota "{}" to perform the operation.').format(
                                        quota.name
                                    )
                                ]

        if any(errs):
            raise ValidationError({'positions': errs})

    def create_order_and_positions(self):  # NOSONAR
        """Phase 4: Create Order and OrderPosition objects, compute prices, save."""
        if self.validated_data.get('locale', None) is None:
            self.validated_data['locale'] = self.event.settings.locale

        self.order = Order(event=self.event, **self.validated_data)
        if not self.validated_data.get('expires'):
            self.order.set_expires(subevents=[p.get('subevent') for p in self.positions_data])
        self.order.meta_info = "{}"
        self.order.total = Decimal('0.00')
        if self.validated_data.get('require_approval') is not None:
            self.order.require_approval = self.validated_data['require_approval']
        if self.simulate:
            self.order = WrappedModel(self.order)
            self.order.last_modified = now()
            self.order.code = 'PREVIEW'
        else:
            self.order.save()

        if self.ia:
            if not self.simulate:
                self.ia.order = self.order
                self.ia.save()
            else:
                self.order.invoice_address = self.ia
                self.ia.last_modified = now()

        # Generate position objects
        self.pos_map = {}
        for pos_data in self.positions_data:
            addon_to = pos_data.pop('addon_to', None)
            attendee_name = pos_data.pop('attendee_name', '')
            if attendee_name and not pos_data.get('attendee_name_parts'):
                pos_data['attendee_name_parts'] = {
                    '_legacy': attendee_name
                }
            pos = OrderPosition(**{k: v for k, v in pos_data.items() if k != 'answers' and k != '_quotas' and k != 'use_reusable_medium'})
            if self.simulate:
                pos.order = self.order._wrapped
            else:
                pos.order = self.order
            if addon_to:
                pos.addon_to = self.pos_map[addon_to]

            self.pos_map[pos.positionid] = pos
            pos_data['__instance'] = pos

        # Calculate prices if not set
        for pos_data in self.positions_data:
            pos = pos_data['__instance']
            if pos.addon_to_id and is_included_for_free(pos.item, pos.addon_to):
                listed_price = Decimal('0.00')
            else:
                listed_price = get_listed_price(pos.item, pos.variation, pos.subevent)

            if pos.price is None:
                if pos.voucher:
                    price_after_voucher = pos.voucher.calculate_price(listed_price)
                else:
                    price_after_voucher = listed_price

                line_price = get_line_price(
                    price_after_voucher=price_after_voucher,
                    custom_price_input=None,
                    custom_price_input_is_net=False,
                    tax_rule=pos.item.tax_rule,
                    invoice_address=self.ia,
                    bundled_sum=Decimal('0.00'),
                )
                pos.price = line_price.gross
                pos._auto_generated_price = True
            else:
                if pos.voucher:
                    if not pos.item.tax_rule or pos.item.tax_rule.price_includes_tax:
                        price_after_voucher = max(pos.price, pos.voucher.calculate_price(listed_price))
                    else:
                        pos._calculate_tax(invoice_address=self.ia)
                        price_after_voucher = max(pos.price - pos.tax_value, pos.voucher.calculate_price(listed_price))
                else:
                    price_after_voucher = listed_price
                pos._auto_generated_price = False
            pos._voucher_discount = listed_price - price_after_voucher
            if pos.voucher:
                pos.voucher_budget_use = max(listed_price - price_after_voucher, Decimal('0.00'))

        order_positions = [pos_data['__instance'] for pos_data in self.positions_data]
        if not any([p.get("discount") for p in self.positions_data]):
            # If any discount is set by the client (i.e. pretixPOS), we do not recalculate but believe the client
            # to avoid differences in end results.
            discount_results = apply_discounts(
                self.event,
                self.order.sales_channel,
                [
                    (cp.item_id, cp.subevent_id, cp.subevent.date_from if cp.subevent_id else None, cp.price,
                     cp.addon_to, cp.is_bundled, pos._voucher_discount)
                    for cp in order_positions
                ]
            )
            for cp, (new_price, discount) in zip(order_positions, discount_results):
                if new_price != cp.price and cp._auto_generated_price:
                    cp.price = new_price
                cp.discount = discount

        # Save instances
        for pos_data in self.positions_data:
            answers_data = pos_data.pop('answers', [])
            use_reusable_medium = pos_data.pop('use_reusable_medium', None)
            pos = pos_data['__instance']
            pos._calculate_tax(invoice_address=self.ia)

            if self.simulate:
                pos = WrappedModel(pos)
                pos.id = 0
                answers = []
                for answ_data in answers_data:
                    options = answ_data.pop('options', [])
                    answ = WrappedModel(QuestionAnswer(**answ_data))
                    answ.options = WrappedList(options)
                    answers.append(answ)
                pos.answers = answers
                pos.pseudonymization_id = "PREVIEW"
                pos.checkins = []
                pos.print_logs = []
                self.pos_map[pos.positionid] = pos
            else:
                if pos.voucher:
                    Voucher.objects.filter(pk=pos.voucher.pk).update(redeemed=F('redeemed') + 1)
                pos.save()
                seen_answers = set()
                for answ_data in answers_data:
                    # Workaround for a pretixPOS bug :-(
                    if answ_data.get('question') in seen_answers:
                        continue
                    seen_answers.add(answ_data.get('question'))

                    options = answ_data.pop('options', [])

                    if isinstance(answ_data['answer'], File):
                        an = answ_data.pop('answer')
                        answ = pos.answers.create(**answ_data, answer='')
                        answ.file.save(os.path.basename(an.name), an, save=False)
                        answ.answer = 'file://' + answ.file.name
                        answ.save()
                    else:
                        answ = pos.answers.create(**answ_data)
                        answ.options.add(*options)

                if use_reusable_medium:
                    use_reusable_medium.linked_orderposition = pos
                    use_reusable_medium.save(update_fields=['linked_orderposition'])
                    use_reusable_medium.log_action(
                        'pretix.reusable_medium.linked_orderposition.changed',
                        data={
                            'by_order': self.order.code,
                            'linked_orderposition': pos.pk,
                        }
                    )

    def attach_fees(self):  # NOSONAR
        """Phase 5: Create OrderFee objects, apply tax rounding, update total."""
        if not self.simulate:
            for cp in self.delete_cps:
                if cp.addon_to_id:
                    continue
                cp.addons.all().delete()
                cp.delete()

        self.order.total = sum([p.price for p in self.pos_map.values()])
        self.fees = []
        for fee_data in self.fees_data:
            is_percentage = fee_data.pop('_treat_value_as_percentage', False)
            if is_percentage:
                fee_data['value'] = round_decimal(self.order.total * (fee_data['value'] / Decimal('100.00')),
                                                  self.event.currency)
            is_split_taxes = fee_data.pop('_split_taxes_like_products', False)

            if is_split_taxes and self.order.total:
                d = defaultdict(lambda: Decimal('0.00'))
                trz = TaxRule.zero()
                for p in self.pos_map.values():
                    tr = p.tax_rule
                    d[tr] += p.price - p.tax_value

                base_values = sorted([tuple(t) for t in d.items()], key=lambda t, trz=trz: (t[0] or trz).rate)
                sum_base = sum(t[1] for t in base_values)
                fee_values = [(t[0], round_decimal(fee_data['value'] * t[1] / sum_base, self.event.currency))
                              for t in base_values]
                sum_fee = sum(t[1] for t in fee_values)

                # If there are rounding differences, we fix them up, but always leaning to the benefit of the tax
                # authorities
                if sum_fee > fee_data['value']:
                    fee_values[0] = (fee_values[0][0], fee_values[0][1] + (fee_data['value'] - sum_fee))
                elif sum_fee < fee_data['value']:
                    fee_values[-1] = (fee_values[-1][0], fee_values[-1][1] + (fee_data['value'] - sum_fee))

                for tr, val in fee_values:
                    fee_data['tax_rule'] = tr
                    fee_data['value'] = val
                    f = OrderFee(**fee_data)
                    f.order = self.order._wrapped if self.simulate else self.order
                    f._calculate_tax()
                    self.fees.append(f)
                    if self.simulate:
                        f.id = 0
                    else:
                        f.save()
            else:
                f = OrderFee(**fee_data)
                f.order = self.order._wrapped if self.simulate else self.order
                f._calculate_tax()
                self.fees.append(f)
                if self.simulate:
                    f.id = 0
                else:
                    f.save()

        rounding_mode = self.validated_data.get("tax_rounding_mode")
        if not rounding_mode and isinstance(self.context.get("auth"), Device):
            # Safety fallback to avoid differences in tax reporting
            brand = self.context.get("auth").software_brand or ""
            if "pretixPOS" in brand or "pretixKIOSK" in brand:
                rounding_mode = "line"
        if not rounding_mode:
            rounding_mode = self.event.settings.tax_rounding
        changed = apply_rounding(
            rounding_mode,
            self.ia,
            self.event.currency,
            [*self.pos_map.values(), *self.fees]
        )
        for line in changed:
            if isinstance(line, OrderPosition):
                line.save(update_fields=[
                    "price", "price_includes_rounding_correction", "tax_value", "tax_value_includes_rounding_correction"
                ])
            elif isinstance(line, OrderFee):
                line.save(update_fields=[
                    "value", "value_includes_rounding_correction", "tax_value", "tax_value_includes_rounding_correction"
                ])

        self.order.total = sum([c.price for c in self.pos_map.values()]) + sum([f.value for f in self.fees])
        if self.simulate:
            self.order.fees = self.fees
            self.order.positions = self.pos_map.values()
            self.order.payments = []
            self.order.refunds = []
            return self.order  # ignore payments
        else:
            self.order.save(update_fields=['total'])

    def process_payments(self):
        """Phase 6: Create payment objects, handle free/paid transitions. Returns the self.order."""
        if self.order.total == Decimal('0.00') and self.validated_data.get('status') == Order.STATUS_PAID and not self.payment_provider:
            self.payment_provider = 'free'

        if self.order.total != Decimal('0.00') and self.order.event.currency == "XXX":
            raise ValidationError('Paid products not supported without a valid currency.')

        if self.order.total == Decimal('0.00') and self.validated_data.get('status') != Order.STATUS_PAID and not self.validated_data.get('require_approval'):
            self.order.status = Order.STATUS_PAID
            self.order.save()
            self.order.payments.create(
                amount=self.order.total, provider='free', state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                payment_date=now()
            )
        elif self.payment_provider == "free" and self.order.total != Decimal('0.00'):
            raise ValidationError('You cannot use the "free" payment provider for non-free orders.')
        elif self.validated_data.get('status') == Order.STATUS_PAID:
            if not self.payment_provider:
                raise ValidationError('You cannot create a paid self.order without a payment provider.')
            if self.validated_data.get('require_approval'):
                raise ValidationError('You cannot create a paid self.order that requires approval.')
            self.order.payments.create(
                amount=self.order.total,
                provider=self.payment_provider,
                info=self.payment_info,
                payment_date=self.payment_date,
                state=OrderPayment.PAYMENT_STATE_CONFIRMED
            )
        elif self.payment_provider:
            self.order.payments.create(
                amount=self.order.total,
                provider=self.payment_provider,
                info=self.payment_info,
                state=OrderPayment.PAYMENT_STATE_CREATED
            )

        self.order.create_transactions(is_new=True, fees=self.fees, positions=self.pos_map.values())
        return self.order

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
OrderChangeManager (Facade)
============================

This is the refactored OrderChangeManager that acts as a **Facade** over three
focused collaborator classes:

- ``OrderChangeValidator``      — quota / seat / membership / order-level checks
- ``OrderFeeManager``           — fee recalculation, rounding, invoice reissue
- ``OrderOperationExecutor``    — per-operation DB mutations + split-order creation

The public API is identical to the original monolithic class.  Callers do not
need to change any code.
"""


import logging
from collections import Counter, defaultdict, namedtuple
from datetime import datetime
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Count, QuerySet, Sum
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy, ngettext_lazy

from pretix.base.models import (
    Item, ItemVariation, Membership, Order, OrderPayment, OrderPosition, Seat,
)
from pretix.base.models.event import SubEvent
from pretix.base.models.orders import InvoiceAddress, OrderFee, OrderRefund
from pretix.base.models.tax import TAXED_ZERO, TaxedPrice, TaxRule
from pretix.base.services.invoices import (
    invoice_transmission_separately, transmit_invoice,
)
from pretix.base.services.order_change.executor import OrderOperationExecutor
from pretix.base.services.order_change.fee_manager import OrderFeeManager
from pretix.base.services.order_change.validators import OrderChangeValidator
from pretix.base.services.orders import (
    OrderError, error_messages, notify_user_changed_order,
)
from pretix.base.services.pricing import get_price
from pretix.base.signals import order_changed
from pretix.helpers import OF_SELF

logger = logging.getLogger(__name__)


class OrderChangeManager:
    error_messages = {
        'product_without_variation': gettext_lazy('You need to select a variation of the product.'),
        'quota': gettext_lazy('The quota {name} does not have enough capacity left to perform the operation.'),
        'quota_missing': gettext_lazy('There is no quota defined that allows this operation.'),
        'product_invalid': gettext_lazy('The selected product is not active or has no price set.'),
        'complete_cancel': gettext_lazy('This operation would leave the order empty. Please cancel the order itself instead.'),
        'paid_to_free_exceeded': gettext_lazy(
            'This operation would make the order free and therefore immediately paid, however '
            'no quota is available.'
        ),
        'addon_to_required': gettext_lazy('This is an add-on product, please select the base position it should be added to.'),
        'addon_invalid': gettext_lazy('The selected base position does not allow you to add this product as an add-on.'),
        'subevent_required': gettext_lazy('You need to choose a subevent for the new position.'),
        'seat_unavailable': gettext_lazy('The selected seat "{seat}" is not available.'),
        'seat_subevent_mismatch': gettext_lazy(
            'You selected seat "{seat}" for a date that does not match the selected ticket date. Please choose a seat again.'
        ),
        'seat_required': gettext_lazy('The selected product requires you to select a seat.'),
        'seat_forbidden': gettext_lazy('The selected product does not allow to select a seat.'),
        'tax_rule_country_blocked': gettext_lazy('The selected country is blocked by your tax rule.'),
        'gift_card_change': gettext_lazy('You cannot change the price of a position that has been used to issue a gift card.'),
        'max_items_per_product': ngettext_lazy(
            "You cannot select more than %(max)s item of the product %(product)s.",
            "You cannot select more than %(max)s items of the product %(product)s.",
            "max"
        ),
        'min_items_per_product': ngettext_lazy(
            "You need to select at least %(min)s item of the product %(product)s.",
            "You need to select at least %(min)s items of the product %(product)s.",
            "min"
        ),
        'max_order_size': gettext_lazy('Orders cannot have more than %(max)s positions.'),
    }
    ItemOperation = namedtuple('ItemOperation', ('position', 'item', 'variation'))
    SubeventOperation = namedtuple('SubeventOperation', ('position', 'subevent'))
    SeatOperation = namedtuple('SubeventOperation', ('position', 'seat'))
    PriceOperation = namedtuple('PriceOperation', ('position', 'price', 'price_diff'))
    TaxRuleOperation = namedtuple('TaxRuleOperation', ('position', 'tax_rule'))
    MembershipOperation = namedtuple('MembershipOperation', ('position', 'membership'))
    CancelOperation = namedtuple('CancelOperation', ('position', 'price_diff'))
    AddOperation = namedtuple('AddOperation', ('item', 'variation', 'price', 'addon_to', 'subevent', 'seat', 'membership',
                                               'valid_from', 'valid_until', 'is_bundled', 'result'))
    SplitOperation = namedtuple('SplitOperation', ('position',))
    FeeValueOperation = namedtuple('FeeValueOperation', ('fee', 'value', 'price_diff'))
    AddFeeOperation = namedtuple('AddFeeOperation', ('fee', 'price_diff'))
    CancelFeeOperation = namedtuple('CancelFeeOperation', ('fee', 'price_diff'))
    RegenerateSecretOperation = namedtuple('RegenerateSecretOperation', ('position',))
    ChangeSecretOperation = namedtuple('ChangeSecretOperation', ('position', 'new_secret'))
    ChangeValidFromOperation = namedtuple('ChangeValidFromOperation', ('position', 'valid_from'))
    ChangeValidUntilOperation = namedtuple('ChangeValidUntilOperation', ('position', 'valid_until'))
    AddBlockOperation = namedtuple('AddBlockOperation', ('position', 'block_name', 'ignore_from_quota_while_blocked'))
    RemoveBlockOperation = namedtuple('RemoveBlockOperation', ('position', 'block_name', 'ignore_from_quota_while_blocked'))
    ForceRecomputeOperation = namedtuple('ForceRecomputeOperation', ())

    class AddPositionResult:
        _position: Optional[OrderPosition]

        def __init__(self):
            self._position = None

        @property
        def position(self) -> OrderPosition:
            if self._position is None:
                raise RuntimeError("Order position has not been created yet. Call commit() first on OrderChangeManager.")
            return self._position

    def __init__(self, order: Order, user=None, auth=None, notify=True, reissue_invoice=True, allow_blocked_seats=False):
        self.order = order
        self.user = user
        self.auth = auth
        self.event = order.event
        self.split_order = None
        self.reissue_invoice = reissue_invoice
        self.allow_blocked_seats = allow_blocked_seats
        self._committed = False
        self._totaldiff_guesstimate = 0
        self._quotadiff = Counter()
        self._seatdiff = Counter()
        self._operations = []
        self.notify = notify
        self._invoice_dirty = False
        self._invoices = []

        # --- Facade collaborators ---
        self._validator = OrderChangeValidator(self)
        self._fee_manager = OrderFeeManager(self)
        self._executor = OrderOperationExecutor(self)

    def change_item(self, position: OrderPosition, item: Item, variation: Optional[ItemVariation]):
        if (not variation and item.has_variations) or (variation and variation.item_id != item.pk):
            raise OrderError(self.error_messages['product_without_variation'])

        new_quotas = (variation.quotas.filter(subevent=position.subevent)
                      if variation else item.quotas.filter(subevent=position.subevent))
        if not new_quotas:
            raise OrderError(self.error_messages['quota_missing'])

        self._quotadiff.update(new_quotas)
        self._quotadiff.subtract(position.quotas)
        self._operations.append(self.ItemOperation(position, item, variation))

    def change_seat(self, position: OrderPosition, seat: Optional[Seat]):
        if isinstance(seat, str):
            subev = None
            if self.event.has_subevents:
                subev = position.subevent
                for p in self._operations:
                    if isinstance(p, self.SubeventOperation) and p.position == position:
                        subev = p.subevent
            try:
                seat = Seat.objects.get(
                    event=self.event,
                    subevent=subev,
                    seat_guid=seat
                )
            except Seat.DoesNotExist:
                raise OrderError(error_messages['seat_invalid'])
        if position.seat:
            self._seatdiff.subtract([position.seat])
        if seat:
            self._seatdiff.update([seat])
        self._operations.append(self.SeatOperation(position, seat))

    def change_membership(self, position: OrderPosition, membership: Membership):
        self._operations.append(self.MembershipOperation(position, membership))

    def change_subevent(self, position: OrderPosition, subevent: SubEvent):
        try:
            price = get_price(position.item, position.variation, voucher=position.voucher, subevent=subevent,
                              invoice_address=self._invoice_address)
        except TaxRule.SaleNotAllowed:
            raise OrderError(self.error_messages['tax_rule_country_blocked'])

        if price is None:  # NOQA
            raise OrderError(self.error_messages['product_invalid'])

        new_quotas = (position.variation.quotas.filter(subevent=subevent)
                      if position.variation else position.item.quotas.filter(subevent=subevent))
        if not new_quotas:
            raise OrderError(self.error_messages['quota_missing'])

        self._quotadiff.update(new_quotas)
        self._quotadiff.subtract(position.quotas)
        self._operations.append(self.SubeventOperation(position, subevent))
        self._invoice_dirty = True

    def change_item_and_subevent(self, position: OrderPosition, item: Item, variation: Optional[ItemVariation],
                                 subevent: SubEvent):
        if (not variation and item.has_variations) or (variation and variation.item_id != item.pk):
            raise OrderError(self.error_messages['product_without_variation'])

        try:
            price = get_price(item, variation, voucher=position.voucher, subevent=subevent,
                              invoice_address=self._invoice_address)
        except TaxRule.SaleNotAllowed:
            raise OrderError(self.error_messages['tax_rule_country_blocked'])

        if price is None:  # NOQA
            raise OrderError(self.error_messages['product_invalid'])

        new_quotas = (variation.quotas.filter(subevent=subevent)
                      if variation else item.quotas.filter(subevent=subevent))
        if not new_quotas:
            raise OrderError(self.error_messages['quota_missing'])

        self._quotadiff.update(new_quotas)
        self._quotadiff.subtract(position.quotas)
        self._operations.append(self.ItemOperation(position, item, variation))
        self._operations.append(self.SubeventOperation(position, subevent))
        self._invoice_dirty = True

    def regenerate_secret(self, position: OrderPosition):
        self._operations.append(self.RegenerateSecretOperation(position))

    def change_ticket_secret(self, position: OrderPosition, new_secret: str):
        self._operations.append(self.ChangeSecretOperation(position, new_secret))

    def change_valid_from(self, position: OrderPosition, new_value: datetime):
        self._operations.append(self.ChangeValidFromOperation(position, new_value))

    def change_valid_until(self, position: OrderPosition, new_value: datetime):
        self._operations.append(self.ChangeValidUntilOperation(position, new_value))

    def add_block(self, position: OrderPosition, block_name: str, ignore_from_quota_while_blocked=None):
        self._operations.append(self.AddBlockOperation(position, block_name, ignore_from_quota_while_blocked))

    def remove_block(self, position: OrderPosition, block_name: str, ignore_from_quota_while_blocked=None):
        self._operations.append(self.RemoveBlockOperation(position, block_name, ignore_from_quota_while_blocked))

    def change_price(self, position: OrderPosition, price: Decimal):
        tax_rule = self._current_tax_rules().get(position.pk, position.tax_rule) or TaxRule.zero()
        price = tax_rule.tax(price, base_price_is='gross', invoice_address=self._invoice_address,
                             force_fixed_gross_price=True)

        if position.issued_gift_cards.exists():
            raise OrderError(self.error_messages['gift_card_change'])

        self._totaldiff_guesstimate += price.gross - position.gross_price_before_rounding

        if self.order.event.settings.invoice_include_free or price.gross != Decimal('0.00') or position.price != Decimal('0.00'):
            self._invoice_dirty = True

        self._operations.append(self.PriceOperation(position, price, price.gross - position.price))

    def change_tax_rule(self, position_or_fee, tax_rule: TaxRule):
        self._operations.append(self.TaxRuleOperation(position_or_fee, tax_rule))
        self._invoice_dirty = True

    def _current_tax_rules(self):
        tax_rules = {}
        for p in self._operations:
            if isinstance(p, self.TaxRuleOperation):
                tax_rules[p.position.pk] = p.tax_rule
            elif isinstance(p, self.ItemOperation):
                tax_rules[p.position.pk] = p.item.tax_rule
        return tax_rules

    def recalculate_taxes(self, keep='net'):
        positions = self.order.positions.select_related('item', 'item__tax_rule')
        ia = self._invoice_address
        tax_rules = self._current_tax_rules()
        self._operations.append(self.ForceRecomputeOperation())

        for pos in positions:
            tax_rule = tax_rules.get(pos.pk, pos.tax_rule)
            if not tax_rule:
                continue
            if not pos.price:
                continue

            try:
                new_rate = tax_rule.tax_rate_for(ia)
                new_code = tax_rule.tax_code_for(ia)
            except TaxRule.SaleNotAllowed:
                raise OrderError(error_messages['tax_rule_country_blocked'])
            # We use override_tax_rate to make sure .tax() doesn't get clever and re-adjusts the pricing itself
            if new_rate != pos.tax_rate or new_code != pos.tax_code:
                if keep == 'net':
                    new_tax = tax_rule.tax(pos.price - pos.tax_value, base_price_is='net', currency=self.event.currency,
                                           override_tax_rate=new_rate, override_tax_code=new_code)
                else:
                    new_tax = tax_rule.tax(pos.price, base_price_is='gross', currency=self.event.currency,
                                           override_tax_rate=new_rate, override_tax_code=new_code)
                self._totaldiff_guesstimate += new_tax.gross - pos.price
                self._operations.append(self.PriceOperation(pos, new_tax, new_tax.gross - pos.price))
                self._invoice_dirty = True

    def cancel_fee(self, fee: OrderFee):
        self._totaldiff_guesstimate -= fee.value
        self._operations.append(self.CancelFeeOperation(fee, -fee.value))
        self._invoice_dirty = True

    def add_fee(self, fee: OrderFee):
        self._totaldiff_guesstimate += fee.value
        self._invoice_dirty = True
        self._operations.append(self.AddFeeOperation(fee, fee.value))

    def change_fee(self, fee: OrderFee, value: Decimal):
        value = (fee.tax_rule or TaxRule.zero()).tax(value, base_price_is='gross', invoice_address=self._invoice_address,
                                                     force_fixed_gross_price=True)
        self._totaldiff_guesstimate += value.gross - fee.value
        self._invoice_dirty = True
        self._operations.append(self.FeeValueOperation(fee, value, value.gross - fee.value))

    def cancel(self, position: OrderPosition):
        self._totaldiff_guesstimate -= position.price
        self._quotadiff.subtract(position.quotas)
        self._operations.append(self.CancelOperation(position, -position.price))
        if position.seat:
            self._seatdiff.subtract([position.seat])

        if self.order.event.settings.invoice_include_free or position.price != Decimal('0.00'):
            self._invoice_dirty = True

    def add_position(self, item: Item, variation: ItemVariation, price: Decimal, addon_to: OrderPosition = None,
                     subevent: SubEvent = None, seat: Seat = None, membership: Membership = None,
                     valid_from: datetime = None, valid_until: datetime = None) -> 'OrderChangeManager.AddPositionResult':
        if isinstance(seat, str):
            if not seat:
                seat = None
            else:
                try:
                    seat = Seat.objects.get(
                        event=self.event,
                        subevent=subevent,
                        seat_guid=seat
                    )
                except Seat.DoesNotExist:
                    raise OrderError(error_messages['seat_invalid'])

        try:
            if price is None:
                price = get_price(item, variation, subevent=subevent, invoice_address=self._invoice_address)
            elif not isinstance(price, TaxedPrice):
                price = item.tax(price, base_price_is='gross', invoice_address=self._invoice_address,
                                 force_fixed_gross_price=True)
        except TaxRule.SaleNotAllowed:
            raise OrderError(self.error_messages['tax_rule_country_blocked'])

        is_bundled = False
        if price is None:
            raise OrderError(self.error_messages['product_invalid'])
        if item.variations.exists() and not variation:
            raise OrderError(self.error_messages['product_without_variation'])
        if not addon_to and item.category and item.category.is_addon:
            raise OrderError(self.error_messages['addon_to_required'])
        if addon_to:
            if not item.category or item.category_id not in addon_to.item.addons.values_list('addon_category', flat=True):
                if addon_to.item.bundles.filter(bundled_item=item, bundled_variation=variation).exists():
                    is_bundled = True
                else:
                    raise OrderError(self.error_messages['addon_invalid'])
        if self.order.event.has_subevents and not subevent:
            raise OrderError(self.error_messages['subevent_required'])

        seated = item.seat_category_mappings.filter(subevent=subevent).exists()
        if seated and not seat and self.event.settings.seating_choice:
            raise OrderError(self.error_messages['seat_required'])
        elif not seated and seat:
            raise OrderError(self.error_messages['seat_forbidden'])
        if seat and subevent and seat.subevent_id != subevent.pk:
            raise OrderError(self.error_messages['seat_subevent_mismatch'].format(seat=seat.name))

        new_quotas = (variation.quotas.filter(subevent=subevent)
                      if variation else item.quotas.filter(subevent=subevent))
        if not new_quotas:
            raise OrderError(self.error_messages['quota_missing'])

        if self.order.event.settings.invoice_include_free or price.gross != Decimal('0.00'):
            self._invoice_dirty = True

        self._totaldiff_guesstimate += price.gross
        self._quotadiff.update(new_quotas)
        if seat:
            self._seatdiff.update([seat])

        result = self.AddPositionResult()
        self._operations.append(self.AddOperation(item, variation, price, addon_to, subevent, seat, membership,
                                                  valid_from, valid_until, is_bundled, result))
        return result

    def split(self, position: OrderPosition):
        if self.order.event.settings.invoice_include_free or position.price != Decimal('0.00'):
            self._invoice_dirty = True

        self._operations.append(self.SplitOperation(position))
        for a in position.addons.all():
            self._operations.append(self.SplitOperation(a))

    def set_addons(self, addons, limit_main_positions=None):
        """
        This is a convenience method to change the add-on products selected on an order. The input structure is similar
        to CartManager.set_addons. It will automatically compute the correct operations to add, cancel, or change
        positions on the order. Every existing add-on not in the input will be canceled. Availability of the
        products is validated (with some exceptions).

        :param addons: A list of dictionaries with the keys ``"addon_to"``, ``"item"``, ``"variation"`` (all ID values),
                       ``"count"``, and ``"price"``.
        :param limit_main_positions: By default, the method works on all methods of the order. If you set this to a
                                     queryset or a list of positions, all other positions and their add-ons will be kept
                                     untouched.
        """
        if self._operations:
            raise ValueError("Setting addons should be the first/only operation")

        # Prepare containers for min/max check of products
        item_counts = Counter()
        for p in self.order.positions.all():
            item_counts[p.item] += 1

        # Prepare various containers to hold data later
        current_addons = defaultdict(lambda: defaultdict(list))  # OrderPos -> currently attached add-ons
        input_addons = defaultdict(Counter)  # OrderPos -> final desired set of add-ons
        selected_addons = defaultdict(Counter)  # OrderPos, ItemAddOn -> final desired set of add-ons
        opcache = {}  # OrderPos.pk -> OrderPos
        quota_diff = Counter()  # Quota -> Number of usages
        available_categories = defaultdict(set)  # OrderPos -> Category IDs to choose from
        price_included = defaultdict(dict)  # OrderPos -> CategoryID -> bool(price is included)
        if isinstance(limit_main_positions, QuerySet):
            toplevel_qs = limit_main_positions
        elif limit_main_positions is not None:
            toplevel_qs = self.order.positions.filter(pk__in=[p.pk for p in limit_main_positions])
        else:
            toplevel_qs = self.order.positions
        toplevel_op = toplevel_qs.filter(
            addon_to__isnull=True
        ).prefetch_related(
            'addons', 'item__addons', 'item__addons__addon_category'
        ).select_related('item', 'variation')

        _items_cache = {
            i.pk: i
            for i in self.event.items.select_related('category').prefetch_related(
                'addons', 'bundles', 'addons__addon_category', 'quotas'
            ).annotate(
                has_variations=Count('variations'),
            ).filter(
                id__in=[a['item'] for a in addons]
            ).order_by()
        }
        _variations_cache = {
            v.pk: v
            for v in ItemVariation.objects.filter(item__event=self.event).prefetch_related(
                'quotas'
            ).select_related('item', 'item__event').filter(
                id__in=[a['variation'] for a in addons if a.get('variation')]
            ).order_by()
        }

        # Prefill some of the cache containers
        for op in toplevel_op:
            if op.canceled:
                continue
            available_categories[op.pk] = {iao.addon_category_id for iao in op.item.addons.all()}
            price_included[op.pk] = {iao.addon_category_id: iao.price_included for iao in op.item.addons.all()}
            opcache[op.pk] = op
            for a in op.addons.all():
                if a.canceled:
                    continue

                if not a.is_bundled:
                    current_addons[op][a.item_id, a.variation_id].append(a)

        # Create operations, perform various checks
        for a in addons:
            # Check whether the specified items are part of what we just fetched from the database
            # If they are not, the user supplied item IDs which either do not exist or belong to
            # a different event
            if a['item'] not in _items_cache or (a['variation'] and a['variation'] not in _variations_cache):
                raise OrderError(error_messages['not_for_sale'])

            # Only attach addons to things that are actually in this user's cart
            if a['addon_to'] not in opcache:
                raise OrderError(error_messages['addon_invalid_base'])

            op = opcache[a['addon_to']]
            item = _items_cache[a['item']]
            subevent = op.subevent  # for now, we might lift this requirement later
            variation = _variations_cache[a['variation']] if a['variation'] is not None else None

            if item.category_id not in available_categories[op.pk]:
                raise OrderError(error_messages['addon_invalid_base'])

            # Fetch all quotas. If there are no quotas, this item is not allowed to be sold.
            quotas = list(item.quotas.filter(subevent=subevent)
                          if variation is None else variation.quotas.filter(subevent=subevent))
            if not quotas:
                raise OrderError(error_messages['unavailable'])

            if (a['item'], a['variation']) in input_addons[op.id]:
                raise OrderError(error_messages['addon_duplicate_item'])

            if item.require_voucher or item.hide_without_voucher or (variation and variation.hide_without_voucher):
                raise OrderError(error_messages['voucher_required'])

            if not item.is_available() or (variation and not variation.is_available()):
                raise OrderError(error_messages['unavailable'])

            if not item.all_sales_channels:
                if self.order.sales_channel.identifier not in (s.identifier for s in item.limit_sales_channels.all()):
                    raise OrderError(error_messages['unavailable'])

            if variation and not variation.all_sales_channels:
                if self.order.sales_channel.identifier not in (s.identifier for s in variation.limit_sales_channels.all()):
                    raise OrderError(error_messages['unavailable'])

            if subevent and item.pk in subevent.item_overrides and not subevent.item_overrides[item.pk].is_available():
                raise OrderError(error_messages['not_for_sale'])

            if subevent and variation and variation.pk in subevent.var_overrides and \
                    not subevent.var_overrides[variation.pk].is_available():
                raise OrderError(error_messages['not_for_sale'])

            if item.has_variations and not variation:
                raise OrderError(error_messages['not_for_sale'])

            if variation and variation.item_id != item.pk:
                raise OrderError(error_messages['not_for_sale'])

            if subevent and subevent.presale_start and now() < subevent.presale_start:
                raise OrderError(error_messages['not_started'])

            if (subevent and subevent.presale_has_ended) or self.event.presale_has_ended:
                raise OrderError(error_messages['ended'])

            if item.require_bundling:
                raise OrderError(error_messages['unavailable'])

            input_addons[op.id][a['item'], a['variation']] = a.get('count', 1)
            selected_addons[op.id, item.category_id][a['item'], a['variation']] = a.get('count', 1)

            if price_included[op.pk].get(item.category_id) or (op.voucher_id and op.voucher.all_addons_included):
                price = TAXED_ZERO
            else:
                price = get_price(
                    item, variation, voucher=None, custom_price=a.get('price'), subevent=op.subevent,
                    custom_price_is_net=self.event.settings.display_net_prices,
                    invoice_address=self._invoice_address,
                )

            if a.get('count', 1) > len(current_addons[op][a['item'], a['variation']]):
                # This add-on is new, add it to the cart
                for quota in quotas:
                    quota_diff[quota] += a.get('count', 1) - len(current_addons[op][a['item'], a['variation']])

                for i in range(a.get('count', 1) - len(current_addons[op][a['item'], a['variation']])):
                    self.add_position(
                        item=item, variation=variation, price=price,
                        addon_to=op, subevent=op.subevent, seat=None,
                    )
                    item_counts[item] += 1

        # Detect removed add-ons and create RemoveOperations
        for cp, al in list(current_addons.items()):
            for k, v in al.items():
                input_num = input_addons[cp.id].get(k, 0)
                current_num = len(current_addons[cp].get(k, []))
                if input_num < current_num:
                    for a in current_addons[cp][k][:current_num - input_num]:
                        if a.canceled:
                            continue
                        is_unavailable = (
                            # If an item is no longer available due to time, it should usually also be no longer
                            # user-removable, because e.g. the stock has already been ordered.
                            # We always pass has_voucher=True because if a product now requires a voucher, it usually does
                            # not mean it should be unremovable for others.
                            # This also prevents accidental removal through the UI because a hidden product will no longer
                            # be part of the input.
                            (a.variation and a.variation.unavailability_reason(has_voucher=True, subevent=a.subevent))
                            or (a.variation and not a.variation.all_sales_channels and not a.variation.limit_sales_channels.contains(self.order.sales_channel))
                            or a.item.unavailability_reason(has_voucher=True, subevent=a.subevent)
                            or (
                                not a.item.all_sales_channels and
                                not a.item.limit_sales_channels.contains(self.order.sales_channel)
                            )
                        )
                        if is_unavailable:
                            # "Re-select" add-on
                            selected_addons[cp.id, a.item.category_id][a.item_id, a.variation_id] += 1
                            continue
                        if a.checkins.filter(list__consider_tickets_used=True).exists():
                            raise OrderError(
                                error_messages['addon_already_checked_in'] % {
                                    'addon': str(a.item.name),
                                }
                            )
                        self.cancel(a)
                        item_counts[a.item] -= 1

        # Check constraints on the add-on combinations
        for op in toplevel_op:
            item = op.item
            for iao in item.addons.all():
                selected = selected_addons[op.id, iao.addon_category_id]
                n_per_i = Counter()
                for (i, v), c in selected.items():
                    n_per_i[i] += c
                if sum(selected.values()) > iao.max_count:
                    raise OrderError(
                        error_messages['addon_max_count'] % {
                            'base': str(item.name),
                            'max': iao.max_count,
                            'cat': str(iao.addon_category.name),
                        }
                    )
                elif sum(selected.values()) < iao.min_count:
                    raise OrderError(
                        error_messages['addon_min_count'] % {
                            'base': str(item.name),
                            'min': iao.min_count,
                            'cat': str(iao.addon_category.name),
                        }
                    )
                elif any(v > 1 for v in n_per_i.values()) and not iao.multi_allowed:
                    raise OrderError(
                        error_messages['addon_no_multi'] % {
                            'base': str(item.name),
                            'cat': str(iao.addon_category.name),
                        }
                    )

        for item, count in item_counts.items():
            if count == 0:
                continue

            if item.max_per_order and count > item.max_per_order:
                raise OrderError(
                    self.error_messages['max_items_per_product'] % {
                        'max': item.max_per_order,
                        'product': item.name
                    }
                )

            if item.min_per_order and count < item.min_per_order:
                raise OrderError(
                    self.error_messages['min_items_per_product'] % {
                        'min': item.min_per_order,
                        'product': item.name
                    }
                )

    def _check_seats(self):
        """Delegate to OrderChangeValidator."""
        self._validator.check_seats()

    def _check_quotas(self):
        """Delegate to OrderChangeValidator."""
        self._validator.check_quotas()

    def _check_paid_price_change(self, totaldiff):
        """Delegate to OrderChangeValidator."""
        self._validator.check_paid_price_change(totaldiff)

    def _check_paid_to_free(self, totaldiff):
        """Delegate to OrderChangeValidator."""
        self._validator.check_paid_to_free(totaldiff)

    def _check_order_size(self):
        """Delegate to OrderChangeValidator."""
        self._validator.check_order_size()

    def _check_complete_cancel(self):
        """Delegate to OrderChangeValidator."""
        self._validator.check_complete_cancel()

    def _check_and_lock_memberships(self):
        """Delegate to OrderChangeValidator."""
        self._validator.check_and_lock_memberships()

    def _create_locks(self):
        """Delegate to OrderChangeValidator."""
        self._validator.create_locks()

    # --- Delegating _perform_operations to executor ---
    def _perform_operations(self):
        """Delegate to OrderOperationExecutor."""
        self._executor.perform_operations()

    # --- Delegating fee/invoice methods to fee_manager ---
    def _recalculate_rounding_total_and_payment_fee(self):
        """Delegate to OrderFeeManager."""
        return self._fee_manager.recalculate_rounding_total_and_payment_fee()

    def _reissue_invoice(self):
        """Delegate to OrderFeeManager."""
        self._fee_manager.reissue_invoice()

    def _clear_tickets_cache(self):
        """Delegate to OrderFeeManager."""
        self._fee_manager.clear_tickets_cache()

    def _get_payment_provider(self):
        """Delegate to OrderFeeManager."""
        return self._fee_manager.get_payment_provider()

    @cached_property
    def open_payment(self):
        lp = self.order.payments.last()
        if lp and lp.state not in (OrderPayment.PAYMENT_STATE_CONFIRMED,
                                   OrderPayment.PAYMENT_STATE_REFUNDED):
            return lp

    @cached_property
    def completed_payment_sum(self):
        payment_sum = self.order.payments.filter(
            state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED)
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        refund_sum = self.order.refunds.filter(
            state__in=(OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_TRANSIT, OrderRefund.REFUND_STATE_DONE)
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        return payment_sum - refund_sum

    @property
    def _invoice_address(self):
        try:
            return self.order.invoice_address
        except InvoiceAddress.DoesNotExist:
            return None

    def guess_totaldiff(self):
        """
        Return the estimated difference of ``order.total`` based on the currently queued operations. This is only
        a guess since it does not account for (a) tax rounding or (b) payment fee changes.
        """
        return self._totaldiff_guesstimate

    def commit(self, check_quotas=True):
        if self._committed:
            # an order change can only be committed once
            raise OrderError(error_messages['internal'])
        self._committed = True

        if not self._operations:
            # Do nothing
            return

        # Clear prefetched objects cache of order. We're going to modify the positions and fees and we have no guarantee
        # that every operation tuple points to a position/fee instance that has been fetched from the same object cache,
        # so it's dangerous to keep the cache around.
        self.order._prefetched_objects_cache = {}

        self._check_order_size()

        with transaction.atomic():
            locked_instance = Order.objects.select_for_update(of=OF_SELF).get(pk=self.order.pk)
            if locked_instance.last_modified != self.order.last_modified:
                raise OrderError(error_messages['race_condition'])

            original_total = self.order.total
            if self.order.status in (Order.STATUS_PENDING, Order.STATUS_PAID):
                if check_quotas:
                    self._check_quotas()
                self._check_seats()
            self._create_locks()
            self._check_complete_cancel()
            self._check_and_lock_memberships()
            try:
                self._perform_operations()
            except TaxRule.SaleNotAllowed:
                raise OrderError(self.error_messages['tax_rule_country_blocked'])
            new_total = self._recalculate_rounding_total_and_payment_fee()
            totaldiff = new_total - original_total
            self._check_paid_price_change(totaldiff)
            self._check_paid_to_free(totaldiff)
            if self.order.status in (Order.STATUS_PENDING, Order.STATUS_PAID):
                self._reissue_invoice()
            self._clear_tickets_cache()
            self.order.touch()
            self.order.create_transactions()
            if self.split_order:
                self.split_order.create_transactions()

        transmit_invoices_task = [i for i in self._invoices if invoice_transmission_separately(i)]
        transmit_invoices_mail = [
            i for i in self._invoices
            if i not in transmit_invoices_task and self.event.settings.invoice_email_attachment and self.order.email
        ]

        if self.split_order:
            split_invoices = list(self.split_order.invoices.all())
            transmit_invoices_task += [
                i for i in split_invoices if invoice_transmission_separately(i)
            ]
            split_transmit_invoices_mail = [
                i for i in split_invoices
                if i not in transmit_invoices_task and self.event.settings.invoice_email_attachment and self.order.email
            ]

        if self.notify:
            notify_user_changed_order(
                self.order, self.user, self.auth,
                transmit_invoices_mail,
            )
            if self.split_order:
                notify_user_changed_order(
                    self.split_order, self.user, self.auth,
                    split_transmit_invoices_mail,
                )

        for i in transmit_invoices_task:
            transmit_invoice.apply_async(args=(self.event.pk, i.pk, False))

        order_changed.send(self.order.event, order=self.order)

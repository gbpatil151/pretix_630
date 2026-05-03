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
OrderOperationExecutor
======================

Executes individual order-change operations (item changes, price changes,
cancellations, additions, splits, etc.) that were previously embedded
inside the OrderChangeManager God Object.
"""

import json
import logging
from decimal import Decimal

from django.db.models import F, Max
from django.db.models.functions import Greatest
from django.utils.timezone import now
from django.utils.translation import gettext as _

from pretix.base.models import Order, OrderPayment, OrderPosition, Voucher
from pretix.base.models.orders import (
    BlockedTicketSecret, InvoiceAddress, OrderFee, OrderRefund,
    generate_secret,
)
from pretix.base.secrets import assign_ticket_secret
from pretix.base.services import tickets
from pretix.base.services.invoices import generate_invoice
from pretix.base.services.orders import (
    OrderError, _calculate_voucher_budget_use,
    _reverse_issued_gift_cards_for_line,
)
from pretix.base.services.pricing import apply_rounding
from pretix.base.signals import order_split
from pretix.helpers.models import modelcopy

logger = logging.getLogger(__name__)


class OrderOperationExecutor:
    """Executes queued order-change operations against the database.

    Each public method corresponds to one former ``_perform_order_change_*``
    method on the old OrderChangeManager God Object.
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def perform_operations(self):
        nextposid = self._ctx.order.all_positions.aggregate(m=Max('positionid'))['m'] + 1
        split_positions = []
        secret_dirty = set()
        position_cache = {}
        fee_cache = {}

        for op in self._ctx._operations:
            if isinstance(op, self._ctx.ItemOperation):
                self._perform_order_change_item_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.MembershipOperation):
                self._perform_order_change_membership_operation(op, position_cache)
            elif isinstance(op, self._ctx.SeatOperation):
                self._perform_order_change_seat_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.SubeventOperation):
                self._perform_order_change_subevent_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.AddFeeOperation):
                self._perform_order_change_add_fee_operation(op)
            elif isinstance(op, self._ctx.FeeValueOperation):
                self._perform_order_change_fee_value_operation(op, fee_cache)
            elif isinstance(op, self._ctx.PriceOperation):
                self._perform_order_change_price_operation(op, position_cache)
            elif isinstance(op, self._ctx.TaxRuleOperation):
                self._perform_order_change_tax_rule_operation(op, position_cache, fee_cache)
            elif isinstance(op, self._ctx.CancelFeeOperation):
                self._perform_order_change_cancel_fee_operation(op, fee_cache)
            elif isinstance(op, self._ctx.CancelOperation):
                self._perform_order_change_cancel_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.AddOperation):
                nextposid = self._perform_order_change_add_operation(op, nextposid)
            elif isinstance(op, self._ctx.SplitOperation):
                self._perform_order_change_split_operation(op, position_cache, split_positions)
            elif isinstance(op, self._ctx.RegenerateSecretOperation):
                self._perform_order_change_regenerate_secret_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.ChangeSecretOperation):
                self._perform_order_change_change_secret_operation(op, secret_dirty)
            elif isinstance(op, self._ctx.ChangeValidFromOperation):
                self._perform_order_change_valid_from_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.ChangeValidUntilOperation):
                self._perform_order_change_valid_until_operation(op, position_cache, secret_dirty)
            elif isinstance(op, self._ctx.AddBlockOperation):
                self._perform_order_change_add_block_operation(op, position_cache)
            elif isinstance(op, self._ctx.RemoveBlockOperation):
                self._perform_order_change_remove_block_operation(op, position_cache)
            elif isinstance(op, self._ctx.ForceRecomputeOperation):
                self._perform_order_change_force_recompute_operation()
            else:
                raise TypeError(f"Unknown operation {type(op)}")

        for p in secret_dirty:
            assign_ticket_secret(
                event=self._ctx.event, position=p, force_invalidate=False, save=True
            )

        if split_positions:
            self._ctx.split_order = self._create_split_order(split_positions)

    def _perform_order_change_item_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.item', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_item': position.item.pk,
            'old_variation': position.variation.pk if position.variation else None,
            'new_item': op.item.pk,
            'new_variation': op.variation.pk if op.variation else None,
            'old_price': position.price,
            'addon_to': position.addon_to_id,
            'new_price': position.price
        })
        position.item = op.item
        position.variation = op.variation
        position._calculate_tax()

        _calculate_voucher_budget_use(position)
        secret_dirty.add(position)
        position.save()

    def _perform_order_change_membership_operation(self, op, position_cache):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.membership', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_membership_id': position.used_membership_id,
            'new_membership_id': op.membership.pk if op.membership else None,
        })
        position.used_membership = op.membership
        position.save()

    def _perform_order_change_seat_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.seat', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_seat': position.seat.name if position.seat else "-",
            'new_seat': op.seat.name if op.seat else "-",
            'old_seat_id': position.seat.pk if position.seat else None,
            'new_seat_id': op.seat.pk if op.seat else None,
        })
        position.seat = op.seat
        secret_dirty.add(position)
        position.save()

    def _perform_order_change_subevent_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.subevent', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_subevent': position.subevent.pk,
            'new_subevent': op.subevent.pk,
            'old_price': position.price,
            'new_price': position.price
        })
        position.subevent = op.subevent
        secret_dirty.add(position)
        _calculate_voucher_budget_use(position)
        position.save()

    def _perform_order_change_add_fee_operation(self, op):
        self._ctx.order.log_action('pretix.event.order.changed.addfee', user=self._ctx.user, auth=self._ctx.auth, data={
            'fee': op.fee.pk,
        })
        op.fee.order = self._ctx.order
        op.fee._calculate_tax()
        op.fee.save()

    def _perform_order_change_fee_value_operation(self, op, fee_cache):
        fee = fee_cache.setdefault(op.fee.pk, op.fee)
        self._ctx.order.log_action('pretix.event.order.changed.feevalue', user=self._ctx.user, auth=self._ctx.auth, data={
            'fee': fee.pk,
            'old_price': fee.value,
            'new_price': op.value.gross
        })
        fee.value = op.value.gross
        fee._calculate_tax()
        fee.save()

    def _perform_order_change_price_operation(self, op, position_cache):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.price', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_price': position.price,
            'addon_to': position.addon_to_id,
            'new_price': op.price.gross
        })
        position.price = op.price.gross
        position.price_includes_rounding_correction = Decimal("0.00")
        position.tax_rate = op.price.rate
        position.tax_value = op.price.tax
        position.tax_value_includes_rounding_correction = Decimal("0.00")
        position.tax_code = op.price.code
        position.save(update_fields=[
            'price', 'price_includes_rounding_correction', 'tax_rate', 'tax_value',
            'tax_value_includes_rounding_correction', 'tax_code'
        ])

    def _perform_order_change_tax_rule_operation(self, op, position_cache, fee_cache):
        if isinstance(op.position, OrderPosition):
            position = position_cache.setdefault(op.position.pk, op.position)
            self._ctx.order.log_action('pretix.event.order.changed.tax_rule', user=self._ctx.user, auth=self._ctx.auth, data={
                'position': position.pk,
                'positionid': position.positionid,
                'addon_to': position.addon_to_id,
                'old_taxrule': position.tax_rule.pk if position.tax_rule else None,
                'new_taxrule': op.tax_rule.pk
            })
            position._calculate_tax(op.tax_rule)
            position.save()
        elif isinstance(op.position, OrderFee):
            fee = fee_cache.setdefault(op.position.pk, op.position)
            self._ctx.order.log_action('pretix.event.order.changed.tax_rule', user=self._ctx.user, auth=self._ctx.auth, data={
                'fee': fee.pk,
                'fee_type': fee.fee_type,
                'old_taxrule': fee.tax_rule.pk if fee.tax_rule else None,
                'new_taxrule': op.tax_rule.pk
            })
            fee._calculate_tax(op.tax_rule)
            fee.save()

    def _perform_order_change_cancel_fee_operation(self, op, fee_cache):
        fee = fee_cache.setdefault(op.fee.pk, op.fee)
        self._ctx.order.log_action('pretix.event.order.changed.cancelfee', user=self._ctx.user, auth=self._ctx.auth, data={
            'fee': fee.pk,
            'fee_type': fee.fee_type,
            'old_price': fee.value,
        })
        fee.canceled = True
        fee.save(update_fields=['canceled'])

    def _perform_order_change_cancel_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        _reverse_issued_gift_cards_for_line(
            position,
            order=self._ctx.order,
            line_price=position.price,
            not_redeemed_message=_(
                'A position can not be canceled since the gift card {card} purchased in this order has '
                'already been redeemed.'
            ),
            user=self._ctx.user,
            auth=self._ctx.auth,
        )

        for m in position.granted_memberships.with_usages().all():
            m.canceled = True
            m.save()

        for opa in position.addons.all():
            opa = position_cache.setdefault(opa.pk, opa)
            _reverse_issued_gift_cards_for_line(
                opa,
                order=self._ctx.order,
                line_price=opa.price,
                not_redeemed_message=_(
                    'A position can not be canceled since the gift card {card} purchased in this order has '
                    'already been redeemed.'
                ),
                user=self._ctx.user,
                auth=self._ctx.auth,
            )

            for m in opa.granted_memberships.with_usages().all():
                m.canceled = True
                m.save()

            self._ctx.order.log_action('pretix.event.order.changed.cancel', user=self._ctx.user, auth=self._ctx.auth, data={
                'position': opa.pk,
                'positionid': opa.positionid,
                'old_item': opa.item.pk,
                'old_variation': opa.variation.pk if opa.variation else None,
                'addon_to': opa.addon_to_id,
                'old_price': opa.price,
            })
            opa.canceled = True
            if opa.voucher:
                Voucher.objects.filter(pk=opa.voucher.pk).update(redeemed=Greatest(0, F('redeemed') - 1))
            if opa in secret_dirty:
                secret_dirty.remove(opa)
            assign_ticket_secret(
                event=self._ctx.event, position=opa, force_invalidate_if_revokation_list_used=True, force_invalidate=False, save=False
            )
            opa.save(update_fields=['canceled', 'secret'])
        self._ctx.order.log_action('pretix.event.order.changed.cancel', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'old_item': position.item.pk,
            'old_variation': position.variation.pk if position.variation else None,
            'old_price': position.price,
            'addon_to': None,
        })
        position.canceled = True
        if position.voucher:
            Voucher.objects.filter(pk=position.voucher.pk).update(redeemed=Greatest(0, F('redeemed') - 1))
        assign_ticket_secret(
            event=self._ctx.event, position=position, force_invalidate_if_revokation_list_used=True, force_invalidate=False, save=False
        )
        if position in secret_dirty:
            secret_dirty.remove(position)
        position.save(update_fields=['canceled', 'secret'])

    def _perform_order_change_add_operation(self, op, nextposid):
        pos = OrderPosition.objects.create(
            item=op.item, variation=op.variation, addon_to=op.addon_to,
            price=op.price.gross, order=self._ctx.order, tax_rate=op.price.rate, tax_code=op.price.code,
            tax_value=op.price.tax, tax_rule=op.item.tax_rule,
            positionid=nextposid, subevent=op.subevent, seat=op.seat,
            used_membership=op.membership, valid_from=op.valid_from, valid_until=op.valid_until,
            is_bundled=op.is_bundled,
        )
        nextposid += 1
        self._ctx.order.log_action('pretix.event.order.changed.add', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': pos.pk,
            'item': op.item.pk,
            'variation': op.variation.pk if op.variation else None,
            'addon_to': op.addon_to.pk if op.addon_to else None,
            'price': op.price.gross,
            'positionid': pos.positionid,
            'membership': pos.used_membership_id,
            'subevent': op.subevent.pk if op.subevent else None,
            'seat': op.seat.pk if op.seat else None,
            'valid_from': op.valid_from.isoformat() if op.valid_from else None,
            'valid_until': op.valid_until.isoformat() if op.valid_until else None,
        })
        op.result._position = pos
        return nextposid

    def _perform_order_change_split_operation(self, op, position_cache, split_positions):
        position = position_cache.setdefault(op.position.pk, op.position)
        split_positions.append(position)

    def _perform_order_change_regenerate_secret_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        position.web_secret = generate_secret()
        position.save(update_fields=["web_secret"])
        assign_ticket_secret(
            event=self._ctx.event, position=position, force_invalidate=True, save=True
        )
        if position in secret_dirty:
            secret_dirty.remove(position)
        tickets.invalidate_cache.apply_async(kwargs={'event': self._ctx.event.pk,
                                                     'order': self._ctx.order.pk})
        self._ctx.order.log_action('pretix.event.order.changed.secret', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
        })

    def _perform_order_change_change_secret_operation(self, op, secret_dirty):
        if OrderPosition.all.filter(order__event=self._ctx.event, secret=op.new_secret).exists():
            raise OrderError('You cannot assign a position secret that already exists.')
        op.position.secret = op.new_secret
        op.position.save(update_fields=["secret"])
        if op.position in secret_dirty:
            secret_dirty.remove(op.position)
        tickets.invalidate_cache.apply_async(kwargs={'event': self._ctx.event.pk,
                                                     'order': self._ctx.order.pk})
        self._ctx.order.log_action('pretix.event.order.changed.secret', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': op.position.pk,
            'positionid': op.position.positionid,
        })

    def _perform_order_change_valid_from_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.valid_from', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'new_value': op.valid_from.isoformat() if op.valid_from else None,
            'old_value': position.valid_from.isoformat() if position.valid_from else None,
        })
        position.valid_from = op.valid_from
        position.save(update_fields=['valid_from'])
        secret_dirty.add(position)

    def _perform_order_change_valid_until_operation(self, op, position_cache, secret_dirty):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.valid_until', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'new_value': op.valid_until.isoformat() if op.valid_until else None,
            'old_value': position.valid_until.isoformat() if position.valid_until else None,
        })
        position.valid_until = op.valid_until
        position.save(update_fields=['valid_until'])
        secret_dirty.add(position)

    def _perform_order_change_add_block_operation(self, op, position_cache):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.add_block', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'block_name': op.block_name,
        })
        if position.blocked:
            if op.block_name not in position.blocked:
                position.blocked = position.blocked + [op.block_name]
        else:
            position.blocked = [op.block_name]
        if op.ignore_from_quota_while_blocked is not None:
            position.ignore_from_quota_while_blocked = op.ignore_from_quota_while_blocked
        position.save(update_fields=['blocked', 'ignore_from_quota_while_blocked'])
        if position.blocked:
            position.blocked_secrets.update_or_create(
                event=self._ctx.event,
                secret=position.secret,
                defaults={
                    'blocked': True,
                    'updated': now(),
                }
            )

    def _perform_order_change_remove_block_operation(self, op, position_cache):
        position = position_cache.setdefault(op.position.pk, op.position)
        self._ctx.order.log_action('pretix.event.order.changed.remove_block', user=self._ctx.user, auth=self._ctx.auth, data={
            'position': position.pk,
            'positionid': position.positionid,
            'block_name': op.block_name,
        })
        if position.blocked and op.block_name in position.blocked:
            position.blocked = [b for b in position.blocked if b != op.block_name]
            if not position.blocked:
                position.blocked = None
            if op.ignore_from_quota_while_blocked is not None:
                position.ignore_from_quota_while_blocked = op.ignore_from_quota_while_blocked
            position.save(update_fields=['blocked', 'ignore_from_quota_while_blocked'])
            if not position.blocked:
                try:
                    bs = position.blocked_secrets.get(secret=position.secret)
                    bs.blocked = False
                    bs.save()
                except BlockedTicketSecret.DoesNotExist:
                    pass
        # todo: revoke list handling

    def _perform_order_change_force_recompute_operation(self):
        self._ctx.order.log_action('pretix.event.order.changed.recomputed', user=self._ctx.user, auth=self._ctx.auth, data={})

    def _create_split_order(self, split_positions):
        split_order = Order.objects.get(pk=self._ctx.order.pk)
        split_order.pk = None
        split_order.code = None
        split_order.datetime = now()
        split_order.secret = generate_secret()
        split_order.require_approval = self._ctx.order.require_approval and any(
            p.requires_approval(invoice_address=self._ctx._invoice_address) for p in split_positions
        )
        split_order.save()
        split_order.log_action('pretix.event.order.changed.split_from', user=self._ctx.user, auth=self._ctx.auth, data={
            'original_order': self._ctx.order.code
        })

        for op in split_positions:
            self._ctx.order.log_action('pretix.event.order.changed.split', user=self._ctx.user, auth=self._ctx.auth, data={
                'position': op.pk,
                'positionid': op.positionid,
                'old_item': op.item.pk,
                'old_variation': op.variation.pk if op.variation else None,
                'old_price': op.price,
                'new_order': split_order.code,
            })
            op.order = split_order
            op.web_secret = generate_secret()
            assign_ticket_secret(
                self._ctx.event, position=op, force_invalidate=True,
            )
            op.save()

        try:
            ia = modelcopy(self._ctx.order.invoice_address)
            ia.pk = None
            ia.order = split_order
            ia.save()
        except InvoiceAddress.DoesNotExist:
            pass

        fees = []
        for fee in self._ctx.order.fees.exclude(fee_type=OrderFee.FEE_TYPE_PAYMENT):
            new_fee = modelcopy(fee)
            new_fee.pk = None
            new_fee.order = split_order
            new_fee.save()
            fees.append(new_fee)

        changed_by_rounding = set(apply_rounding(
            self._ctx.order.tax_rounding_mode,
            self._ctx._invoice_address,
            self._ctx.event.currency,
            [p for p in split_positions if not p.canceled] + fees
        ))
        split_order.total = sum([p.price for p in split_positions if not p.canceled])

        if split_order.total != Decimal('0.00') and self._ctx.order.status != Order.STATUS_PAID:
            pp = self._ctx._get_payment_provider()
            if pp:
                payment_fee = pp.calculate_fee(split_order.total)
            else:
                payment_fee = Decimal('0.00')
            fee = split_order.fees.get_or_create(fee_type=OrderFee.FEE_TYPE_PAYMENT, defaults={'value': 0})[0]
            fee.value = payment_fee
            fee._calculate_tax()
            if payment_fee != 0:
                fee.save()
                fees.append(fee)
            elif fee.pk:
                if fee in fees:
                    fees.remove(fee)
                fee.delete()

            changed_by_rounding |= set(apply_rounding(
                self._ctx.order.tax_rounding_mode,
                self._ctx._invoice_address,
                self._ctx.event.currency,
                [p for p in split_positions if not p.canceled] + fees
            ))
            split_order.total = sum([p.price for p in split_positions if not p.canceled]) + sum([f.value for f in fees])

        for line in changed_by_rounding:
            if isinstance(line, OrderPosition):
                line.save(update_fields=[
                    "price", "price_includes_rounding_correction", "tax_value", "tax_value_includes_rounding_correction"
                ])
            elif isinstance(line, OrderFee):
                line.save(update_fields=[
                    "value", "value_includes_rounding_correction", "tax_value", "tax_value_includes_rounding_correction"
                ])
        split_order.total = sum([p.price for p in split_positions if not p.canceled]) + sum([f.value for f in fees])

        remaining_total = sum([p.price for p in self._ctx.order.positions.all()]) + sum([f.value for f in self._ctx.order.fees.all()])
        offset_amount = min(max(0, self._ctx.completed_payment_sum - remaining_total), split_order.total)
        if offset_amount >= split_order.total and not split_order.require_approval:
            split_order.status = Order.STATUS_PAID
        else:
            split_order.status = Order.STATUS_PENDING
            if self._ctx.order.status == Order.STATUS_PAID:
                split_order.set_expires(
                    now(),
                    list(set(p.subevent_id for p in split_positions))
                )
        split_order.save()

        if offset_amount > Decimal('0.00'):
            split_order.payments.create(
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                amount=offset_amount,
                payment_date=now(),
                provider='offsetting',
                info=json.dumps({'orders': [self._ctx.order.code]})
            )
            self._ctx.order.refunds.create(
                state=OrderRefund.REFUND_STATE_DONE,
                amount=offset_amount,
                execution_date=now(),
                provider='offsetting',
                info=json.dumps({'orders': [split_order.code]})
            )

        if split_order.total != Decimal('0.00') and self._ctx.order.invoices.filter(is_cancellation=False).last():
            try:
                generate_invoice(split_order)
            except Exception as e:
                logger.exception("Could not generate invoice.")
                split_order.log_action("pretix.event.order.invoice.failed", data={
                    "exception": str(e)
                })

        order_split.send(sender=self._ctx.order.event, original=self._ctx.order, split_order=split_order)
        return split_order

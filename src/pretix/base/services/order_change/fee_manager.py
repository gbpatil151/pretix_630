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
OrderFeeManager
===============

Handles fee recalculation, rounding, payment-fee adjustment, invoice
reissuance, and ticket-cache invalidation — all responsibilities formerly
embedded inside the ``OrderChangeManager`` God Object.
"""

import logging
from decimal import Decimal

from pretix.base.models import OrderPosition
from pretix.base.models.orders import OrderFee
from pretix.base.services import tickets
from pretix.base.services.invoices import (
    generate_cancellation, generate_invoice, invoice_qualified,
)
from pretix.base.services.pricing import apply_rounding

logger = logging.getLogger(__name__)


class OrderFeeManager:
    """Encapsulates fee / invoice / rounding logic for an order-change
    transaction.

    Each public method corresponds to one former ``_recalculate_*``,
    ``_reissue_*``, or ``_clear_*`` method on the old God Object.
    """

    def __init__(self, ctx):
        """
        Parameters
        ----------
        ctx : OrderChangeManager
            The facade instance.
        """
        self._ctx = ctx

    # ------------------------------------------------------------------
    # Rounding, total recalculation, and payment-fee adjustment
    # ------------------------------------------------------------------
    def recalculate_rounding_total_and_payment_fee(self):
        """Recompute rounding corrections, adjust the payment fee for the
        current open payment, and persist the new order total.

        Returns the recalculated total.
        """
        positions = list(self._ctx.order.positions.all())
        fees = list(self._ctx.order.fees.all())
        total = sum([p.price for p in positions]) + sum([f.value for f in fees])
        payment_fee = Decimal('0.00')
        fee_changed = False
        if self._ctx.open_payment:
            current_fee = Decimal('0.00')
            fee = None
            if self._ctx.open_payment.fee:
                fee = self._ctx.open_payment.fee
                if any(isinstance(op, (self._ctx.FeeValueOperation, self._ctx.CancelFeeOperation))
                       for op in self._ctx._operations):
                    fee.refresh_from_db()
                if not self._ctx.open_payment.fee.canceled:
                    current_fee = self._ctx.open_payment.fee.value
            total -= current_fee

            if fee and any(isinstance(op, self._ctx.FeeValueOperation) and op.fee == fee
                           for op in self._ctx._operations):
                # Do not automatically modify a fee that is being manually modified right now
                payment_fee = fee.value
            elif fee and any(isinstance(op, self._ctx.CancelFeeOperation) and op.fee == fee
                             for op in self._ctx._operations):
                # Do not automatically modify a fee that is being manually removed right now
                payment_fee = Decimal('0.00')
            elif self._ctx.order.pending_sum - current_fee != 0:
                prov = self._ctx.open_payment.payment_provider
                if prov:
                    payment_fee = prov.calculate_fee(total - self._ctx.completed_payment_sum)

            if payment_fee:
                fee = fee or OrderFee(fee_type=OrderFee.FEE_TYPE_PAYMENT, order=self._ctx.order)
                fee.value = payment_fee
                fee._calculate_tax()
                fee.save()
                fee_changed = True
                if not self._ctx.open_payment.fee:
                    self._ctx.open_payment.fee = fee
                    self._ctx.open_payment.save(update_fields=['fee'])
            elif fee and not fee.canceled:
                fee.delete()
                fee_changed = True

        if fee_changed:
            fees = list(self._ctx.order.fees.all())

        changed = apply_rounding(
            self._ctx.order.tax_rounding_mode,
            self._ctx._invoice_address,
            self._ctx.order.event.currency,
            [*positions, *fees],
        )
        for line in changed:
            if isinstance(line, OrderPosition):
                line.save(update_fields=[
                    "price", "price_includes_rounding_correction",
                    "tax_value", "tax_value_includes_rounding_correction",
                ])
            elif isinstance(line, OrderFee):
                line.save(update_fields=[
                    "value", "value_includes_rounding_correction",
                    "tax_value", "tax_value_includes_rounding_correction",
                ])
        total = sum([p.price for p in positions]) + sum([f.value for f in fees])

        self._ctx.order.total = total
        self._ctx.order.save()
        return total

    # ------------------------------------------------------------------
    # Invoice reissuance
    # ------------------------------------------------------------------
    def reissue_invoice(self):
        """Cancel & regenerate the invoice when the order total or line items
        have changed, respecting the event's invoice-generation settings."""
        i = self._ctx.order.invoices.filter(is_cancellation=False).last()
        if self._ctx.reissue_invoice and self._ctx._invoice_dirty:
            order_now_qualified = invoice_qualified(self._ctx.order)
            invoice_should_be_generated_now = (
                self._ctx.event.settings.invoice_generate == "True" or (
                    self._ctx.event.settings.invoice_generate == "paid" and
                    self._ctx.open_payment is not None and
                    self._ctx.open_payment.payment_provider.requires_invoice_immediately
                ) or (
                    self._ctx.event.settings.invoice_generate == "paid" and
                    self._ctx.order.status == self._ctx.order.STATUS_PAID
                ) or (
                    # Backwards-compatible behaviour
                    self._ctx.event.settings.invoice_generate not in ("True", "paid") and
                    i and
                    not i.canceled
                )
            )
            invoice_should_be_generated_later = not invoice_should_be_generated_now and (
                self._ctx.event.settings.invoice_generate in ("True", "paid")
            )

            if order_now_qualified:
                if invoice_should_be_generated_now:
                    try:
                        if i and not i.canceled:
                            self._ctx._invoices.append(generate_cancellation(i))
                        self._ctx._invoices.append(generate_invoice(self._ctx.order))
                    except Exception as e:
                        logger.exception("Could not generate invoice.")
                        self._ctx.order.log_action("pretix.event.order.invoice.failed", data={
                            "exception": str(e)
                        })
                elif invoice_should_be_generated_later:
                    self._ctx.order.invoice_dirty = True
                    self._ctx.order.save(update_fields=["invoice_dirty"])
            else:
                try:
                    if i and not i.canceled:
                        self._ctx._invoices.append(generate_cancellation(i))
                except Exception as e:
                    logger.exception("Could not generate invoice.")
                    self._ctx.order.log_action("pretix.event.order.invoice.failed", data={
                        "exception": str(e)
                    })

    # ------------------------------------------------------------------
    # Ticket cache invalidation
    # ------------------------------------------------------------------
    def clear_tickets_cache(self):
        """Dispatch async tasks to invalidate cached ticket renderings."""
        tickets.invalidate_cache.apply_async(
            kwargs={'event': self._ctx.event.pk, 'order': self._ctx.order.pk}
        )
        if self._ctx.split_order:
            tickets.invalidate_cache.apply_async(
                kwargs={'event': self._ctx.event.pk, 'order': self._ctx.split_order.pk}
            )

    # ------------------------------------------------------------------
    # Payment provider lookup
    # ------------------------------------------------------------------
    def get_payment_provider(self):
        """Return the payment provider of the last payment, or None."""
        lp = self._ctx.order.payments.last()
        if not lp:
            return None
        pprov = lp.payment_provider
        if not pprov:
            return None
        return pprov

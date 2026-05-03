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
OrderChangeValidator
====================

Handles all validation / checking responsibilities that were previously embedded
inside OrderChangeManager:

- Quota availability checks
- Seat validity checks
- Order-size limit enforcement
- Membership validation & locking
- Complete-cancel guard
- Paid ↔ free price-change transitions
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError

from pretix.base.models import (
    CartPosition, Order, OrderPayment, Quota,
)
from pretix.base.payment import PaymentException
from pretix.base.services.locking import lock_objects
from pretix.base.services.memberships import validate_memberships_in_order
from pretix.base.services.orders import OrderError, error_messages
from pretix.base.services.quotas import QuotaAvailability

from pretix.base.signals import order_canceled


class OrderChangeValidator:
    """Encapsulates all validation logic for an order-change transaction.

    Each public method corresponds to one former ``_check_*`` or ``_create_locks``
    method on the old ``OrderChangeManager`` God Object.
    """

    def __init__(self, ctx):
        """
        Parameters
        ----------
        ctx : OrderChangeManager
            The facade instance — provides access to ``order``, ``event``,
            ``user``, ``auth``, ``_operations``, ``_quotadiff``, ``_seatdiff``,
            ``error_messages``, and related helpers.
        """
        self._ctx = ctx

    # ------------------------------------------------------------------
    # Quota validation
    # ------------------------------------------------------------------
    def check_quotas(self):
        """Verify that every quota touched by the queued operations has
        sufficient remaining capacity."""
        qa = QuotaAvailability()
        qa.queue(*[k for k, v in self._ctx._quotadiff.items() if v > 0])
        qa.compute()
        for quota, diff in self._ctx._quotadiff.items():
            if diff <= 0:
                continue
            avail = qa.results[quota]
            if avail[0] != Quota.AVAILABILITY_OK or (avail[1] is not None and avail[1] < diff):
                raise OrderError(self._ctx.error_messages['quota'].format(name=quota.name))

    # ------------------------------------------------------------------
    # Seat validation
    # ------------------------------------------------------------------
    def check_seats(self):
        """Ensure every newly-assigned seat is still available, and that seat ↔
        subevent pairings remain consistent."""
        for seat, diff in self._ctx._seatdiff.items():
            if diff <= 0:
                continue
            if not seat.is_available(
                sales_channel=self._ctx.order.sales_channel,
                ignore_distancing=True,
                always_allow_blocked=self._ctx.allow_blocked_seats,
            ) or diff > 1:
                raise OrderError(
                    self._ctx.error_messages['seat_unavailable'].format(seat=seat.name)
                )

        if self._ctx.event.has_subevents:
            state = {}
            for p in self._ctx.order.positions.all():
                state[p] = {'seat': p.seat, 'subevent': p.subevent}
            for op in self._ctx._operations:
                if isinstance(op, self._ctx.SeatOperation):
                    state[op.position]['seat'] = op.seat
                elif isinstance(op, self._ctx.SubeventOperation):
                    state[op.position]['subevent'] = op.subevent
            for v in state.values():
                if v['seat'] and v['seat'].subevent_id != v['subevent'].pk:
                    raise OrderError(
                        self._ctx.error_messages['seat_subevent_mismatch'].format(seat=v['seat'].name)
                    )

    # ------------------------------------------------------------------
    # Order-size limit
    # ------------------------------------------------------------------
    def check_order_size(self):
        """Prevent the order from exceeding PRETIX_MAX_ORDER_SIZE."""
        add_count = len([
            op for op in self._ctx._operations
            if isinstance(op, self._ctx.AddOperation)
        ])
        if (len(self._ctx.order.positions.all()) + add_count) > settings.PRETIX_MAX_ORDER_SIZE:
            raise OrderError(
                self._ctx.error_messages['max_order_size'] % {
                    'max': settings.PRETIX_MAX_ORDER_SIZE,
                }
            )

    # ------------------------------------------------------------------
    # Complete-cancel guard
    # ------------------------------------------------------------------
    def check_complete_cancel(self):
        """Ensure the operations do not empty the order entirely (callers
        should cancel the order directly in that case)."""
        current = self._ctx.order.positions.count()
        cancels = sum([
            1 + o.position.addons.filter(canceled=False).count()
            for o in self._ctx._operations
            if isinstance(o, self._ctx.CancelOperation)
        ]) + len([
            o for o in self._ctx._operations
            if isinstance(o, self._ctx.SplitOperation)
        ])
        adds = len([o for o in self._ctx._operations if isinstance(o, self._ctx.AddOperation)])
        if current > 0 and current - cancels + adds < 1:
            raise OrderError(self._ctx.error_messages['complete_cancel'])

    # ------------------------------------------------------------------
    # Paid price-change handling
    # ------------------------------------------------------------------
    def check_paid_price_change(self, totaldiff):
        """Handle status transitions when the total changes on a paid/pending
        order and cancel / adjust open payments accordingly."""
        order = self._ctx.order
        if order.status == Order.STATUS_PAID and totaldiff > 0:
            if order.pending_sum > Decimal('0.00'):
                order.status = Order.STATUS_PENDING
                order.set_expires(
                    __import__('django.utils.timezone', fromlist=['now']).now(),
                    order.event.subevents.filter(
                        id__in=order.positions.values_list('subevent_id', flat=True)
                    ),
                )
                order.save()
        elif order.status in (Order.STATUS_PENDING, Order.STATUS_EXPIRED) and totaldiff < 0:
            if order.pending_sum <= Decimal('0.00') and not order.require_approval:
                order.status = Order.STATUS_PAID
                order.save()
            elif self._ctx.open_payment:
                self._cancel_open_payment()
        elif order.status in (Order.STATUS_PENDING, Order.STATUS_EXPIRED) and totaldiff > 0:
            if self._ctx.open_payment:
                self._cancel_open_payment()

    def _cancel_open_payment(self):
        """Attempt to cancel the current open payment, logging success or
        failure to the order."""
        try:
            self._ctx.open_payment.payment_provider.cancel_payment(self._ctx.open_payment)
            self._ctx.order.log_action(
                'pretix.event.order.payment.canceled',
                {
                    'local_id': self._ctx.open_payment.local_id,
                    'provider': self._ctx.open_payment.provider,
                },
                user=self._ctx.user,
                auth=self._ctx.auth,
            )
        except PaymentException as e:
            self._ctx.order.log_action(
                'pretix.event.order.payment.canceled.failed',
                {
                    'local_id': self._ctx.open_payment.local_id,
                    'provider': self._ctx.open_payment.provider,
                    'error': str(e),
                },
                user=self._ctx.user,
                auth=self._ctx.auth,
            )

    # ------------------------------------------------------------------
    # Paid → free transition
    # ------------------------------------------------------------------
    def check_paid_to_free(self, totaldiff):
        """When the order total drops to zero, either cancel it or create a
        free payment to move it to paid status.  Also handles the split order."""
        order = self._ctx.order

        if self._ctx.event.currency == 'XXX' and order.total + totaldiff > Decimal("0.00"):
            raise OrderError(error_messages['currency_XXX'])

        if order.total == 0 and (totaldiff < 0 or (self._ctx.split_order and self._ctx.split_order.total > 0)) and not order.require_approval:
            if not order.fees.exists() and not order.positions.exists():
                # The order is completely empty now, so we cancel it.
                order.status = Order.STATUS_CANCELED
                order.save(update_fields=['status'])
                order_canceled.send(order.event, order=order)
            elif order.status != Order.STATUS_CANCELED:
                # if the order becomes free, mark it paid using the 'free' provider
                p = order.payments.create(
                    state=OrderPayment.PAYMENT_STATE_CREATED,
                    provider='free',
                    amount=0,
                    fee=None,
                )
                try:
                    p.confirm(send_mail=False, count_waitinglist=False, user=self._ctx.user, auth=self._ctx.auth)
                except Quota.QuotaExceededException:
                    raise OrderError(self._ctx.error_messages['paid_to_free_exceeded'])

        if self._ctx.split_order and self._ctx.split_order.total == 0 and not self._ctx.split_order.require_approval:
            p = self._ctx.split_order.payments.create(
                state=OrderPayment.PAYMENT_STATE_CREATED,
                provider='free',
                amount=0,
                fee=None,
            )
            try:
                p.confirm(send_mail=False, count_waitinglist=False, user=self._ctx.user, auth=self._ctx.auth)
            except Quota.QuotaExceededException:
                raise OrderError(self._ctx.error_messages['paid_to_free_exceeded'])

    # ------------------------------------------------------------------
    # Membership checks
    # ------------------------------------------------------------------
    def check_and_lock_memberships(self):
        """Simulate applying operations to a fake cart and then delegate to
        ``validate_memberships_in_order`` to check membership constraints."""
        fake_cart = []
        positions_to_fake_cart = {}

        for p in self._ctx.order.positions.all():
            cp = CartPosition(
                event=self._ctx.event,
                item=p.item,
                variation=p.variation,
                attendee_name_parts=p.attendee_name_parts,
                used_membership=p.used_membership,
                subevent=p.subevent,
                seat=p.seat,
            )
            fake_cart.append(cp)
            positions_to_fake_cart[p] = cp

        for op in self._ctx._operations:
            if isinstance(op, self._ctx.ItemOperation):
                positions_to_fake_cart[op.position].item = op.item
                positions_to_fake_cart[op.position].variation = op.variation
            elif isinstance(op, self._ctx.SubeventOperation):
                positions_to_fake_cart[op.position].subevent = op.subevent
            elif isinstance(op, self._ctx.SeatOperation):
                positions_to_fake_cart[op.position].seat = op.seat
            elif isinstance(op, self._ctx.MembershipOperation):
                positions_to_fake_cart[op.position].used_membership = op.membership
            elif isinstance(op, self._ctx.ChangeValidFromOperation):
                positions_to_fake_cart[op.position].override_valid_from = op.valid_from
            elif isinstance(op, self._ctx.ChangeValidUntilOperation):
                positions_to_fake_cart[op.position].override_valid_until = op.valid_until
            elif isinstance(op, self._ctx.CancelOperation) and op.position in positions_to_fake_cart:
                fake_cart.remove(positions_to_fake_cart[op.position])
            elif isinstance(op, self._ctx.AddOperation):
                cp = CartPosition(
                    event=self._ctx.event,
                    item=op.item,
                    variation=op.variation,
                    used_membership=op.membership,
                    subevent=op.subevent,
                    seat=op.seat,
                )
                cp.override_valid_from = op.valid_from
                cp.override_valid_until = op.valid_until
                fake_cart.append(cp)

        try:
            validate_memberships_in_order(
                self._ctx.order.customer, fake_cart, self._ctx.event,
                lock=True, ignored_order=self._ctx.order, testmode=self._ctx.order.testmode,
            )
        except ValidationError as e:
            raise OrderError(e.message)

    # ------------------------------------------------------------------
    # Lock acquisition
    # ------------------------------------------------------------------
    def create_locks(self):
        """Acquire fine-grained or event-wide locks depending on whether
        seating-distance enforcement is required."""
        full_lock_required = (
            any(diff > 0 for diff in self._ctx._seatdiff.values())
            and self._ctx.event.settings.seating_minimal_distance > 0
        )
        if full_lock_required:
            lock_objects([self._ctx.event])
        else:
            lock_objects(
                [q for q, d in self._ctx._quotadiff.items() if q.size is not None and d > 0]
                + [s for s, d in self._ctx._seatdiff.items() if d > 0],
                shared_lock_objects=[self._ctx.event],
            )

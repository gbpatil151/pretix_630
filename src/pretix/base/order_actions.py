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
from django.utils.translation import gettext_lazy as _

from pretix.base.logaction import LogActionType, log_action_mediator

order_actions = [
    LogActionType(
        action_type='pretix.event.order.placed',
        display_text=_('The order has been created.'),
        webhook_event=_('New order placed'),
        notification_type=(_('New order placed'), _('A new order has been placed: {order.code}'))
    ),
    LogActionType(
        action_type='pretix.event.order.placed.require_approval',
        display_text=_('The order requires approval before it can continue to be processed.'),
        webhook_event=_('New order requires approval'),
        notification_type=(_('New order requires approval'), _('A new order has been placed that requires approval: {order.code}'))
    ),
    LogActionType(
        action_type='pretix.event.order.paid',
        display_text=_('The order has been marked as paid.'),
        webhook_event=_('Order marked as paid'),
        notification_type=(_('Order marked as paid'), _('Order {order.code} has been marked as paid.'))
    ),
    LogActionType(
        action_type='pretix.event.order.canceled',
        display_text=_('The order has been canceled.'),
        webhook_event=_('Order canceled'),
        notification_type=(_('Order canceled'), _('Order {order.code} has been canceled.'))
    ),
    LogActionType(
        action_type='pretix.event.order.reactivated',
        display_text=_('The order has been reactivated.'),
        webhook_event=_('Order reactivated'),
        notification_type=(_('Order reactivated'), _('Order {order.code} has been reactivated.'))
    ),
    LogActionType(
        action_type='pretix.event.order.expired',
        display_text=_('The order has been marked as expired.'),
        webhook_event=_('Order expired'),
        notification_type=(_('Order expired'), _('Order {order.code} has been marked as expired.'))
    ),
    LogActionType(
        action_type='pretix.event.order.modified',
        display_text=_('The order details have been changed.'),
        webhook_event=_('Order information changed'),
        notification_type=(_('Order information changed'), _('The ticket information of order {order.code} has been changed.'))
    ),
    LogActionType(
        action_type='pretix.event.order.contact.changed',
        display_text=_('The email address has been changed from "{old_email}" to "{new_email}".'),
        webhook_event=_('Order contact address changed'),
        notification_type=(_('Order contact address changed'), _('The contact address of order {order.code} has been changed.'))
    ),
    LogActionType(
        action_type='pretix.event.order.changed.*',
        display_text=None,  # Specific subclasses in logdisplay handle this
        webhook_event=_('Order changed'),
        notification_type=(_('Order changed'), _('Order {order.code} has been changed.'))
    ),
    LogActionType(
        action_type='pretix.event.order.overpaid',
        display_text=None,
        webhook_event=None,
        notification_type=(_('Order has been overpaid'), _('Order {order.code} has been overpaid.'))
    ),
    LogActionType(
        action_type='pretix.event.order.refund.created.externally',
        display_text=None,
        webhook_event=_('External refund of payment'),
        notification_type=(_('External refund of payment'), _('An external refund for {order.code} has occurred.'))
    ),
    LogActionType(
        action_type='pretix.event.order.refund.requested',
        display_text=None,
        webhook_event=_('Refund of payment requested by customer'),
        notification_type=(_('Refund requested'), _('You have been requested to issue a refund for {order.code}.'))
    ),
    LogActionType(
        action_type='pretix.event.order.expirychanged',
        display_text=_('The order\'s expiry date has been changed.'),
        webhook_event=_('Order expiry date changed'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.refund.created',
        display_text=None,
        webhook_event=_('Refund of payment created'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.refund.done',
        display_text=None,
        webhook_event=_('Refund of payment completed'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.refund.canceled',
        display_text=None,
        webhook_event=_('Refund of payment canceled'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.refund.failed',
        display_text=None,
        webhook_event=_('Refund of payment failed'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.payment.confirmed',
        display_text=None,
        webhook_event=_('Payment confirmed'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.approved',
        display_text=_('The order has been approved.'),
        webhook_event=_('Order approved'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.denied',
        display_text=_('The order has been denied (comment: "{comment}").'),
        webhook_event=_('Order denied'),
        notification_type=None
    ),
    LogActionType(
        action_type='pretix.event.order.deleted',
        display_text=_('The test mode order {code} has been deleted.'),
        webhook_event=_('Order deleted'),
        notification_type=None
    )
]

for action in order_actions:
    log_action_mediator.register(action)

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
Order Change Facade Package
============================

This package implements the Facade pattern to decompose the former God Object
``OrderChangeManager`` into focused collaborator classes:

- ``OrderChangeValidator``     — quota, seat, membership, and order-level checks
- ``OrderFeeManager``          — fee recalculation, rounding, invoice reissue
- ``OrderOperationExecutor``   — per-operation DB mutations + split-order creation

The ``OrderChangeManager`` class re-exported here is now a thin Facade that
delegates to these collaborators while preserving the identical public API.
"""

from pretix.base.services.order_change.manager import OrderChangeManager  # noqa: F401

__all__ = ['OrderChangeManager']

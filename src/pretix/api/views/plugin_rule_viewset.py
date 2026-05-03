#
# This file is part of pretix (Community Edition).\n#
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
Template Method base class for plugin Rule API viewsets.

Several pretix plugins (sendmail, autocheckin, etc.) expose a ``RuleViewSet``
that follows an identical CRUD lifecycle:

1. ``get_queryset`` – filter the plugin's Rule model by the current event.
2. ``perform_create`` – save the new rule and log a "rule.added" action.
3. ``perform_update`` – save changes and log a "rule.changed" action.
4. ``perform_destroy`` – log a "rule.deleted" action then delete.

The only differences between plugins are:

* The Django model class (``Rule``, ``AutoCheckinRule``, …)
* The DRF serializer class
* The log-action prefix (``pretix.plugins.sendmail``, ``pretix.plugins.autocheckin``, …)

``BasePluginRuleViewSet`` extracts the shared skeleton into hook methods,
eliminating the duplication. Plugin viewsets subclass it and override only the
three abstract hooks.
"""
from abc import abstractmethod

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets

from pretix.api.pagination import TotalOrderingFilter


class BasePluginRuleViewSet(viewsets.ModelViewSet):
    """
    Template Method base class for plugin rule-management viewsets.

    Subclasses **must** implement:

    * :meth:`get_rule_model` – return the plugin's Rule model class.
    * :meth:`get_serializer_class` – return the plugin's serializer class.
      (Alternatively override ``serializer_class`` directly, but the hook
      keeps the pattern symmetric.)
    * :meth:`get_action_prefix` – return the log-action prefix string
      (e.g. ``'pretix.plugins.sendmail'``).

    Subclasses **may** override:

    * :meth:`get_serializer_context` – to inject extra context into the
      serializer (autocheckin does this for ``event``).
    * Any other DRF viewset attribute (``filter_backends``,
      ``filterset_class``, etc.) as needed.
    """

    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('id',)
    ordering_fields = ('id',)
    permission = 'can_change_event_settings'

    # ------------------------------------------------------------------
    # Abstract hook methods (Template Method pattern)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_rule_model(self):
        """Return the Django model class for this plugin's rules."""
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def get_action_prefix(self):
        """
        Return the log-action prefix string.

        Example: ``'pretix.plugins.sendmail'``
        The CRUD suffixes (``.rule.added``, ``.rule.changed``,
        ``.rule.deleted``) are appended automatically.
        """
        raise NotImplementedError  # pragma: no cover

    # ------------------------------------------------------------------
    # Shared CRUD lifecycle (defined once here, not in every plugin)
    # ------------------------------------------------------------------

    def get_queryset(self):
        return self.get_rule_model().objects.filter(event=self.request.event)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        serializer.instance.log_action(
            f'{self.get_action_prefix()}.rule.added',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data,
        )

    def perform_update(self, serializer):
        super().perform_update(serializer)
        serializer.instance.log_action(
            f'{self.get_action_prefix()}.rule.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data,
        )

    def perform_destroy(self, instance):
        instance.log_action(
            f'{self.get_action_prefix()}.rule.deleted',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data,
        )
        super().perform_destroy(instance)

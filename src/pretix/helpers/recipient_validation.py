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
Factory Method helpers for mail recipient field validation.

Several scheduled-export forms and API serializers share an identical
recipient-cleaning pattern:

1. Strip whitespace from the comma-separated address list.
2. Reject lists with more than ``max_count`` entries.

Previously each form/serializer repeated this logic verbatim in three
separate ``clean_*`` / ``validate_*`` methods (to/cc/bcc), causing six
copies of the same code across two layers.

``make_recipient_validator`` is a **factory function** (Factory Method
pattern) that takes the limit as a parameter and *produces* a ready-to-call
validator. Callers receive a concrete validator object without knowing how it
was constructed.

Usage (Django form)::

    from pretix.helpers.recipient_validation import make_recipient_validator

    _validate_recipients = make_recipient_validator(max_count=25)

    class MyForm(forms.Form):
        def clean_mail_additional_recipients(self):
            return _validate_recipients(
                self.cleaned_data['mail_additional_recipients'],
                use_drf=False,
            )

Usage (DRF serializer)::

    from pretix.helpers.recipient_validation import make_recipient_validator

    _validate_recipients = make_recipient_validator(max_count=25)

    class MySerializer(serializers.ModelSerializer):
        def validate_mail_additional_recipients(self, value):
            return _validate_recipients(value, use_drf=True)
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError as DRFValidationError

MAX_RECIPIENTS = 25


def make_recipient_validator(max_count=MAX_RECIPIENTS):
    """
    Factory Method: return a validator callable for comma-separated email
    recipient fields.

    The returned callable:
    - Strips all whitespace from the value.
    - Raises a ``ValidationError`` (Django or DRF, depending on ``use_drf``)
      when the number of comma-separated entries exceeds *max_count*.
    - Returns the cleaned (whitespace-stripped) value on success.

    :param max_count: Maximum number of recipients allowed (default: 25).
    :returns: A validator callable ``(value, *, use_drf=False) -> str``.
    """

    def _validate(value, *, use_drf=False):
        cleaned = value.replace(' ', '')
        if cleaned and len(cleaned.split(',')) > max_count:
            msg = _('Please enter less than %(count)d recipients.') % {'count': max_count}
            if use_drf:
                raise DRFValidationError(str(msg))
            raise DjangoValidationError(msg)
        return cleaned

    return _validate

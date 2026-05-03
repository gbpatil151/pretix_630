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

from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from pytz import common_timezones

from pretix.base.models import ScheduledEventExport
from pretix.base.models.exports import ScheduledOrganizerExport
from pretix.helpers.recipient_validation import make_recipient_validator

# Factory Method: produce a single validator used by every recipient field.
_validate_recipients = make_recipient_validator(max_count=25)


class ScheduledEventExportForm(forms.ModelForm):
    class Meta:
        model = ScheduledEventExport
        fields = ['mail_additional_recipients', 'mail_additional_recipients_cc', 'mail_additional_recipients_bcc',
                  'mail_subject', 'mail_template', 'schedule_rrule_time', 'locale']
        widgets = {
            'mail_additional_recipients': forms.TextInput,
            'mail_additional_recipients_cc': forms.TextInput,
            'mail_additional_recipients_bcc': forms.TextInput,
            'schedule_rrule_time': forms.TimeInput(attrs={'class': 'timepickerfield', 'autocomplete': 'off'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        locale_names = dict(settings.LANGUAGES)
        self.fields['locale'] = forms.ChoiceField(
            label=_('Language'),
            choices=[(a, locale_names[a]) for a in self.instance.event.settings.locales]
        )

    def clean_mail_additional_recipients(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients'])

    def clean_mail_additional_recipients_cc(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients_cc'])

    def clean_mail_additional_recipients_bcc(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients_bcc'])


class ScheduledOrganizerExportForm(forms.ModelForm):
    class Meta:
        model = ScheduledOrganizerExport
        fields = ['mail_additional_recipients', 'mail_additional_recipients_cc', 'mail_additional_recipients_bcc',
                  'mail_subject', 'mail_template', 'schedule_rrule_time', 'locale', 'timezone']
        widgets = {
            'mail_additional_recipients': forms.TextInput,
            'mail_additional_recipients_cc': forms.TextInput,
            'mail_additional_recipients_bcc': forms.TextInput,
            'schedule_rrule_time': forms.TimeInput(attrs={'class': 'timepickerfield', 'autocomplete': 'off'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        locale_names = dict(settings.LANGUAGES)
        self.fields['locale'] = forms.ChoiceField(
            label=_('Language'),
            choices=[(a, locale_names[a]) for a in self.instance.organizer.settings.locales]
        )
        self.fields['timezone'] = forms.ChoiceField(
            choices=((a, a) for a in common_timezones),
            label=_("Timezone"),
        )

    def clean_mail_additional_recipients(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients'])

    def clean_mail_additional_recipients_cc(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients_cc'])

    def clean_mail_additional_recipients_bcc(self):
        return _validate_recipients(self.cleaned_data['mail_additional_recipients_bcc'])

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
import logging
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext, gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView

from pretix.base.models import CachedFile, OrderPosition
from pretix.base.services.tickets import invalidate_cache
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.pdf import (
    BaseEditorView, BaseLayoutEditorView, BaseOrderPrintDo,
)
from pretix.helpers.models import modelcopy
from pretix.plugins.ticketoutputpdf.forms import TicketLayoutForm
from pretix.plugins.ticketoutputpdf.ticketoutput import PdfTicketOutput

from ...helpers.compat import CompatDeleteView
from .models import TicketLayout
from .tasks import tickets_create_pdf

logger = logging.getLogger(__name__)


class EditorView(BaseEditorView):
    title = _('Default ticket layout')

    def get_output(self, *args, **kwargs):
        return PdfTicketOutput(self.request.event, *args, **kwargs)

    def save_layout(self):
        super().save_layout()
        invalidate_cache.apply_async(kwargs={'event': self.request.event.pk, 'provider': 'pdf'})

    def get_layout_settings_key(self):
        return 'ticketoutput_pdf_layout'

    def get_background_settings_key(self):
        return 'ticketoutput_pdf_background'

    def get_default_background(self):
        return static('pretixpresale/pdf/ticket_default_a4.pdf')

    def generate(self, p: OrderPosition, override_layout=None, override_background=None):
        prov = self.get_output(
            override_layout=override_layout,
            override_background=override_background
        )
        fname, mimet, data = prov.generate(p)
        return fname, mimet, data

    def get_current_layout(self):
        prov = self.get_output()
        return (
            self.request.event.settings.get(self.get_layout_settings_key(), as_type=list)
            or prov._default_layout()
        )


class LayoutListView(EventPermissionRequiredMixin, ListView):
    model = TicketLayout
    permission = ('can_change_event_settings')
    template_name = 'pretixplugins/ticketoutputpdf/index.html'
    context_object_name = 'layouts'

    def get_queryset(self):
        return self.request.event.ticket_layouts.prefetch_related('item_assignments')


class LayoutCreate(EventPermissionRequiredMixin, CreateView):
    model = TicketLayout
    form_class = TicketLayoutForm
    template_name = 'pretixplugins/ticketoutputpdf/edit.html'
    permission = 'can_change_event_settings'
    context_object_name = 'layout'
    success_url = '/ignored'

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        if not self.request.event.ticket_layouts.filter(default=True).exists():
            form.instance.default = True
        messages.success(self.request, _('The new ticket layout has been created.'))
        super().form_valid(form)
        if form.instance.background and form.instance.background.name:
            form.instance.background.save('background.pdf', form.instance.background)
        form.instance.log_action('pretix.plugins.ticketoutputpdf.layout.added', user=self.request.user,
                                 data=dict(form.cleaned_data))
        return redirect(reverse('plugins:ticketoutputpdf:edit', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'layout': form.instance.pk
        }))

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs)

    @cached_property
    def copy_from(self):
        if self.request.GET.get("copy_from") and not getattr(self, 'object', None):
            try:
                return self.request.event.ticket_layouts.get(pk=self.request.GET.get("copy_from"))
            except TicketLayout.DoesNotExist:
                pass

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()

        if self.copy_from:
            i = modelcopy(self.copy_from)
            i.default = False
            i.pk = None
            kwargs['instance'] = i
            kwargs.setdefault('initial', {})
        return kwargs


class LayoutSetDefault(EventPermissionRequiredMixin, DetailView):
    model = TicketLayout
    permission = 'can_change_event_settings'

    def get_object(self, queryset=None) -> TicketLayout:
        try:
            return self.request.event.ticket_layouts.get(
                id=self.kwargs['layout']
            )
        except TicketLayout.DoesNotExist:
            raise Http404(_("The requested layout does not exist."))

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        messages.success(self.request, _('Your changes have been saved.'))
        obj = self.get_object()
        self.request.event.ticket_layouts.exclude(pk=obj.pk).update(default=False)
        obj.default = True
        obj.save(update_fields=['default'])
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse('plugins:ticketoutputpdf:index', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class LayoutDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = TicketLayout
    template_name = 'pretixplugins/ticketoutputpdf/delete.html'
    permission = 'can_change_event_settings'
    context_object_name = 'layout'

    def get_object(self, queryset=None) -> TicketLayout:
        try:
            return self.request.event.ticket_layouts.get(
                id=self.kwargs['layout']
            )
        except TicketLayout.DoesNotExist:
            raise Http404(_("The requested layout does not exist."))

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.log_action(action='pretix.plugins.ticketoutputpdf.layout.deleted', user=request.user)
        self.object.delete()
        if not self.request.event.ticket_layouts.filter(default=True).exists():
            f = self.request.event.ticket_layouts.first()
            if f:
                f.default = True
                f.save(update_fields=['default'])
        messages.success(self.request, _('The selected ticket layout been deleted.'))
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse('plugins:ticketoutputpdf:index', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class LayoutGetDefault(EventPermissionRequiredMixin, View):
    permission = 'can_change_event_settings'

    def get(self, request, *args, **kwargs):
        layout = self.request.event.ticket_layouts.get_or_create(
            default=True,
            defaults={
                'name': gettext('Default layout'),
            }
        )[0]
        return redirect(reverse('plugins:ticketoutputpdf:edit', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'layout': layout.pk
        }))


class LayoutEditorView(BaseLayoutEditorView):
    def get_layout_model(self):
        return TicketLayout

    def get_action_prefix(self):
        return 'pretix.plugins.ticketoutputpdf'

    def get_output_filename(self):
        return 'ticket.pdf'

    def get_render_background_title(self):
        return 'Ticket'

    @property
    def title(self):
        return _('Ticket PDF layout: {}').format(self.layout)

    def post_save_hook(self):
        invalidate_cache.apply_async(kwargs={'event': self.request.event.pk, 'provider': 'pdf'})

    def get_default_background_relpath(self):
        return 'pretixpresale/pdf/ticket_default_a4.pdf'


class OrderPrintDo(BaseOrderPrintDo):
    task = tickets_create_pdf

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(self.request.event.orders, code=request.GET.get("code"))
        cf = CachedFile(web_download=True, session_key=self.request.session.session_key)
        cf.date = now()
        cf.type = 'application/pdf'
        cf.expires = now() + timedelta(days=3)
        position = get_object_or_404(order.positions, pk=request.GET.get('position'))
        cf.filename = f'tickets_{self.request.event.slug}_{order.code}-{position.positionid}.pdf'
        cf.save()
        return self.do(
            self.request.event.pk,
            str(cf.id),
            position.pk,
            request.GET.get('channel'),
        )

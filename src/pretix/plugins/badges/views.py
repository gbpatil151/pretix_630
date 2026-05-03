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
import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, DetailView, ListView
from pypdf import PdfWriter

from pretix.base.models import CachedFile
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.pdf import BaseEditorView, BaseLayoutEditorView, BaseOrderPrintDo
from pretix.helpers.models import modelcopy
from pretix.plugins.badges.forms import BadgeLayoutForm
from pretix.plugins.badges.tasks import badges_create_pdf

from ...helpers.compat import CompatDeleteView
from .models import BadgeLayout
from .templates import TEMPLATES


class LayoutListView(EventPermissionRequiredMixin, ListView):
    model = BadgeLayout
    permission = ('can_change_event_settings', 'can_view_orders')
    template_name = 'pretixplugins/badges/index.html'
    context_object_name = 'layouts'

    def get_queryset(self):
        return self.request.event.badge_layouts.prefetch_related('item_assignments')


class LayoutCreate(EventPermissionRequiredMixin, CreateView):
    model = BadgeLayout
    form_class = BadgeLayoutForm
    template_name = 'pretixplugins/badges/edit.html'
    permission = 'can_change_event_settings'
    context_object_name = 'layout'
    success_url = '/ignored'

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if self.copy_from:
            del form.fields['template']
        return form

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        if not self.request.event.badge_layouts.filter(default=True).exists():
            form.instance.default = True
        messages.success(self.request, _('The new badge layout has been created.'))
        if not self.copy_from:
            form.instance.layout = json.dumps(TEMPLATES[form.cleaned_data["template"]]["layout"])
        super().form_valid(form)
        if not self.copy_from:
            p = PdfWriter()
            p.add_blank_page(
                width=Decimal('%.5f' % TEMPLATES[form.cleaned_data["template"]]["pagesize"][0]),
                height=Decimal('%.5f' % TEMPLATES[form.cleaned_data["template"]]["pagesize"][1]),
            )
            buffer = BytesIO()
            p.write(buffer)
            buffer.seek(0)
            form.instance.background.save('background.pdf', ContentFile(buffer.read()))
        elif form.instance.background and form.instance.background.name:
            form.instance.background.save('background.pdf', form.instance.background)
        form.instance.log_action('pretix.plugins.badges.layout.added', user=self.request.user,
                                 data=dict(form.cleaned_data))
        return redirect(reverse('plugins:badges:edit', kwargs={
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
                return self.request.event.badge_layouts.get(pk=self.request.GET.get("copy_from"))
            except BadgeLayout.DoesNotExist:
                pass

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()

        if self.copy_from:
            i = modelcopy(self.copy_from)
            i.pk = None
            i.default = False
            kwargs['instance'] = i
            kwargs.setdefault('initial', {})
        return kwargs


class LayoutSetDefault(EventPermissionRequiredMixin, DetailView):
    model = BadgeLayout
    permission = 'can_change_event_settings'

    def get_object(self, queryset=None) -> BadgeLayout:
        try:
            return self.request.event.badge_layouts.get(
                id=self.kwargs['layout']
            )
        except BadgeLayout.DoesNotExist:
            raise Http404(_("The requested badge layout does not exist."))

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        messages.success(self.request, _('Your changes have been saved.'))
        obj = self.get_object()
        self.request.event.badge_layouts.exclude(pk=obj.pk).update(default=False)
        obj.default = True
        obj.save(update_fields=['default'])
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse('plugins:badges:index', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class LayoutDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = BadgeLayout
    template_name = 'pretixplugins/badges/delete.html'
    permission = 'can_change_event_settings'
    context_object_name = 'layout'

    def get_object(self, queryset=None) -> BadgeLayout:
        try:
            return self.request.event.badge_layouts.get(
                id=self.kwargs['layout']
            )
        except BadgeLayout.DoesNotExist:
            raise Http404(_("The requested badge layout does not exist."))

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.object.log_action(action='pretix.plugins.badges.layout.deleted', user=request.user)
        self.object.delete()
        if not self.request.event.badge_layouts.filter(default=True).exists():
            f = self.request.event.badge_layouts.first()
            if f:
                f.default = True
                f.save(update_fields=['default'])
        messages.success(self.request, _('The selected badge layout been deleted.'))
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse('plugins:badges:index', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class LayoutEditorView(BaseLayoutEditorView):
    def get_layout_model(self):
        return BadgeLayout

    def get_action_prefix(self):
        return 'pretix.plugins.badges'

    def get_output_filename(self):
        return 'badge.pdf'

    def get_render_background_title(self):
        return 'Badge'

    @property
    def title(self):
        return _('Badge layout: {}').format(self.layout)

    def get_default_background_relpath(self):
        return 'pretixplugins/badges/badge_default_a6l.pdf'


class OrderPrintDo(BaseOrderPrintDo):
    task = badges_create_pdf

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(self.request.event.orders, code=request.GET.get("code"))
        cf = CachedFile(web_download=True, session_key=self.request.session.session_key)
        cf.date = now()
        cf.type = 'application/pdf'
        cf.expires = now() + timedelta(days=3)
        if 'position' in request.GET:
            qs = order.positions.filter(pk=request.GET.get('position'))
            positions = [p.pk for p in qs]
            if len(positions) < 5:
                cf.filename = f'badges_{self.request.event.slug}_{order.code}_{"_".join(str(p.positionid) for p in qs)}.pdf'
        else:
            positions = [p.pk for p in order.positions.all()]
            cf.filename = f'badges_{self.request.event.slug}_{order.code}.pdf'
        cf.save()
        return self.do(
            self.request.event.pk,
            str(cf.id),
            positions,
        )

import os
from dataclasses import dataclass, field

from django.core.files import File
from django.core.files.storage import default_storage
from django.utils.crypto import get_random_string

from pretix.base.models import (
    Discount, EventMetaValue, Item, ItemAddOn, ItemBundle, ItemCategory,
    ItemMetaValue, ItemProgramTime, ItemVariationMetaValue, Question, Quota,
)
from pretix.base.models.event import EventFooterLink
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import event_copy_data
from pretix.helpers.hierarkey import clean_filename


@dataclass
class CopyContext:
    tax_map: dict = field(default_factory=dict)
    category_map: dict = field(default_factory=dict)
    item_meta_properties_map: dict = field(default_factory=dict)
    item_map: dict = field(default_factory=dict)
    variation_map: dict = field(default_factory=dict)
    question_map: dict = field(default_factory=dict)
    checkin_list_map: dict = field(default_factory=dict)
    quota_map: dict = field(default_factory=dict)


class EventCopyBuilder:
    def __init__(self, source_event, target_event):
        self.source = source_event
        self.target = target_event
        self.ctx = CopyContext()

    def copy_basic_info(self, skip_meta_data=False):
        #  Note: avoid self.set_active_plugins(), it causes trouble e.g. for the badges plugin.
        #  Plugins can create data in installed() hook based on existing data of the event.
        #  Calling set_active_plugins() results in defaults being created while actually data
        #  should come from the copied event. Instead plugins should use event_copy_data to move
        #  over their data.
        self.target.plugins = self.source.plugins
        self.target.is_public = self.source.is_public
        if self.source.date_admission:
            self.target.date_admission = self.target.date_from + (self.source.date_admission - self.source.date_from)
        self.target.testmode = self.source.testmode
        self.target.all_sales_channels = self.source.all_sales_channels
        self.target.save()
        self.target.log_action('pretix.object.cloned', data={'source': self.source.slug, 'source_id': self.source.pk})

        if hasattr(self.source, 'alternative_domain_assignment'):
            self.source.alternative_domain_assignment.domain.event_assignments.create(event=self.target)

        if not self.target.all_sales_channels:
            self.target.limit_sales_channels.set(
                self.target.organizer.sales_channels.filter(
                    identifier__in=self.source.limit_sales_channels.values_list("identifier", flat=True)
                )
            )

        if not skip_meta_data:
            for emv in EventMetaValue.objects.filter(event=self.source):
                emv.pk = None
                emv.event = self.target
                emv.save(force_insert=True)

        for fl in EventFooterLink.objects.filter(event=self.source):
            fl.pk = None
            fl.event = self.target
            fl.save(force_insert=True)

        return self

    def copy_tax_rules(self):
        for t in self.source.tax_rules.all():
            self.ctx.tax_map[t.pk] = t
            t.pk = None
            t.event = self.target
            t.save(force_insert=True)
            t.log_action('pretix.object.cloned')
        return self

    def copy_categories(self):
        for c in ItemCategory.objects.filter(event=self.source):
            self.ctx.category_map[c.pk] = c
            c.pk = None
            c.event = self.target
            c.save(force_insert=True)
            c.log_action('pretix.object.cloned')
        return self

    def copy_items(self):
        for imp in self.source.item_meta_properties.all():
            self.ctx.item_meta_properties_map[imp.pk] = imp
            imp.pk = None
            imp.event = self.target
            imp.save(force_insert=True)
            imp.log_action('pretix.object.cloned')

        for i in Item.objects.filter(event=self.source).prefetch_related(
            'variations', 'limit_sales_channels', 'require_membership_types',
            'variations__limit_sales_channels', 'variations__require_membership_types',
            'matched_by_cross_selling_categories',
        ):
            vars = list(i.variations.all())
            require_membership_types = list(i.require_membership_types.all())
            limit_sales_channels = list(i.limit_sales_channels.all())
            matched_by_cross_selling_categories = list(i.matched_by_cross_selling_categories.all())
            self.ctx.item_map[i.pk] = i
            i.pk = None
            i.event = self.target
            i._prefetched_objects_cache = {}
            if i.picture:
                i.picture.save(os.path.basename(i.picture.name), i.picture)
            if i.category_id:
                i.category = self.ctx.category_map[i.category_id]
            if i.tax_rule_id:
                i.tax_rule = self.ctx.tax_map[i.tax_rule_id]

            if i.grant_membership_type and self.source.organizer_id != self.target.organizer_id:
                i.grant_membership_type = None

            i.save()  # no force_insert since i.picture.save could have already inserted
            i.log_action('pretix.object.cloned')

            if require_membership_types and self.source.organizer_id == self.target.organizer_id:
                i.require_membership_types.set(require_membership_types)

            if not i.all_sales_channels:
                i.limit_sales_channels.set(self.target.organizer.sales_channels.filter(identifier__in=[s.identifier for s in limit_sales_channels]))

            for v in vars:
                require_membership_types = list(v.require_membership_types.all())
                limit_sales_channels = list(v.limit_sales_channels.all())
                self.ctx.variation_map[v.pk] = v
                v.pk = None
                v.item = i
                v._prefetched_objects_cache = {}
                v.save(force_insert=True)

                if require_membership_types and self.source.organizer_id == self.target.organizer_id:
                    v.require_membership_types.set(require_membership_types)
                if not v.all_sales_channels:
                    v.limit_sales_channels.set(self.target.organizer.sales_channels.filter(identifier__in=[s.identifier for s in limit_sales_channels]))

            if matched_by_cross_selling_categories:
                i.matched_by_cross_selling_categories.set([self.ctx.category_map[c.pk] for c in matched_by_cross_selling_categories])

        for i in self.target.items.filter(hidden_if_item_available__isnull=False):
            i.hidden_if_item_available = self.ctx.item_map[i.hidden_if_item_available_id]
            i.save()
            
        return self

    def copy_item_meta_values(self):
        for imv in ItemMetaValue.objects.filter(item__event=self.source):
            imv.pk = None
            imv.property = self.ctx.item_meta_properties_map[imv.property_id]
            imv.item = self.ctx.item_map[imv.item.pk]
            imv.save(force_insert=True)

        for imv in ItemVariationMetaValue.objects.filter(variation__item__event=self.source):
            imv.pk = None
            imv.property = self.ctx.item_meta_properties_map[imv.property_id]
            imv.variation = self.ctx.variation_map[imv.variation_id]
            imv.save(force_insert=True)
            
        return self

    def copy_bundles_and_addons(self):
        for ia in ItemAddOn.objects.filter(base_item__event=self.source).prefetch_related('base_item', 'addon_category'):
            ia.pk = None
            ia.base_item = self.ctx.item_map[ia.base_item.pk]
            ia.addon_category = self.ctx.category_map[ia.addon_category.pk]
            ia.save(force_insert=True)

        for ia in ItemBundle.objects.filter(base_item__event=self.source).prefetch_related('base_item', 'bundled_item', 'bundled_variation'):
            ia.pk = None
            ia.base_item = self.ctx.item_map[ia.base_item.pk]
            ia.bundled_item = self.ctx.item_map[ia.bundled_item.pk]
            if ia.bundled_variation:
                ia.bundled_variation = self.ctx.variation_map[ia.bundled_variation.pk]
            ia.save(force_insert=True)

        if not self.target.has_subevents and not self.source.has_subevents:
            for ipt in ItemProgramTime.objects.filter(item__event=self.source).prefetch_related('item'):
                ipt.pk = None
                ipt.item = self.ctx.item_map[ipt.item.pk]
                ipt.save(force_insert=True)
                
        return self

    def copy_quotas(self):
        for q in Quota.objects.filter(event=self.source, subevent__isnull=True).prefetch_related('items', 'variations'):
            self.ctx.quota_map[q.pk] = q
            items = list(q.items.all())
            vars = list(q.variations.all())
            oldid = q.pk
            q.pk = None
            q._prefetched_objects_cache = {}
            q.event = self.target
            q.closed = False
            q.save(force_insert=True)
            q.log_action('pretix.object.cloned')
            for i in items:
                if i.pk in self.ctx.item_map:
                    q.items.add(self.ctx.item_map[i.pk])
            for v in vars:
                q.variations.add(self.ctx.variation_map[v.pk])
            self.target.items.filter(hidden_if_available_id=oldid).update(hidden_if_available=q)
            
        return self

    def copy_discounts(self):
        for d in Discount.objects.filter(event=self.source).prefetch_related(
            'condition_limit_products', 'benefit_limit_products', 'limit_sales_channels'
        ):
            c_items = list(d.condition_limit_products.all())
            b_items = list(d.benefit_limit_products.all())
            limit_sales_channels = list(d.limit_sales_channels.all())
            d.pk = None
            d.event = self.target
            d._prefetched_objects_cache = {}
            d.save(force_insert=True)
            d.log_action('pretix.object.cloned')
            for i in c_items:
                if i.pk in self.ctx.item_map:
                    d.condition_limit_products.add(self.ctx.item_map[i.pk])
            for i in b_items:
                if i.pk in self.ctx.item_map:
                    d.benefit_limit_products.add(self.ctx.item_map[i.pk])

            if not d.all_sales_channels:
                d.limit_sales_channels.set(self.target.organizer.sales_channels.filter(identifier__in=[s.identifier for s in limit_sales_channels]))
                
        return self

    def copy_questions(self):
        for q in Question.objects.filter(event=self.source).prefetch_related('items', 'options'):
            items = list(q.items.all())
            opts = list(q.options.all())
            self.ctx.question_map[q.pk] = q
            q.pk = None
            q._prefetched_objects_cache = {}
            q.event = self.target
            q.save(force_insert=True)
            q.log_action('pretix.object.cloned')

            for i in items:
                q.items.add(self.ctx.item_map[i.pk])
            for o in opts:
                o.pk = None
                o.question = q
                o.save(force_insert=True)

        for q in self.target.questions.filter(dependency_question__isnull=False):
            q.dependency_question = self.ctx.question_map[q.dependency_question_id]
            q.save(update_fields=['dependency_question'])
            
        return self

    def copy_checkin_lists(self):
        def _walk_rules(rules):
            if isinstance(rules, dict):
                for k, v in rules.items():
                    if k == 'lookup':
                        if rules[k][0] == 'product':
                            rules[k][1] = str(self.ctx.item_map.get(int(v[1]), 0).pk) if int(v[1]) in self.ctx.item_map else "0"
                        elif rules[k][0] == 'variation':
                            rules[k][1] = str(self.ctx.variation_map.get(int(v[1]), 0).pk) if int(v[1]) in self.ctx.variation_map else "0"
                    else:
                        _walk_rules(v)
            elif isinstance(rules, list):
                for i in rules:
                    _walk_rules(i)

        for cl in self.source.checkin_lists.filter(subevent__isnull=True).prefetch_related(
            'limit_products'
        ):
            items = list(cl.limit_products.all())
            self.ctx.checkin_list_map[cl.pk] = cl
            cl.pk = None
            cl._prefetched_objects_cache = {}
            cl.event = self.target
            rules = cl.rules
            _walk_rules(rules)
            cl.rules = rules
            cl.save(force_insert=True)
            cl.log_action('pretix.object.cloned')
            for i in items:
                cl.limit_products.add(self.ctx.item_map[i.pk])
                
        return self

    def copy_seating_plans(self):
        if self.source.seating_plan:
            if self.source.seating_plan.organizer_id == self.target.organizer_id:
                self.target.seating_plan = self.source.seating_plan
            else:
                sp = self.source.seating_plan
                sp.pk = None
                sp.organizer = self.target.organizer
                sp.save(force_insert=True)
                self.target.seating_plan = sp
            self.target.save()

        for m in self.source.seat_category_mappings.filter(subevent__isnull=True):
            m.pk = None
            m.event = self.target
            m.product = self.ctx.item_map[m.product_id]
            m.save(force_insert=True)

        for s in self.source.seats.filter(subevent__isnull=True):
            s.pk = None
            s.event = self.target
            if s.product_id:
                s.product = self.ctx.item_map[s.product_id]
            s.save(force_insert=True)
            
        return self

    def copy_settings(self):
        valid_sales_channel_identifers = set(self.target.organizer.sales_channels.values_list("identifier", flat=True))
        skip_settings = {
            'ticket_secrets_pretix_sig1_pubkey',
            'ticket_secrets_pretix_sig1_privkey',
            # no longer used, but we still don't need to copy them
            'presale_css_file',
            'presale_css_checksum',
            'presale_widget_css_file',
            'presale_widget_css_checksum',
        } | {
            # Some settings might already exist due to e.g. the timezone being special in the API
            s.key for s in self.target.settings._objects.all()
        }
        settings_to_save = []
        for s in self.source.settings._objects.all():
            if s.key in skip_settings:
                continue

            s.object = self.target
            s.pk = None
            if s.value.startswith('file://') and settings_hierarkey.get_declared_type(s.key) == File:
                fi = default_storage.open(s.value[len('file://'):], 'rb')
                nonce = get_random_string(length=8)
                fname_base = clean_filename(os.path.basename(s.value))

                # TODO: make sure pub is always correct
                fname = 'pub/%s/%s/%s.%s.%s' % (
                    self.target.organizer.slug, self.target.slug, fname_base, nonce, s.value.split('.')[-1]
                )
                newname = default_storage.save(fname, fi)
                s.value = 'file://' + newname
                settings_to_save.append(s)
            elif s.key.startswith('payment_') and s.key.endswith('__restrict_to_sales_channels'):
                data = self.source.settings._unserialize(s.value, as_type=list)
                data = [ident for ident in data if ident in valid_sales_channel_identifers]
                s.value = self.source.settings._serialize(data)
                settings_to_save.append(s)
            else:
                settings_to_save.append(s)
        self.source.settings._objects.bulk_create(settings_to_save)

        self.target.settings.flush()
        return self

    def emit_signals(self):
        event_copy_data.send(
            sender=self.target, other=self.source,
            tax_map=self.ctx.tax_map, category_map=self.ctx.category_map, item_map=self.ctx.item_map, variation_map=self.ctx.variation_map,
            question_map=self.ctx.question_map, checkin_list_map=self.ctx.checkin_list_map, quota_map=self.ctx.quota_map,
        )
        return self

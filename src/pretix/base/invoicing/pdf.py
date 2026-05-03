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
import datetime
import logging
import math
import re
import textwrap
import unicodedata
from abc import ABC, abstractmethod
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from itertools import groupby
from typing import List, Tuple

import bleach
from bidi import get_display
from django.contrib.staticfiles import finders
from django.db.models import Sum
from django.dispatch import receiver
from django.utils.formats import date_format, localize
from django.utils.translation import (
    get_language, gettext, gettext_lazy, pgettext,
)
from reportlab.lib import colors, pagesizes
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, StyleSheet1
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
    PageTemplate, Spacer, Table, TableStyle,
)

from pretix.base.decimal import round_decimal
from pretix.base.models import Event, Invoice, Order, OrderPayment
from pretix.base.services.currencies import SOURCE_NAMES
from pretix.base.signals import register_invoice_renderers
from pretix.base.templatetags.money import money_filter
from pretix.helpers.reportlab import (
    FontFallbackParagraph, ThumbnailingImageReader, register_ttf_font_if_new,
    reshaper,
)
from pretix.presale.style import get_fonts

logger = logging.getLogger(__name__)


def addon_aware_groupby(iterable, key, is_addon):
    """
    We use groupby() to visually group identical lines on an invoice. For example, instead of

    Product 1       5.00 EUR
    Product 1       5.00 EUR
    Product 1       5.00 EUR
    Product 2       7.00 EUR

    We want to print

    3x Product 1    5.00 EUR = 15.00 EUR
    Product 2       7.00 EUR

    However, this fails for setups with addon-products since groupby() only groups consecutive
    lines with the same identity. So in

    Product 1       5.00 EUR
    + Addon 1       2.00 EUR
    Product 1       5.00 EUR
    + Addon 1       2.00 EUR
    Product 1       5.00 EUR
    + Addon 2       3.00 EUR

    There is no consecutive repetition of the same entity. This function provides a specialised groupby which
    understands the product/addon relationship and packs groups of these addons together if they are, in fact,
    identical groups:

    2x Product 1    5.00 EUR = 10.00 EUR
    + 2x Addon 1    2.00 EUR =  4.00 EUR
    Product 1       5.00 EUR
    + Addon 2       3.00 EUR
    """
    packed_groups = []

    for i in iterable:
        if is_addon(i):
            packed_groups[-1].append(i)
        else:
            packed_groups.append([i])
    # Each packed_groups element contains a list with the parent product as first element, and any addon products following

    def _reorder(packed_groups):
        # Emit the products as individual products again, reordered by "all parent products, then all addon products"
        # within each group.
        for _, repeated_groups in groupby(packed_groups, key=lambda g: tuple(key(a) for a in g)):
            for repeated_items in zip(*repeated_groups):
                yield from repeated_items

    return groupby(_reorder(packed_groups), key)


class NumberedCanvas(Canvas):
    def __init__(self, *args, **kwargs):
        self.font_regular = kwargs.pop('font_regular')
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            Canvas.showPage(self)
        Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont(self.font_regular, 8)
        text = pgettext("invoice", "Page %d of %d") % (self._pageNumber, page_count,)
        try:
            text = get_display(reshaper.reshape(text))
        except:
            logger.exception('Reshaping/Bidi fixes failed on string {}'.format(repr(text)))
        self.drawRightString(self._pagesize[0] - 20 * mm, 10 * mm, text)
        self.restoreState()


class BaseInvoiceRenderer:
    """
    This is the base class for all invoice renderers.
    """

    def __init__(self, event: Event):
        self.event = event

    def __str__(self):
        return self.identifier

    def generate(self, invoice: Invoice) -> Tuple[str, str, str]:
        """
        This method should generate the invoice file and return a tuple consisting of a
        filename, a file type and file content. The extension will be taken from the filename
        which is otherwise ignored.
        """
        raise NotImplementedError()

    @property
    def verbose_name(self) -> str:
        """
        A human-readable name for this renderer. This should be short but
        self-explanatory. Good examples include 'German DIN 5008' or 'Italian invoice'.
        """
        raise NotImplementedError()  # NOQA

    @property
    def identifier(self) -> str:
        """
        A short and unique identifier for this renderer.
        This should only contain lowercase letters and in most
        cases will be the same as your package name.
        """
        raise NotImplementedError()  # NOQA


class BaseReportlabInvoiceRenderer(BaseInvoiceRenderer):
    """
    This is a convenience class to avoid duplicate code when implementing invoice renderers
    that are based on reportlab.
    """
    pagesize = pagesizes.A4
    left_margin = 25 * mm
    right_margin = 20 * mm
    top_margin = 20 * mm
    bottom_margin = 15 * mm
    doc_template_class = BaseDocTemplate
    canvas_class = Canvas
    font_regular = 'OpenSans'
    font_bold = 'OpenSansBd'

    def _init(self):
        """
        Initialize the renderer. By default, this registers fonts and sets ``self.stylesheet``.
        """
        self._register_fonts()
        self.stylesheet = self._get_stylesheet()

    def _get_stylesheet(self):
        """
        Get a stylesheet. By default, this contains the "Normal" and "Heading1" styles.
        """
        stylesheet = StyleSheet1()
        stylesheet.add(ParagraphStyle(name='Normal', fontName=self.font_regular, fontSize=10, leading=12))
        stylesheet.add(ParagraphStyle(name='Bold', fontName=self.font_bold, fontSize=10, leading=12))
        stylesheet.add(ParagraphStyle(name='BoldRight', fontName=self.font_bold, fontSize=10, leading=12, alignment=TA_RIGHT))
        stylesheet.add(ParagraphStyle(name='BoldRightNoSplit', fontName=self.font_bold, fontSize=10, leading=12, alignment=TA_RIGHT,
                                      splitLongWords=False))
        stylesheet.add(ParagraphStyle(name='NormalRight', fontName=self.font_regular, fontSize=10, leading=12, alignment=TA_RIGHT))
        stylesheet.add(ParagraphStyle(name='BoldInverseCenter', fontName=self.font_bold, fontSize=10, leading=12,
                                      textColor=colors.white, alignment=TA_CENTER))
        stylesheet.add(ParagraphStyle(name='InvoiceFrom', parent=stylesheet['Normal']))
        stylesheet.add(ParagraphStyle(name='Heading1', fontName=self.font_bold, fontSize=15, leading=15 * 1.2))
        stylesheet.add(ParagraphStyle(name='FineprintHeading', fontName=self.font_bold, fontSize=8, leading=12))
        stylesheet.add(ParagraphStyle(name='Fineprint', fontName=self.font_regular, fontSize=8, leading=10))
        stylesheet.add(ParagraphStyle(name='FineprintRight', fontName=self.font_regular, fontSize=8, leading=10, alignment=TA_RIGHT))
        stylesheet.add(ParagraphStyle(name='WarningBlock', fontName=self.font_bold, fontSize=10, leading=12,
                                      alignment=TA_LEFT, borderWidth=1 * mm, borderColor=colors.black,
                                      borderPadding=2 * mm, spaceBefore=5 * mm, spaceAfter=5 * mm))
        return stylesheet

    def _register_fonts(self):
        """
        Register fonts with reportlab. By default, this registers the OpenSans font family
        """
        register_ttf_font_if_new('OpenSans', finders.find('fonts/OpenSans-Regular.ttf'))
        register_ttf_font_if_new('OpenSansIt', finders.find('fonts/OpenSans-Italic.ttf'))
        register_ttf_font_if_new('OpenSansBd', finders.find('fonts/OpenSans-Bold.ttf'))
        register_ttf_font_if_new('OpenSansBI', finders.find('fonts/OpenSans-BoldItalic.ttf'))
        pdfmetrics.registerFontFamily('OpenSans', normal='OpenSans', bold='OpenSansBd',
                                      italic='OpenSansIt', boldItalic='OpenSansBI')

        for family, styles in get_fonts(event=self.event, pdf_support_required=True).items():
            register_ttf_font_if_new(family, finders.find(styles['regular']['truetype']))
            if family == self.event.settings.invoice_renderer_font:
                self.font_regular = family
                if 'bold' in styles:
                    self.font_bold = family + ' B'
            if 'italic' in styles:
                register_ttf_font_if_new(family + ' I', finders.find(styles['italic']['truetype']))
            if 'bold' in styles:
                register_ttf_font_if_new(family + ' B', finders.find(styles['bold']['truetype']))
            if 'bolditalic' in styles:
                register_ttf_font_if_new(family + ' B I', finders.find(styles['bolditalic']['truetype']))

    def _normalize(self, text):
        # reportlab does not support unicode combination characters
        # It's important we do this before we use ArabicReshaper
        text = unicodedata.normalize("NFKC", text)

        # reportlab does not support RTL, ligature-heavy scripts like Arabic. Therefore, we use ArabicReshaper
        # to resolve all ligatures and python-bidi to switch RTL texts.
        try:
            text = "<br />".join(get_display(reshaper.reshape(l)) for l in re.split("<br ?/>", text))
        except:
            logger.exception('Reshaping/Bidi fixes failed on string {}'.format(repr(text)))

        return text

    def _upper(self, val):
        # We uppercase labels, but not in every language
        if get_language().startswith('el'):
            return val
        return val.upper()

    def _on_other_page(self, canvas: Canvas, doc):
        """
        Called when a new page is rendered that is *not* the first page.
        """
        pass

    def _on_first_page(self, canvas: Canvas, doc):
        """
        Called when a new page is rendered that is the first page.
        """
        pass

    def _get_story(self, doc):
        """
        Called to create the story to be inserted into the main frames.
        """
        raise NotImplementedError()

    def _get_first_page_frames(self, doc):
        """
        Called to create a list of frames for the first page.
        """
        return [
            Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - 75 * mm,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                  id='normal')
        ]

    def _get_other_page_frames(self, doc):
        """
        Called to create a list of frames for the other pages.
        """
        return [
            Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                  id='normal')
        ]

    def _build_doc(self, fhandle):
        """
        Build a PDF document in a given file handle
        """
        self._init()
        doc = self.doc_template_class(fhandle, pagesize=self.pagesize,
                                      leftMargin=self.left_margin, rightMargin=self.right_margin,
                                      topMargin=self.top_margin, bottomMargin=self.bottom_margin)

        doc.addPageTemplates([
            PageTemplate(
                id='FirstPage',
                frames=self._get_first_page_frames(doc),
                onPage=self._on_first_page,
                pagesize=self.pagesize
            ),
            PageTemplate(
                id='OtherPages',
                frames=self._get_other_page_frames(doc),
                onPage=self._on_other_page,
                pagesize=self.pagesize
            )
        ])
        story = self._get_story(doc)
        doc.build(story, canvasmaker=self.canvas_class)
        return doc

    def generate(self, invoice: Invoice):
        self.invoice = invoice
        buffer = BytesIO()
        self._build_doc(buffer)
        buffer.seek(0)
        return 'invoice.pdf', 'application/pdf', buffer.read()

    def _clean_text(self, text, tags=None):
        return self._normalize(bleach.clean(
            text,
            tags=set(tags) if tags else set()
        ).strip().replace('<br>', '<br />').replace('\n', '<br />\n'))


class PaidMarker(Flowable):
    def __init__(self, text='paid', color=None, font='OpenSansBd', size=20):
        super().__init__()
        self.text = text
        self.color = color
        self.font = font
        self.size = size
        self._showBoundary = True

    def wrap(self, availwidth, availheight):
        # Fake a size, we don't care if we exceed the table
        return 10, self.size / 2

    def draw(self):
        self.canv.translate(0, - self.size / 2)
        self.canv.rotate(2)
        self.canv.setFont(self.font, self.size)
        self.canv.setFillColor(self.color)
        width = self.canv.stringWidth(self.text, self.font, self.size)
        self.canv.drawRightString(0, 0, self.text)

        self.canv.setStrokeColor(self.color)
        self.canv.roundRect(-width - self.size / 2, -self.size / 4, width + self.size, self.size + self.size / 4, 3)


class InvoiceSectionStrategy(ABC):
    """
    Strategy for one part of the classic invoice PDF story (pretix_630 #66).

    Returns Platypus Flowables and may mutate ``InvoiceStoryContext`` (e.g. line-item table data).
    Subclasses of ``ClassicInvoiceRenderer`` can extend ``invoice_story_strategies`` to add sections
    without changing the compositor loop.
    """
    @abstractmethod
    def render(self, ctx: "InvoiceStoryContext") -> List[Flowable]:
        raise NotImplementedError()


class InvoiceStoryContext:
    """Mutable per-document state shared while building the main invoice table and tax summary."""

    def __init__(self, renderer: "ClassicInvoiceRenderer", doc):
        self.renderer = renderer
        self.doc = doc
        self.invoice = renderer.invoice
        self.all_lines = list(self.invoice.lines.all())
        self.has_taxes = any(il.tax_value for il in self.all_lines) or self.invoice.reverse_charge
        self.header_dates = renderer._date_range_in_header()
        self.tz = self.invoice.event.timezone
        self.has_multiple_service_dates = len(set(
            (il.period_start, il.period_end) for il in self.all_lines
        )) > 1
        self.request_show_service_date = False
        self.taxvalue_map = defaultdict(Decimal)
        self.grossvalue_map = defaultdict(Decimal)
        self.total = Decimal("0.00")
        self.tstyledata = [
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, -1), renderer.font_regular),
            ("FONTNAME", (0, 0), (-1, 0), renderer.font_bold),
            ("FONTNAME", (0, -1), (-1, -1), renderer.font_bold),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ]
        if self.has_taxes:
            self.tdata = [(
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Description")), renderer.stylesheet["Bold"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Qty")), renderer.stylesheet["BoldRightNoSplit"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Tax rate")), renderer.stylesheet["BoldRightNoSplit"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Net")), renderer.stylesheet["BoldRightNoSplit"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Gross")), renderer.stylesheet["BoldRightNoSplit"]),
            )]
        else:
            self.tdata = [(
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Description")), renderer.stylesheet["Bold"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Qty")), renderer.stylesheet["BoldRightNoSplit"]),
                FontFallbackParagraph(renderer._normalize(pgettext("invoice", "Amount")), renderer.stylesheet["BoldRightNoSplit"]),
            )]
        if self.has_taxes:
            self.colwidths = [a * doc.width for a in (.50, .05, .15, .15, .15)]
        else:
            self.colwidths = [a * doc.width for a in (.65, .20, .15)]


class InvoicePdfTitleHeaderSection(InvoiceSectionStrategy):
    """Page template switches and document title (Invoice / Tax Invoice / Cancellation)."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        return [
            NextPageTemplate("FirstPage"),
            FontFallbackParagraph(
                r._normalize(
                    pgettext("invoice", "Tax Invoice") if str(r.invoice.invoice_from_country) == "AU"
                    else pgettext("invoice", "Invoice")
                ) if not r.invoice.is_cancellation else r._normalize(pgettext("invoice", "Cancellation")),
                r.stylesheet["Heading1"],
            ),
            Spacer(1, 5 * mm),
            NextPageTemplate("OtherPages"),
        ]


class InvoicePdfIntroSection(InvoiceSectionStrategy):
    """Delegates to ``ClassicInvoiceRenderer._get_intro()`` (unchanged API per #66)."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        return list(ctx.renderer._get_intro())


class InvoicePdfLineItemsSection(InvoiceSectionStrategy):
    """Grouped line items: description cells, optional periods, tax columns — fills ``ctx.tdata`` / tax maps."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        doc = ctx.doc

        def _group_key(line):
            return (
                line.description, line.tax_rate, line.tax_name, line.net_value, line.gross_value, line.subevent,
                line.period_start, line.period_end,
            )

        def day(dt: datetime.datetime) -> datetime.date:
            if dt is None:
                return None
            return dt.astimezone(ctx.tz).date()

        for (description, tax_rate, tax_name, net_value, gross_value, subevent, period_start, period_end), lines in addon_aware_groupby(
            ctx.all_lines,
            key=_group_key,
            is_addon=lambda l: l.description.startswith("  +"),
        ):
            description_p_list = []
            description = description.replace("<br>", "<br />").replace("<br />\n", "\n").replace("<br />", "\n")
            curr_description = description.split("\n", maxsplit=1)[0]
            cellpadding = 6
            max_width = ctx.colwidths[0] - cellpadding
            max_height = r.stylesheet["Normal"].leading * 5
            p_style = r.stylesheet["Normal"]
            for __ in range(1000):
                p = FontFallbackParagraph(
                    r._clean_text(curr_description, tags=["br"]),
                    p_style,
                )
                h = p.wrap(max_width, doc.height)[1]
                if h <= max_height:
                    description_p_list.append(p)
                    if curr_description == description:
                        break
                    description = description[len(curr_description):].lstrip()
                    curr_description = description.split("\n", maxsplit=1)[0]
                    max_width = sum(ctx.colwidths[0:3 if ctx.has_taxes else 2]) - cellpadding
                    max_height = r.stylesheet["Fineprint"].leading * 8
                    p_style = r.stylesheet["Fineprint"]
                    continue
                if not description_p_list:
                    max_height = r.stylesheet["Normal"].leading
                if h > max_height * 1.1:
                    wrap_to = math.ceil(len(curr_description) * max_height * 1.1 / h)
                else:
                    wrap_to = max(len(curr_description) - 10, math.ceil(len(curr_description) * 0.95))
                curr_description = textwrap.wrap(curr_description, wrap_to, replace_whitespace=False, drop_whitespace=False)[0]

            period_start_day = day(period_start)
            period_end_day = day(period_end)
            if period_start and period_end and period_end_day != period_start_day:
                if period_start_day == ctx.header_dates[0] and period_end_day == ctx.header_dates[1]:
                    period_line = ""
                elif (r.event.has_subevents and subevent and day(subevent.date_from) == period_start_day and
                      day(subevent.date_to) == period_end_day):
                    period_line = ""
                else:
                    period_line = f"{date_format(period_start_day, 'SHORT_DATE_FORMAT')} – {date_format(period_end_day, 'SHORT_DATE_FORMAT')}"
            elif period_start or period_end:
                delivery_day = period_end_day or period_start_day
                if delivery_day in ctx.header_dates:
                    period_line = ""
                elif r.event.has_subevents and subevent and delivery_day in (day(subevent.date_from), day(subevent.date_to)):
                    period_line = ""
                elif (delivery_day == r.invoice.date) and ctx.header_dates[0] is None:
                    period_line = ""
                else:
                    period_line = date_format(delivery_day, "SHORT_DATE_FORMAT")
            else:
                period_line = ""

            if not ctx.has_multiple_service_dates and period_line:
                ctx.request_show_service_date = period_line
            elif period_line:
                description_p_list.append(FontFallbackParagraph(
                    period_line,
                    r.stylesheet["Fineprint"],
                ))

            lines = list(lines)
            if ctx.has_taxes:
                if len(lines) > 1:
                    single_price_line = pgettext("invoice", "Single price: {net_price} net / {gross_price} gross").format(
                        net_price=money_filter(net_value, r.invoice.event.currency),
                        gross_price=money_filter(gross_value, r.invoice.event.currency),
                    )
                    description_p_list.append(FontFallbackParagraph(
                        single_price_line,
                        r.stylesheet["Fineprint"],
                    ))
                ctx.tdata.append((
                    description_p_list.pop(0),
                    str(len(lines)),
                    localize(tax_rate) + " %",
                    FontFallbackParagraph(
                        money_filter(net_value * len(lines), r.invoice.event.currency).replace("\xa0", " "),
                        r.stylesheet["NormalRight"],
                    ),
                    FontFallbackParagraph(
                        money_filter(gross_value * len(lines), r.invoice.event.currency).replace("\xa0", " "),
                        r.stylesheet["NormalRight"],
                    ),
                ))
                for p in description_p_list:
                    ctx.tdata.append((p, "", "", "", ""))
                    ctx.tstyledata.append((
                        "SPAN",
                        (0, len(ctx.tdata) - 1),
                        (2, len(ctx.tdata) - 1),
                    ))
            else:
                if len(lines) > 1:
                    single_price_line = pgettext("invoice", "Single price: {price}").format(
                        price=money_filter(gross_value, r.invoice.event.currency),
                    )
                    description_p_list.append(FontFallbackParagraph(
                        single_price_line,
                        r.stylesheet["Fineprint"],
                    ))
                ctx.tdata.append((
                    description_p_list.pop(0),
                    str(len(lines)),
                    FontFallbackParagraph(
                        money_filter(gross_value * len(lines), r.invoice.event.currency).replace("\xa0", " "),
                        r.stylesheet["NormalRight"],
                    ),
                ))
                for p in description_p_list:
                    ctx.tdata.append((p, "", ""))
                    ctx.tstyledata.append((
                        "SPAN",
                        (0, len(ctx.tdata) - 1),
                        (1, len(ctx.tdata) - 1),
                    ))

            ctx.tstyledata += [
                (
                    "BOTTOMPADDING",
                    (0, len(ctx.tdata) - len(description_p_list)),
                    (-1, len(ctx.tdata) - 2),
                    0,
                ),
                (
                    "TOPPADDING",
                    (0, len(ctx.tdata) - len(description_p_list)),
                    (-1, len(ctx.tdata) - 1),
                    0,
                ),
            ]
            ctx.taxvalue_map[tax_rate, tax_name] += (gross_value - net_value) * len(lines)
            ctx.grossvalue_map[tax_rate, tax_name] += gross_value * len(lines)
            ctx.total += gross_value * len(lines)
        return []


class InvoicePdfInvoiceTotalRowSection(InvoiceSectionStrategy):
    """Invoice total row appended to the main line-items table."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        if ctx.has_taxes:
            ctx.tdata.append([
                FontFallbackParagraph(r._normalize(pgettext("invoice", "Invoice total")), r.stylesheet["Bold"]),
                "", "", "",
                money_filter(ctx.total, r.invoice.event.currency),
            ])
        else:
            ctx.tdata.append([
                FontFallbackParagraph(r._normalize(pgettext("invoice", "Invoice total")), r.stylesheet["Bold"]),
                "",
                money_filter(ctx.total, r.invoice.event.currency),
            ])
        return []


class InvoicePdfEmbeddedPaymentsTableSection(InvoiceSectionStrategy):
    """Pending balance, gift card, or paid marker rows inside the same table as line items."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        inv = r.invoice
        if inv.is_cancellation:
            return []
        if inv.event.settings.invoice_show_payments and inv.order.status == Order.STATUS_PENDING:
            pending_sum = inv.order.pending_sum
            if pending_sum != ctx.total:
                ctx.tdata.append(
                    [FontFallbackParagraph(r._normalize(pgettext("invoice", "Received payments")), r.stylesheet["Normal"])]
                    + (["", "", ""] if ctx.has_taxes else [""])
                    + [money_filter(pending_sum - ctx.total, inv.event.currency)]
                )
                ctx.tdata.append(
                    [FontFallbackParagraph(r._normalize(pgettext("invoice", "Outstanding payments")), r.stylesheet["Bold"])]
                    + (["", "", ""] if ctx.has_taxes else [""])
                    + [money_filter(pending_sum, inv.event.currency)]
                )
                ctx.tstyledata += [
                    ("FONTNAME", (0, len(ctx.tdata) - 3), (-1, len(ctx.tdata) - 3), r.font_bold),
                ]
        elif inv.event.settings.invoice_show_payments and inv.order.payments.filter(
                state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED), provider="giftcard"
        ).exists():
            giftcard_sum = inv.order.payments.filter(
                state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED),
                provider="giftcard",
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
            ctx.tdata.append(
                [FontFallbackParagraph(r._normalize(pgettext("invoice", "Paid by gift card")), r.stylesheet["Normal"])]
                + (["", "", ""] if ctx.has_taxes else [""])
                + [money_filter(giftcard_sum, inv.event.currency)]
            )
            ctx.tdata.append(
                [FontFallbackParagraph(r._normalize(pgettext("invoice", "Remaining amount")), r.stylesheet["Bold"])]
                + (["", "", ""] if ctx.has_taxes else [""])
                + [money_filter(ctx.total - giftcard_sum, inv.event.currency)]
            )
            ctx.tstyledata += [
                ("FONTNAME", (0, len(ctx.tdata) - 3), (-1, len(ctx.tdata) - 3), r.font_bold),
            ]
        elif inv.payment_provider_stamp:
            pm = PaidMarker(
                text=r._normalize(inv.payment_provider_stamp),
                color=colors.HexColor(r.event.settings.theme_color_success),
                font=r.font_bold,
                size=16,
            )
            ctx.tdata[-1][-2] = pm
        return []


class InvoicePdfMainLineItemsTableSection(InvoiceSectionStrategy):
    """Turn accumulated ``tdata`` / ``tstyledata`` into the main Platypus ``Table``."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        table = Table(ctx.tdata, colWidths=ctx.colwidths, repeatRows=1)
        table.setStyle(TableStyle(ctx.tstyledata))
        return [table]


class InvoicePdfPostTableNotesSection(InvoiceSectionStrategy):
    """Spacing and optional consolidated service-period line after the main table."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        out = [Spacer(1, 10 * mm)]
        if ctx.request_show_service_date:
            out.append(FontFallbackParagraph(
                r._normalize(pgettext("invoice", "Invoice period: {daterange}").format(daterange=ctx.request_show_service_date)),
                r.stylesheet["Normal"],
            ))
        return out


class InvoicePdfPaymentProviderTextSection(InvoiceSectionStrategy):
    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        if not r.invoice.payment_provider_text:
            return []
        return [
            FontFallbackParagraph(
                r._normalize(r.invoice.payment_provider_text),
                r.stylesheet["Normal"],
            ),
        ]


class InvoicePdfAdditionalTextSection(InvoiceSectionStrategy):
    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        if not r.invoice.additional_text:
            return []
        out = []
        if r.invoice.payment_provider_text:
            out.append(Spacer(1, 3 * mm))
        out.append(
            FontFallbackParagraph(
                r._clean_text(r.invoice.additional_text, tags=["br"]),
                r.stylesheet["Normal"],
            )
        )
        out.append(Spacer(1, 5 * mm))
        return out


class InvoicePdfTaxSummarySection(InvoiceSectionStrategy):
    """Included taxes table and optional foreign-currency breakdown when tax rows exist."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer
        doc = ctx.doc
        out: List[Flowable] = []
        tstyledata = [
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, -1), r.font_regular),
        ]
        thead = [
            FontFallbackParagraph(r._normalize(pgettext("invoice", "Tax rate")), r.stylesheet["Fineprint"]),
            FontFallbackParagraph(r._normalize(pgettext("invoice", "Net value")), r.stylesheet["FineprintRight"]),
            FontFallbackParagraph(r._normalize(pgettext("invoice", "Gross value")), r.stylesheet["FineprintRight"]),
            FontFallbackParagraph(r._normalize(pgettext("invoice", "Tax")), r.stylesheet["FineprintRight"]),
            "",
        ]
        tdata = [thead]
        for idx, gross in ctx.grossvalue_map.items():
            rate, name = idx
            if rate == 0 and gross == 0:
                continue
            tax = ctx.taxvalue_map[idx]
            tdata.append([
                FontFallbackParagraph(r._normalize(localize(rate) + " % " + name), r.stylesheet["Fineprint"]),
                money_filter(gross - tax, r.invoice.event.currency),
                money_filter(gross, r.invoice.event.currency),
                money_filter(tax, r.invoice.event.currency),
                "",
            ])

        def fmt(val):
            try:
                return money_filter(val, r.invoice.foreign_currency_display)
            except ValueError:
                return localize(val) + " " + r.invoice.foreign_currency_display

        if any(rate != 0 and gross != 0 for (rate, name), gross in ctx.grossvalue_map.items()) and ctx.has_taxes:
            colwidths = [a * doc.width for a in (.25, .15, .15, .15, .3)]
            table = Table(tdata, colWidths=colwidths, repeatRows=2, hAlign=TA_LEFT)
            table.setStyle(TableStyle(tstyledata))
            out.append(Spacer(5 * mm, 5 * mm))
            out.append(KeepTogether([
                FontFallbackParagraph(r._normalize(pgettext("invoice", "Included taxes")), r.stylesheet["FineprintHeading"]),
                table,
            ]))
            if r.invoice.foreign_currency_display and r.invoice.foreign_currency_rate:
                tdata_fc = [thead]
                for idx, gross in ctx.grossvalue_map.items():
                    rate, name = idx
                    if rate == 0:
                        continue
                    tax = ctx.taxvalue_map[idx]
                    gross_fc = round_decimal(gross * r.invoice.foreign_currency_rate)
                    tax_fc = round_decimal(tax * r.invoice.foreign_currency_rate)
                    net_fc = gross_fc - tax_fc
                    tdata_fc.append([
                        FontFallbackParagraph(r._normalize(localize(rate) + " % " + name), r.stylesheet["Fineprint"]),
                        fmt(net_fc), fmt(gross_fc), fmt(tax_fc), "",
                    ])
                table_fc = Table(tdata_fc, colWidths=colwidths, repeatRows=2, hAlign=TA_LEFT)
                table_fc.setStyle(TableStyle(tstyledata))
                out.append(KeepTogether([
                    Spacer(1, height=2 * mm),
                    FontFallbackParagraph(
                        r._normalize(pgettext(
                            "invoice", "Using the conversion rate of 1:{rate} as published by the {authority} on "
                                       "{date}, this corresponds to:"
                        ).format(
                            rate=localize(r.invoice.foreign_currency_rate),
                            authority=SOURCE_NAMES.get(r.invoice.foreign_currency_source, "?"),
                            date=date_format(r.invoice.foreign_currency_rate_date, "SHORT_DATE_FORMAT"),
                        )),
                        r.stylesheet["Fineprint"],
                    ),
                    Spacer(1, height=3 * mm),
                    table_fc,
                ]))
        return out


class InvoicePdfStandaloneForeignCurrencySection(InvoiceSectionStrategy):
    """Foreign total note when no included-taxes table is shown."""

    def render(self, ctx: InvoiceStoryContext) -> List[Flowable]:
        r = ctx.renderer

        def fmt(val):
            try:
                return money_filter(val, r.invoice.foreign_currency_display)
            except ValueError:
                return localize(val) + " " + r.invoice.foreign_currency_display

        if any(rate != 0 and gross != 0 for (rate, name), gross in ctx.grossvalue_map.items()) and ctx.has_taxes:
            return []
        if r.invoice.foreign_currency_display and r.invoice.foreign_currency_rate:
            foreign_total = round_decimal(ctx.total * r.invoice.foreign_currency_rate)
            return [
                Spacer(1, 5 * mm),
                FontFallbackParagraph(
                    r._normalize(pgettext(
                        "invoice", "Using the conversion rate of 1:{rate} as published by the {authority} on "
                                   "{date}, the invoice total corresponds to {total}."
                    ).format(
                        rate=localize(r.invoice.foreign_currency_rate),
                        date=date_format(r.invoice.foreign_currency_rate_date, "SHORT_DATE_FORMAT"),
                        authority=SOURCE_NAMES.get(r.invoice.foreign_currency_source, "?"),
                        total=fmt(foreign_total),
                    )),
                    r.stylesheet["Fineprint"],
                ),
            ]
        return []


DEFAULT_CLASSIC_INVOICE_STORY_STRATEGIES = (
    InvoicePdfTitleHeaderSection(),
    InvoicePdfIntroSection(),
    InvoicePdfLineItemsSection(),
    InvoicePdfInvoiceTotalRowSection(),
    InvoicePdfEmbeddedPaymentsTableSection(),
    InvoicePdfMainLineItemsTableSection(),
    InvoicePdfPostTableNotesSection(),
    InvoicePdfPaymentProviderTextSection(),
    InvoicePdfAdditionalTextSection(),
    InvoicePdfTaxSummarySection(),
    InvoicePdfStandaloneForeignCurrencySection(),
)


class ClassicInvoiceRenderer(BaseReportlabInvoiceRenderer):
    identifier = 'classic'
    verbose_name = pgettext('invoice', 'Classic renderer (pretix 1.0)')
    # Ordered PDF body sections (Strategy pattern; pretix_630#66). Subclasses may override this tuple.
    invoice_story_strategies = DEFAULT_CLASSIC_INVOICE_STORY_STRATEGIES

    def canvas_class(self, *args, **kwargs):
        kwargs['font_regular'] = self.font_regular
        return NumberedCanvas(*args, **kwargs)

    def _on_other_page(self, canvas: Canvas, doc):
        canvas.saveState()
        canvas.setFont(self.font_regular, 8)

        for i, line in enumerate(self.invoice.footer_text.split('\n')[::-1]):
            canvas.drawCentredString(self.pagesize[0] / 2, 25 + (3.5 * i) * mm, self._normalize(line.strip()))

        canvas.restoreState()

    invoice_to_width = 85 * mm
    invoice_to_height = 50 * mm
    invoice_to_left = 25 * mm
    invoice_to_top = 52 * mm

    def _draw_invoice_to(self, canvas):
        p = FontFallbackParagraph(self._clean_text(self.invoice.address_invoice_to),
                                  style=self.stylesheet['Normal'])
        p.wrapOn(canvas, self.invoice_to_width, self.invoice_to_height)
        p_size = p.wrap(self.invoice_to_width, self.invoice_to_height)
        p.drawOn(canvas, self.invoice_to_left, self.pagesize[1] - p_size[1] - self.invoice_to_top)

    invoice_from_width = 70 * mm
    invoice_from_height = 50 * mm
    invoice_from_left = 25 * mm
    invoice_from_top = 17 * mm

    def _draw_invoice_from(self, canvas):
        p = FontFallbackParagraph(
            self._clean_text(self.invoice.full_invoice_from),
            style=self.stylesheet['InvoiceFrom']
        )
        p.wrapOn(canvas, self.invoice_from_width, self.invoice_from_height)
        p_size = p.wrap(self.invoice_from_width, self.invoice_from_height)
        p.drawOn(canvas, self.invoice_from_left, self.pagesize[1] - p_size[1] - self.invoice_from_top)

    def _draw_invoice_from_label(self, canvas):
        textobject = canvas.beginText(25 * mm, (297 - 15) * mm)
        textobject.setFont(self.font_bold, 8)
        textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Invoice from'))))
        canvas.drawText(textobject)

    def _draw_invoice_to_label(self, canvas):
        textobject = canvas.beginText(25 * mm, (297 - 50) * mm)
        textobject.setFont(self.font_bold, 8)
        textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Invoice to'))))
        canvas.drawText(textobject)

    logo_width = 25 * mm
    logo_height = 25 * mm
    logo_left = 95 * mm
    logo_top = 13 * mm
    logo_anchor = 'n'

    def _draw_logo(self, canvas):
        if self.invoice.event.settings.invoice_logo_image:
            logo_file = self.invoice.event.settings.get('invoice_logo_image', binary_file=True)
            ir = ThumbnailingImageReader(logo_file)
            try:
                ir.resize(self.logo_width, self.logo_height, 300)
            except:
                logger.exception("Can not resize image")
                pass
            try:
                # Valid ZUGFeRD invoices must be compliant with PDF/A-3. pretix-zugferd ensures this by passing them
                # through ghost script. Unfortunately, if the logo contains transparency, this will still fail.
                # I was unable to figure out a way to fix this in GhostScript, so the easy fix is to remove the
                # transparency, as our invoices always have a white background anyways.
                ir.remove_transparency()
            except:
                logger.exception("Can not remove transparency from logo")
                pass
            canvas.drawImage(ir,
                             self.logo_left,
                             self.pagesize[1] - self.logo_height - self.logo_top,
                             width=self.logo_width, height=self.logo_height,
                             preserveAspectRatio=True, anchor=self.logo_anchor,
                             mask='auto')

    def _draw_metadata(self, canvas):
        textobject = canvas.beginText(125 * mm, (297 - 38) * mm)
        textobject.setFont(self.font_bold, 8)
        textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Order code'))))
        textobject.moveCursor(0, 5)
        textobject.setFont(self.font_regular, 10)
        textobject.textLine(self._normalize(self.invoice.order.full_code))
        canvas.drawText(textobject)

        textobject = canvas.beginText(125 * mm, (297 - 50) * mm)
        textobject.setFont(self.font_bold, 8)
        if self.invoice.is_cancellation:
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Cancellation number'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(self.invoice.number))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_bold, 8)
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Original invoice'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(self.invoice.refers.number))
        else:
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Invoice number'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(self.invoice.number))
        textobject.moveCursor(0, 5)

        if self.invoice.is_cancellation:
            textobject.setFont(self.font_bold, 8)
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Cancellation date'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(date_format(self.invoice.date, "DATE_FORMAT")))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_bold, 8)
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Original invoice date'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(date_format(self.invoice.refers.date, "DATE_FORMAT")))
            textobject.moveCursor(0, 5)
        else:
            textobject.setFont(self.font_bold, 8)
            textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Invoice date'))))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_regular, 10)
            textobject.textLine(self._normalize(date_format(self.invoice.date, "DATE_FORMAT")))
            textobject.moveCursor(0, 5)

        canvas.drawText(textobject)

    event_left = 125 * mm
    event_top = 17 * mm
    event_width = 65 * mm
    event_height = 50 * mm

    def _draw_event_label(self, canvas):
        textobject = canvas.beginText(125 * mm, (297 - 15) * mm)
        textobject.setFont(self.font_bold, 8)
        textobject.textLine(self._normalize(self._upper(pgettext('invoice', 'Event'))))
        canvas.drawText(textobject)

    def _date_range_in_header(self):
        if self.invoice.event.has_subevents or not self.invoice.event.settings.show_dates_on_frontpage:
            return None, None
        tz = self.invoice.event.timezone
        show_end_date = (
            self.invoice.event.settings.show_date_to and
            self.invoice.event.date_to and
            self.invoice.event.date_to.astimezone(tz).date() != self.invoice.event.date_from.astimezone(tz).date()
        )
        if show_end_date:
            return self.invoice.event.date_from.astimezone(tz).date(), self.invoice.event.date_to.astimezone(tz).date()
        else:
            return self.invoice.event.date_from.astimezone(tz).date(), None

    def _draw_event(self, canvas):
        def shorten(txt):
            txt = str(txt)
            txt = bleach.clean(txt, tags=set()).strip()
            p = FontFallbackParagraph(self._normalize(txt.strip().replace('\n', '<br />\n')), style=self.stylesheet['Normal'])
            p_size = p.wrap(self.event_width, self.event_height)

            while p_size[1] > 2 * self.stylesheet['Normal'].leading:
                txt = ' '.join(txt.replace('…', '').split()[:-1]) + '…'
                p = FontFallbackParagraph(self._normalize(txt.strip().replace('\n', '<br />\n')), style=self.stylesheet['Normal'])
                p_size = p.wrap(self.event_width, self.event_height)
            return txt

        d_from, d_to = self._date_range_in_header()
        if d_from and d_to:
            p_str = (
                shorten(self.invoice.event.name) + '\n' +
                pgettext('invoice', '{from_date}\nuntil {to_date}').format(
                    from_date=date_format(d_from, "DATE_FORMAT"),
                    to_date=date_format(d_to, "DATE_FORMAT"),
                )
            )
        elif d_from:
            p_str = shorten(self.invoice.event.name) + '\n' + date_format(d_from, "DATE_FORMAT")
        else:
            p_str = shorten(self.invoice.event.name)

        p = FontFallbackParagraph(self._normalize(p_str.strip().replace('\n', '<br />\n')), style=self.stylesheet['Normal'])
        p.wrapOn(canvas, self.event_width, self.event_height)
        p_size = p.wrap(self.event_width, self.event_height)
        p.drawOn(canvas, self.event_left, self.pagesize[1] - self.event_top - p_size[1])
        self._draw_event_label(canvas)

    def _draw_footer(self, canvas):
        canvas.setFont(self.font_regular, 8)
        for i, line in enumerate(self.invoice.footer_text.split('\n')[::-1]):
            canvas.drawCentredString(self.pagesize[0] / 2, 25 + (3.5 * i) * mm, self._normalize(line.strip()))

    def _draw_testmode(self, canvas):
        if self.invoice.order.testmode:
            canvas.saveState()
            canvas.setFont(self.font_bold, 30)
            canvas.setFillColorRGB(32, 0, 0)
            canvas.drawRightString(self.pagesize[0] - 20 * mm, (297 - 100) * mm, self._normalize(gettext('TEST MODE')))
            canvas.restoreState()

    def _draw_watermark(self, canvas):
        watermark = self.invoice.transmission_type_instance.pdf_watermark()
        if watermark:
            canvas.saveState()
            for font_size in range(200, 20, -10):
                width = stringWidth(watermark, self.font_bold, font_size)
                if width < self.pagesize[0]:
                    break

            canvas.translate(self.pagesize[0] / 2, self.pagesize[1] / 2)
            canvas.rotate(math.atan(self.pagesize[1] / self.pagesize[0]) / math.pi * 180)
            canvas.setFont(self.font_bold, font_size)
            canvas.setFillColorRGB(.92, .92, .92)
            canvas.drawCentredString(0, - font_size / 2, self._normalize(watermark))
            canvas.restoreState()

    def _on_first_page(self, canvas: Canvas, doc):
        canvas.setCreator('pretix.eu')
        canvas.setTitle(pgettext('invoice', 'Invoice {num}').format(num=self.invoice.number))

        canvas.saveState()
        self._draw_watermark(canvas)
        self._draw_footer(canvas)
        self._draw_testmode(canvas)
        self._draw_invoice_from_label(canvas)
        self._draw_invoice_from(canvas)
        self._draw_invoice_to_label(canvas)
        self._draw_invoice_to(canvas)
        self._draw_metadata(canvas)
        self._draw_logo(canvas)
        self._draw_event(canvas)
        canvas.restoreState()

    def _get_first_page_frames(self, doc):
        footer_length = 3.5 * len(self.invoice.footer_text.split('\n')) * mm
        return [
            Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - 75 * mm,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=footer_length,
                  id='normal')
        ]

    def _get_other_page_frames(self, doc):
        footer_length = 3.5 * len(self.invoice.footer_text.split('\n')) * mm
        return [
            Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=footer_length,
                  id='normal')
        ]

    def _get_intro(self):
        story = []

        type_info_text = self.invoice.transmission_type_instance.pdf_info_text()
        if type_info_text:
            story.append(FontFallbackParagraph(
                type_info_text,
                self.stylesheet['WarningBlock']
            ))

        if self.invoice.custom_field:
            story.append(FontFallbackParagraph(
                '{}: {}'.format(
                    self._clean_text(str(self.invoice.event.settings.invoice_address_custom_field)),
                    self._clean_text(self.invoice.custom_field),
                ),
                self.stylesheet['Normal']
            ))

        if self.invoice.internal_reference:
            story.append(FontFallbackParagraph(
                self._normalize(pgettext('invoice', 'Customer reference: {reference}').format(
                    reference=self._clean_text(self.invoice.internal_reference),
                )),
                self.stylesheet['Normal']
            ))

        if self.invoice.invoice_to_vat_id:
            story.append(FontFallbackParagraph(
                self._normalize(pgettext('invoice', 'Customer VAT ID')) + ': ' +
                self._clean_text(self.invoice.invoice_to_vat_id),
                self.stylesheet['Normal']
            ))

        if self.invoice.invoice_to_beneficiary:
            story.append(FontFallbackParagraph(
                self._normalize(pgettext('invoice', 'Beneficiary')) + ':<br />' +
                self._clean_text(self.invoice.invoice_to_beneficiary),
                self.stylesheet['Normal']
            ))

        if self.invoice.introductory_text:
            # While all intro fields are appended without any blank lines; we do want one before the optional intro
            # text. However, if there are no prior intro fields, adding an additional spacer will waste space.
            if story:
                story.append(Spacer(1, 5 * mm))

            story.append(FontFallbackParagraph(
                self._clean_text(self.invoice.introductory_text, tags=['br']),
                self.stylesheet['Normal']
            ))
            story.append(Spacer(1, 5 * mm))

        return story

    def _get_story(self, doc):
        # Compositor: shared context plus ordered section strategies (gbpatil151/pretix_630#66).
        ctx = InvoiceStoryContext(self, doc)
        story: List[Flowable] = []
        for section in self.invoice_story_strategies:
            story.extend(section.render(ctx))
        return story


class Modern1Renderer(ClassicInvoiceRenderer):
    identifier = 'modern1'
    verbose_name = gettext_lazy('Default invoice renderer (European-style letter)')
    bottom_margin = 16.9 * mm
    top_margin = 16.9 * mm
    right_margin = 20 * mm
    invoice_to_height = 27.3 * mm
    invoice_to_width = 80 * mm
    invoice_to_left = 25 * mm
    invoice_to_top = (40 + 17.7) * mm
    invoice_from_left = 125 * mm
    invoice_from_top = 50 * mm
    invoice_from_width = pagesizes.A4[0] - invoice_from_left - right_margin
    invoice_from_height = 50 * mm

    logo_width = 75 * mm
    logo_height = 25 * mm
    logo_left = pagesizes.A4[0] - logo_width - right_margin
    logo_top = top_margin
    logo_anchor = 'e'

    event_left = 25 * mm
    event_top = top_margin
    event_width = 80 * mm
    event_height = 25 * mm

    def _get_stylesheet(self):
        stylesheet = super()._get_stylesheet()
        stylesheet.add(ParagraphStyle(name='Sender', fontName=self.font_regular, fontSize=8, leading=10))
        stylesheet['InvoiceFrom'].alignment = TA_RIGHT
        return stylesheet

    def _draw_invoice_from(self, canvas):
        if not self.invoice.invoice_from:
            return
        c = [
            self._clean_text(l)
            for l in self.invoice.address_invoice_from.strip().split('\n')
        ]
        p = FontFallbackParagraph(self._normalize(' · '.join(c)), style=self.stylesheet['Sender'])
        p.wrapOn(canvas, self.invoice_to_width, 15.7 * mm)
        p.drawOn(canvas, self.invoice_to_left, self.pagesize[1] - self.invoice_to_top + 2 * mm)
        super()._draw_invoice_from(canvas)

    def _draw_invoice_to_label(self, canvas):
        pass

    def _draw_invoice_from_label(self, canvas):
        pass

    def _draw_event_label(self, canvas):
        pass

    def _get_first_page_frames(self, doc):
        footer_length = 3.5 * len(self.invoice.footer_text.split('\n')) * mm
        if self.event.settings.invoice_renderer_highlight_order_code:
            margin_top = 100 * mm
        else:
            margin_top = 95 * mm
        return [
            Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - margin_top,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=footer_length,
                  id='normal')
        ]

    def _draw_metadata(self, canvas):
        # Draws the "invoice number -- date" line. This has gotten a little more complicated since we
        # encountered some events with very long invoice numbers. In this case, we automatically reduce
        # the font size until it fits.
        begin_top = 100 * mm

        def _draw(label, value, value_size, x, width, bold=False, sublabel=None):
            if canvas.stringWidth(value, self.font_regular, value_size) > width and value_size > 6:
                return False
            textobject = canvas.beginText(x, self.pagesize[1] - begin_top)
            textobject.setFont(self.font_regular, 8)
            textobject.textLine(self._normalize(label))
            textobject.moveCursor(0, 5)
            textobject.setFont(self.font_bold if bold else self.font_regular, value_size)
            textobject.textLine(self._normalize(value))

            if sublabel:
                textobject.moveCursor(0, 1)
                textobject.setFont(self.font_regular, 8)
                textobject.textLine(self._normalize(sublabel))

            return textobject

        value_size = 10
        while value_size >= 5:
            if self.event.settings.invoice_renderer_highlight_order_code:
                kwargs = dict(bold=True, sublabel=pgettext('invoice', '(Please quote at all times.)'))
            else:
                kwargs = {}
            objects = [
                _draw(pgettext('invoice', 'Order code'), self.invoice.order.full_code, value_size, self.left_margin, 45 * mm, **kwargs)
            ]

            p = FontFallbackParagraph(
                self._normalize(date_format(self.invoice.date, "DATE_FORMAT")),
                style=ParagraphStyle(name=f'Normal{value_size}', fontName=self.font_regular, fontSize=value_size, leading=value_size * 1.2)
            )
            w = stringWidth(p.text, p.frags[0].fontName, p.frags[0].fontSize)
            p.wrapOn(canvas, w, 15 * mm)
            date_x = self.pagesize[0] - w - self.right_margin

            if self.invoice.is_cancellation:
                objects += [
                    _draw(pgettext('invoice', 'Cancellation number'), self.invoice.number,
                          value_size, self.left_margin + 50 * mm, 45 * mm),
                    _draw(pgettext('invoice', 'Original invoice'), self.invoice.refers.number,
                          value_size, self.left_margin + 100 * mm, date_x - self.left_margin - 100 * mm - 5 * mm),
                ]
            else:
                objects += [
                    _draw(pgettext('invoice', 'Invoice number'), self.invoice.number,
                          value_size, self.left_margin + 70 * mm, date_x - self.left_margin - 70 * mm - 5 * mm),
                ]

            if all(objects):
                for o in objects:
                    canvas.drawText(o)
                break
            value_size -= 1

        p.drawOn(canvas, date_x, self.pagesize[1] - begin_top - 10 - 6)

        textobject = canvas.beginText(date_x, self.pagesize[1] - begin_top)
        textobject.setFont(self.font_regular, 8)
        if self.invoice.is_cancellation:
            textobject.textLine(self._normalize(pgettext('invoice', 'Cancellation date')))
        else:
            textobject.textLine(self._normalize(pgettext('invoice', 'Invoice date')))
        canvas.drawText(textobject)


class Modern1SimplifiedRenderer(Modern1Renderer):
    identifier = 'modern1simplified'
    verbose_name = gettext_lazy('Simplified invoice renderer')

    logo_left = Modern1Renderer.left_margin
    logo_width = pagesizes.A4[0] - Modern1Renderer.right_margin - logo_left
    logo_height = 25 * mm
    logo_top = 13 * mm
    logo_anchor = 'nw'

    def _draw_invoice_from(self, canvas):
        super(Modern1Renderer, self)._draw_invoice_from(canvas)

    def _draw_event(self, canvas):
        pass

    def _get_intro(self):
        i = []

        if not self.invoice.event.has_subevents and self.invoice.event.settings.show_dates_on_frontpage:
            i.append(FontFallbackParagraph(
                pgettext('invoice', 'Event date: {date_range}').format(
                    date_range=self.invoice.event.get_date_range_display(),
                ),
                self.stylesheet['Normal'],
            ))
            i.append(Spacer(2 * mm, 2 * mm))

        return i + super()._get_intro()


@receiver(register_invoice_renderers, dispatch_uid="invoice_renderer_classic")
def recv_classic(sender, **kwargs):
    return [ClassicInvoiceRenderer, Modern1Renderer, Modern1SimplifiedRenderer]

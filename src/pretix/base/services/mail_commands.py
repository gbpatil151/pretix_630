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
Command pattern for the mail_send_task pipeline.

Anti-pattern removed: Spaghetti Code
Design pattern applied: Command

``mail_send_task`` was a 377-line monolithic function with deeply nested
conditionals and interleaved concerns:
  - resolving and locking the OutgoingMail record
  - constructing the MIME email object and inlining CID images
  - attaching tickets, ical files, invoices, cached files, and other files
  - applying email filter signals (per-event and global)
  - delivering via the SMTP backend with retry logic
  - recording transmission results on invoices and logging

Each concern is now encapsulated in its own Command class. The task
function becomes a short orchestrator that runs commands in sequence.

Structure
---------
MailSendContext  -- shared mutable state passed between commands
MailSendCommand  -- abstract base (the Command interface)
ResolveMailCommand       -- load + lock OutgoingMail, guard status
BuildEmailCommand        -- construct MIME message, inline CID images
AttachFilesCommand       -- attach all file types (tickets/ical/invoices/other)
FilterAndSendCommand     -- apply email filters, deliver, retry on failure
RecordSuccessCommand     -- update invoice transmission records, log success
"""
import dataclasses
import logging
import mimetypes
import os
import re
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import List, Optional

import hashlib

from celery.exceptions import MaxRetriesExceededError
from django.conf import settings
from django.core.mail import SafeMIMEMultipart
from django.core.mail.message import SafeMIMEText
from django.db import transaction
from django.utils.timezone import now
from text_unidecode import unidecode

from pretix.base.models import Invoice
from pretix.base.models.mail import OutgoingMail
from pretix.base.services.tickets import get_tickets_for_order
from pretix.base.signals import email_filter, global_email_filter
from pretix.helpers import OF_SELF
from pretix.helpers.hierarkey import clean_filename
from pretix.presale.ical import get_private_icals

logger = logging.getLogger('pretix.base.mail')


@dataclasses.dataclass
class MailSendContext:
    """
    Shared mutable state threaded through the mail-send Command pipeline.

    Each Command reads what it needs and may write new attributes for
    downstream commands to consume.
    """
    # Set by caller (mail_send_task)
    outgoing_mail_pk: int
    celery_task: object  # the bound Celery task instance (self)

    # Populated by ResolveMailCommand
    outgoing_mail: Optional[OutgoingMail] = None

    # Populated by BuildEmailCommand
    email: Optional[object] = None  # CustomEmail instance

    # Populated by AttachFilesCommand
    invoices_to_mark_transmitted: List = dataclasses.field(default_factory=list)

    # Populated by BuildEmailCommand / AttachFilesCommand
    log_target: Optional[object] = None
    error_log_action_type: Optional[str] = None


class MailSendCommand(ABC):
    """Abstract base for all mail-send pipeline commands."""

    @abstractmethod
    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        """
        Execute this command step.

        :returns: ``False`` to abort the pipeline (treated as task return value),
                  ``None`` to continue.
        :raises: any exception to let Celery handle it (retries, etc.)
        """
        raise NotImplementedError  # pragma: no cover


class ResolveMailCommand(MailSendCommand):
    """
    Load and lock the OutgoingMail record; guard against duplicate delivery.

    Uses SELECT FOR UPDATE inside an atomic block so concurrent workers
    cannot deliver the same message twice.
    """

    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        with transaction.atomic():
            try:
                ctx.outgoing_mail = OutgoingMail.objects.select_for_update(of=OF_SELF).get(
                    pk=ctx.outgoing_mail_pk
                )
            except OutgoingMail.DoesNotExist:
                logger.info(f"Ignoring job for non existing email {ctx.outgoing_mail_pk}")
                return False

            om = ctx.outgoing_mail
            if om.status == OutgoingMail.STATUS_INFLIGHT:
                logger.info(f"Ignoring job for inflight email {om.guid}")
                return False
            elif om.status not in (OutgoingMail.STATUS_AWAITING_RETRY, OutgoingMail.STATUS_QUEUED):
                logger.info(f"Ignoring job for email {om.guid} in final state {om.status}")
                return False

            om.status = OutgoingMail.STATUS_INFLIGHT
            om.inflight_since = now()
            om.save(update_fields=["status", "inflight_since"])
        return None  # continue


class BuildEmailCommand(MailSendCommand):
    """
    Construct the MIME email object from the OutgoingMail record.

    Inlines any <img> tags as CID attachments so HTML emails render
    correctly in clients that block remote images.
    """

    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        from pretix.base.services.mail import (
            CustomEmail, attach_cid_images, replace_images_with_cid_paths,
        )
        om = ctx.outgoing_mail

        headers = dict(om.headers)
        headers.setdefault('X-PX-Correlation', str(om.guid))

        ctx.email = CustomEmail(
            subject=om.subject,
            body=om.body_plain,
            from_email=om.sender,
            to=om.to,
            cc=om.cc,
            bcc=om.bcc,
            headers=headers,
        )

        if om.body_html is not None:
            html_message = SafeMIMEMultipart(_subtype='related', encoding=settings.DEFAULT_CHARSET)
            html_with_cid, cid_images = replace_images_with_cid_paths(om.body_html)
            html_message.attach(SafeMIMEText(html_with_cid, 'html', settings.DEFAULT_CHARSET))
            attach_cid_images(html_message, cid_images, verify_ssl=True)
            ctx.email.attach_alternative(html_message, "multipart/related")

        ctx.log_target, ctx.error_log_action_type = om.log_parameters()
        return None  # continue


class AttachFilesCommand(MailSendCommand):
    """
    Attach all requested files to the email:
    tickets, iCal files, invoices, other storage files, and cached files.

    Retries via Celery if ticket files are not yet available.
    """

    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        from pretix.base.i18n import language
        from django.utils.translation import pgettext

        om = ctx.outgoing_mail
        email = ctx.email
        task = ctx.celery_task

        with om.scope_manager():
            # ---- Tickets ----
            if om.should_attach_tickets and om.order:
                with language(om.order.locale, om.event.settings.region):
                    args = []
                    attach_size = 0
                    for name, ct in get_tickets_for_order(om.order, base_position=om.orderposition):
                        try:
                            content = ct.file.read()
                            args.append((name, content, ct.type))
                            attach_size += len(content)
                        except Exception as e:
                            try:
                                logger.exception(f'Could not attach tickets to email {om.guid}, will retry')
                                retry_after = 60
                                om.error = "Tickets not ready"
                                om.error_detail = str(e)
                                om.sent = now()
                                om.status = OutgoingMail.STATUS_AWAITING_RETRY
                                om.retry_after = now() + timedelta(seconds=retry_after)
                                om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after",
                                                       "actual_attachments"])
                                task.retry(max_retries=5, countdown=retry_after)
                            except MaxRetriesExceededError:
                                logger.exception(
                                    f'Too many retries attaching tickets to email {om.guid}, skip attachment'
                                )

                    if attach_size * 1.37 < settings.FILE_UPLOAD_MAX_SIZE_EMAIL_ATTACHMENT - 1024 * 1024:
                        for a in args:
                            try:
                                email.attach(*a)
                            except Exception:
                                pass
                    else:
                        om.order.log_action(
                            'pretix.event.order.email.attachments.skipped',
                            data={
                                'subject': 'Attachments skipped',
                                'message': 'Attachment have not been send because {} bytes are likely too large to arrive.'.format(attach_size),
                                'recipient': '',
                                'invoices': [],
                            }
                        )

            # ---- iCal ----
            if om.should_attach_ical and om.order:
                fname = re.sub('[^a-zA-Z0-9 ]', '-', unidecode(pgettext('attachment_filename', 'Calendar invite')))
                icals = get_private_icals(
                    om.event,
                    [om.orderposition] if om.orderposition else om.order.positions.all()
                )
                for i, cal in enumerate(icals):
                    name = '{}{}.ics'.format(fname, f'-{i + 1}' if i > 0 else '')
                    email.attach(name, cal.serialize(), 'text/calendar')

            # ---- Invoices ----
            invoices_to_mark_transmitted = []
            for inv in om.should_attach_invoices.all():
                if inv.file:
                    try:
                        with language(om.order.locale if om.order else inv.locale, om.event.settings.region):
                            filename = pgettext('invoice', 'Invoice {num}').format(
                                num=inv.number
                            ).replace(' ', '_') + '.pdf'
                        if not re.match("^[a-zA-Z0-9-_%./,&:# ]+$", filename):
                            filename = inv.number.replace(' ', '_') + '.pdf'
                        filename = re.sub("[^a-zA-Z0-9-_.]+", "_", filename)
                        content = inv.file.file.read()
                        with language(inv.order.locale):
                            email.attach(filename, content, 'application/pdf')
                        invoices_to_mark_transmitted.append(inv)
                    except Exception:
                        logger.exception(f'Could not attach invoice to email {om.guid}')
                    else:
                        if inv.transmission_type == "email":
                            from pretix.base.models import InvoiceAddress
                            expected_recipients = [
                                (inv.invoice_to_transmission_info or {}).get("transmission_email_address")
                                or inv.order.email,
                            ]
                            try:
                                expected_recipients.append(
                                    (inv.order.invoice_address.transmission_info or {}).get("transmission_email_address")
                                    or inv.order.email
                                )
                            except InvoiceAddress.DoesNotExist:
                                pass
                            expected_recipients = {e.lower() for e in expected_recipients if e}
                            if any(t in expected_recipients for t in om.to):
                                invoices_to_mark_transmitted.append(inv)

            ctx.invoices_to_mark_transmitted = invoices_to_mark_transmitted

            # ---- Other storage files ----
            for fname in om.should_attach_other_files:
                ftype, _ = mimetypes.guess_type(fname)
                try:
                    from django.core.files.storage import default_storage
                    data = default_storage.open(fname).read()
                    email.attach(clean_filename(os.path.basename(fname)), data, ftype)
                except Exception:
                    logger.exception(f'Could not attach file to email {om.guid}')

            # ---- Cached files ----
            for cf in om.should_attach_cached_files.all():
                if cf.file:
                    try:
                        email.attach(cf.filename, cf.file.file.read(), cf.type)
                    except Exception:
                        logger.exception(f'Could not attach file to email {om.guid}')

            # Record attachment metadata
            om.actual_attachments = [
                {"name": a[0], "size": len(a[1]), "type": a[2]}
                for a in email.attachments
            ]

        return None  # continue


class FilterAndSendCommand(MailSendCommand):
    """
    Apply per-event and global email-filter signals, then deliver.

    Handles SMTP errors with exponential back-off retries via Celery.
    Records the withheld/failed state on the OutgoingMail record.
    """

    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        from pretix.base.services.mail import WithholdMailException, _format_error, _retry_strategy

        om = ctx.outgoing_mail
        email = ctx.email
        task = ctx.celery_task

        with om.scope_manager():
            # ---- Email filter signals ----
            try:
                if om.event:
                    with om.scope_manager():
                        email = email_filter.send_chained(
                            sender=om.event,
                            chain_kwarg_name='message',
                            message=email,
                            order=om.order,
                            user=om.user,
                            outgoing_mail=om,
                        )

                email = global_email_filter.send_chained(
                    sender=om.event,
                    chain_kwarg_name='message',
                    message=email,
                    user=om.user,
                    order=om.order,
                    organizer=om.organizer,
                    customer=om.customer,
                    outgoing_mail=om,
                )
            except WithholdMailException as e:
                om.status = OutgoingMail.STATUS_WITHHELD
                om.error = e.error
                om.error_detail = e.error_detail
                om.sent = now()
                om.retry_after = None
                om.actual_attachments = [
                    {"name": a[0], "size": len(a[1]), "type": a[2]}
                    for a in email.attachments
                ]
                om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                logger.info(f"Email {om.guid} withheld")
                return False

            # Refresh attachment metadata after filters may have mutated email
            om.actual_attachments = [
                {"name": a[0], "size": len(a[1]), "type": a[2]}
                for a in email.attachments
            ]

            ctx.email = email  # filters may return a new object

            # ---- SMTP delivery ----
            backend = om.get_mail_backend()
            try:
                backend.send_messages([email])
            except Exception as e:
                logger.exception(f'Error sending email {om.guid}')
                retry_strategy = _retry_strategy(e)
                err, err_detail = _format_error(e)

                om.error = err
                om.error_detail = err_detail
                om.sent = now()

                try:
                    if retry_strategy == "microsoft_concurrency" and settings.HAS_REDIS:
                        from django_redis import get_redis_connection
                        redis_key = "pretix_mail_retry_" + hashlib.sha1(
                            f"{getattr(backend, 'username', '_')}@{getattr(backend, 'host', '_')}".encode()
                        ).hexdigest()
                        rc = get_redis_connection("redis")
                        cnt = rc.incr(redis_key)
                        rc.expire(redis_key, 300)
                        max_retries = 10
                        retry_after = min(30 + cnt * 10, 1800)
                        om.status = OutgoingMail.STATUS_AWAITING_RETRY
                        om.retry_after = now() + timedelta(seconds=retry_after)
                        om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                        task.retry(max_retries=max_retries, countdown=retry_after)
                    elif retry_strategy in ("microsoft_concurrency", "quick"):
                        retry_after = [10, 30, 60, 300, 900, 900][task.request.retries]
                        om.status = OutgoingMail.STATUS_AWAITING_RETRY
                        om.retry_after = now() + timedelta(seconds=retry_after)
                        om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                        task.retry(max_retries=5, countdown=retry_after)
                    elif retry_strategy == "slow":
                        retry_after = [60, 300, 600, 1200, 1800, 1800][task.request.retries]
                        om.status = OutgoingMail.STATUS_AWAITING_RETRY
                        om.retry_after = now() + timedelta(seconds=retry_after)
                        om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                        task.retry(max_retries=5, countdown=retry_after)
                except MaxRetriesExceededError:
                    for i in ctx.invoices_to_mark_transmitted:
                        i.set_transmission_failed(provider="email_pdf", data={
                            "reason": "exception",
                            "exception": "{}, max retries exceeded".format(err),
                            "detail": err_detail,
                        })
                    if ctx.log_target:
                        ctx.log_target.log_action(
                            ctx.error_log_action_type,
                            data={
                                'subject': f'{err} (max retries exceeded)',
                                'message': err_detail,
                                'recipient': '',
                                'invoices': [],
                            }
                        )
                    om.status = OutgoingMail.STATUS_FAILED
                    om.sent = now()
                    om.retry_after = None
                    om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                    return False

                # Non-retryable error
                om.status = OutgoingMail.STATUS_FAILED
                om.sent = now()
                om.retry_after = None
                om.save(update_fields=["status", "error", "error_detail", "sent", "retry_after", "actual_attachments"])
                for i in ctx.invoices_to_mark_transmitted:
                    i.set_transmission_failed(provider="email_pdf", data={
                        "reason": "exception",
                        "exception": err,
                        "detail": err_detail,
                    })
                if ctx.log_target:
                    ctx.log_target.log_action(
                        ctx.error_log_action_type,
                        data={
                            'subject': err,
                            'message': err_detail,
                            'recipient': '',
                            'invoices': [],
                        }
                    )
                return False

        return None  # continue to RecordSuccessCommand


class RecordSuccessCommand(MailSendCommand):
    """
    Mark the mail as sent, update invoice transmission records, and log success.
    """

    def execute(self, ctx: MailSendContext) -> Optional[bool]:
        om = ctx.outgoing_mail

        om.status = OutgoingMail.STATUS_SENT
        om.error = None
        om.error_detail = None
        om.sent = now()
        om.retry_after = None
        om.save(update_fields=["status", "error", "error_detail", "sent", "actual_attachments", "retry_after"])

        for i in ctx.invoices_to_mark_transmitted:
            if i.transmission_status != Invoice.TRANSMISSION_STATUS_COMPLETED:
                i.transmission_date = now()
                i.transmission_status = Invoice.TRANSMISSION_STATUS_COMPLETED
                i.transmission_provider = "email_pdf"
                i.transmission_info = {
                    "sent": [
                        {
                            "recipients": om.to,
                            "datetime": now().isoformat(),
                        }
                    ]
                }
                i.save(update_fields=[
                    "transmission_date", "transmission_provider", "transmission_status",
                    "transmission_info"
                ])
            elif i.transmission_provider == "email_pdf":
                i.transmission_info["sent"].append(
                    {
                        "recipients": om.to,
                        "datetime": now().isoformat(),
                    }
                )
                i.save(update_fields=["transmission_info"])

            i.order.log_action(
                "pretix.event.order.invoice.sent",
                data={
                    "full_invoice_no": i.full_invoice_no,
                    "transmission_provider": "email_pdf",
                    "transmission_type": "email",
                    "data": {
                        "recipients": om.to,
                    },
                }
            )
        return None  # pipeline done; caller returns True


# Ordered pipeline used by mail_send_task
MAIL_SEND_PIPELINE = [
    ResolveMailCommand,
    BuildEmailCommand,
    AttachFilesCommand,
    FilterAndSendCommand,
    RecordSuccessCommand,
]
